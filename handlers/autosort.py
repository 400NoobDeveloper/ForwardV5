# ============================================================
# /autosort — one pass over a source channel that classifies
# every file (plugins/classify.py) and copies it straight to
# whichever destination channel the user mapped to that category
# in /settings -> Channels -> "Auto-Sort Channels".
#
# Unlike /fwd (plugins/public.py + plugins/regix.py), which always
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
# marker, see plugins/classify.py "deep SERIES identification") with a normal episode length
# and a landscape thumbnail go there (see _series_gate()); the rest goes to Other.
# Everything else that can't be sorted into a specific
# category (no channel mapped for it, TMDB unreachable, unsupported
# message type, ...) is added to the "Other" channel, so nothing has
# to be forwarded by hand afterwards.
# ============================================================

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
from plugins.controls import Translation, CLIENT, start_clone_bot, target_link, frwd_status_text
from plugins.classify import (
    classify_async, CATEGORIES, VIDEO_CATEGORIES, SKIP_CATEGORIES,
    UNIDENTIFIED, TMDB_UNAVAILABLE, ENV_TMDB_API_KEY,
    get_filename, movie_thumb_gate, movie_duration_minutes, tmdb_movie_runtime, runtime_mismatch,
    MOVIE_MIN_MINUTES, MOVIE_THUMB_RATIO_MIN, MOVIE_THUMB_RATIO_MAX,
    SERIES_MIN_MINUTES, SERIES_MAX_MINUTES, SERIES_THUMB_RATIO_MIN, SERIES_THUMB_RATIO_MAX, SERIES_RATIO_UNKNOWN,
)
from pyrogram.enums import ParseMode
from plugins.utils import check_filters, caption_for_copy, text_without_links

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger(__name__)

CLIENT = CLIENT()

