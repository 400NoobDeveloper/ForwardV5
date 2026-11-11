# ============================================================
# /autosortai - sort + forward a source channel using OpenAI.
#
# Own pipeline: it does NOT call /autosort (handlers/autosort.py) or
# /fwd (handlers/settings.py + plugins/regix.py), and while it runs the
# user's task lock is held, so neither of those can start (they both
# refuse to run while the lock is set).
#
# For every message, one at a time:
#   1. skip empty/service messages, duplicates and anything the
#      /settings -> Filters exclude;
#   2. ask OpenAI (the user's own key from /settings -> Private Files
#      -> OpenAI) which category the media belongs to;
#   3. copy it to the channel mapped to that category in
#      /settings -> Channels -> "Auto-Sort Channels" (Other / Misc when
#      OpenAI is unsure, fails, or no channel is mapped).
#
# Privacy: only the file name, caption, media type, size and duration
# are sent to OpenAI - never the file itself (same promise as the
# OpenAI page in /settings).
# ============================================================

import re
import json
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
from plugins.controls import Translation, CLIENT, start_clone_bot, _verify_openai_key
from plugins.classify import _openai_chat, _OPENAI_BAD_KEYS, OPENAI_MIN_CONFIDENCE, get_filename
from plugins.utils import check_filters, clean_caption

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger(__name__)

CLIENT = CLIENT()

# run_id -> pending run config (set by /autosortai, consumed by the Yes button)
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
}

# What OpenAI is told about each kind. Only the kinds the user actually
# mapped a channel for are offered, so it can't answer with a category
# that has nowhere to go. "<kind>_files" channels are handled in code
# (same kind, but the item was sent as a document instead of a video).
KIND_HELP = {
    "movie": "feature films",
    "series": "live-action TV shows / web-series episodes",
    "anime": "Japanese/Asian-style animated shows or films, incl. Hindi/English dubs",
    "education": "course, lecture, tutorial, bootcamp or training material; numbered "
                 "module names like '5.1.3 Title' are typical",
    "pdf": "PDF documents, books, notes",
    "image": "photos, wallpapers, memes, screenshots",
    "audio": "music, songs, podcasts, voice notes",
}
VIDEO_KINDS = ("movie", "series", "anime", "education")

# ── Pacing ──────────────────────────────────────────────────
# One OpenAI call + one copy per file already takes a second or two, so
# this is only the extra pause between files. A FloodWait raises it, a
# long clean streak eases it back down.
BASE_DELAY = 1.0
MAX_DELAY = 12.0
FLOODWAIT_BACKOFF = 1.6
EASE_DOWN_EVERY = 50
EASE_DOWN_FACTOR = 0.9
# This many OpenAI failures in a row (outage, quota used up, ...) stops
# the run instead of quietly dumping the rest of the channel into Other.
MAX_CONSECUTIVE_AI_FAILURES = 10
PROGRESS_EVERY = 20

_MEDIA_ATTRS = ("video", "document", "audio", "voice", "photo", "animation", "video_note", "sticker")


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


# ============================================================
# Analysis
# ============================================================
def _media_of(message):
    """(attribute name, media object) of the message's media, or (None, None)."""
    for attr in _MEDIA_ATTRS:
        obj = getattr(message, attr, None)
        if obj:
            return attr, obj
    return None, None


def _clean_text(text, limit=300) -> str:
    text = re.sub(r"https?://\S+|t\.me/\S+|@\w+", " ", text or "")
    return " ".join(text.split())[:limit]


