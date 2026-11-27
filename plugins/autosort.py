# ============================================================
# /autosort — one pass over a source channel that classifies
# every file (plugins/classify.py) and copies it straight to
# whichever destination channel the user mapped to that category
# in /settings -> Channels -> "Auto-Sort Channels".
#
# Unlike /fwd (handlers/forward.py), which always
# sends everything to ONE target channel per run, this command can
# fan out to several channels (movies / series / anime / pdf /
# image / audio / misc) in the same pass, so a 1-lakh-message
# channel doesn't need one run per category.
#
# Only two things are skipped: duplicates, and whatever the user's
# /settings -> Filters exclude (type / extension / keyword / size /
# whitelist). If a "Duplicate" channel is set in /settings -> Channels ->
# "Auto-Sort Channels", duplicates are forwarded there instead of skipped
# (this applies to /autosort only). The Movie channel is stricter: a file the classifier
# calls a movie only goes there when its length, thumbnail shape and TMDB runtime look
# like a real movie (see _movie_gate()); otherwise it goes to Other. The Series channel is
# stricter too: only files identified as real series episodes (TMDB / TVmaze / episode
# marker, see plugins/classify.py "deep SERIES identification") with a thumbnail, a normal
# episode length and a landscape thumbnail shape go there (see _series_gate()); the rest
# goes to Other. The Anime channel has the same kind of gate (see _anime_gate()): only files
# identified as real anime (AniList / Jikan / TMDB / anime list, see plugins/classify.py
# "deep ANIME identification") with a thumbnail, a normal length and a landscape thumbnail
# shape go there; the rest goes to Other.
# Everything else that can't be sorted into a specific
# category (no channel mapped for it, TMDB unreachable, unsupported
# message type, ...) is added to the "Other" channel, so nothing has
# to be forwarded by hand afterwards.
#
# Gates and "why Other" (all log / UI only, nothing is dropped):
#   * Only a NATIVE video (message.video) is judged by its thumbnail shape, duration and TMDB runtime.
#     A video sent as a DOCUMENT has no preview size and no duration in Telegram, so those gates
#     do not apply to it.
#   * A split part (".part004.mkv") is a slice of a film: the duration guard and the TMDB runtime
#     check skip it (SPLIT_PARTS_SKIP_DURATION, default on); the thumbnail rules still apply.
#   * ONE thumbnail rule for ALL native-video channels (Movies, Series, Anime, Education): a video with NO
#     thumbnail (the video's own width / height never rescues it) or with a thumbnail outside the landscape band
#     goes to Other (reasons no_thumb / ratio, the "Ratio->Other" counter). Movies: MOVIE_REQUIRE_THUMB (default
#     on); Education: EDUCATION_REQUIRE_THUMB, EDUCATION_THUMB_RATIO_MIN/MAX, EDUCATION_RATIO_UNKNOWN (video only,
#     no duration limits, "education_files" documents are never gated).
#   * A season pack ("S01", "Season 1", Complete / Batch ...) is a whole season in one file: SERIES_MAX_MINUTES /
#     ANIME_MAX_MINUTES do not apply to it (SEASONPACK_SKIP_MAX_DURATION, default on); the minimum length, the
#     thumbnail and the ratio rules still do.
#   * /autosort's confirmation message says whether AniList is ON; when it is OFF it offers an "Enable AniList"
#     button above Yes / No (AUTOSORT_ASK_ANILIST, default on).
#   * A TMDB outage is retried (AUTOSORT_TMDB_RETRIES, default 2, after ~3 s and ~8 s) before the
#     file goes to Other.
#   * Every file that ends in Other gets a reason (stats["other_why"], one "autosort other-route"
#     INFO line per file). The final "completed" message lists the reason counts.
#
# Order of decisions for ONE file (round 3):
#   0. NSFW keywords (AUTOSORT_NSFW_FIRST): decided BEFORE any TMDB / AniList call. The NSFW / NSFW-files channel when one is
#      mapped, else Other with the reason nsfw_unmapped. The caption keeps its words (links removed only).
#   1. pdf / image / audio / archive / education (strong course names): decided by the classifier before any lookup.
#   2. Step 1 - identification: classify_async() asks TMDB, then AniList, then the fallbacks and fills info["ident"] (the
#      canonical title / year). The caption of a movie / series / anime (+ "_files") is REBUILT from that record
#      (plugins/channels.py ident_caption()); a file that was not identified keeps today's file-name caption.
#   3. Step 2 - the category is decided again from the edited caption (movie vs episodic from its marker, "_files" from how
#      the file was sent, series vs anime from the Step 1 record - TMDB / AniList are NOT asked a second time). A consistency
#      check compares caption and record (type, season / episode, year); when they disagree it is logged and the Step 1
#      result is used (AUTOSORT_STEP2_CHECK, default on).
#   4. The thumbnail / duration / runtime gates, exactly as before, then the channel (or Other with the reason).
# ============================================================

import os
import re
import math
import time
import asyncio
import logging

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.errors.exceptions.not_acceptable_406 import ChannelPrivate as PrivateChat
from pyrogram.errors.exceptions.bad_request_400 import (
    ChannelInvalid,
    ChannelPrivate,
    UsernameInvalid,
    UsernameNotModified,
)
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.database import db
from core.config import temp
from plugins.controls import Translation, CLIENT, start_clone_bot, target_link, frwd_status_text, _test_anilist
from plugins.classify import (
    classify_async, CATEGORIES, VIDEO_CATEGORIES, SKIP_CATEGORIES,
    UNIDENTIFIED, TMDB_UNAVAILABLE, ENV_TMDB_API_KEY,
    get_filename, movie_thumb_gate, movie_duration_minutes, tmdb_movie_runtime, runtime_mismatch, has_video_thumb,
    is_nsfw, nsfw_category,
    is_split_video_part, SPLIT_PARTS_SKIP_DURATION,
    MOVIE_MIN_MINUTES, MOVIE_THUMB_RATIO_MIN, MOVIE_THUMB_RATIO_MAX,
    SERIES_MIN_MINUTES, SERIES_MAX_MINUTES, SERIES_THUMB_RATIO_MIN, SERIES_THUMB_RATIO_MAX, SERIES_RATIO_UNKNOWN,
    SERIES_REQUIRE_THUMB,
    ANIME_MIN_MINUTES, ANIME_MAX_MINUTES, ANIME_THUMB_RATIO_MIN, ANIME_THUMB_RATIO_MAX, ANIME_RATIO_UNKNOWN,
    ANIME_REQUIRE_THUMB,
    MOVIE_REQUIRE_THUMB, EDUCATION_REQUIRE_THUMB, EDUCATION_THUMB_GATE,
    EDUCATION_THUMB_RATIO_MIN, EDUCATION_THUMB_RATIO_MAX, EDUCATION_RATIO_UNKNOWN,
    SEASONPACK_SKIP_MAX_DURATION,
)
from pyrogram.enums import ParseMode
from plugins.channels import check_filters, autosort_caption, text_without_links, ident_caption, parse_edited_caption

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger(__name__)

CLIENT = CLIENT()

# run_id -> pending run config, populated by /autosort, consumed by the
# Yes/No confirmation callback. Same short-lived, in-memory pattern as
# plugins/channels.py's STS, just scoped to this one command.
_PENDING: dict = {}
_next_id = 0

# AUTOSORT_ASK_ANILIST=1 (default): the /autosort confirmation message says whether AniList is ON for this user and, when
# it is OFF, offers an "Enable AniList" button above Yes / No. 0 = the old message, no AniList line, no button.
AUTOSORT_ASK_ANILIST = os.environ.get("AUTOSORT_ASK_ANILIST", "1").strip().lower() not in ("0", "false", "no", "off")

# AUTOSORT_REMOVE_LINKS=1 (default): /autosort ALWAYS removes the links from every caption and text message it copies (only the
# links, the words stay), whatever the /settings "Remove caption link" toggle says. 0 = old behaviour (only when that toggle is on).
AUTOSORT_REMOVE_LINKS = os.environ.get("AUTOSORT_REMOVE_LINKS", "1").strip().lower() not in ("0", "false", "no", "off")


# AUTOSORT_NSFW_FIRST=1 (default): a file whose name / caption carries adult-content keywords (plugins/classify.py is_nsfw()) is
# decided BEFORE any TMDB / AniList call: the NSFW / NSFW-files channel when one is mapped, else Other (reason nsfw_unmapped).
# 0 = old behaviour (the file goes through the normal classification).
AUTOSORT_NSFW_FIRST = os.environ.get("AUTOSORT_NSFW_FIRST", "1").strip().lower() not in ("0", "false", "no", "off")

# AUTOSORT_STEP2_CHECK=1 (default): Step 2 decides the category from the edited caption and checks it against the Step 1 record
# (type, season / episode, year); a disagreement is logged and the Step 1 result is used. 0 = no check, the Step 1 result is used.
AUTOSORT_STEP2_CHECK = os.environ.get("AUTOSORT_STEP2_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")

