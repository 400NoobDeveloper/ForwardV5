"""plugins/autosort_create.py - /settings -> Extra Settings -> AUTOSORT.

Creates one PRIVATE Telegram channel (or supergroup) for every Auto-Sort category that has no
channel yet, makes the forwarding identity an admin there, and saves each new chat id with
db.set_category_channel() - the SAME mapping /autosort already reads.

A Telegram BOT cannot create chats, so this uses a separate user-account STRING SESSION that
is kept in the user's configs under "autosort_session" (core/database.py). That session is used
ONLY here: it is never added to /settings -> Bots, never used by /autosort, and is never
logged, printed or written to a file (in_memory client, scrubbed error texts).

Plain functions only - the callback branches live in handlers/settings.py ("asmenu",
"asback", "assession", "assessionset", "assessionreset", "assessiontest", "ascreate",
"ascreatetype_channel" / "ascreatetype_group", "asgo", "ascancel").
"""

import asyncio
import html
import logging
import random
import re
import time

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.types import ChatPrivileges, InlineKeyboardButton, InlineKeyboardMarkup

from core.config import Config
from core.database import db
from plugins.controls import AUTOSORT_CATEGORIES, SESSION_STRING_SIZE

try:
    from pyromod.listen.listen import ListenerCanceled
except Exception:                                 # pyromod missing: never raised
    class ListenerCanceled(Exception):
        pass

logger = logging.getLogger(__name__)

AUTOSORT_CREATE_DELAY = 5          # seconds between two chat creations (a random 80-120 % of it is used)
TITLE_LIMIT = 128                  # Telegram: chat title 1-128 characters
ABOUT_LIMIT = 255                  # Telegram: chat description up to 255 characters
PREFIX_LIMIT = 60                  # longest name prefix accepted
MIN_EDIT_GAP = 1.0                 # progress message is edited at most about once a second
LONG_FLOOD = 60                    # FloodWait below this: sleep and retry once; at or above: stop the run

# user_id -> {"cancel": bool, "cfg": {...}} while that user's creation run is going
RUNNING: dict = {}
# user_id -> {"kind": "channel" | "group", "prefix": str} between "type / prefix" and "Confirm"
PENDING: dict = {}

# errors after which creating more chats cannot work: stop the run, keep what was done
_STOP_IDS = {
    "CHANNELS_TOO_MUCH": "this account is already in too many channels / groups (CHANNELS_TOO_MUCH)",
    "USER_RESTRICTED": "Telegram has restricted this account (USER_RESTRICTED)",
    "PEER_FLOOD": "Telegram is limiting this account right now (PEER_FLOOD)",
    "USER_DEACTIVATED": "this account is deactivated (USER_DEACTIVATED)",
    "USER_DEACTIVATED_BAN": "this account is banned (USER_DEACTIVATED_BAN)",
    "AUTH_KEY_UNREGISTERED": "the session is no longer valid - RESET it and set a new one",
    "AUTH_KEY_DUPLICATED": "the session is being used somewhere else - RESET it and set a new one",
    "SESSION_REVOKED": "the session was revoked - RESET it and set a new one",
    "SESSION_EXPIRED": "the session expired - RESET it and set a new one",
}

_MARKS = {"wait": "⏳", "make": "🛠", "ok": "✅", "admin": "⚠️", "fail": "❌"}