def _describe(message):
    """Everything about the message that's allowed to leave the bot, as
    (media_type, prompt_text, cache_key). prompt_text is '' when there
    is nothing to analyse (no file name and no caption)."""
    kind, media = _media_of(message)
    name = _clean_text(get_filename(message))
    caption = _clean_text(getattr(message, "caption", None))
    lines = [f"media type: {kind}"]
    if name:
        lines.append(f"file name: {name}")
    if caption and caption != name:
        lines.append(f"caption: {caption}")
    mime = getattr(media, "mime_type", None)
    if mime:
        lines.append(f"mime type: {mime}")
    duration = getattr(media, "duration", None)
    if isinstance(duration, int) and duration > 0:
        lines.append(f"duration: {duration // 60} min")
    size = getattr(media, "file_size", None)
    if isinstance(size, int) and size > 0:
        lines.append(f"size: {size // (1024 * 1024)} MB")
    if not (name or caption):
        return kind, "", ""
    # Digits masked, so "Lecture 1" ... "Lecture 200" cost one request.
    key = kind + "|" + re.sub(r"\d+", "#", f"{name} | {caption}".lower())
    return kind, "\n".join(lines), key


def _kinds_for(mapping: dict) -> list:
    """Kinds OpenAI may answer with, derived from the mapped channels."""
    kinds = []
    for cat in mapping:
        base = cat[:-6] if cat.endswith("_files") else cat
        if base in KIND_HELP and base not in kinds:
            kinds.append(base)
    return kinds


def _system_prompt(kinds: list) -> str:
    rules = "\n".join(f"- {k}: {KIND_HELP[k]}" for k in kinds)
    return (
        "You sort Telegram media into one category using only its file name, caption "
        "and basic details. Reply with JSON only: "
        '{"category": "' + "|".join(kinds + ["other"]) + '", "confidence": 0.0-1.0}.\n'
        f"{rules}\n"
        "- other: anything else - personal or random media, samples, trailers, or "
        "whenever you are not sure.\n"
        "Never guess a title you do not recognise; answer other instead."
    )


def _route(kind: str, media_type: str, mapping: dict) -> str:
    """Channel category for an OpenAI kind. Videos sent as documents prefer
    the '<kind>_files' channel, falling back to the plain one."""
    if kind == "other":
        return "other"
    if kind in VIDEO_KINDS and media_type == "document" and f"{kind}_files" in mapping:
        return f"{kind}_files"
    if kind in mapping:
        return kind
    if f"{kind}_files" in mapping:
        return f"{kind}_files"
    return "other"


def _type_default(media_type: str, message, mapping: dict) -> str:
    """No file name and no caption -> nothing for OpenAI to read, so the
    media type alone decides (no API call)."""
    if media_type == "photo" and "image" in mapping:
        return "image"
    if media_type in ("audio", "voice") and "audio" in mapping:
        return "audio"
    mime = getattr(getattr(message, "document", None), "mime_type", "") or ""
    if media_type == "document" and mime == "application/pdf" and "pdf" in mapping:
        return "pdf"
    return "other"


async def _analyse(session, key, system, kinds, message, mapping, cache, stats):
    """(category, ok). ok=False only when OpenAI itself failed (that file
    goes to Other); a confident-or-not verdict is always ok=True."""
    media_type, prompt, ck = _describe(message)
    if media_type is None or not prompt:
        return _type_default(media_type, message, mapping), True
    if ck in cache:
        stats["ai_cached"] += 1
        return cache[ck], True

    stats["ai_calls"] += 1
    content = await _openai_chat(session, [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ], key)
    if content is None:
        return "other", False

    kind = "other"
    try:
        verdict = json.loads(content)
        cat = str(verdict.get("category", "")).strip().lower()
        conf = float(verdict.get("confidence", 0))
        if cat in kinds and conf >= OPENAI_MIN_CONFIDENCE:
            kind = cat
    except (ValueError, TypeError, AttributeError):
        kind = "other"
    category = _route(kind, media_type, mapping)
    cache[ck] = category
    if len(cache) > 5000:
        cache.pop(next(iter(cache)))
    return category, True


