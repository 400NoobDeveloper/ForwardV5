# ============================================================
# /botsettings — Owner-only interactive settings panel
# ============================================================

import json
import logging
import os

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.config import Config

logger = logging.getLogger(__name__)

# ── Small-caps unicode helper ──────────────────────────────────
_SMALL_CAPS_MAP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘqʀꜱᴛᴜᴠᴡxʏᴢ",
)


def _sc(text: str) -> str:
    """Convert text to small-caps unicode (used for button labels)."""
    return text.lower().translate(_SMALL_CAPS_MAP)


# ── Persistence ─────────────────────────────────────────────
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_SETTINGS_FILE = os.path.join(_DATA_DIR, "settings.json")

# In-memory: user_id -> (setting key, msg_id) they're currently setting
_awaiting_input: dict = {}


def _load() -> dict:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_SETTINGS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict):
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _get(key: str) -> str:
    data = _load()
    if key in data and data[key]:
        return str(data[key])
    return "not set"


def _set(key: str, value: str):
    data = _load()
    data[key] = value
    _save(data)


def _reset(key: str):
    data = _load()
    data.pop(key, None)
    _save(data)


# ── Settings definition ──────────────────────────────────────
CHANNEL_SETTINGS = [
    ("log_channel", "Log Channel"),
    ("force_sub", "Force Sub"),
    ("news_channel", "News Channel"),
]

MAIN_SETTINGS = [
    ("sudo", "Sudo"),
]

SETTINGS = CHANNEL_SETTINGS + MAIN_SETTINGS
CHANNEL_KEYS = {k for k, _ in CHANNEL_SETTINGS}

def _is_owner(user_id: int) -> bool:
    return user_id in Config.BOT_OWNER_ID


# ── UI builders ──────────────────────────────────────────────
def _main_text() -> str:
    lines = [f"**{_sc('Bot Settings')}**\n"]
    for key, label in MAIN_SETTINGS:
        lines.append(f"{label:<14}: {_get(key)}")
    lines.append("")
    lines.append(_sc("Set Channel") + ":")
    for key, label in CHANNEL_SETTINGS:
        lines.append(f"  {label:<14}: {_get(key)}")
    lines.append("\nTap a button below to configure a setting.")
    return "\n".join(lines)


def _main_markup() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(_sc("Set Channel"), callback_data="bset_channels")],
        [InlineKeyboardButton(_sc("Sudo"), callback_data="bset_open:sudo")],
        [InlineKeyboardButton(_sc("Close"), callback_data="bset_close")],
    ]
    return InlineKeyboardMarkup(rows)


def _channel_text() -> str:
    lines = [f"**{_sc('Set Channel')}**\n"]
    for key, label in CHANNEL_SETTINGS:
        lines.append(f"{label:<14}: {_get(key)}")
    lines.append("\nTap a button below to configure a channel.")
    return "\n".join(lines)


def _channel_markup() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(_sc(label), callback_data=f"bset_open:{key}")]
        for key, label in CHANNEL_SETTINGS
    ]
    rows.append([InlineKeyboardButton(_sc("Back"), callback_data="bset_back:main")])
    return InlineKeyboardMarkup(rows)


def _sub_text(key: str, label: str) -> str:
    val = _get(key)
    # Braille blank chars (⠀) are invisible but force a consistent bubble width
    # regardless of how short the value text is.
    _PAD = "⠀" * 36
    return (
        f"**{_sc(label)}**\n\n"
        f"Current value: `{val}`\n\n"
        f"Choose an action:\n{_PAD}"
    )


def _sub_markup(key: str) -> InlineKeyboardMarkup:
    back_target = "channels" if key in CHANNEL_KEYS else "main"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(_sc("Set"), callback_data=f"bset_set:{key}")],
        [InlineKeyboardButton(_sc("Reset"), callback_data=f"bset_reset:{key}")],
        [InlineKeyboardButton(_sc("Back"), callback_data=f"bset_back:{back_target}")],
    ])


# ── Handlers ──────────────────────────────────────────────────
# NOTE: these are module-level @Client... decorators (not wrapped in a
# register_*() function) because core/bot.py loads this project's plugins
# via pyrogram's `plugins={"root": "plugins"}` auto-discovery. A handler
# defined inside a function that nothing ever calls will never fire.

@Client.on_message(filters.command("botsettings") & filters.private)
async def cmd_botsettings(client: Client, message: Message):
    if not _is_owner(message.from_user.id):
        return await message.reply_text("ᴏᴡɴᴇʀ ᴏɴʟʏ.", quote=True)
    await message.reply_text(
        _main_text(),
        reply_markup=_main_markup(),
        parse_mode=ParseMode.MARKDOWN,
        quote=True,
    )