# AUTOSORT_FILES_FALLBACK=1 (default): when "X_files" has no channel, the channel mapped for "X" is used (and the other way round)
# before the file falls back to Other (movie / series / anime / education / nsfw). 0 = old behaviour (unmapped -> Other).
AUTOSORT_FILES_FALLBACK = os.environ.get("AUTOSORT_FILES_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "off")

# AUTOSORT_MSG_LOG=1 (default): ONE "autosort msg" INFO line per message (id, name, classified, final, outcome, reason), the
# per-message error isolation and the reconciliation line of the completed message. 0 = old behaviour.
AUTOSORT_MSG_LOG = os.environ.get("AUTOSORT_MSG_LOG", "1").strip().lower() not in ("0", "false", "no", "off")

_TWIN_BASES = ("movie", "series", "anime", "education", "nsfw")


def _twin_category(category):
    """"movie_files" <-> "movie" (series / anime / education / nsfw too); None for every other category."""
    if not category:
        return None
    base = category[:-6] if category.endswith("_files") else category
    if base not in _TWIN_BASES:
        return None
    return base if category.endswith("_files") else base + "_files"


def _files_fallback(category, mapping):
    """AUTOSORT_FILES_FALLBACK: the twin category whose channel is used when `category` has none, else None. Never raises."""
    if not AUTOSORT_FILES_FALLBACK:
        return None
    try:
        twin = _twin_category(category)
        if twin and mapping.get(category) is None and mapping.get(twin) is not None:
            return twin
    except Exception as e:
        logger.warning("autosort: files fallback rule failed for %s (%s) - old behaviour", category, e)
    return None


def _fallback_lines(mapping):
    """The "Fallbacks" lines of the /autosort confirmation message: which unmapped category uses which channel."""
    out = []
    for cat in CATEGORIES:
        twin = _files_fallback(cat, mapping)
        if twin:
            out.append(f"{CATEGORY_LABELS.get(cat, cat)} → uses {CATEGORY_LABELS.get(twin, twin)}")
    return out


def _new_run_id() -> str:
    global _next_id
    _next_id += 1
    return str(_next_id)


CATEGORY_LABELS = {
    "movie": "🎬 Movie (Video)",
    "movie_files": "🎬🗂 Movie (Files)",
    "series": "📺 Series (Video)",
    "series_files": "📺🗂 Series (Files)",
    "anime": "🍥 Anime (Video)",
    "anime_files": "🍥🗂 Anime (Files)",
    "education": "🎓 Education (Video)",
    "education_files": "🎓🗂 Education (Files)",
    "pdf": "📄 PDF",
    "image": "🖼 Image",
    "audio": "🎵 Audio",
    "files": "🗂 Files (Unidentified)",
    "other": "📦 Other / Misc",
    "duplicate": "♻️ Duplicate",
}

# ── Pacing ──────────────────────────────────────────────────
# Rather than a fixed 1s/3s-per-file sleep (which pushes big channels
# into repeated FloodWaits), the per-file delay is calculated up front
# from how many messages are actually in range, so the whole run lands
# in roughly TARGET_RUN_SECONDS regardless of channel size. It then
# adapts live: every FloodWait nudges the delay up (and it eases back
# down slowly after a long clean streak), so a run tunes itself to
# whatever rate Telegram is actually allowing that day instead of
# guessing once and hoping.
TARGET_RUN_SECONDS = 3.5 * 24 * 60 * 60   # ~3.5 days for a full run
MIN_DELAY = 1.0                            # never faster than this, even for tiny runs
MAX_DELAY = 12.0                           # cap so a bad FloodWait streak doesn't stall forever
FLOODWAIT_BACKOFF = 1.6                    # multiply delay by this on every FloodWait
EASE_DOWN_EVERY = 50                       # after this many clean sends, ease delay back down
EASE_DOWN_FACTOR = 0.9


def _base_delay(total_files: int) -> float:
    if total_files <= 0:
        return MIN_DELAY
    return min(MAX_DELAY, max(MIN_DELAY, TARGET_RUN_SECONDS / total_files))


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


@Client.on_message(filters.private & filters.command("autosort"))
async def autosort_cmd(bot: Client, message):
    user_id = message.from_user.id

    if temp.lock.get(user_id) and str(temp.lock.get(user_id)) == "True":
        return await message.reply_text("<code>Please wait until your previous task completes.</code>")

    mapping = await db.get_category_channels(user_id)
    if not mapping:
        return await message.reply_text(
            "<b>No auto-sort channels configured yet.</b>\n"
            "Go to /settings → <b>Channels</b> → <b>Auto-Sort Channels</b> and set a "
            "destination channel for at least one category (movie / series / anime / "
            "pdf / image / audio / other), then run /autosort again."
        )

    # "Other" is the catch-all: everything that doesn't fit a mapped category
    # (or can't be copied normally) lands there instead of being skipped.
    if "other" not in mapping:
        return await message.reply_text(
            "<b>Other / Misc channel required.</b>\n"
            "Every file that doesn't fit a mapped category (and isn't removed by "
            "your /settings filters) goes to the <b>Misc / Other</b> channel. "
            "Set it in /settings → <b>Channels</b> → <b>Auto-Sort Channels</b>, "
            "then run /autosort again."
        )

    # Movie / series / anime routing is confirmed against TMDB (anything TMDB
    # can't confirm goes to Other), so a key is required for those categories.
    if any(c in VIDEO_CATEGORIES for c in mapping) and not (
        await db.get_tmdb_key(user_id) or ENV_TMDB_API_KEY
    ):
        return await message.reply_text(
            "<b>TMDB API key required.</b>\n"
            "Movies/series/anime are only forwarded when TMDB identifies them. "
            "Add your key in /settings → <b>TMDB</b>, then run /autosort again."
        )

    _bot = await db.get_bot(user_id)
    if not _bot:
        return await message.reply_text("<code>You didn't add any bot. Please add one using /settings first.</code>")

    fromid = await bot.ask(message.chat.id, Translation.FROM_MSG)
    if fromid.text and fromid.text.startswith('/'):
        return await message.reply(Translation.CANCEL)

    if fromid.text and not fromid.forward_date:
        regex = re.compile(r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")
        match = regex.match(fromid.text.replace("?single", ""))
        if not match:
            return await message.reply('Invalid link')
        chat_id = match.group(4)
        last_msg_id = int(match.group(5))
        if chat_id.isnumeric():
            chat_id = int("-100" + chat_id)
    elif fromid.forward_from_chat and fromid.forward_from_chat.type in [enums.ChatType.CHANNEL]:
        last_msg_id = fromid.forward_from_message_id
        chat_id = fromid.forward_from_chat.username or fromid.forward_from_chat.id
        if last_msg_id is None:
            return await message.reply_text(
                "**This may be a forwarded message from a group sent by an anonymous admin. "
                "Please send the last message's link from the group instead.**"
            )
    else:
        return await message.reply_text("**Invalid! Forward the last message from the source channel, or send its link.**")

    try:
        title = (await bot.get_chat(chat_id)).title
    except (PrivateChat, ChannelPrivate, ChannelInvalid):
        title = "private"
    except (UsernameInvalid, UsernameNotModified):
        return await message.reply('Invalid link specified.')
    except Exception as e:
        return await message.reply(f'Error - {e}')

    skipno = await bot.ask(message.chat.id, Translation.SKIP_MSG)
    if skipno.text.startswith('/'):
        return await message.reply(Translation.CANCEL)
    if not skipno.text.lstrip('-').isdigit():
        return await message.reply_text("Skip number must be numeric.")

    run_id = _new_run_id()
    _PENDING[run_id] = {
        "bot": _bot,
        "from_chat": chat_id,
        "from_title": title,
        "last_msg_id": int(last_msg_id),
        "skip": int(skipno.text),
        "mapping": mapping,
        "owner": user_id,
    }

    mapping_lines = "\n".join(
        f"{CATEGORY_LABELS.get(cat, cat)} → `{chat_id_}`" for cat, chat_id_ in mapping.items()
    )
    fb_lines = _fallback_lines(mapping)
    fb_cats = {c for c in CATEGORIES if _files_fallback(c, mapping)}
    unmapped = [c for c in CATEGORIES if c not in mapping and c not in fb_cats]
    unmapped_note = (
        f"\n\n<i>Not configured (will go to Other): {', '.join(unmapped)}</i>"
        if unmapped else ""
    )
    if fb_lines:
        unmapped_note += "\n\n<b>Fallbacks (AUTOSORT_FILES_FALLBACK):</b>\n" + "\n".join(fb_lines)

    dup_note = (
        "Duplicates go to the Duplicate channel."
        if "duplicate" in mapping else "Duplicates are skipped (no Duplicate channel set)."
    )

    total_files = max(int(last_msg_id) - int(skipno.text), 1)
    est_delay = _base_delay(total_files)
    est_seconds = total_files * est_delay

    buttons = [[
        InlineKeyboardButton('Yes', callback_data=f"autosort_start_{run_id}"),
        InlineKeyboardButton('No', callback_data="close_btn"),
    ]]

    # AniList status (no network call): ON = one status line; OFF = a warning and an "Enable AniList" row ABOVE Yes / No.
    # Yes still starts the run exactly as before (without AniList when it is off). Never raises: on any problem the
    # message is the old one.
    anilist_note = ""
    if AUTOSORT_ASK_ANILIST:
        try:
            if await db.get_anilist_enabled(user_id):
                anilist_note = "<b>AniList:</b> ON <i>(anime are checked against AniList)</i>\n\n"
            else:
                anilist_note = (
                    "<b>⚠️ AniList is OFF:</b> anime can land in Series or Movies. AniList needs no API key "
                    "(the token in Private Files is optional). Tap <b>Enable AniList</b> to turn it on, "
                    "or Yes to start without it.\n\n"
                )
                buttons.insert(0, [InlineKeyboardButton("Enable AniList", callback_data=f"autosort_anion_{run_id}")])
        except Exception as e:
            logger.warning("autosort: could not read the AniList setting (%s) - confirmation without the AniList line", e)
            anilist_note = ""

    text_head = (
        f"<b>DOUBLE CHECKING ⚠️</b>\n\n"
        f"<b>FROM CHANNEL:</b> <code>{title}</code>\n"
        f"<b>SKIP MESSAGES:</b> <code>{skipno.text}</code>\n"
        f"<b>APPROX FILES:</b> <code>{total_files}</code>\n"
        f"<b>ESTIMATED TIME:</b> <code>~{_format_duration(est_seconds)}</code> "
        f"<i>(paced automatically, speeds up/slows down live to dodge FloodWaits)</i>\n\n"
        f"<b>Routing:</b>\n{mapping_lines}{unmapped_note}\n\n"
    )
    text_tail = (
        f"<i>{dup_note} Anything your /settings filters exclude is skipped. "
        f"Everything else is forwarded.</i>\n\n"
        f"<i>Your bot/userbot must be admin in every destination channel listed above.</i>\n\n"
        f"Click Yes to start."
    )
    _PENDING[run_id]["text_head"], _PENDING[run_id]["text_tail"] = text_head, text_tail   # for "Enable AniList" below
    await message.reply_text(text_head + anilist_note + text_tail, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^autosort_anion_(.+)$"))
async def autosort_anion(bot: Client, query):
    """"Enable AniList" button of the /autosort confirmation: turns the user's AniList toggle ON, checks once that AniList
    answers (the SAME check as the Private Files -> AniList TEST button, 8 s, never raises), and shows the normal Yes / No
    keyboard again. The pending run is NOT consumed: Yes (autosort_start) still starts it."""
    user_id = query.from_user.id
    run_id = query.matches[0].group(1)
    cfg = _PENDING.get(run_id)
    if not cfg or cfg["owner"] != user_id:
        return await query.answer("This button has expired.", show_alert=True)

    try:
        await db.set_anilist_enabled(user_id, True)
    except Exception as e:
        logger.warning("autosort: could not turn AniList on for user %s: %s", user_id, e)
        return await query.answer("Could not turn AniList on. Try again, or use /settings -> Private Files -> AniList.",
                                  show_alert=True)
    try:
        await query.answer("AniList enabled. Checking it...")
    except Exception:
        pass

    reachable = False
    try:
        reachable, _detail = await asyncio.wait_for(_test_anilist(await db.get_anilist_key(user_id)), timeout=8)
    except Exception as e:                       # includes asyncio.TimeoutError: "not reachable" is an answer, not an error
        logger.info("autosort: AniList reachability check failed for user %s: %s", user_id, e)
        reachable = False

    status = ("<b>✅ AniList enabled.</b> AniList is reachable.\n\n" if reachable else
              "<b>✅ AniList enabled.</b> AniList is not reachable right now - Jikan (MyAnimeList) is used until it answers.\n\n")
    text = (cfg.get("text_head") or "<b>DOUBLE CHECKING ⚠️</b>\n\n") + status + (cfg.get("text_tail") or "Click Yes to start.")
    buttons = [[
        InlineKeyboardButton('Yes', callback_data=f"autosort_start_{run_id}"),
        InlineKeyboardButton('No', callback_data="close_btn"),
    ]]
    try:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning("autosort: could not update the confirmation message: %s", e)


@Client.on_callback_query(filters.regex(r"^autosort_start_(.+)$"))
async def autosort_start(bot: Client, query):
    user_id = query.from_user.id
    run_id = query.matches[0].group(1)
    cfg = _PENDING.pop(run_id, None)
    if not cfg or cfg["owner"] != user_id:
        return await query.answer("This button has expired.", show_alert=True)

    if temp.lock.get(user_id) and str(temp.lock.get(user_id)) == "True":
        return await query.answer("Please wait until your previous task completes.", show_alert=True)

    mapping = cfg["mapping"]
    dest_chats = list(set(mapping.values()))
    busy = [c for c in dest_chats if c in temp.IS_FRWD_CHAT]
    if busy:
        return await query.answer(f"A task is already running on channel {busy[0]}. Please wait.", show_alert=True)

    m = await query.message.edit_text("<code>Verifying access, please wait...</code>")

    try:
        client = await start_clone_bot(CLIENT.client(cfg["bot"]))
    except Exception as e:
        return await m.edit(str(e))

    try:
        await client.get_messages(cfg["from_chat"], 1)
    except Exception:
        return await _fail(
            m, client, user_id,
            f"**Source chat may be private. Add your userbot as a member, or make your "
            f"[bot](t.me/{cfg['bot']['username']}) an admin there.**",
        )

    for category, dest in mapping.items():
        try:
            k = await client.send_message(dest, "Testing")
            await k.delete()
        except Exception:
            return await _fail(
                m, client, user_id,
                f"**Couldn't post to the {CATEGORY_LABELS.get(category, category)} channel "
                f"(`{dest}`). Make your [bot/userbot](t.me/{cfg['bot']['username']}) admin there.**",
            )

    for c in dest_chats:
        temp.IS_FRWD_CHAT.append(c)
    temp.lock[user_id] = True
    temp.CANCEL[user_id] = False

    await _run(client, user_id, m, cfg)


async def _notify(client, user_id, text, mapping):
    """'Forwarding Started / Completed / Process Cancelled' message in the bot chat.
    The 🥀 links to the Other / Misc channel - the one channel /autosort always has."""
    try:
        link = await target_link(client, mapping["other"])
        await client.send_message(user_id, frwd_status_text(text, link))
    except Exception as e:
        logger.debug("status message failed: %s", e)


async def _fail(m, client, user_id, text):
    await _safe_edit(m, text)
    try:
        await client.stop()
    except Exception:
        pass
    temp.lock[user_id] = False


async def _safe_edit(m, text, button=None):
    try:
        return await m.edit(text, reply_markup=button)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await _safe_edit(m, text, button)
    except Exception as e:
        logger.debug("autosort progress edit failed: %s", e)


# user_id -> live state of that user's running /autosort (read by the status buttons)
_LIVE: dict = {}

# bases with a Video and a Files channel, then the single ones - order of the popup
_POPUP_PAIRS = (("movie", "Movie"), ("series", "Series"), ("anime", "Anime"),
                ("education", "Education"), ("nsfw", "NSFW"))
_POPUP_SINGLES = (("pdf", "PDF"), ("image", "Image"), ("audio", "Audio"), ("files", "Files"), ("other", "Other"),
                  ("duplicate", "Duplicate"))


def _progress_text(stats: dict, mapping: dict, pacing: dict, total_files: int, status: str = "Forwarding") -> str:
    """The 'Forwarded Status' box (same template as /fwd). FECHED MSG shows current/total."""
    fetched = stats["fetched"]
    finished = status in ("completed", "cancelled")
    pct = 100.0 if status == "completed" else min(max(fetched * 100 / max(total_files, 1), 0.0), 100.0)
    full = math.floor(pct / 10)
    progress = "◉" + "◉" * full + "◎" * (10 - full)
    # ETA from the real speed of this run (paused time excluded); pace only until a few files are done
    active = max(time.time() - stats["start"] - stats.get("paused_secs", 0), 1)
    per_file = active / fetched if fetched >= 5 else pacing["delay"]
    eta = 0 if finished else max(total_files - fetched, 0) * per_file
    text = Translation.TEXT.format(
        f"{fetched}/{total_files}", stats["forwarded"], stats["duplicate"], stats["errors"],
        stats["filtered"], status, "{:.1f}".format(pct), _format_duration(eta), progress,
    )
    if status in ("completed", "cancelled"):
        text = _with_reconcile(text, stats)
    if status == "completed":
        text = _with_sources(_with_other_because(text, stats), stats)
    return text


def _with_reconcile(text: str, stats: dict) -> str:
    """AUTOSORT_MSG_LOG: "fetched N = forwarded + filtered + duplicate + skipped + errors" (+ empty / service counted apart)."""
    if not AUTOSORT_MSG_LOG:
        return text
    f, fw, fl, du = stats["fetched"], stats["forwarded"], stats["filtered"], stats["duplicate"]
    sk, er, em = stats.get("skipped", 0), stats["errors"], stats.get("empty", 0)
    total = fw + fl + du + sk + er + em
    line = (f"\n\n<b>Check:</b> fetched {f} = forwarded {fw} + filtered {fl} + duplicate {du} + skipped {sk} + errors {er}"
            f" + empty/service {em}" + (" ✅" if total == f else f" ⚠️ (sum {total})"))
    fb = stats.get("files_fallback") or 0
    if fb:
        line += f"\n<b>Files fallback used:</b> {fb}"
    return text + line if len(text) + len(line) <= 4096 else text


def _with_other_because(text: str, stats: dict) -> str:
    """The final "completed" message only: "Other because: not_found 120, ratio 8, ..." (top 6 reasons by
    count). Left out when nothing went to Other or when it would push the message over Telegram's 4096."""
    why = stats.get("other_why") or {}
    if not why:
        return text
    top = sorted(why.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    block = "\n\n<b>Other because:</b> " + ", ".join(f"{k} {n}" for k, n in top)
    return text + block if len(text) + len(block) <= 4096 else text


# ── Which identification sources worked in this run ("Sources:" line of the completed message) ──
_SERVICE_HOSTS = {"api.themoviedb.org": "tmdb", "graphql.anilist.co": "anilist", "api.jikan.moe": "jikan",
                  "api.tvmaze.com": "tvmaze", "www.wikidata.org": "wikidata", "v3.sg.media-imdb.com": "imdb",
                  "www.omdbapi.com": "omdb"}


class _CountedRequest:
    """`async with session.get(...) as resp`, counted: one call per request, one error when it raised or answered HTTP >= 400."""

    def __init__(self, cm, bucket: list):
        self._cm, self._bucket = cm, bucket

    async def __aenter__(self):
        try:
            resp = await self._cm.__aenter__()
        except Exception:
            self._bucket[0] += 1
            self._bucket[1] += 1
            raise
        self._bucket[0] += 1
        if (getattr(resp, "status", 200) or 200) >= 400:
            self._bucket[1] += 1
        return resp

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


class _CountingSession:
    """The run's aiohttp session. It counts the requests per service - [calls, errors] in `calls` - and passes everything
    else through, so the classifier and the movie gate use it exactly like the plain session."""

    def __init__(self, session, calls: dict):
        self._session, self._calls = session, calls

    def _bucket(self, url) -> list:
        parts = str(url).split("/")
        return self._calls.setdefault(_SERVICE_HOSTS.get(parts[2] if len(parts) > 2 else "", "other"), [0, 0])

    def get(self, url, **kw):
        return _CountedRequest(self._session.get(url, **kw), self._bucket(url))

    def post(self, url, **kw):
        return _CountedRequest(self._session.post(url, **kw), self._bucket(url))

    async def close(self):
        await self._session.close()

    def __getattr__(self, name):
        return getattr(self._session, name)


def _count_hit(stats: dict, classified: str, info: dict) -> None:
    """A file the classifier identified through IMDb / OMDb, TVmaze or Wikidata is one "hit" of that source (and one
    identified as anime by the offline anime database is a hit of "offline")."""
    if classified.startswith("anime") and (info.get("anime") or {}).get("source") == "offline":
        hits = stats.setdefault("hits", {})
        hits["offline"] = hits.get("offline", 0) + 1
    if classified.startswith("series"):
        src = (info.get("series") or {}).get("source")
    elif classified.startswith(("movie", "anime")):
        src = (info.get("movie") or {}).get("source")
    else:
        return
    key = {"imdb": "imdb", "omdb": "imdb", "tvmaze": "tvmaze", "wikidata": "wikidata"}.get(src)
    if key:
        hits = stats.setdefault("hits", {})
        hits[key] = hits.get(key, 0) + 1


def _with_sources(text: str, stats: dict) -> str:
    """The final "completed" message only: ONE line, "Sources: TMDB 412 calls / 3 errors · AniList 120 calls / 0 errors
    (or OFF) · IMDb 4 hits · TVmaze 7 hits · Wikidata 2 hits". Calls / errors are the HTTP requests of the run; a hit is a
    file that source identified."""
    calls, hits = stats.get("calls") or {}, stats.get("hits") or {}

    def ce(name: str) -> str:
        n, e = calls.get(name, (0, 0))
        return f"{n} calls / {e} errors"

    anilist = ce("anilist") if stats.get("anilist_on") else "OFF"
    line = (f"\n\n<b>Sources:</b> TMDB {ce('tmdb')} · AniList {anilist} · IMDb {hits.get('imdb', 0)} hits · "
            f"TVmaze {hits.get('tvmaze', 0)} hits · Wikidata {hits.get('wikidata', 0)} hits")
    if hits.get("offline"):
        line += f" · Offline anime DB {hits['offline']} hits"
    return text + line if len(text) + len(line) <= 4096 else text


def _status_markup(state: str) -> InlineKeyboardMarkup:
    """state: running | paused | completed | cancelled. First row = the 'Progressing' button
    (tap = category counts); one Pause/Resume toggle; Cancel."""
    title = {"running": "Progressing", "paused": "Paused", "completed": "Completed", "cancelled": "Cancelled"}[state]
    rows = [[InlineKeyboardButton(title, "autosort_prog")]]
    if state in ("running", "paused"):
        rows.append([InlineKeyboardButton("ʀᴇsᴜᴍᴇ" if state == "paused" else "ᴘᴀᴜsᴇ", "autosort_pause")])
        rows.append([InlineKeyboardButton("• ᴄᴀɴᴄᴇʟ", "autosort_cancel")])
    return InlineKeyboardMarkup(rows)


def _category_popup(stats: dict, mapping: dict) -> str:
    """Counts per category for the Progressing popup (Telegram allows 200 characters)."""
    lines = ["Video/Files"]
    ratio_shown = False
    for base, name in _POPUP_PAIRS:
        if base in mapping or f"{base}_files" in mapping:
            lines.append(f"{name} {stats.get(base, 0)}/{stats.get(base + '_files', 0)}")
            if base in ("movie", "series") and not ratio_shown:
                # movies / series sent to Other by the thumbnail-ratio gates (one shared count)
                lines.append(f"Ratio→Other {stats.get('ratio_other', 0)}")
                ratio_shown = True
    for key, name in _POPUP_SINGLES:
        if key in mapping:
            lines.append(f"{name} {stats.get(key, 0)}")
    return "\n".join(lines)[:200]


async def _copy_with_retry(client, dest, from_chat, msg_id, m, stats, mapping, pacing, total_files, caption=None, forward=False, text=None):
    try:
        if forward:
            # copy_message() can't rebuild some message types (games, invoices,
            # giveaways, ...). A plain forward works for anything.
            await client.forward_messages(chat_id=dest, from_chat_id=from_chat, message_ids=msg_id)
        elif text is not None:
            # Text message whose links were removed: copy_message() can't edit
            # text, so send the cleaned text instead.
            await client.send_message(chat_id=dest, text=text, parse_mode=ParseMode.HTML)
        else:
            kwargs = {"caption": caption, "parse_mode": ParseMode.HTML} if caption is not None else {}
            await client.copy_message(chat_id=dest, from_chat_id=from_chat, message_id=msg_id, **kwargs)
        return True
    except FloodWait as e:
        # Telegram itself just told us the safe wait time - honour it exactly,
        # and also nudge our own pacing up so we don't walk straight back
        # into another one on the very next file.
        pacing["floodwaits"] += 1
        pacing["streak"] = 0
        pacing["delay"] = min(pacing["delay"] * FLOODWAIT_BACKOFF, MAX_DELAY)
        await _safe_edit(
            m,
            _progress_text(stats, mapping, pacing, total_files)
            + f"\n\n<i>FloodWait: sleeping {e.value}s (pace raised to {pacing['delay']:.1f}s/file)...</i>",
            _status_markup("running"),
        )
        await asyncio.sleep(e.value)
        return await _copy_with_retry(client, dest, from_chat, msg_id, m, stats, mapping, pacing, total_files, caption, forward, text)
    except Exception as e:
        logger.debug("autosort copy failed for msg %s: %s", msg_id, e)
        return False


# ── TMDB outage retry ───────────────────────────────────────
# classify_async() answers TMDB_UNAVAILABLE when TMDB failed (not "not found"). One bad minute should not
# send a good file to Other, so /autosort asks again: after ~3 s, then ~8 s (AUTOSORT_TMDB_RETRIES = how
# many times, default 2, 0 = off). Still failing -> the file goes to Other with the reason "tmdb_error".
def _env_retries(name: str, default: int) -> int:
    try:
        return max(0, min(5, int(os.environ.get(name, "").strip() or default)))
    except ValueError:
        return default


AUTOSORT_TMDB_RETRIES = _env_retries("AUTOSORT_TMDB_RETRIES", 2)
_TMDB_RETRY_WAITS = (3.0, 8.0)


async def _classify_with_retry(message, session, tmdb_key, use_anilist, anilist_key):
    """-> (category, info, retries). A fresh info dict per attempt, so notes of a failed attempt never leak."""
    retries = 0
    while True:
        info = {}   # filled by classify_async: the TMDB id of the match, info["series"] / info["anime"] details
        # deep_series=True is /autosort only: TVmaze + a confidence score decide "series".
        # deep_anime=True is /autosort only too: anime name cleaning, AniList (Jikan) before TMDB for
        # anime candidates, the anime list, no TMDB-keyword anime verdicts (info["anime"] = details)
        category = await classify_async(message, session, tmdb_key, use_anilist, anilist_key, info=info,
                                        deep_series=True, deep_anime=True)
        if category != TMDB_UNAVAILABLE or retries >= AUTOSORT_TMDB_RETRIES:
            info["tmdb_retries"] = retries
            return category, info, retries
        wait = _TMDB_RETRY_WAITS[min(retries, len(_TMDB_RETRY_WAITS) - 1)]
        retries += 1
        logger.info("autosort tmdb-retry: msg=%s name=%r attempt=%d/%d waiting=%.0fs",
                    message.id, _movie_label(message), retries, AUTOSORT_TMDB_RETRIES, wait)
        await asyncio.sleep(wait)


# ── Movie-channel gate ──────────────────────────────────────
# classify_async() is shared with /fwd, so the extra checks that keep the Movie
# channel to real, landscape-thumbnail movies live here, not in the classifier.
MOVIE_GATE_CATEGORIES = ("movie", "movie_files")


async def _movie_gate(message, category, info, session, tmdb_key):
    """Checks a file the classifier called movie / movie_files. First failure
    wins and the file becomes "other" (never dropped):
      1. duration known and < MOVIE_MIN_MINUTES     -> a clip, trailer or short
      1b. no thumbnail at all (MOVIE_REQUIRE_THUMB) -> a thumbnail-less video is not a movie (reason no_thumb)
      2. thumbnail ratio outside the band / unknown -> poster-style thumbnail
      3. duration vs TMDB runtime off by > tolerance -> not that film
    These three apply to a NATIVE video only. A video sent as a document has no preview size and no
    duration in Telegram, so it skips all of them. A split part (".part004.mkv") is only a slice of the
    film: it skips 1 and 3 (SPLIT_PARTS_SKIP_DURATION), the thumbnail rule (2) still applies.
    Returns a dict: category (final), reason (None = unchanged), why (no_thumb | duration | ratio |
    runtime), ratio_hit (failed on the ratio gate), ratio, source, minutes, tmdb_id."""
    native = getattr(message, "video", None) is not None
    split = native and SPLIT_PARTS_SKIP_DURATION and is_split_video_part(message)
    if native:
        try:
            # MOVIE_REQUIRE_THUMB (default on): a video with NO thumbnail is not a movie - its own width / height never
            # rescues it (the same rule as the series / anime gates)
            ok, ratio, source = movie_thumb_gate(message, require_thumb=MOVIE_REQUIRE_THUMB)
        except Exception as e:
            logger.warning("autosort: movie thumbnail rule failed (%s) - old behaviour for this file", e)
            ok, ratio, source = movie_thumb_gate(message)
        minutes = movie_duration_minutes(message)
    else:
        ok, ratio, source, minutes = True, None, "document", None
    tmdb_id = info.get("tmdb_id") if info.get("tmdb_media") == "movie" else None
    reason, why, ratio_hit = None, None, False
    if minutes is not None and not split and minutes < MOVIE_MIN_MINUTES:
        reason, why = f"duration {minutes:.0f}m -> other", "duration"
    elif source == "no-thumb":
        ratio_hit, why = True, "no_thumb"
        reason = "no thumbnail -> other"
    elif not ok:
        ratio_hit, why = True, "ratio"
        if ratio is None:
            reason = "ratio unknown -> other"
        elif ratio < 1.0:
            reason = f"ratio {ratio:.2f} poster -> other"
        else:
            reason = f"ratio {ratio:.2f} outside {MOVIE_THUMB_RATIO_MIN:g}-{MOVIE_THUMB_RATIO_MAX:g} -> other"
    elif tmdb_id and minutes is not None and not split:
        # only worth a TMDB call when the file's own length is known
        runtime = await tmdb_movie_runtime(session, tmdb_id, tmdb_key)
        if runtime_mismatch(minutes, runtime):
            reason, why = f"runtime mismatch {minutes:.0f}m vs TMDB {runtime}m -> other", "runtime"
    return {
        "category": "other" if reason else category, "reason": reason, "why": why, "ratio_hit": ratio_hit,
        "ratio": ratio, "source": source, "minutes": minutes, "tmdb_id": tmdb_id, "split": bool(split),
    }


def _movie_label(message) -> str:
    """File name, else the first caption line (for the log)."""
    name = get_filename(message)
    if not name:
        for line in (getattr(message, "caption", None) or "").splitlines():
            if line.strip():
                name = line.strip()
                break
    return (name or "-")[:80]


def _log_movie_route(message, classified, gated, final, gate, info):
    """ONE INFO line per movie / movie_files file (and per TMDB movie the
    classifier already moved to anime): what it was, what was measured, where it went."""
    notes = []
    if gate and gate["reason"]:
        notes.append(gate["reason"])
    elif classified.startswith("anime"):
        notes.append("anime origin -> anime")
    if final != gated:
        notes.append("no channel mapped -> other")
    gate = gate or {}
    ratio, minutes = gate.get("ratio"), gate.get("minutes")
    logger.info(
        "autosort movie-route: msg=%s name=%r class=%s tmdb=%s ratio=%s src=%s dur=%s final=%s reason=%s",
        message.id, _movie_label(message), classified, info.get("tmdb_id") or "-",
        f"{ratio:.2f}" if ratio is not None else "-", gate.get("source", "-"),
        f"{minutes:.0f}m" if minutes is not None else "-", final, "; ".join(notes) or "-",
    )


# ── Series-channel gate ─────────────────────────────────────
# The classifier (deep_series=True) only says "series" for a file it is confident is a
# real episode; this gate then checks the file itself. Same style as _movie_gate().
SERIES_GATE_CATEGORIES = ("series", "series_files")


def _series_gate(message, category, info):
    """Checks a file the classifier called series / series_files. First failure
    wins and the file becomes "other" (never dropped):
      1. no thumbnail at all (SERIES_REQUIRE_THUMB)  -> a thumbnail-less file is not an episode
      2. duration known and < SERIES_MIN_MINUTES     -> a clip, promo or teaser
      3. duration known and > SERIES_MAX_MINUTES     -> a compilation, marathon or film
                                                        (not for a season pack, "S01 Combined": that IS long)
      4. thumbnail ratio outside the band / unknown  -> poster / square / portrait thumbnail
    All four apply to a NATIVE video only: a video sent as a document has no preview size and no
    duration in Telegram, so it skips them all. A split part skips 2 and 3 (SPLIT_PARTS_SKIP_DURATION);
    the thumbnail rules (1, 4) still apply to it. The video's own width / height never rescues a
    missing thumbnail for series. The show's own runtime (TMDB / TVmaze) is NOT a veto here, it is
    only logged. No thumbnail counts as a thumbnail reject (ratio_hit), so it shares the
    "Ratio->Other" counter.
    Returns a dict: category (final), reason (None = unchanged), why (no_thumb | duration | ratio),
    ratio_hit, ratio, source, minutes, thumb (does the file have a thumbnail)."""
    native = getattr(message, "video", None) is not None
    split = native and SPLIT_PARTS_SKIP_DURATION and is_split_video_part(message)
    pack = bool((info.get("series") or {}).get("season_pack"))      # "S01 Combined / Complete": a whole season in one file
    if SEASONPACK_SKIP_MAX_DURATION and (info.get("series") or {}).get("pack"):
        pack = True                                                 # any season pack ("S01", "Season 1", Batch ...) is long by nature
    if native:
        ok, ratio, source = movie_thumb_gate(
            message, SERIES_THUMB_RATIO_MIN, SERIES_THUMB_RATIO_MAX, SERIES_RATIO_UNKNOWN,
            require_thumb=SERIES_REQUIRE_THUMB)
    else:
        ok, ratio, source = True, None, "document"
    minutes = movie_duration_minutes(message) if native else None
    reason, why, ratio_hit = None, None, False
    if source == "no-thumb":
        ratio_hit, why = True, "no_thumb"
        reason = "no thumbnail -> other"
    elif minutes is not None and not split and minutes < SERIES_MIN_MINUTES:
        reason, why = f"duration {minutes:.0f}m below {SERIES_MIN_MINUTES:g}m -> other", "duration"
    elif minutes is not None and not split and not pack and minutes > SERIES_MAX_MINUTES:
        reason, why = f"duration {minutes:.0f}m above {SERIES_MAX_MINUTES:g}m -> other", "duration"
    elif not ok:
        ratio_hit, why = True, "ratio"
        if ratio is None:
            reason = "ratio unknown -> other"
        elif ratio < 1.0:
            reason = f"ratio {ratio:.2f} poster -> other"
        else:
            reason = f"ratio {ratio:.2f} outside {SERIES_THUMB_RATIO_MIN:g}-{SERIES_THUMB_RATIO_MAX:g} -> other"
    return {
        "category": "other" if reason else category, "reason": reason, "why": why, "ratio_hit": ratio_hit,
        "ratio": ratio, "source": source, "minutes": minutes, "thumb": has_video_thumb(message),
    }


def _log_series_route(message, classified, gated, final, gate, info):
    """ONE INFO line per series candidate: every file the classifier called series, and
    every file it looked at as a series but rejected (sample / promo, no second source,
    course name, ...). Says what it was (cleaned title, language), where the verdict came
    from (tmdb | tvmaze | wikidata | openai | fallback), how it scored, what the thumbnail
    and the duration looked like and where it went."""
    s = info.get("series") or {}
    notes = []
    if gate and gate["reason"]:
        notes.append(gate["reason"])
    if s.get("reason") and s["reason"] != "ok":
        notes.append(s["reason"])
    if s.get("ambiguous"):
        notes.append("ambiguous franchise version")
    if s.get("season_pack") or s.get("pack"):
        notes.append("season pack")
    if s.get("tv_source"):
        notes.append("tv-source tag")
    if final != gated:
        notes.append("no channel mapped -> other")
    gate = gate or {}
    ratio = gate.get("ratio")
    minutes = gate["minutes"] if "minutes" in gate else movie_duration_minutes(message)
    runtime = s.get("runtime")
    thumb = gate["thumb"] if "thumb" in gate else has_video_thumb(message)
    logger.info(
        "autosort series-route: msg=%s name=%r title=%r lang=%s class=%s source=%s id=%s score=%s signals=%s "
        "thumb=%s ratio=%s src=%s dur=%s rt=%s final=%s reason=%s",
        message.id, _movie_label(message), s.get("title") or "-", s.get("language") or "-", classified,
        s.get("source") or "-", s.get("id") or "-",
        s.get("score", 0), ",".join(s.get("signals") or []) or "-", "yes" if thumb else "no",
        f"{ratio:.2f}" if ratio is not None else "-", gate.get("source", "-"),
        f"{minutes:.0f}m" if minutes is not None else "-", f"{runtime}m" if runtime else "-",
        final, "; ".join(notes) or "-",
    )


def _log_series_to_anime(message, classified, final, info):
    """ONE INFO line per file that looked like a series episode but the classifier (TMDB or
    AniList, the anime authorities) moved to anime - so a wrong anime routing is easy to find."""
    s = info.get("series") or {}
    logger.info(
        "autosort series->anime: msg=%s name=%r title=%r lang=%s class=%s final=%s reason=%s",
        message.id, _movie_label(message), s.get("title") or "-", s.get("language") or "-", classified, final,
        s.get("to_anime") or "anime verdict",
    )


# ── Anime-channel gate ──────────────────────────────────────
# The classifier (deep_anime=True) only says "anime" for a file AniList / Jikan / TMDB / the anime
# list vouch for; this gate then checks the file itself. Same style as _series_gate().
ANIME_GATE_CATEGORIES = ("anime", "anime_files")


def _anime_gate(message, category, info):
    """Checks a file the classifier called anime / anime_files. First failure wins and the file
    becomes "other" (never dropped):
      1. no thumbnail at all (ANIME_REQUIRE_THUMB)  -> a blank preview is not a proper episode
      2. duration known and < ANIME_MIN_MINUTES     -> a clip, opening, AMV
      3. duration known and > ANIME_MAX_MINUTES     -> a compilation or marathon
                                                        (not for a season pack, "S01": that IS long - SEASONPACK_SKIP_MAX_DURATION)
      4. thumbnail ratio outside the band / unknown -> poster / portrait / square thumbnail
    All four apply to a NATIVE video only: a video sent as a document has no preview size and no
    duration in Telegram, so it skips them all. A split part skips 2 and 3 (SPLIT_PARTS_SKIP_DURATION);
    the thumbnail rules (1, 4) still apply to it. The video's own width / height never rescues a
    missing thumbnail, and there is no file-size or bitrate rule.
    No thumbnail counts as a thumbnail reject (ratio_hit), so it shares the "Ratio->Other" counter.
    Returns a dict: category (final), reason (None = unchanged), why (no_thumb | duration | ratio),
    ratio_hit, ratio, source, minutes, thumb (does the file have a thumbnail)."""
    native = getattr(message, "video", None) is not None
    split = native and SPLIT_PARTS_SKIP_DURATION and is_split_video_part(message)
    # a season pack ("My Hero Academia S01 ... Multi Audio") is a whole season in one file: ANIME_MAX_MINUTES does not apply
    pack = bool(SEASONPACK_SKIP_MAX_DURATION and ((info.get("anime") or {}).get("pack") or (info.get("series") or {}).get("pack")))
    if native:
        ok, ratio, source = movie_thumb_gate(
            message, ANIME_THUMB_RATIO_MIN, ANIME_THUMB_RATIO_MAX, ANIME_RATIO_UNKNOWN,
            require_thumb=ANIME_REQUIRE_THUMB)
    else:
        ok, ratio, source = True, None, "document"
    minutes = movie_duration_minutes(message) if native else None
    reason, why, ratio_hit = None, None, False
    if source == "no-thumb":
        ratio_hit, why = True, "no_thumb"
        reason = "no thumbnail -> other"
    elif minutes is not None and not split and minutes < ANIME_MIN_MINUTES:
        reason, why = f"duration {minutes:.1f}m below {ANIME_MIN_MINUTES:g}m -> other", "duration"
    elif minutes is not None and not split and not pack and minutes > ANIME_MAX_MINUTES:
        reason, why = f"duration {minutes:.0f}m above {ANIME_MAX_MINUTES:g}m -> other", "duration"
    elif not ok:
        ratio_hit, why = True, "ratio"
        if ratio is None:
            reason = "ratio unknown -> other"
        elif ratio < 1.0:
            reason = f"ratio {ratio:.2f} poster -> other"
        else:
            reason = f"ratio {ratio:.2f} outside {ANIME_THUMB_RATIO_MIN:g}-{ANIME_THUMB_RATIO_MAX:g} -> other"
    return {
        "category": "other" if reason else category, "reason": reason, "why": why, "ratio_hit": ratio_hit,
        "ratio": ratio, "source": source, "minutes": minutes, "thumb": has_video_thumb(message),
    }


def _log_anime_route(message, classified, gated, final, gate, info):
    """ONE INFO line per file the classifier called anime / anime_files, and per file where a TMDB anime
    verdict was downgraded (the old keyword rule said anime, the new rules did not - e.g. Blue Eye
    Samurai). Says what it was (cleaned title, language, season / episode), which source decided
    (anilist | animelist | tmdb | jikan | -) and what that source said (AniList id, format, origin),
    what the thumbnail and the duration looked like and where it went."""
    a = info.get("anime") or {}
    notes = []
    if gate and gate.get("reason"):
        notes.append(gate["reason"])
    if a.get("downgraded"):
        notes.append("downgraded: " + a["downgraded"])
    notes += list(a.get("notes") or [])
    if a.get("pack"):
        notes.append("season pack")
    if final != gated:
        notes.append("no channel mapped -> other")
    gate = gate or {}
    ratio = gate.get("ratio")
    minutes = gate["minutes"] if "minutes" in gate else movie_duration_minutes(message)
    thumb = gate["thumb"] if "thumb" in gate else has_video_thumb(message)
    if "ratio" not in gate:
        _ok, ratio, src = movie_thumb_gate(message)
    else:
        src = gate.get("source", "-")
    logger.info(
        "autosort anime-route: msg=%s name=%r title=%r lang=%s class=%s source=%s anilist=%s mal=%s fmt=%s origin=%s "
        "season=%s ep=%s signals=%s thumb=%s ratio=%s src=%s dur=%s final=%s reason=%s",
        message.id, _movie_label(message), a.get("title") or "-", a.get("language") or "-", classified,
        a.get("source") or "-", a.get("anilist_id") or "-", a.get("mal_id") or "-", a.get("format") or "-",
        a.get("origin") or "-", a.get("season") or "-", a.get("episode") or "-",
        ",".join(a.get("signals") or []) or "-", "yes" if thumb else "no",
        f"{ratio:.2f}" if ratio is not None else "-", src,
        f"{minutes:.0f}m" if minutes is not None else "-", final, "; ".join(notes) or "-",
    )


# ── Education-channel gate ──────────────────────────────────
# Same shared thumbnail rule as the other native-video channels, for the education VIDEO channel only: an
# "education_files" document is never gated. No duration limits (a lecture can be any length).
EDUCATION_GATE_CATEGORIES = ("education",)


def _education_gate(message, category, info):
    """Checks a file the classifier called education (a NATIVE video). First failure wins and the file becomes
    "other" (never dropped):
      1. no thumbnail at all (EDUCATION_REQUIRE_THUMB) -> the video's own width / height never rescues it (no_thumb)
      2. thumbnail ratio outside EDUCATION_THUMB_RATIO_MIN..MAX (default 1.30..2.40) or unknown
         (EDUCATION_RATIO_UNKNOWN, default other) -> poster / portrait / square thumbnail (ratio)
    No duration rule. A document ("education_files") is not gated; EDUCATION_THUMB_GATE=0 switches the gate off.
    No thumbnail counts as a thumbnail reject (ratio_hit), so it shares the "Ratio->Other" counter.
    Returns a dict like _series_gate(): category (final), reason (None = unchanged), why (no_thumb | ratio), ratio_hit,
    ratio, source, minutes, thumb."""
    native = getattr(message, "video", None) is not None
    if native and EDUCATION_THUMB_GATE:
        ok, ratio, source = movie_thumb_gate(
            message, EDUCATION_THUMB_RATIO_MIN, EDUCATION_THUMB_RATIO_MAX, EDUCATION_RATIO_UNKNOWN,
            require_thumb=EDUCATION_REQUIRE_THUMB)
    else:
        ok, ratio, source = True, None, "document" if not native else "off"
    minutes = movie_duration_minutes(message) if native else None
    reason, why, ratio_hit = None, None, False
    if source == "no-thumb":
        ratio_hit, why = True, "no_thumb"
        reason = "no thumbnail -> other"
    elif not ok:
        ratio_hit, why = True, "ratio"
        if ratio is None:
            reason = "ratio unknown -> other"
        elif ratio < 1.0:
            reason = f"ratio {ratio:.2f} poster -> other"
        else:
            reason = f"ratio {ratio:.2f} outside {EDUCATION_THUMB_RATIO_MIN:g}-{EDUCATION_THUMB_RATIO_MAX:g} -> other"
    return {
        "category": "other" if reason else category, "reason": reason, "why": why, "ratio_hit": ratio_hit,
        "ratio": ratio, "source": source, "minutes": minutes, "thumb": has_video_thumb(message),
    }


def _log_education_route(message, classified, gated, final, gate, info):
    """ONE INFO line per file the classifier called education: what it was, what the thumbnail looked like, where it went."""
    gate = gate or {}
    notes = []
    if gate.get("reason"):
        notes.append(gate["reason"])
    if final != gated:
        notes.append("no channel mapped -> other")
    ratio, minutes = gate.get("ratio"), gate.get("minutes")
    logger.info(
        "autosort education-route: msg=%s name=%r class=%s thumb=%s ratio=%s src=%s dur=%s final=%s reason=%s",
        message.id, _movie_label(message), classified, "yes" if gate.get("thumb") else "no",
        f"{ratio:.2f}" if ratio is not None else "-", gate.get("source", "-"),
        f"{minutes:.0f}m" if minutes is not None else "-", final, "; ".join(notes) or "-",
    )


# ── Why did a file end in Other? ────────────────────────────
# Reasons (stats["other_why"], the "autosort other-route" log line, the "Other because:" block of the
# final message):
#   tmdb_error    TMDB did not answer, even after the retries
#   unidentified  the name has no usable title, or the classifier gave up without a better reason
#   not_found     the title was searched (TMDB / TVmaze / Wikidata ...) and nothing matched
#   extra         sample / trailer / promo / recap / NCOP-style name
#   education_vetoed  course words in the name, but a source knows the show (the veto is kept, nothing says "course")
#   season_pack   a season pack ("S01 Combined") of a show no source vouches for, and the marker alone did not pass
#   archive       (the classifier's reason for an archive; it goes to the Files channel, and to Other as
#                 "unmapped" when no Files channel is mapped)
#   other_type    not a video / pdf / image / audio / archive, or copy_message could not rebuild it
#   unparseable   no usable title in any of the candidate names (file name + every caption line)
#   nsfw_unmapped adult-content keywords, no NSFW channel mapped (decided before any lookup)
#   no_thumb | ratio | duration | runtime   a movie / series / anime gate rejected a NATIVE video
#   unmapped      the category is fine, but no channel is mapped for it in /settings
# (the same reasons by the names you may be used to: gate_thumbnail = no_thumb / ratio, gate_runtime = runtime / duration,
#  no_channel_mapped = unmapped.) A file the classifier gave up on had a FINAL CHECK first (plugins/classify.py _final_check()):
#  the detail then ends with "final pass: N names, M titles, sources ...".
def _other_why(skipped, classified, gated, final, gate, info):
    """(reason, detail) for a file that ended in Other. skipped = the SKIP category classify_async
    returned (tmdb_error / unidentified) or None; gate = the movie / series / anime gate dict or None."""
    if skipped == TMDB_UNAVAILABLE:
        n = info.get("tmdb_retries", 0)
        return "tmdb_error", f"TMDB did not answer ({n} retr{'y' if n == 1 else 'ies'})"
    if skipped:
        return "unidentified", "the classifier could not identify it"
    if gate and gate.get("reason"):
        return gate.get("why") or "unidentified", gate["reason"].replace(" -> other", "")
    if gated != final:
        return "unmapped", f"no channel mapped for {gated}"
    why = info.get("other_reason")
    if why:
        detail = info.get("other_detail") or "-"
        fp = info.get("final_pass") or {}
        if fp.get("names") and fp.get("summary") and "final pass" not in detail:
            detail += f" | final pass: {fp['summary']}"
        return why, detail
    return "unidentified", "classified as other, the classifier reported no reason"


def _nsfw_first(message) -> bool:
    """AUTOSORT_NSFW_FIRST: True when the file's name / caption carries adult-content keywords. Never raises (a failure = False =
    the old behaviour for this file)."""
    if not AUTOSORT_NSFW_FIRST:
        return False
    try:
        return bool(is_nsfw(message))
    except Exception as e:
        logger.warning("autosort: NSFW check failed for msg %s (%s) - normal classification", message.id, e)
        return False


def _step2(message, classified, caption, facts):
    """Step 2: decide the category from the EDITED caption. The caption says movie (no season / episode) or episodic; the file's
    own type says "_files" (a document) or video; series vs anime comes from the Step 1 record (TMDB / AniList are not asked
    again). Consistency check: the caption must read back as the same type, season / episode and year as the record. Any
    disagreement is logged (WARNING) and the Step 1 result is returned. -> the category."""
    if not AUTOSORT_STEP2_CHECK or not caption or not facts:
        return classified
    got = parse_edited_caption(caption)
    kind = facts["kind"]
    decided = kind + ("_files" if getattr(message, "document", None) is not None else "")
    problems = []
    if got is None:
        problems.append("the edited caption cannot be read back")
    else:
        if got["episodic"] != (kind != "movie"):
            problems.append(f"type: caption reads {'episodic' if got['episodic'] else 'movie'}, record says {kind}")
        if facts["year"] and got["year"] != facts["year"]:
            problems.append(f"year: caption {got['year']}, record {facts['year']}")
        if (facts["season"], facts["episode"]) != (got["season"], got["episode"]):
            problems.append(f"season/episode: caption {got['season']}/{got['episode']}, record {facts['season']}/{facts['episode']}")
    if decided != classified:
        problems.append(f"category: caption + file type say {decided}, Step 1 said {classified}")
    if problems:
        logger.warning("autosort step2: msg=%s name=%r the edited caption disagrees with the identification (%s) - "
                       "using the Step 1 result %s", message.id, _movie_label(message), "; ".join(problems), classified)
        return classified
    return decided


def _edited_caption(message, classified, info, remove_links):
    """Step 1 + Step 2 for ONE file. -> (caption for copy_message | None, category). NSFW / pdf / education / audio / image / files /
    other keep their caption with only the links removed (autosort_caption does that); a movie / series / anime that was identified
    gets the caption rebuilt from the identification record; one that was not identified keeps today's file-name caption."""
    ident = (info or {}).get("ident")
    caption = facts = None
    if ident:
        caption, facts = ident_caption(message, classified, ident)
    if caption is None:
        return autosort_caption(message, classified, remove_links), classified
    classified = _step2(message, classified, caption, facts)
    logger.info("autosort caption: msg=%s name=%r class=%s record=%s/%s/%s id=%s -> %r", message.id, _movie_label(message),
                classified, ident.get("source") or "-", ident.get("title"), ident.get("year") or "-", ident.get("id") or "-",
                caption.split("\n", 1)[0][:80])
    return caption, classified


def _msg_log(message, classified, final, outcome, reason):
    """AUTOSORT_MSG_LOG: ONE INFO line per message of the range - its single outcome
    (forwarded | filtered | duplicate | skipped | error | empty). Never raises."""
    if not AUTOSORT_MSG_LOG:
        return
    try:
        logger.info("autosort msg: id=%s name=%r class=%s final=%s outcome=%s reason=%s",
                    getattr(message, "id", "-"), _movie_label(message), classified or "-", final or "-", outcome,
                    (str(reason) if reason else "-")[:200])
    except Exception:
        pass


def _log_other_route(message, classified, final, why, detail):
    """ONE INFO line per file that ended in Other: what it was, what the classifier said, why."""
    logger.info("autosort other-route: msg=%s name=%r class=%s final=%s reason=%s detail=%s",
                message.id, _movie_label(message), classified, final, why, (detail or "-")[:240])


async def _run(client, user_id, m, cfg):
    mapping = cfg["mapping"]
    from_chat = cfg["from_chat"]
    stats = {"fetched": 0, "forwarded": 0, "unsorted": 0, "errors": 0, "filtered": 0, "fallback": 0, "duplicate": 0,
             "ratio_other": 0, UNIDENTIFIED: 0, TMDB_UNAVAILABLE: 0, "other_why": {}, "calls": {}, "hits": {},
             "anilist_on": False, "start": time.time(), "empty": 0, "skipped": 0, "files_fallback": 0}
    for category in mapping:
        stats[category] = 0

    total_files = max(int(cfg["last_msg_id"]) - int(cfg["skip"]), 1)
    pacing = {"delay": _base_delay(total_files), "min": _base_delay(total_files), "floodwaits": 0, "streak": 0}

    tmdb_session = _CountingSession(aiohttp.ClientSession(), stats["calls"]) if aiohttp else None
    tmdb_key = await db.get_tmdb_key(user_id)
    use_anilist = await db.get_anilist_enabled(user_id)
    anilist_key = await db.get_anilist_key(user_id)
    stats["anilist_on"] = bool(use_anilist)
    if not use_anilist:
        logger.info("autosort: AniList is OFF for this run (/settings): anime candidates are checked with TMDB, "
                    "the anime list and Jikan only")
    # /settings -> Filters enforcement (message type toggles, extension
    # exclude-list, keyword include-list, file size limit, movie/series
    # whitelists) - see plugins/channels.py's check_filters(). Duplicate
    # skipping is always on here (same file_unique_id seen earlier in
    # this run), whatever the "Skip Duplicate" toggle says.
    configs = await db.get_configs(user_id)
    disabled_types = set(await db.get_filters(user_id))
    seen_file_ids = set()
    other_dest = mapping["other"]
    dup_dest = mapping.get("duplicate")
    remove_links = AUTOSORT_REMOVE_LINKS or bool(configs.get("remove_caption_link"))
    dest_chats = list(set(mapping.values()))
    live = _LIVE[user_id] = {"stats": stats, "mapping": mapping, "pacing": pacing, "total": total_files, "paused": False}
    await _notify(client, user_id, "Forwarding Started", mapping)

    try:
        pling = 0
        async for message in client.iter_messages(
            client,
            chat_id=from_chat,
            limit=int(cfg["last_msg_id"]),
            offset=cfg["skip"],
            continuous=False,
        ):
            # Pause button: wait here (Cancel still works while paused)
            if live["paused"] and not temp.CANCEL.get(user_id):
                await _safe_edit(m, _progress_text(stats, mapping, pacing, total_files, "Paused"), _status_markup("paused"))
                paused_at = time.time()
                while live["paused"] and not temp.CANCEL.get(user_id):
                    await asyncio.sleep(1)
                stats["paused_secs"] = stats.get("paused_secs", 0) + time.time() - paused_at
                if not temp.CANCEL.get(user_id):
                    await _safe_edit(m, _progress_text(stats, mapping, pacing, total_files), _status_markup("running"))

            if temp.CANCEL.get(user_id):
                await _safe_edit(m, _progress_text(stats, mapping, pacing, total_files, "cancelled"), _status_markup("cancelled"))
                await _notify(client, user_id, "Forwarding Process Cancelled", mapping)
                break

            if pling % 20 == 0:
                await _safe_edit(m, _progress_text(stats, mapping, pacing, total_files), _status_markup("running"))
            pling += 1
            stats["fetched"] += 1

            try:
                if message.empty or message.service:
                    if AUTOSORT_MSG_LOG:
                        stats["empty"] = stats.get("empty", 0) + 1     # counted apart, not an error
                        _msg_log(message, "-", "-", "empty", "empty or service message")
                    else:
                        stats["errors"] += 1
                    continue

                allowed, reason = check_filters(message, configs, disabled_types, seen_file_ids)
                if not allowed:
                    if reason == "duplicate" and dup_dest is not None:
                        # Duplicate channel is set -> forward the repeat there
                        # instead of skipping it.
                        ok = await _copy_with_retry(
                            client, dup_dest, from_chat, message.id, m, stats, mapping, pacing, total_files,
                            caption=autosort_caption(message, None, remove_links),
                        )
                        if not ok:
                            ok = await _copy_with_retry(
                                client, dup_dest, from_chat, message.id, m, stats, mapping, pacing, total_files,
                                forward=True,
                            )
                        _msg_log(message, "duplicate", "duplicate" if ok else "-", "duplicate" if ok else "error",
                                 "same file seen earlier in this run" if ok else "copy to the Duplicate channel failed")
                        if ok:
                            stats["duplicate"] += 1
                            pacing["streak"] += 1
                            if pacing["streak"] % EASE_DOWN_EVERY == 0 and pacing["delay"] > pacing["min"]:
                                pacing["delay"] = max(pacing["min"], pacing["delay"] * EASE_DOWN_FACTOR)
                        else:
                            stats["errors"] += 1
                        await asyncio.sleep(pacing["delay"])
                        continue
                    stats[reason] = stats.get(reason, 0) + 1
                    _msg_log(message, "-", "-", reason, "/settings filters" if reason == "filtered" else "same file seen earlier")
                    continue

                new_text = None
                if remove_links and message.text:
                    new_text = text_without_links(message)
                    if new_text == "":
                        # a message that was nothing but links: nothing left to forward
                        stats["filtered"] = stats.get("filtered", 0) + 1
                        _msg_log(message, "text", "-", "filtered", "text was only links")
                        continue

                skipped = None
                nsfw_hit = _nsfw_first(message)
                if nsfw_hit:
                    # 0) NSFW keywords: decided here, before any TMDB / AniList call. The NSFW channel when one is mapped, else Other.
                    info = {}
                    category = nsfw_category(message, mapping) or "other"
                    if category == "other":
                        info = {"other_reason": "nsfw_unmapped",
                                "other_detail": "adult-content keywords in the name / caption, no NSFW channel is mapped"}
                else:
                    # classify_async (deep_series / deep_anime, /autosort only), asked again when TMDB failed
                    category, info, _retries = await _classify_with_retry(
                        message, tmdb_session, tmdb_key, use_anilist, anilist_key)
                    if category in SKIP_CATEGORIES:
                        # TMDB unreachable (after the retries) -> don't drop it, put it in Other.
                        stats[category] += 1
                        skipped = category
                        category = "other"
                classified = category
                _count_hit(stats, classified, info)
                # Step 1 (caption from the identification record) + Step 2 (category from the edited caption). Done before the
                # gates; a failure here falls back to today's caption and the classifier's category.
                cap_class = "nsfw" if nsfw_hit else classified          # an adult file never gets a rebuilt caption (not even the "other" one)
                try:
                    caption, classified = _edited_caption(message, cap_class, info, remove_links)
                    if nsfw_hit:
                        classified = category
                except Exception as e:
                    logger.warning("autosort: caption step failed for msg %s (%s: %s) - today's caption", message.id, type(e).__name__, e)
                    caption = autosort_caption(message, cap_class, remove_links)
                    if nsfw_hit:
                        classified = category
                # Movie channel gate (duration / thumbnail ratio / TMDB runtime). Done
                # here, not in classify_async(), which /fwd shares. A file that fails
                # is not dropped, filtered or counted as an error: it goes to Other.
                gate = sgate = agate = egate = None
                if classified in MOVIE_GATE_CATEGORIES:
                    gate = await _movie_gate(message, classified, info, tmdb_session, tmdb_key)
                    category = gate["category"]
                elif classified in SERIES_GATE_CATEGORIES:
                    # Same idea for the Series channel: episode length + landscape thumbnail.
                    sgate = _series_gate(message, classified, info)
                    category = sgate["category"]
                elif classified in ANIME_GATE_CATEGORIES:
                    # ... and for the Anime channel: a thumbnail, a normal length, a landscape thumbnail.
                    agate = _anime_gate(message, classified, info)
                    category = agate["category"]
                elif classified in EDUCATION_GATE_CATEGORIES:
                    # ... and for the Education VIDEO channel: a thumbnail of a landscape shape (no length rule).
                    try:
                        egate = _education_gate(message, classified, info)
                        category = egate["category"]
                    except Exception as e:                    # new rule: one warning, the old behaviour for this file
                        logger.warning("autosort: education thumbnail rule failed for msg %s (%s) - not gated", message.id, e)
                        egate = None
                gated = category
                dest = mapping.get(category)
                twin = _files_fallback(category, mapping) if dest is None else None
                if twin:
                    # AUTOSORT_FILES_FALLBACK: "movie_files" has no channel -> the Movie channel (and the other way round)
                    stats["files_fallback"] = stats.get("files_fallback", 0) + 1
                    logger.info("autosort files-fallback: msg=%s name=%r %s has no channel -> %s",
                                message.id, _movie_label(message), category, twin)
                    category = gated = twin
                    dest = mapping[twin]
                if dest is None:
                    # No channel mapped for this category -> Other.
                    stats["unsorted"] += 1
                    category = "other"
                    dest = other_dest
                why = detail = None
                if category == "other":
                    why, detail = _other_why(skipped, classified, gated, category, gate or sgate or agate or egate, info)
                if gate is not None or (info.get("tmdb_media") == "movie" and classified.startswith("anime")):
                    _log_movie_route(message, classified, gated, category, gate, info)
                if sgate is not None or (info.get("series") or {}).get("candidate"):
                    _log_series_route(message, classified, gated, category, sgate, info)
                if classified.startswith("anime") and (info.get("series") or {}).get("to_anime"):
                    _log_series_to_anime(message, classified, category, info)
                if classified.startswith("anime") or (info.get("anime") or {}).get("downgraded"):
                    _log_anime_route(message, classified, gated, category, agate or sgate or gate, info)
                if egate is not None:
                    _log_education_route(message, classified, gated, category, egate, info)

                # Caption: movie / series / anime (+ "_files") get a rebuilt caption (name, year or season-episode,
                # quality ...); every other category keeps its caption, only the links are removed. Decided by the
                # classifier's category, so a real movie a gate sent to Other is still captioned. None = leave as is.
                ok = await _copy_with_retry(
                    client, dest, from_chat, message.id, m, stats, mapping, pacing, total_files,
                    caption=caption, text=new_text,
                )
                if not ok and new_text is None:
                    # Unsupported by copy_message -> plain-forward it to Other.
                    ok = await _copy_with_retry(
                        client, other_dest, from_chat, message.id, m, stats, mapping, pacing, total_files,
                        forward=True,
                    )
                    if ok:
                        stats["fallback"] += 1
                        category = "other"
                        why, detail = "other_type", "copy_message failed, forwarded to Other"
                if ok:
                    stats["forwarded"] += 1
                    stats[category] = stats.get(category, 0) + 1     # FINAL category ("other" for a gated movie / series / anime)
                    if category == "other":
                        why = why or "unidentified"
                        stats["other_why"][why] = stats["other_why"].get(why, 0) + 1
                        _log_other_route(message, classified, category, why, detail)
                    _msg_log(message, classified, category, "forwarded", why or ("files fallback" if twin else "-"))
                    if ((gate is not None and gate["ratio_hit"]) or (sgate is not None and sgate["ratio_hit"])
                            or (agate is not None and agate["ratio_hit"]) or (egate is not None and egate["ratio_hit"])):
                        stats["ratio_other"] += 1
                    # Clean run for a while -> ease the pace back down toward the
                    # baseline instead of staying at whatever a past FloodWait
                    # pushed it to forever.
                    pacing["streak"] += 1
                    if pacing["streak"] % EASE_DOWN_EVERY == 0 and pacing["delay"] > pacing["min"]:
                        pacing["delay"] = max(pacing["min"], pacing["delay"] * EASE_DOWN_FACTOR)
                else:
                    stats["errors"] += 1
                    _msg_log(message, classified, category, "error", "copy and forward both failed")

                await asyncio.sleep(pacing["delay"])
            except Exception as e:
                # AUTOSORT_MSG_LOG: one failed message (TMDB / caption / copy) never stops the run or skips the next ones
                if not AUTOSORT_MSG_LOG:
                    raise
                stats["errors"] += 1
                logger.warning("autosort: msg %s failed (%s: %s) - counted as error, run continues",
                               getattr(message, "id", "-"), type(e).__name__, e)
                _msg_log(message, "-", "-", "error", f"{type(e).__name__}: {e}")
                await asyncio.sleep(pacing["delay"])
        else:
            await _safe_edit(m, _progress_text(stats, mapping, pacing, total_files, "completed"), _status_markup("completed"))
            await _notify(client, user_id, "Forwarding Completed", mapping)
    except Exception as e:
        await _safe_edit(m, f"<b>ERROR:</b>\n<code>{e}</code>")
    finally:
        _LIVE.pop(user_id, None)
        if tmdb_session:
            await tmdb_session.close()
        for c in dest_chats:
            if c in temp.IS_FRWD_CHAT:
                temp.IS_FRWD_CHAT.remove(c)
        temp.lock[user_id] = False
        try:
            await client.stop()
        except Exception:
            pass


@Client.on_callback_query(filters.regex(r"^autosort_cancel$"))
async def autosort_cancel(bot: Client, query):
    user_id = query.from_user.id
    temp.CANCEL[user_id] = True
    await query.answer("Cancelling after the current file finishes...", show_alert=True)


@Client.on_callback_query(filters.regex(r"^autosort_prog$"))
async def autosort_prog(bot: Client, query):
    run = _LIVE.get(query.from_user.id)
    if not run:
        return await query.answer("This task has finished.", show_alert=True)
    await query.answer(_category_popup(run["stats"], run["mapping"]), show_alert=True)


@Client.on_callback_query(filters.regex(r"^autosort_pause$"))
async def autosort_pause(bot: Client, query):
    run = _LIVE.get(query.from_user.id)
    if not run:
        return await query.answer("No auto-sort task is running.", show_alert=True)
    run["paused"] = not run["paused"]
    await query.answer("Paused. Tap Resume to continue." if run["paused"] else "Resumed.")
    await _safe_edit(
        query.message,
        _progress_text(run["stats"], run["mapping"], run["pacing"], run["total"], "Paused" if run["paused"] else "Forwarding"),
        _status_markup("paused" if run["paused"] else "running"),
    )