# ============================================================
# /autosortai
# ============================================================
@Client.on_message(filters.private & filters.command("autosortai"))
async def autosortai_cmd(bot: Client, message):
    user_id = message.from_user.id

    if temp.lock.get(user_id) and str(temp.lock.get(user_id)) == "True":
        return await message.reply_text("<code>Please wait until your previous task completes.</code>")

    if aiohttp is None:
        return await message.reply_text("<code>aiohttp is not installed on this bot, so OpenAI can't be reached.</code>")

    key = await db.get_openai_key(user_id)
    if not key:
        return await message.reply_text(
            "<b>OpenAI API key required.</b>\n"
            "Add your key in /settings → <b>Private Files</b> → <b>OpenAI</b>, then run /autosortai again."
        )

    mapping = await db.get_category_channels(user_id)
    if not mapping:
        return await message.reply_text(
            "<b>No auto-sort channels configured yet.</b>\n"
            "Go to /settings → <b>Channels</b> → <b>Auto-Sort Channels</b>, set a destination "
            "channel for at least one category, then run /autosortai again."
        )
    if "other" not in mapping:
        return await message.reply_text(
            "<b>Other / Misc channel required.</b>\n"
            "Anything OpenAI can't place (or can't be reached for) goes to the <b>Misc / Other</b> "
            "channel. Set it in /settings → <b>Channels</b> → <b>Auto-Sort Channels</b>, "
            "then run /autosortai again."
        )
    if not _kinds_for(mapping):
        return await message.reply_text(
            "<b>Nothing for OpenAI to sort into.</b>\n"
            "Only the Other channel is mapped. Map at least one more category "
            "(movie / series / anime / education / pdf / image / audio) in /settings."
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
    if not skipno.text or skipno.text.startswith('/'):
        return await message.reply(Translation.CANCEL)
    if not skipno.text.lstrip('-').isdigit():
        return await message.reply_text("Skip number must be numeric.")

    checking = await message.reply_text("<code>Checking your OpenAI key...</code>")
    ok, detail = await _verify_openai_key(key)
    if not ok:
        return await checking.edit_text(
            f"<b>❌ OpenAI rejected your key:</b>\n<code>{detail}</code>\n\n"
            "Fix it in /settings → <b>Private Files</b> → <b>OpenAI</b>."
        )
    await checking.delete()

    run_id = _new_run_id()
    _PENDING[run_id] = {
        "bot": _bot,
        "from_chat": chat_id,
        "from_title": title,
        "last_msg_id": int(last_msg_id),
        "skip": int(skipno.text),
        "mapping": mapping,
        "key": key,
        "owner": user_id,
    }

    mapping_lines = "\n".join(
        f"{CATEGORY_LABELS.get(cat, cat)} → `{dest}`" for cat, dest in mapping.items()
    )
    total_files = max(int(last_msg_id) - int(skipno.text), 1)
    buttons = [[
        InlineKeyboardButton('Yes', callback_data=f"aisort_start_{run_id}"),
        InlineKeyboardButton('No', callback_data="close_btn"),
    ]]
    await message.reply_text(
        f"<b>DOUBLE CHECKING ⚠️</b>\n\n"
        f"<b>MODE:</b> <code>AI sort (OpenAI), one file at a time</code>\n"
        f"<b>FROM CHANNEL:</b> <code>{title}</code>\n"
        f"<b>SKIP MESSAGES:</b> <code>{skipno.text}</code>\n"
        f"<b>APPROX FILES:</b> <code>{total_files}</code>\n\n"
        f"<b>Routing:</b>\n{mapping_lines}\n\n"
        f"<i>Each file's name/caption is sent to OpenAI (about one request per file, billed to your key). "
        f"Unsure or failed → Other. Skipped: duplicates and anything your /settings filters exclude. "
        f"/autosort and /fwd can't run until this finishes.</i>\n\n"
        f"<i>Your bot/userbot must be admin in every destination channel listed above.</i>\n\n"
        f"Click Yes to start.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^aisort_start_(.+)$"))
