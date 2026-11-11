# ============================================================
# Forward V2 - Start command handler
# ============================================================

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from core.database import db

# TODO: replace with your own updates/news channel link
NEWS_CHANNEL = "https://t.me/dev_gagan"

# Only one button here, as requested — Settings.
main_buttons = [[
    InlineKeyboardButton('sᴇᴛᴛɪɴɢs', callback_data='settings#main')
]]


async def _get_bot_name(client: Client) -> str:
    """Always use the current Telegram bot name, so if the bot is renamed the message updates automatically."""
    try:
        me = await client.get_me()
        return me.first_name or "this bot"
    except Exception:
        return "this bot"


async def _build_start_text(client: Client) -> str:
    bot_name = await _get_bot_name(client)
    return (
        f"Hey there! My name is <b>{bot_name}</b> - I'm a <b>powerful</b> auto forward bot! "
        "Use /help to find out how to use me to my full potential.\n\n"
        f"Join my <a href=\"{NEWS_CHANNEL}\">news channel</a> to get information on all the latest updates.\n\n"
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