@Client.on_callback_query(filters.regex(r"^bset_channels$"))
async def cb_channels(client: Client, query: CallbackQuery):
    if not _is_owner(query.from_user.id):
        return await query.answer("ᴏᴡɴᴇʀ ᴏɴʟʏ.", show_alert=True)
    await query.edit_message_text(
        _channel_text(),
        reply_markup=_channel_markup(),
        parse_mode=ParseMode.MARKDOWN,
    )
    await query.answer()


@Client.on_callback_query(filters.regex(r"^bset_open:(.+)$"))
async def cb_open(client: Client, query: CallbackQuery):
    if not _is_owner(query.from_user.id):
        return await query.answer("ᴏᴡɴᴇʀ ᴏɴʟʏ.", show_alert=True)
    key = query.matches[0].group(1)
    label = dict(SETTINGS).get(key, key)
    await query.edit_message_text(
        _sub_text(key, label),
        reply_markup=_sub_markup(key),
        parse_mode=ParseMode.MARKDOWN,
    )
    await query.answer()


@Client.on_callback_query(filters.regex(r"^bset_set:(.+)$"))
async def cb_set(client: Client, query: CallbackQuery):
    if not _is_owner(query.from_user.id):
        return await query.answer("ᴏᴡɴᴇʀ ᴏɴʟʏ.", show_alert=True)
    key = query.matches[0].group(1)
    label = dict(SETTINGS).get(key, key)
    # Tag this user as awaiting input for this key, store the msg_id
    _awaiting_input[query.from_user.id] = (key, query.message.id)
    await query.edit_message_text(
        f"**Set {label}**\n\n"
        f"Send the new value for **{label}** now.\n\n"
        "Send /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await query.answer()


@Client.on_callback_query(filters.regex(r"^bset_reset:(.+)$"))
async def cb_reset(client: Client, query: CallbackQuery):
    if not _is_owner(query.from_user.id):
        return await query.answer("ᴏᴡɴᴇʀ ᴏɴʟʏ.", show_alert=True)
    key = query.matches[0].group(1)
    label = dict(SETTINGS).get(key, key)
    _reset(key)
    if key in CHANNEL_KEYS:
        await query.edit_message_text(
            _channel_text(),
            reply_markup=_channel_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text(
            _main_text(),
            reply_markup=_main_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
    await query.answer(f"✅ {label} has been reset.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^bset_back:(main|channels)$"))
async def cb_back(client: Client, query: CallbackQuery):
    if not _is_owner(query.from_user.id):
        return await query.answer("ᴏᴡɴᴇʀ ᴏɴʟʏ.", show_alert=True)
    target = query.matches[0].group(1)
    if target == "channels":
        await query.edit_message_text(
            _channel_text(),
            reply_markup=_channel_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text(
            _main_text(),
            reply_markup=_main_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
    await query.answer()


@Client.on_callback_query(filters.regex(r"^bset_close$"))
async def cb_close(client: Client, query: CallbackQuery):
    if not _is_owner(query.from_user.id):
        return await query.answer("ᴏᴡɴᴇʀ ᴏɴʟʏ.", show_alert=True)
    await query.message.delete()
    await query.answer()


@Client.on_message(filters.private & filters.text, group=48)
async def handle_botsettings_value_input(client: Client, message: Message):
    """Capture free-text replies when owner is in 'set' mode."""
    user_id = message.from_user.id
    if user_id not in _awaiting_input:
        return

    text = (message.text or "").strip()

    # Cancel
    if text.lower() in ("/cancel", "cancel"):
        key, msg_id = _awaiting_input.pop(user_id)
        label = dict(SETTINGS).get(key, key)
        await client.edit_message_text(
            message.chat.id,
            msg_id,
            _sub_text(key, label),
            reply_markup=_sub_markup(key),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Any other command (e.g. /start, /help) means the owner has moved
    # on rather than answering the prompt — don't swallow it as the
    # setting value. Clear the awaiting state and let that command's
    # own handler (which already ran earlier) be the only reply.
    if text.startswith("/"):
        _awaiting_input.pop(user_id, None)
        return

    key, msg_id = _awaiting_input.pop(user_id)
    label = dict(SETTINGS).get(key, key)
    _set(key, text)

    # Restore the settings message with updated menu (channel submenu or main)
    if key in CHANNEL_KEYS:
        await client.edit_message_text(
            message.chat.id,
            msg_id,
            _channel_text(),
            reply_markup=_channel_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await client.edit_message_text(
            message.chat.id,
            msg_id,
            _main_text(),
            reply_markup=_main_markup(),
            parse_mode=ParseMode.MARKDOWN,
        )
    await message.reply_text(
        f"✅ **{label}** updated to `{text}`",
        parse_mode=ParseMode.MARKDOWN,
    )