async def autosortai_start(bot: Client, query):
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


@Client.on_callback_query(filters.regex(r"^aisort_cancel$"))
async def autosortai_cancel(bot: Client, query):
    temp.CANCEL[query.from_user.id] = True
    await query.answer("Cancelling after the current file finishes...", show_alert=True)


# ============================================================
# Run
# ============================================================
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
        logger.debug("autosortai progress edit failed: %s", e)


def _progress_text(stats: dict, mapping: dict, pacing: dict, total_files: int) -> str:
    elapsed = int(time.time() - stats["start"])
    lines = [
        "<b>🤖 AI AUTO-SORT IN PROGRESS</b>\n",
        f"Fetched: <code>{stats['fetched']}/{total_files}</code>",
        f"Forwarded: <code>{stats['forwarded']}</code>",
        f"OpenAI requests: <code>{stats['ai_calls']}</code> (reused for similar names: <code>{stats['ai_cached']}</code>)",
        f"Sent to Other (OpenAI failed): <code>{stats['ai_failed']}</code>",
        f"Sent to Other (unsupported type): <code>{stats['fallback']}</code>",
        f"Filtered (type/keyword/size): <code>{stats['filtered']}</code>",
        f"Duplicates skipped: <code>{stats['duplicate']}</code>",
        f"Deleted/errored: <code>{stats['errors']}</code>",
        f"Elapsed: <code>{_format_duration(elapsed)}</code>",
        f"Current pace: <code>{pacing['delay']:.1f}s/file</code>"
        + (f" (FloodWaits hit: {pacing['floodwaits']})" if pacing["floodwaits"] else ""),
    ]
    if stats.get("last"):
        lines.append(f"Last: <code>{stats['last']}</code>")
    lines.append("\n<b>By category:</b>")
    for category in mapping:
        lines.append(f"{CATEGORY_LABELS.get(category, category)}: <code>{stats.get(category, 0)}</code>")
    return "\n".join(lines)


async def _deliver(client, dest, from_chat, msg_id, pacing, caption=None, forward=False):
    """Copy (or plain-forward) one message. True on success. FloodWaits are
    waited out exactly as Telegram asks and nudge the pace up."""
    for _ in range(5):
        try:
            if forward:
                # copy_message() can't rebuild some message types; a plain forward can.
                await client.forward_messages(chat_id=dest, from_chat_id=from_chat, message_ids=msg_id)
            else:
                kwargs = {"caption": caption} if caption is not None else {}
                await client.copy_message(chat_id=dest, from_chat_id=from_chat, message_id=msg_id, **kwargs)
            return True
        except FloodWait as e:
            pacing["floodwaits"] += 1
            pacing["streak"] = 0
            pacing["delay"] = min(pacing["delay"] * FLOODWAIT_BACKOFF, MAX_DELAY)
            await asyncio.sleep(e.value)
        except Exception as e:
            logger.debug("autosortai copy failed for msg %s: %s", msg_id, e)
            return False
    return False


