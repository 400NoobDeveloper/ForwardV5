# ============================================================
# /botsettings — Owner-only settings panel
#
#   /botsettings → main panel (Set Channel, Set Variables, Maintenance, Close)
#   Set Channel  → Log Channel / Force Sub / News Channel / Back
#   Log Channel  → Variable + Description + Current Value  (SET / RESET / BACK)
#   SET          → "send the channel ID or @username" prompt (BACK)
#   Set Variables→ Sudo / Back
#   Maintenance  → True / False toggle screen
#
# Values are saved in data/settings.json.
# ============================================================

import json
import logging
import os
from contextlib import suppress

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.config import Config

logger = logging.getLogger(__name__)

# ── Small-caps button labels ──────────────────────────────────
_SC = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ",
)


def _sc(text: str) -> str:
    return text.lower().translate(_SC)


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(_sc(label), callback_data=data)


# ── Persistence ───────────────────────────────────────────────
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_SETTINGS_FILE = os.path.join(_DATA_DIR, "settings.json")

# user_id -> (setting key, chat_id, message_id of the prompt being answered)
_awaiting_input: dict = {}


def _load() -> dict:
    try:
        with open(_SETTINGS_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _get(key: str, default=None):
    value = _load().get(key)
    return default if value in (None, "") else value


def _set(key: str, value):
    data = _load()
    data[key] = value
    _save(data)


def _reset(key: str):
    data = _load()
    data.pop(key, None)
    _save(data)


def _is_owner(user_id: int) -> bool:
    return user_id in Config.BOT_OWNER_ID


# ── Sudo list (stored as a list of user IDs) ──────────────────
def _sudo_list() -> list:
    raw = _get("sudo", [])
    if isinstance(raw, str):                      # old format: plain text
        raw = raw.replace(",", " ").split()
    out = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            pass
    return out


# ── Variable names / descriptions ─────────────────────────────
_VARS = {
    "log_channel": {
        "label": "Log Channel",
        "var": "LOG_CHANNEL",
        "desc": "Channel where the bot sends its logs.",
        "prompt": "Send the channel ID (e.g. -1001234567890) or @username.",
    },
    "force_sub": {
        "label": "Force Sub",
        "var": "FORCE_SUB",
        "desc": "Channel users must join before they can use the bot.",
        "prompt": "Send the channel ID (e.g. -1001234567890) or @username.",
    },
    "news_channel": {
        "label": "News Channel",
        "var": "NEWS_CHANNEL",
        "desc": "Channel linked in\nthe /start message so users can\nfollow the latest updates.",
        "prompt": "Send the channel username (@mychannel) or invite link.",
    },
    "sudo": {
        "label": "Sudo Users",
        "var": "SUDO_USERS",
        "desc": "Users with permanent, full access to every command of this bot.",
        "prompt": (
            "Send a numeric Telegram\n"
            "user ID to make that user SUDO.\n"
            "Send remove <user_id> to revoke.\n\n"
            "Example: 123456789 or remove 123456789"
        ),
    },
}

CHANNEL_KEYS = ("log_channel", "force_sub", "news_channel")


def _back_target(key: str) -> str:
    return "bset:setch" if key in CHANNEL_KEYS else "bset:setvar"


def _current_value(key: str) -> str:
    if key == "sudo":
        users = _sudo_list()
        return "\n" + "\n".join(f"• {u}" for u in users) if users else "not set"
    value = _get(key)
    return "not set" if value is None else str(value)


def _set_or_not(key: str) -> str:
    if key == "sudo":
        return str(len(_sudo_list()))
    return "SET" if _get(key) else "NOT SET"


def _on_off(flag: bool) -> str:
    return "ON" if flag else "OFF"


# ── Screens ───────────────────────────────────────────────────
def _main_text() -> str:
    return (
        "Variable: BOT_SETTINGS\n\n"
        "Description: Manage the channels, variables (sudo users) and "
        "maintenance mode of this bot from one place.\n\n"
        "Tap a button below to configure\n"
        "a setting."
    )


def _main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("Set Channel", "bset:setch")],
        [_btn("Set Variables", "bset:setvar")],
        [_btn(f"Maintenance: {_on_off(bool(_get('maintenance', False)))}", "bset:maint")],
        [_btn("Close", "bset:close")],
    ])


def _setch_text() -> str:
    return (
        "Variable: SET_CHANNEL\n\n"
        "Description: Manage the log channel, force sub channel and news "
        "channel used by this bot.\n\n"
        "Choose the channel you want to set."
    )


def _setch_kb() -> InlineKeyboardMarkup:
    news = "CUSTOM" if _get("news_channel") else "DEFAULT"
    return InlineKeyboardMarkup([
        [_btn(f"Log Channel: {_set_or_not('log_channel')}", "bset:view:log_channel")],
        [_btn(f"Force Sub: {_set_or_not('force_sub')}", "bset:view:force_sub")],
        [_btn(f"News Channel: {news}", "bset:view:news_channel")],
        [_btn("Back", "bset:main")],
    ])


def _setvar_text() -> str:
    return (
        "Variable: SET_VARIABLES\n\n"
        "Description: Manage the sudo users of this bot.\n\n"
        "Choose the variable you want to set."
    )


def _setvar_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn(f"Sudo: {_set_or_not('sudo')}", "bset:view:sudo")],
        [_btn("Back", "bset:main")],
    ])


def _detail_text(key: str) -> str:
    v = _VARS[key]
    closing = (
        "Tap a button below to configure\na setting."
        if key == "sudo"
        else "Choose the channel you want to\nset for the above Var."
    )
    return (
        f"Variable: {v['var']}\n\n"
        f"Description: {v['desc']}\n\n"
        f"Current Value: {_current_value(key)}\n\n"
        f"{closing}"
    )


