# ============================================================
# Forward V2 - Start command handler
# ============================================================

import html
import json
import os

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from core.database import db

# Used until a News Channel is set in /botsettings
DEFAULT_NEWS_CHANNEL = "https://t.me/dev_gagan"

_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "settings.json")
_news_link_cache = {}          # saved value -> resolved link (only successful lookups)

# Only one button here, as requested — Settings.
main_buttons = [[
    InlineKeyboardButton('sᴇᴛᴛɪɴɢs', callback_data='settings#main')
]]


async def _news_channel_url(client: Client) -> str:
    """Link for the News Channel saved in /botsettings (default if none)."""
    try:
        with open(_SETTINGS_FILE, encoding="utf-8") as f:
            value = json.load(f).get("news_channel")
    except Exception:
        value = None
    if value in (None, ""):
        return DEFAULT_NEWS_CHANNEL

    value = str(value).strip()
    if value in _news_link_cache:
        return _news_link_cache[value]

    link = None
    if value.lstrip("-").isdigit():                 # numeric chat ID (e.g. from Verify)
        try:
            chat = await client.get_chat(int(value))
            if chat.username:
                link = f"https://t.me/{chat.username}"
            elif chat.invite_link:
                link = chat.invite_link
        except Exception:
            link = None
    elif value.startswith("@"):
        link = f"https://t.me/{value[1:]}"
    elif value.startswith(("http://", "https://")):
        link = value
    elif value.startswith(("t.me/", "telegram.me/")):
        link = f"https://{value}"
    else:
        link = f"https://t.me/{value}"

    if not link:                                    # private chat with no usable link
        return DEFAULT_NEWS_CHANNEL
    _news_link_cache[value] = link
    return link


async def _get_bot_name(client: Client) -> str:
    """Always use the current Telegram bot name, so if the bot is renamed the message updates automatically."""
    try:
        me = await client.get_me()
        return me.first_name or "this bot"
    except Exception:
        return "this bot"


async def _build_start_text(client: Client) -> str:
    bot_name = await _get_bot_name(client)
    news_url = html.escape(await _news_channel_url(client), quote=True)
    return (
        f"Hey there! My name is <b>{bot_name}</b> - I'm a <b>powerful</b> auto forward bot! "
        "Use /help to find out how to use me to my full potential.\n\n"
        f"Join my <a href=\"{news_url}\">news channel</a> to get information on all the latest updates.\n\n"
        "Check /privacy to view the privacy policy, and interact with your data."
    )


async def send_start_menu(client: Client, message):
    text = await _build_start_text(client)
    await message.reply_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(main_buttons),
        disable_web_page_preview=True,
        quote=True,
    )


@Client.on_message(filters.private & filters.command(['start']))
async def start(client, message):
    user = message.from_user
    try:
        await send_start_menu(client, message)
        try:
            if user and not await db.is_user_exist(user.id):
                await db.add_user(user.id, user.first_name)
        except Exception:
            pass
    except Exception:
        bot_name = await _get_bot_name(client)
        await message.reply_text(
            f"Hey there! My name is {bot_name} - I'm a powerful auto forward bot! Use /help to find out how to use me to my full potential.",
            quote=True
        )


@Client.on_callback_query(filters.regex(r'^back$'))
async def back_to_start(client, query):
    text = await _build_start_text(client)
    await query.message.edit_text(
        text=text,
        reply_markup=InlineKeyboardMarkup(main_buttons),
        disable_web_page_preview=True,
    )
    await query.answer()