async def _run(client, user_id, m, cfg):
    mapping = cfg["mapping"]
    from_chat = cfg["from_chat"]
    key = cfg["key"]
    kinds = _kinds_for(mapping)
    system = _system_prompt(kinds)
    other_dest = mapping["other"]
    dest_chats = list(set(mapping.values()))

    stats = {"fetched": 0, "forwarded": 0, "errors": 0, "filtered": 0, "duplicate": 0, "fallback": 0,
             "ai_calls": 0, "ai_cached": 0, "ai_failed": 0, "last": "", "start": time.time()}
    for category in mapping:
        stats[category] = 0
    total_files = max(int(cfg["last_msg_id"]) - int(cfg["skip"]), 1)
    pacing = {"delay": BASE_DELAY, "min": BASE_DELAY, "floodwaits": 0, "streak": 0}

    # /settings -> Filters (type toggles, extensions, keywords, size) and
    # always-on duplicate skipping - plugins/utils.py's check_filters().
    configs = await db.get_configs(user_id)
    disabled_types = set(await db.get_filters(user_id))
    seen_file_ids = set()
    cache: dict = {}
    ai_fail_streak = 0
    cancel_button = InlineKeyboardMarkup([[InlineKeyboardButton('• Cancel', 'aisort_cancel')]])
    session = aiohttp.ClientSession()

    try:
        pling = 0
        async for message in client.iter_messages(
            client,
            chat_id=from_chat,
            limit=int(cfg["last_msg_id"]),
            offset=cfg["skip"],
            continuous=False,
        ):
            if temp.CANCEL.get(user_id):
                await _safe_edit(m, _progress_text(stats, mapping, pacing, total_files) + "\n\n<b>❌ Cancelled</b>")
                break

            if pling % PROGRESS_EVERY == 0:
                await _safe_edit(m, _progress_text(stats, mapping, pacing, total_files), cancel_button)
            pling += 1
            stats["fetched"] += 1

            if message.empty or message.service:
                stats["errors"] += 1
                continue

            allowed, reason = check_filters(message, configs, disabled_types, seen_file_ids)
            if not allowed:
                stats[reason] = stats.get(reason, 0) + 1
                continue

            # ---- one file -> one OpenAI analysis ----
            category, ok = await _analyse(session, key, system, kinds, message, mapping, cache, stats)
            if key in _OPENAI_BAD_KEYS:
                await _safe_edit(
                    m,
                    _progress_text(stats, mapping, pacing, total_files)
                    + "\n\n<b>🛑 Stopped: OpenAI rejected your key.</b>\nFix it in /settings → Private Files → OpenAI "
                      "and run /autosortai again (use a higher skip number to continue).",
                )
                break
            if ok:
                ai_fail_streak = 0
            else:
                stats["ai_failed"] += 1
                ai_fail_streak += 1
                if ai_fail_streak >= MAX_CONSECUTIVE_AI_FAILURES:
                    await _safe_edit(
                        m,
                        _progress_text(stats, mapping, pacing, total_files)
                        + f"\n\n<b>🛑 Stopped: OpenAI failed {ai_fail_streak} times in a row</b> "
                          "(outage, quota or rate limit). Check your OpenAI account and run /autosortai again.",
                    )
                    break

            dest = mapping.get(category) or other_dest
            if dest == other_dest:
                category = "other"
            stats["last"] = (get_filename(message) or clean_caption(message, True) or "")[:60] + f" → {category}"

            sent = await _deliver(client, dest, from_chat, message.id, pacing, caption=clean_caption(message, True))
            if not sent:
                # Unsupported by copy_message -> plain-forward it to Other.
                sent = await _deliver(client, other_dest, from_chat, message.id, pacing, forward=True)
                if sent:
                    stats["fallback"] += 1
                    category = "other"
            if sent:
                stats["forwarded"] += 1
                stats[category] = stats.get(category, 0) + 1
                pacing["streak"] += 1
                if pacing["streak"] % EASE_DOWN_EVERY == 0 and pacing["delay"] > pacing["min"]:
                    pacing["delay"] = max(pacing["min"], pacing["delay"] * EASE_DOWN_FACTOR)
            else:
                stats["errors"] += 1

            await asyncio.sleep(pacing["delay"])
        else:
            await _safe_edit(m, _progress_text(stats, mapping, pacing, total_files) + "\n\n<b>🎉 Completed</b>")
    except Exception as e:
        await _safe_edit(m, f"<b>ERROR:</b>\n<code>{e}</code>")
    finally:
        await session.close()
        for c in dest_chats:
            if c in temp.IS_FRWD_CHAT:
                temp.IS_FRWD_CHAT.remove(c)
        temp.lock[user_id] = False
        try:
            await client.stop()
        except Exception:
            pass