# run_id -> pending run config, populated by /autosort, consumed by the
# Yes/No confirmation callback. Same short-lived, in-memory pattern as
# plugins/utils.py's STS, just scoped to this one command.
_PENDING: dict = {}
_next_id = 0


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
    unmapped = [c for c in CATEGORIES if c not in mapping]
    unmapped_note = (
        f"\n\n<i>Not configured (will go to Other): {', '.join(unmapped)}</i>"
        if unmapped else ""
    )

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
    await message.reply_text(
        f"<b>DOUBLE CHECKING ⚠️</b>\n\n"
        f"<b>FROM CHANNEL:</b> <code>{title}</code>\n"
        f"<b>SKIP MESSAGES:</b> <code>{skipno.text}</code>\n"
        f"<b>APPROX FILES:</b> <code>{total_files}</code>\n"
        f"<b>ESTIMATED TIME:</b> <code>~{_format_duration(est_seconds)}</code> "
        f"<i>(paced automatically, speeds up/slows down live to dodge FloodWaits)</i>\n\n"
        f"<b>Routing:</b>\n{mapping_lines}{unmapped_note}\n\n"
        f"<i>{dup_note} Anything your /settings filters exclude is skipped. "
        f"Everything else is forwarded.</i>\n\n"
        f"<i>Your bot/userbot must be admin in every destination channel listed above.</i>\n\n"
        f"Click Yes to start.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


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
    return Translation.TEXT.format(
        f"{fetched}/{total_files}", stats["forwarded"], stats["duplicate"], stats["errors"],
        stats["filtered"], status, "{:.1f}".format(pct), _format_duration(eta), progress,
    )


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


# ── Movie-channel gate ──────────────────────────────────────
# classify_async() is shared with /fwd, so the extra checks that keep the Movie
# channel to real, landscape-thumbnail movies live here, not in the classifier.
MOVIE_GATE_CATEGORIES = ("movie", "movie_files")


async def _movie_gate(message, category, info, session, tmdb_key):
    """Checks a file the classifier called movie / movie_files. First failure
    wins and the file becomes "other" (never dropped):
      1. duration known and < MOVIE_MIN_MINUTES     -> a clip, trailer or short
      2. thumbnail ratio outside the band / unknown -> poster-style thumbnail
      3. duration vs TMDB runtime off by > tolerance -> not that film
    Returns a dict: category (final), reason (None = unchanged), ratio_hit
    (failed on the ratio gate), ratio, source, minutes, tmdb_id."""
    ok, ratio, source = movie_thumb_gate(message)
    minutes = movie_duration_minutes(message)
    tmdb_id = info.get("tmdb_id") if info.get("tmdb_media") == "movie" else None
    reason, ratio_hit = None, False
    if minutes is not None and minutes < MOVIE_MIN_MINUTES:
        reason = f"duration {minutes:.0f}m -> other"
    elif not ok:
        ratio_hit = True
        if ratio is None:
            reason = "ratio unknown -> other"
        elif ratio < 1.0:
            reason = f"ratio {ratio:.2f} poster -> other"
        else:
            reason = f"ratio {ratio:.2f} outside {MOVIE_THUMB_RATIO_MIN:g}-{MOVIE_THUMB_RATIO_MAX:g} -> other"
    elif tmdb_id and minutes is not None:
        # only worth a TMDB call when the file's own length is known
        runtime = await tmdb_movie_runtime(session, tmdb_id, tmdb_key)
        if runtime_mismatch(minutes, runtime):
            reason = f"runtime mismatch {minutes:.0f}m vs TMDB {runtime}m -> other"
    return {
        "category": "other" if reason else category, "reason": reason, "ratio_hit": ratio_hit,
        "ratio": ratio, "source": source, "minutes": minutes, "tmdb_id": tmdb_id,
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
      1. duration known and < SERIES_MIN_MINUTES     -> a clip, promo or teaser
      2. duration known and > SERIES_MAX_MINUTES     -> a compilation, marathon or film
      3. thumbnail ratio outside the band / unknown  -> poster-style thumbnail
    Documents carry no duration, so for them only the thumbnail is checked. The show's
    own runtime (TMDB / TVmaze) is NOT a veto here, it is only logged.
    Returns a dict: category (final), reason (None = unchanged), ratio_hit, ratio,
    source, minutes."""
    ok, ratio, source = movie_thumb_gate(message, SERIES_THUMB_RATIO_MIN, SERIES_THUMB_RATIO_MAX, SERIES_RATIO_UNKNOWN)
    minutes = movie_duration_minutes(message) if getattr(message, "video", None) is not None else None
    reason, ratio_hit = None, False
    if minutes is not None and minutes < SERIES_MIN_MINUTES:
        reason = f"duration {minutes:.0f}m below {SERIES_MIN_MINUTES:g}m -> other"
    elif minutes is not None and minutes > SERIES_MAX_MINUTES:
        reason = f"duration {minutes:.0f}m above {SERIES_MAX_MINUTES:g}m -> other"
    elif not ok:
        ratio_hit = True
        if ratio is None:
            reason = "ratio unknown -> other"
        elif ratio < 1.0:
            reason = f"ratio {ratio:.2f} poster -> other"
        else:
            reason = f"ratio {ratio:.2f} outside {SERIES_THUMB_RATIO_MIN:g}-{SERIES_THUMB_RATIO_MAX:g} -> other"
    return {
        "category": "other" if reason else category, "reason": reason, "ratio_hit": ratio_hit,
        "ratio": ratio, "source": source, "minutes": minutes,
    }


def _log_series_route(message, classified, gated, final, gate, info):
    """ONE INFO line per series candidate: every file the classifier called series, and
    every file it looked at as a series but rejected (sample / promo, no second source,
    course name, ...). Says what it was, how it scored and where it went."""
    s = info.get("series") or {}
    notes = []
    if gate and gate["reason"]:
        notes.append(gate["reason"])
    if s.get("reason") and s["reason"] != "ok":
        notes.append(s["reason"])
    if final != gated:
        notes.append("no channel mapped -> other")
    gate = gate or {}
    ratio = gate.get("ratio")
    minutes = gate["minutes"] if "minutes" in gate else movie_duration_minutes(message)
    runtime = s.get("runtime")
    logger.info(
        "autosort series-route: msg=%s name=%r class=%s source=%s id=%s score=%s signals=%s ratio=%s src=%s dur=%s rt=%s final=%s reason=%s",
        message.id, _movie_label(message), classified, s.get("source") or "-", s.get("id") or "-",
        s.get("score", 0), ",".join(s.get("signals") or []) or "-",
        f"{ratio:.2f}" if ratio is not None else "-", gate.get("source", "-"),
        f"{minutes:.0f}m" if minutes is not None else "-", f"{runtime}m" if runtime else "-",
        final, "; ".join(notes) or "-",
    )


async def _run(client, user_id, m, cfg):
    mapping = cfg["mapping"]
    from_chat = cfg["from_chat"]
    stats = {"fetched": 0, "forwarded": 0, "unsorted": 0, "errors": 0, "filtered": 0, "fallback": 0, "duplicate": 0,
             "ratio_other": 0, UNIDENTIFIED: 0, TMDB_UNAVAILABLE: 0, "start": time.time()}
    for category in mapping:
        stats[category] = 0

    total_files = max(int(cfg["last_msg_id"]) - int(cfg["skip"]), 1)
    pacing = {"delay": _base_delay(total_files), "min": _base_delay(total_files), "floodwaits": 0, "streak": 0}

    tmdb_session = aiohttp.ClientSession() if aiohttp else None
    tmdb_key = await db.get_tmdb_key(user_id)
    use_anilist = await db.get_anilist_enabled(user_id)
    anilist_key = await db.get_anilist_key(user_id)
    # /settings -> Filters enforcement (message type toggles, extension
    # exclude-list, keyword include-list, file size limit, movie/series
    # whitelists) - see plugins/utils.py's check_filters(). Duplicate
    # skipping is always on here (same file_unique_id seen earlier in
    # this run), whatever the "Skip Duplicate" toggle says.
    configs = await db.get_configs(user_id)
    disabled_types = set(await db.get_filters(user_id))
    seen_file_ids = set()
    other_dest = mapping["other"]
    dup_dest = mapping.get("duplicate")
    remove_links = bool(configs.get("remove_caption_link"))
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

            if message.empty or message.service:
                stats["errors"] += 1
                continue

            allowed, reason = check_filters(message, configs, disabled_types, seen_file_ids)
            if not allowed:
                if reason == "duplicate" and dup_dest is not None:
                    # Duplicate channel is set -> forward the repeat there
                    # instead of skipping it.
                    ok = await _copy_with_retry(
                        client, dup_dest, from_chat, message.id, m, stats, mapping, pacing, total_files,
                        caption=caption_for_copy(message, remove_links),
                    )
                    if not ok:
                        ok = await _copy_with_retry(
                            client, dup_dest, from_chat, message.id, m, stats, mapping, pacing, total_files,
                            forward=True,
                        )
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
                continue

            new_text = None
            if remove_links and message.text:
                new_text = text_without_links(message)
                if new_text == "":
                    # a message that was nothing but links: nothing left to forward
                    stats["filtered"] = stats.get("filtered", 0) + 1
                    continue

            info = {}   # filled by classify_async: the TMDB id of the match, and info["series"] details
            # deep_series=True is /autosort only: TVmaze + a confidence score decide "series"
            category = await classify_async(message, tmdb_session, tmdb_key, use_anilist, anilist_key, info=info, deep_series=True)
            if category in SKIP_CATEGORIES:
                # TMDB unreachable -> don't drop it, put it in Other.
                stats[category] += 1
                category = "other"
            classified = category
            # Movie channel gate (duration / thumbnail ratio / TMDB runtime). Done
            # here, not in classify_async(), which /fwd shares. A file that fails
            # is not dropped, filtered or counted as an error: it goes to Other.
            gate = sgate = None
            if classified in MOVIE_GATE_CATEGORIES:
                gate = await _movie_gate(message, classified, info, tmdb_session, tmdb_key)
                category = gate["category"]
            elif classified in SERIES_GATE_CATEGORIES:
                # Same idea for the Series channel: episode length + landscape thumbnail.
                sgate = _series_gate(message, classified, info)
                category = sgate["category"]
            gated = category
            dest = mapping.get(category)
            if dest is None:
                # No channel mapped for this category -> Other.
                stats["unsorted"] += 1
                category = "other"
                dest = other_dest
            if gate is not None or (info.get("tmdb_media") == "movie" and classified.startswith("anime")):
                _log_movie_route(message, classified, gated, category, gate, info)
            if sgate is not None or (info.get("series") or {}).get("candidate"):
                _log_series_route(message, classified, gated, category, sgate, info)

            # Caption -> just the media/movie name line; None = leave caption as is.
            ok = await _copy_with_retry(
                client, dest, from_chat, message.id, m, stats, mapping, pacing, total_files,
                caption=caption_for_copy(message, remove_links), text=new_text,
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
            if ok:
                stats["forwarded"] += 1
                stats[category] = stats.get(category, 0) + 1     # FINAL category ("other" for a gated movie)
                if (gate is not None and gate["ratio_hit"]) or (sgate is not None and sgate["ratio_hit"]):
                    stats["ratio_other"] += 1
                # Clean run for a while -> ease the pace back down toward the
                # baseline instead of staying at whatever a past FloodWait
                # pushed it to forever.
                pacing["streak"] += 1
                if pacing["streak"] % EASE_DOWN_EVERY == 0 and pacing["delay"] > pacing["min"]:
                    pacing["delay"] = max(pacing["min"], pacing["delay"] * EASE_DOWN_FACTOR)
            else:
                stats["errors"] += 1

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