# ======================================================================
# small helpers
# ======================================================================
def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _back(data: str = "settings#asmenu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn('ʙᴀᴄᴋ', data)]])


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _fmt_wait(seconds) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    parts = ([f"{h}h"] if h else []) + ([f"{m}m"] if m else []) + ([f"{s}s"] if s or not (h or m) else [])
    return " ".join(parts)


def _error_id(e) -> str:
    """Telegram's error name (CHANNELS_TOO_MUCH ...) - class attribute ID, else found in the text,
    else the class name. Never contains anything but an error name."""
    eid = getattr(e, "ID", None)
    if isinstance(eid, str) and eid:
        return eid
    m = re.search(r"[A-Z][A-Z0-9_]{4,}", str(e))
    return m.group(0) if m else type(e).__name__


def _scrub(err, secret: str = "") -> str:
    """Error text that is safe to show: the string session is cut out and the text is short."""
    text = f"{type(err).__name__}: {err}"
    if secret:
        text = text.replace(secret, "***")
    return text[:200]


def looks_like_session(text) -> bool:
    """Same idea as CLIENT.add_session(): a Pyrogram string session is a long url-safe base64 text
    (at least SESSION_STRING_SIZE characters, no spaces). The real proof is get_me() later."""
    s = (text or "").strip()
    return len(s) >= SESSION_STRING_SIZE and re.fullmatch(r"[A-Za-z0-9_\-]+={0,2}", s) is not None


def make_client(user_id: int, session: str) -> Client:
    """The creation client: unique name, nothing written to disk, no update handling."""
    return Client(
        f"AUTOSORT_{user_id}",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        session_string=session,
        in_memory=True,
        no_updates=True,
    )


async def _stop(client) -> None:
    try:
        await client.stop()
    except Exception:
        pass


async def check_session(user_id: int, session: str):
    """Start the session, read the account, stop it. -> (me, None) or (None, error text).
    Never raises; the error text never contains the string."""
    client = make_client(user_id, session)
    try:
        await client.start()
        me = await client.get_me()
        if getattr(me, "is_bot", False):
            return None, "this is a BOT session - use the string session of a normal user account"
        return me, None
    except Exception as e:
        return None, _scrub(e, session)
    finally:
        await _stop(client)


def _who(me) -> str:
    name = " ".join(p for p in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if p) or "-"
    user = getattr(me, "username", None)
    return f"{_esc(name)}" + (f" (@{_esc(user)})" if user else "")


async def safe_edit(message, text: str, markup=None, final: bool = False):
    """edit_text that never raises. MessageNotModified is ignored; a FloodWait skips a progress
    edit (the next one carries the news) but is waited out for a final edit."""
    for _ in range(2):
        try:
            return await message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except MessageNotModified:
            return None
        except FloodWait as e:
            if not final or e.value > 30:
                return None
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            logger.debug("autosort create: edit failed: %s", type(e).__name__)
            return None
    return None


async def unset_categories(user_id: int) -> list:
    """[(key, label)] of the AUTOSORT_CATEGORIES that have no channel yet, in list order."""
    mapping = await db.get_category_channels(user_id)
    return [(key, label) for key, label in AUTOSORT_CATEGORIES if not mapping.get(key)]


def chat_title(prefix: str, label: str) -> str:
    prefix = (prefix or "").strip()
    return (f"{prefix} • {label}" if prefix else label)[:TITLE_LIMIT]


# ======================================================================
# pages (text, markup)
# ======================================================================
async def menu_page(user_id: int):
    has_session = bool(await db.get_autosort_session(user_id))
    mapping = await db.get_category_channels(user_id)
    done = sum(1 for key, _label in AUTOSORT_CATEGORIES if mapping.get(key))
    text = (
        "<b><u>🗂 AUTOSORT</u></b>\n\n"
        "<b>Creates a private channel (or group) for every Auto-Sort category that has none yet, "
        "makes your bot an admin there and saves it in Auto-Sort Channels - so /autosort works "
        "without any manual step.\n"
        "Telegram bots cannot create chats, so this needs the string session of your own user "
        "account (only used here).</b>\n\n"
        f"<b>String session:</b> {'set ✅' if has_session else 'not set ❌'}\n"
        f"<b>Categories with a channel:</b> {done}/{len(AUTOSORT_CATEGORIES)}"
    )
    markup = InlineKeyboardMarkup([
        [_btn('sᴇssɪᴏɴ', 'settings#assession'), _btn('ᴄʀᴇᴀᴛᴇ', 'settings#ascreate')],
        [_btn('ʙᴀᴄᴋ', 'settings#asback')],
    ])
    return text, markup


async def session_page(user_id: int):
    has_session = bool(await db.get_autosort_session(user_id))
    if has_session:
        text = ("<b><u>🗂 AUTOSORT - SESSION</u></b>\n\n"
                "<b>String session:</b> set ✅ <code>••••••••</code>\n"
                "<b>RESET deletes it completely. TEST starts it once and shows the account name.</b>")
        rows = [[_btn('ʀᴇsᴇᴛ', 'settings#assessionreset'), _btn('ᴛᴇsᴛ', 'settings#assessiontest')]]
    else:
        text = ("<b><u>🗂 AUTOSORT - SESSION</u></b>\n\n"
                "<b>String session:</b> not set ❌\n"
                "<b>Tap SET and send the Pyrogram string session of your own user account.</b>")
        rows = [[_btn('sᴇᴛ', 'settings#assessionset')]]
    rows.append([_btn('ʙᴀᴄᴋ', 'settings#asmenu')])
    return text, InlineKeyboardMarkup(rows)


def no_session_page():
    text = ("<b><u>🗂 AUTOSORT</u></b>\n\n"
            "<b>No string session is saved yet. Set one first - it is needed to create the chats.</b>")
    return text, InlineKeyboardMarkup([[_btn('sᴇᴛ', 'settings#assessionset')], [_btn('ʙᴀᴄᴋ', 'settings#asmenu')]])


def type_page():
    text = ("<b><u>🗂 AUTOSORT - CREATE</u></b>\n\n"
            "<b>Channels or Groups?</b>\n"
            "Both work as /autosort destinations. They are created private (no username).")
    return text, InlineKeyboardMarkup([
        [_btn('📢 ᴄʜᴀɴɴᴇʟs', 'settings#ascreatetype_channel'), _btn('👥 ɢʀᴏᴜᴘs', 'settings#ascreatetype_group')],
        [_btn('ʙᴀᴄᴋ', 'settings#asmenu')],
    ])


def all_set_page():
    return ("<b><u>🗂 AUTOSORT</u></b>\n\n<b>Everything is already set - every category has a channel.</b>",
            _back())


def confirm_page(kind: str, prefix: str, todo: list):
    lines = "\n".join(f"• {_esc(chat_title(prefix, label))}" for _key, label in todo)
    text = (
        "<b><u>🗂 AUTOSORT - CONFIRM</u></b>\n\n"
        f"<b>Type:</b> {'Channels' if kind == 'channel' else 'Groups'} (private)\n"
        f"<b>Prefix:</b> {('<code>' + _esc(prefix) + '</code>') if prefix else 'none'}\n"
        f"<b>To create ({len(todo)}):</b>\n{lines}\n\n"
        "<i>Categories that already have a channel are left untouched. "
        "Each chat is made with a pause of a few seconds to stay clear of Telegram limits.</i>"
    )
    return text, InlineKeyboardMarkup([[_btn('ᴄᴏɴꜰɪʀᴍ', 'settings#asgo'), _btn('ᴄᴀɴᴄᴇʟ', 'settings#asmenu')]])


# ======================================================================
# SET / TEST the string session
# ======================================================================
async def set_session_flow(bot, user_id: int):
    """Warning -> wait for the string -> delete the user's message at once -> validate -> save.
    Every outcome is shown on the prompt message; the string itself is never shown or logged."""
    back = _back("settings#assession")
    prompt = await bot.send_message(
        user_id,
        "<b>❪ SET AUTOSORT STRING SESSION ❫</b>\n\n"
        "<b>⚠️ WARNING ⚠️</b>\n"
        "<code>A string session is FULL ACCESS to your Telegram account. Use your OWN account only. "
        "There is a chance that Telegram limits or bans an account that creates many chats. "
        "It is used ONLY to create the Auto-Sort chats, is not shared with /autosort, and you can "
        "delete it any time with RESET.</code>\n\n"
        "<b>Send the Pyrogram string session now.</b>\n"
        "/cancel - cancel this process",
        parse_mode=ParseMode.HTML,
    )
    try:
        reply = await bot.listen(chat_id=user_id, timeout=300)
    except asyncio.TimeoutError:
        return await safe_edit(prompt, "Process has been automatically cancelled", back, final=True)
    except ListenerCanceled:
        return
    secret = (reply.text or "").strip()
    try:                                  # the message holds the secret: delete it before anything else
        await reply.delete()
    except Exception:
        pass
    if secret == "/cancel":
        return await safe_edit(prompt, "<b>process canceled</b>", back, final=True)
    if not looks_like_session(secret):
        return await safe_edit(
            prompt, "<b>❌ That does not look like a Pyrogram string session.</b>\n\nNot saved.", back, final=True)
    await safe_edit(prompt, "<code>Checking the session...</code>")
    me, error = await check_session(user_id, secret)
    if error:
        return await safe_edit(prompt, f"<b>❌ Session rejected:</b>\n<code>{_esc(error)}</code>\n\nNot saved.",
                               back, final=True)
    await db.set_autosort_session(user_id, secret)
    secret = ""
    await safe_edit(prompt, f"<b>✅ Session saved.</b>\nAccount: <b>{_who(me)}</b>", back, final=True)


async def test_session(user_id: int, message):
    session = await db.get_autosort_session(user_id)
    back = _back("settings#assession")
    if not session:
        return await safe_edit(message, "<b>No string session is saved.</b>", back, final=True)
    await safe_edit(message, "<code>Starting the session...</code>")
    me, error = await check_session(user_id, session)
    if error:
        return await safe_edit(message, f"<b>❌ Session not working:</b>\n<code>{_esc(error)}</code>", back, final=True)
    await safe_edit(message, f"<b>✅ Session works.</b>\nAccount: <b>{_who(me)}</b>", back, final=True)


# ======================================================================
# CREATE: type -> prefix -> confirm
# ======================================================================
async def prefix_flow(bot, user_id: int, kind: str):
    """Ask for the optional name prefix, then show the confirmation (only the categories that
    are not set yet). The chosen type and prefix wait in PENDING until Confirm."""
    PENDING.pop(user_id, None)
    back = _back()
    if not await unset_categories(user_id):
        text, markup = all_set_page()
        return await bot.send_message(user_id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
    prompt = await bot.send_message(
        user_id,
        "<b>❪ AUTOSORT NAME PREFIX ❫</b>\n\n"
        "<b>Send a prefix for the chat names, e.g. <code>My Library</code> - the chats are then called "
        "<code>My Library • Movies (Video)</code> and so on.</b>\n"
        "/skip - no prefix\n"
        "/cancel - cancel this process",
        parse_mode=ParseMode.HTML,
    )
    try:
        reply = await bot.listen(chat_id=user_id, timeout=300)
    except asyncio.TimeoutError:
        return await safe_edit(prompt, "Process has been automatically cancelled", back, final=True)
    except ListenerCanceled:
        return
    text = (reply.text or "").strip()
    try:
        await reply.delete()
    except Exception:
        pass
    if text == "/cancel":
        return await safe_edit(prompt, "<b>process canceled</b>", back, final=True)
    if text.lower() == "/skip":
        prefix = ""
    elif text and not text.startswith("/"):
        prefix = text[:PREFIX_LIMIT]
    else:
        return await safe_edit(prompt, "<b>Invalid. Send a plain text prefix, or /skip.</b>", back, final=True)
    todo = await unset_categories(user_id)
    if not todo:
        page, markup = all_set_page()
        return await safe_edit(prompt, page, markup, final=True)
    PENDING[user_id] = {"kind": kind, "prefix": prefix}
    page, markup = confirm_page(kind, prefix, todo)
    await safe_edit(prompt, page, markup, final=True)


def claim_run(user_id: int):
    """Called by the Confirm button. NO await in here: the lock is taken before anything else can
    run, so a second tap can never start a second run. -> (cfg | None, error text | None)."""
    if user_id in RUNNING:
        return None, "A creation run is already in progress."
    cfg = PENDING.pop(user_id, None)
    if not cfg:
        return None, "This button has expired. Open AUTOSORT -> CREATE again."
    RUNNING[user_id] = {"cancel": False, "cfg": cfg}
    return cfg, None


def request_cancel(user_id: int) -> bool:
    state = RUNNING.get(user_id)
    if not state:
        return False
    state["cancel"] = True
    return True


# ======================================================================
# creation run
# ======================================================================
async def _flood_retry(make_call):
    """await make_call(); a FloodWait below LONG_FLOOD is slept out and the call tried ONCE more.
    A long FloodWait (or a second one) is raised to the caller."""
    try:
        return await make_call()
    except FloodWait as e:
        if e.value >= LONG_FLOOD:
            raise
        await asyncio.sleep(e.value + 1)
        return await make_call()


async def _create_chat(client, kind: str, title: str, about: str):
    if kind == "channel":
        return await client.create_channel(title, about)
    return await client.create_supergroup(title, about)


def _privileges(kind: str) -> ChatPrivileges:
    if kind == "channel":
        return ChatPrivileges(can_manage_chat=True, can_post_messages=True, can_edit_messages=True,
                              can_delete_messages=True, can_invite_users=True)
    return ChatPrivileges(can_manage_chat=True, can_delete_messages=True, can_invite_users=True,
                          can_pin_messages=True)


async def promote(client, chat_id, ident: dict, kind: str):
    """Make ident ({"id", "username"}) an admin of chat_id. -> (ok, reason).

    The creation client is a fresh in-memory session that has never seen the bot, so a bare
    numeric id cannot be resolved; the @username is tried first (that lookup also teaches the
    client the peer), the id second. Direct promote first, then add + promote."""
    targets = []
    if ident.get("username"):
        targets.append("@" + str(ident["username"]).lstrip("@"))
    if ident.get("id"):
        targets.append(int(ident["id"]))
    if not targets:
        return False, "no id / username saved"
    privileges = _privileges(kind)
    reason = "unknown error"
    for target in targets:
        try:
            await _flood_retry(lambda: client.promote_chat_member(chat_id, target, privileges=privileges))
            return True, None
        except FloodWait as e:
            return False, f"FloodWait {_fmt_wait(e.value)}"
        except Exception as e:
            reason = _error_id(e)
        try:
            await _flood_retry(lambda: client.add_chat_members(chat_id, target))
            await _flood_retry(lambda: client.promote_chat_member(chat_id, target, privileges=privileges))
            return True, None
        except FloodWait as e:
            return False, f"FloodWait {_fmt_wait(e.value)}"
        except Exception as e:
            reason = _error_id(e)
    return False, reason


async def forwarding_identities(user_id: int, creator_id, ui_me):
    """Who has to be admin in the new chats. -> (primary, extra, note)

    primary - the identity /autosort posts with (db.get_bot): a bot token -> that bot; a userbot
              session -> that account, unless it is the SAME account as the creation session
              (then it already owns the chat and nothing is needed).
    extra   - the bot that runs this settings UI, when it is a different one.
    note    - "no_forwarder" (nothing saved in /settings -> Bots), "same_account", or None."""
    saved = await db.get_bot(user_id)
    saved_id = saved.get("id") if saved else None
    primary, note = None, None
    if not saved:
        note = "no_forwarder"
    elif saved.get("is_bot") == False and saved_id == creator_id:       # same test as CLIENT.client()
        note = "same_account"
    else:
        primary = {"id": saved_id, "username": saved.get("username")}
    extra = None
    ui_id = getattr(ui_me, "id", None)
    if ui_id and ui_id != saved_id and ui_id != creator_id:
        extra = {"id": ui_id, "username": getattr(ui_me, "username", None)}
    return primary, extra, note


async def _pause(state: dict, seconds: float) -> None:
    """Sleep in one-second steps so the Cancel button is noticed quickly."""
    end = time.monotonic() + seconds
    while not state["cancel"]:
        left = end - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(1.0, left))


def _render(title: str, order: list, rows: dict, labels: dict, footer: str = "") -> str:
    lines = []
    for key in order:
        if key not in rows:
            continue
        mark, detail = rows[key]
        line = f"{_MARKS[mark]} {_esc(labels[key])}"
        if detail:
            line += f" - <i>{_esc(detail[:90])}</i>"
        lines.append(line)
    text = f"<b><u>🗂 AUTOSORT - {title}</u></b>\n\n" + "\n".join(lines)
    if footer:
        text += "\n\n" + footer
    else:
        text += "\n\n<i>⏳ waiting · 🛠 creating · ✅ done · ⚠️ admin missing · ❌ failed</i>"
    return text


async def run_creation(user_id: int, bot, message):
    """The whole run for a user who holds the RUNNING lock (claim_run). `message` is edited as the
    ONE progress message; `bot` is the client that runs the settings UI (its own account is
    promoted too when it differs from the saved forwarding identity). Never raises."""
    state = RUNNING.get(user_id)
    if state is None:
        return
    kind, prefix = state["cfg"]["kind"], state["cfg"]["prefix"]
    labels = dict(AUTOSORT_CATEGORIES)
    cancel_markup = InlineKeyboardMarkup([[_btn('• ᴄᴀɴᴄᴇʟ', 'settings#ascancel')]])
    order, rows = [], {}
    created = admin_problems = failed = 0
    stop, note = None, None
    client = None
    last_edit = 0.0

    async def show(force: bool = False):
        nonlocal last_edit
        now = time.monotonic()
        if not force and now - last_edit < MIN_EDIT_GAP:
            return
        last_edit = now
        await safe_edit(message, _render("CREATING", order, rows, labels), cancel_markup)

    async def finish(title: str, footer: str):
        await safe_edit(message, _render(title, order, rows, labels, footer), _back(), final=True)

    try:
        session = await db.get_autosort_session(user_id)
        if not session:
            text, markup = no_session_page()
            return await safe_edit(message, text, markup, final=True)
        todo = await unset_categories(user_id)
        if not todo:
            text, markup = all_set_page()
            return await safe_edit(message, text, markup, final=True)
        order = [key for key, _label in todo]
        rows = {key: ["wait", ""] for key in order}
        await show(force=True)

        client = make_client(user_id, session)
        try:
            await client.start()
            me = await client.get_me()
        except Exception as e:
            return await safe_edit(
                message, f"<b>❌ Could not start the session:</b>\n<code>{_esc(_scrub(e, session))}</code>",
                _back(), final=True)
        if getattr(me, "is_bot", False):
            return await safe_edit(message, "<b>❌ The saved session belongs to a bot. RESET it and set a user "
                                            "account's string session.</b>", _back(), final=True)
        ui_me = getattr(bot, "me", None)
        if ui_me is None:
            try:
                ui_me = await bot.get_me()
            except Exception:
                ui_me = None
        primary, extra, note = await forwarding_identities(user_id, getattr(me, "id", None), ui_me)

        for index, key in enumerate(list(order)):
            if state["cancel"]:
                stop = "cancelled by you"
                break
            if (await db.get_category_channels(user_id)).get(key):
                rows.pop(key, None)                  # set by hand meanwhile: never overwrite it
                continue
            rows[key] = ["make", ""]
            await show()
            title = chat_title(prefix, labels[key])
            about = f"Auto-Sort: {labels[key]}"[:ABOUT_LIMIT]
            try:
                chat = await _flood_retry(lambda: _create_chat(client, kind, title, about))
            except FloodWait as e:
                rows[key] = ["wait", f"Telegram asks to wait {_fmt_wait(e.value)}"]
                stop = f"Telegram asks to wait {_fmt_wait(e.value)} before more chats can be created"
                break
            except RPCError as e:
                eid = _error_id(e)
                failed += 1
                rows[key] = ["fail", eid]
                if eid in _STOP_IDS:
                    stop = _STOP_IDS[eid]
                    break
                logger.warning("autosort create: %s failed: %s", key, eid)
                await show()
                if index < len(order) - 1:
                    await _pause(state, AUTOSORT_CREATE_DELAY * random.uniform(0.8, 1.2))
                continue
            except Exception as e:
                failed += 1
                rows[key] = ["fail", type(e).__name__]
                stop = f"unexpected error ({type(e).__name__})"
                logger.warning("autosort create: %s failed: %s", key, type(e).__name__)
                break

            chat_id = chat.id
            try:                                     # saved the moment the chat exists
                await db.set_category_channel(user_id, key, chat_id)
            except Exception as e:
                failed += 1
                rows[key] = ["fail", f"created (id {chat_id}) but not saved"]
                stop = f"the database refused to save a chat ({type(e).__name__})"
                break
            created += 1
            rows[key] = ["ok", ""]
            if primary:
                ok, why = await promote(client, chat_id, primary, kind)
                if not ok:
                    admin_problems += 1
                    rows[key] = ["admin", f"bot is not admin ({why}) - add it by hand"]
            if extra:
                await promote(client, chat_id, extra, kind)      # the settings bot: best effort
            await show()
            if index < len(order) - 1:
                await _pause(state, AUTOSORT_CREATE_DELAY * random.uniform(0.8, 1.2))

        reached = [k for k in order if k in rows]
        left = sum(1 for k in reached if rows[k][0] in ("wait", "make"))
        footer = [f"<b>Created:</b> {created}   <b>Bot-admin problems:</b> {admin_problems}   <b>Failed:</b> {failed}"]
        if stop:
            footer.append(f"<b>Stopped:</b> {_esc(stop)}")
        if left:
            footer.append(f"<b>Not created yet:</b> {left} - tap CREATE again to continue where it stopped.")
        if admin_problems:
            footer.append("<i>Those chats are saved already; add the bot as admin there by hand.</i>")
        if note == "no_forwarder":
            footer.append("<i>No bot / userbot is saved in /settings -> Bots, so nobody was made admin for "
                          "/autosort. Add one there and make it admin in the new chats.</i>")
        await finish("STOPPED" if stop else "FINISHED", "\n".join(footer))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("autosort create: run failed: %s", type(e).__name__)
        await safe_edit(message, f"<b>❌ The run stopped:</b> <code>{_esc(type(e).__name__)}</code>\n"
                                 "Everything created so far is saved; tap CREATE again to continue.",
                        _back(), final=True)
    finally:
        if client is not None:
            await _stop(client)
        RUNNING.pop(user_id, None)