def _detail_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("Set", f"bset:set:{key}")],
        [_btn("Reset", f"bset:reset:{key}")],
        [_btn("Back", _back_target(key))],
    ])


def _prompt_text(key: str) -> str:
    v = _VARS[key]
    return (
        f"Variable: {v['var']}\n\n"
        f"Description: {v['desc']}\n\n"
        f"Current Value: {_current_value(key)}\n\n"
        f"{v['prompt']}"
    )


def _prompt_kb(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("Back", _back_target(key))]])


def _maint_text() -> str:
    return (
        "Variable: MAINTENANCE_MODE\n\n"
        "Description: When True the bot is under maintenance. Default: False.\n\n"
        f"Current Value: {bool(_get('maintenance', False))}\n\n"
        "Choose a valid value for the above Var"
    )


def _maint_kb() -> InlineKeyboardMarkup:
    turn_on = not bool(_get("maintenance", False))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(str(turn_on), callback_data=f"bset:setbool:{int(turn_on)}")],
        [InlineKeyboardButton("Back", callback_data="bset:main")],
    ])


# ── Helpers ───────────────────────────────────────────────────
async def _show(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    with suppress(MessageNotModified):
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.DISABLED
        )


def _parse_channel(raw: str):
    """Typed text -> value to store (None = clear)."""
    if raw.lower() in ("none", "off", "disable", "disabled", "0", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


# ── Handlers ──────────────────────────────────────────────────
# Module-level decorators on purpose: handlers are auto-loaded by pyrogram's
# plugin loader (main.py), so nothing has to call a register() function.

@Client.on_message(filters.command("botsettings") & filters.private)
async def cmd_botsettings(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        return await message.reply_text("ᴏᴡɴᴇʀ ᴏɴʟʏ.", quote=True)
    _awaiting_input.pop(message.from_user.id, None)
    await message.reply_text(
        _main_text(),
        reply_markup=_main_kb(),
        parse_mode=ParseMode.DISABLED,
        quote=True,
    )


@Client.on_callback_query(filters.regex(r"^bset:"))
async def cb_botsettings(client: Client, query: CallbackQuery):
    if not _is_owner(query.from_user.id):
        return await query.answer("ᴏᴡɴᴇʀ ᴏɴʟʏ.", show_alert=True)

    parts = query.data.split(":")
    action = parts[1]
    uid = query.from_user.id
    toast = None

    if action == "close":
        _awaiting_input.pop(uid, None)
        with suppress(Exception):
            await query.message.delete()
        return await query.answer()

    # any navigation cancels a pending "send the value" prompt
    if action != "set":
        _awaiting_input.pop(uid, None)

    if action == "main":
        await _show(query, _main_text(), _main_kb())

    elif action == "setch":
        await _show(query, _setch_text(), _setch_kb())

    elif action == "setvar":
        await _show(query, _setvar_text(), _setvar_kb())

    elif action == "view" and parts[2] in _VARS:
        await _show(query, _detail_text(parts[2]), _detail_kb(parts[2]))

    elif action == "set" and parts[2] in _VARS:
        key = parts[2]
        _awaiting_input[uid] = (key, query.message.chat.id, query.message.id)
        await _show(query, _prompt_text(key), _prompt_kb(key))

    elif action == "reset" and parts[2] in _VARS:
        key = parts[2]
        _reset(key)
        await _show(query, _detail_text(key), _detail_kb(key))
        toast = f"{_VARS[key]['label']} reset."

    elif action == "maint":
        await _show(query, _maint_text(), _maint_kb())

    elif action == "setbool":
        _set("maintenance", parts[2] == "1")
        await _show(query, _maint_text(), _maint_kb())
        toast = f"Maintenance {'ON' if parts[2] == '1' else 'OFF'}."

    await query.answer(toast)


@Client.on_message(filters.private & filters.text, group=48)
async def handle_botsettings_value_input(client: Client, message: Message):
    """Capture the owner's reply while a 'Set' prompt is open."""
    uid = message.from_user.id
    if uid not in _awaiting_input:
        return

    text = (message.text or "").strip()
    key, chat_id, msg_id = _awaiting_input[uid]

    if text.lower() in ("/cancel", "cancel"):
        _awaiting_input.pop(uid, None)
        with suppress(Exception):
            await client.edit_message_text(
                chat_id, msg_id, _detail_text(key),
                reply_markup=_detail_kb(key), parse_mode=ParseMode.DISABLED,
            )
        return

    # Another command (/start, /help ...) means the owner moved on - don't
    # swallow it as the value.
    if text.startswith("/"):
        _awaiting_input.pop(uid, None)
        return

    if key == "sudo":
        users = _sudo_list()
        try:
            if text.lower().startswith("remove "):
                target = int(text.split(None, 1)[1].strip())
                users = [u for u in users if u != target]
            else:
                target = int(text)
                if target not in users:
                    users.append(target)
        except (ValueError, IndexError):
            return await message.reply_text(
                "Invalid input. Send a numeric user ID, or remove <user_id>.",
                quote=True, parse_mode=ParseMode.DISABLED,
            )
        if users:
            _set("sudo", users)
        else:
            _reset("sudo")
    else:
        value = _parse_channel(text)
        if value is None:
            _reset(key)
        else:
            _set(key, value)

    _awaiting_input.pop(uid, None)
    with suppress(Exception):
        await message.delete()                    # the value that was typed
    with suppress(MessageNotModified):
        await client.edit_message_text(
            chat_id, msg_id, _detail_text(key),
            reply_markup=_detail_kb(key), parse_mode=ParseMode.DISABLED,
        )
