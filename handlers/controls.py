"""plugins/controls.py - all helper code for handlers/xsetting.py in one file.

Sections: Translation (bot texts), tools (start_clone_bot, CLIENT, configs, parse_buttons),
/diagnose_media helper, /unequify buttons, and the /settings controls (key checks, AniList
test, button builders, constants). The commands themselves live in handlers/xsetting.py.
"""


import asyncio
import logging
import re
from typing import AsyncGenerator, Optional, Union
from pyrogram import Client
from core.config import Config
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from core.database import db
import aiohttp
from pyrogram import filters


# ======================================================================
# Translation texts
# ======================================================================

class Translation(object):
  START_TXT = """<b>ʜᴇʟʟᴏ {}</b>

<i>ɪ'ᴍ ᴀ <b>ᴘᴏᴡᴇʀғᴜʟʟ</b> ᴀᴜᴛᴏ ғᴏʀᴡᴀʀᴅ ʙᴏᴛ

ɪ ᴄᴀɴ ғᴏʀᴡᴀʀᴅ ᴀʟʟ ᴍᴇssᴀɢᴇ ғʀᴏᴍ ᴏɴᴇ ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴀɴᴏᴛʜᴇʀ ᴄʜᴀɴɴᴇʟ</i> <b>➜ ᴡɪᴛʜ ᴍᴏʀᴇ ғᴇᴀᴛᴜʀᴇs.
ᴄʟɪᴄᴋ ʜᴇʟᴘ ʙᴜᴛᴛᴏɴ ᴛᴏ ᴋɴᴏᴡ ᴍᴏʀᴇ ᴀʙᴏᴜᴛ ᴍᴇ</b>"""


  HELP_TXT = """<b><u>🔆 HELP</b></u>

<u>**📚 Available commands:**</u>
<b>⏣ __/start - check I'm alive__ 
⏣ __/forward - forward messages__
⏣ __/unequify - delete duplicate messages in channels__
⏣ __/settings - configure your settings__
⏣ __/reset - reset your settings__</b>

<b><u>💢 Features:</b></u>
<b>► __Forward message from public channel to your channel without admin permission. if the channel is private need admin permission__
► __Forward message from private channel to your channel by using userbot(user must be member in there)__
► __custom caption__
► __custom button__
► __support restricted chats__
► __skip duplicate messages__
► __filter type of messages__
► __skip messages based on extensions & keywords & size__</b>
"""
  
  HOW_USE_TXT = """<b><u>⚠️ Before Forwarding:</b></u>
<b>► __add a bot or userbot__
► __add atleast one to channel__ `(your bot/userbot must be admin in there)`
► __You can add chats or bots by using /settings__
► __if the **From Channel** is private your userbot must be member in there or your bot must need admin permission in there also__
► __Then use /forward to forward messages__</b>"""
  
  ABOUT_TXT = """<b>╭──────❰ 🤖 Bot Details ❱──────〄
│ 
│ 🤖 Mʏ Nᴀᴍᴇ : <a href=https://t.me/devganbot>Dev Gagan Botᴛ</a>
│ 👨‍💻 ᴅᴇᴠᴘʟᴏᴇʀ : <a href=https://t.me/dev_gagan>Team SPY</a>
│ 🤖 ᴜᴘᴅᴀᴛᴇ  : <a href=https://t.me/dev_gagan>devgagan</a>
│ 📡 ʜᴏsᴛ ᴏɴ : <a href=https://devgagan.in/>Dev Gagan Host</a>
│ 🗣️ ʟᴀɴɢᴜᴀɢᴇ  : ᴘʏᴛʜᴏɴ 3 
{python_version}
│ 📚 ʟɪʙʀᴀʀʏ  : ᴘʏʀᴏɢʀᴀᴍ  
╰────────────────────⍟</b>"""
  
  STATUS_TXT = """<b>╭──────❪ 🤖 Bot Status ❫─────⍟
│
├👨 ᴜsᴇʀs  : {}
│
├🤖 ʙᴏᴛs : {}
│
├📣 ᴄʜᴀɴɴᴇʟ  : {} 
╰───────────────────⍟</b>""" 
  
  FROM_MSG = "<b>❪ SET SOURCE CHAT ❫\n\nForward the last message or last message link of source chat.\nType <code>me</code> or <code>saved</code> to forward from Saved Messages.\n/cancel - cancel this process</b>"
  TO_MSG = "<b>❪ CHOOSE TARGET CHAT ❫\n\nChoose your target chat from the given buttons.\n/cancel - Cancel this process</b>"
  SAVED_MSG_MODE = "<b>❪ SELECT MODE ❫\n\nChoose the forwarding mode for Saved Messages:\n\n1. <code>batch</code> - Forward existing messages (you can set a limit or choose All).\n2. <code>live</code> - Forward existing messages and keep waiting for new ones (Continuous).</b>"
  SAVED_MSG_LIMIT = "<b>❪ NUMBER OF MESSAGES ❫\n\nHow many messages do you want to forward from Saved Messages?\nEnter a number (e.g., 100) or type <code>all</code> to forward everything.</b>"
  SKIP_MSG = "<b>❪ SET MESSAGE SKIPING NUMBER ❫</b>\n\n<b>Skip the message as much as you enter the number and the rest of the message will be forwarded\nDefault Skip Number =</b> <code>0</code>\n<code>eg: You enter 0 = 0 message skiped\n You enter 5 = 5 message skiped</code>\n/cancel <b>- cancel this process</b>"
  CATEGORY_MSG = "<b>❪ FILTER BY CATEGORY ❫</b>\n\n<b>Only files matching this category will be forwarded, everything else is skipped. Choose one:</b>\n\n<code>all | movie | series | anime | pdf | image | audio</code>\n\n<i>Classification is based on the file name (uses anitopy/guessit) - photos always count as \"image\", .pdf files always count as \"pdf\".</i>\n/cancel <b>- cancel this process</b>"
  CANCEL = "<b>Process Cancelled Succefully !</b>"
  BOT_DETAILS = "<b><u>📄 BOT DETAILS</b></u>\n\n<b>➣ NAME:</b> <code>{}</code>\n<b>➣ BOT ID:</b> <code>{}</code>\n<b>➣ USERNAME:</b> @{}"
  USER_DETAILS = "<b><u>📄 USERBOT DETAILS</b></u>\n\n<b>➣ NAME:</b> <code>{}</code>\n<b>➣ USER ID:</b> <code>{}</code>\n<b>➣ USERNAME:</b> @{}"  
         
  TEXT = """<b>╭────❰ <u>Forwarded Status</u> ❱────❍
┃
┣⊸<b>🕵 ғᴇᴄʜᴇᴅ ᴍsɢ :</b> <code>{}</code>
┣⊸<b>✅ sᴜᴄᴄᴇғᴜʟʟʏ ғᴡᴅ :</b> <code>{}</code>
┣⊸<b>👥 ᴅᴜᴘʟɪᴄᴀᴛᴇ ᴍsɢ :</b> <code>{}</code>
┣⊸<b>🗑️ ᴅᴇʟᴇᴛᴇᴅ ᴍsɢ :</b> <code>{}</code>
┣⊸<b>🪆 sᴋɪᴘᴘᴇᴅ ᴍsɢ :</b> <code>{}</code>
┣⊸<b>📊 sᴛᴀᴛᴜs  :</b> <code>{}</code>
┣⊸<b>⏳ ᴘʀᴏɢʀᴇss  :</b> <code>{}</code> %
┣⊸<b>⏰ ᴇᴛᴀ :</b> <code>{}</code>
┃
╰────⌊ <b>{}</b> ⌉───❍</b>"""

  TEXT1 = """<b>╭─❰ <u>Forwarded Status</u> ❱─❍
┃
┣⊸🕵𝙁𝙚𝙘𝙝𝙚𝙙 𝙈𝙨𝙜 : {}
┣⊸✅𝙎𝙪𝙘𝙘𝙚𝙛𝙪𝙡𝙮 𝙁𝙬𝙙 : {}
┣⊸👥𝘿𝙪𝙥𝙡𝙞𝙘𝙖𝙩𝙚 𝙈𝙨𝙜: {}
┣⊸🗑𝘿𝙚𝙡𝙚𝙩𝙚𝙙 𝙈𝙨𝙜: {}
┣⊸🪆𝙎𝙠𝙞𝙥𝙥𝙚𝙙 : {}
┣⊸📊𝙎𝙩𝙖𝙩𝙨 : {}
┣⊸⏳𝙋𝙧𝙤𝙜𝙧𝙚𝙨𝙨 : {}
┣⊸𝙀𝙏𝘼 : {}
┃
╰─⌊ {} ⌉─❍</b>"""

  DUPLICATE_TEXT = """
╔════❰ ᴜɴᴇǫᴜɪғʏ sᴛᴀᴛᴜs ❱═❍⊱❁۪۪
║╭━━━━━━━━━━━━━━━➣
║┣⪼ <b>ғᴇᴛᴄʜᴇᴅ ғɪʟᴇs:</b> <code>{}</code>
║┃
║┣⪼ <b>ᴅᴜᴘʟɪᴄᴀᴛᴇ ᴅᴇʟᴇᴛᴇᴅ:</b> <code>{}</code> 
║╰━━━━━━━━━━━━━━━➣
╚════❰ {} ❱══❍⊱❁۪۪
"""
  DOUBLE_CHECK = """<b><u>DOUBLE CHECKING ⚠️</b></u>
<code>Before forwarding the messages Click the Yes button only after checking the following</code>

<b>★ YOUR BOT:</b> [{botname}](t.me/{botuname})
<b>★ FROM CHANNEL:</b> `{from_chat}`
<b>★ TO CHANNEL:</b> `{to_chat}`
<b>★ SKIP MESSAGES:</b> `{skip}`

<i>° [{botname}](t.me/{botuname}) must be admin in **TARGET CHAT**</i> (`{to_chat}`)
<i>° If the **SOURCE CHAT** is private your userbot must be member or your bot must be admin in there also</b></i>

<b>If the above is checked then the yes button can be clicked</b>"""


# ======================================================================
# Shared helpers: start_clone_bot, CLIENT, get/update_configs, parse_buttons
# ======================================================================

logger = logging.getLogger(__name__)

logger.setLevel(logging.INFO)

BTN_URL_REGEX = re.compile(r"(\[([^\[]+?)]\[buttonurl:/{0,2}(.+?)(:same)?])")

BOT_TOKEN_TEXT = "<b>1) create a bot using @BotFather\n2) Then you will get a message with bot token\n3) Forward that message to me</b>"

SESSION_STRING_SIZE = 351

async def start_clone_bot(FwdBot, data=None):
   await FwdBot.start()
   #
   async def iter_messages(
      self,
      chat_id: Union[int, str],
      limit: int,
      offset: int = 0,
      search: str = None,
      filter: "types.TypeMessagesFilter" = None,
      continuous: bool = False
      ) -> Optional[AsyncGenerator["types.Message", None]]:
        """Walk a chat by message id, in batches of up to 200.

        limit      - last message id to include (ids offset..limit are visited).
        offset     - first message id to visit (the "skip" number).
        continuous - never stop; keep polling for new messages (live mode).
        """
        BATCH = 200
        UNBOUNDED = 1_000_000          # "all" in Saved Messages uses a huge limit
        current = int(offset or 0) + 1     # skip N = ignore message ids 1..N
        empty_batches = 0
        errors = 0

        while True:
            if continuous:
                ids = list(range(current, current + BATCH))
            else:
                if limit > 0 and current > limit:
                    return
                last = current + BATCH - 1
                if limit > 0:
                    last = min(last, limit)
                ids = list(range(current, last + 1))

            try:
                messages = await self.get_messages(chat_id, ids)
                errors = 0
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                continue
            except Exception as e:
                errors += 1
                logger.warning(f"iter_messages: get_messages failed ({errors}/3): {e}")
                if errors >= 3:
                    raise
                await asyncio.sleep(3)
                continue

            if not isinstance(messages, list):
                messages = [messages]
            valid = [m for m in messages if m and not m.empty]

            if valid:
                empty_batches = 0
                for message in valid:
                    yield message
                if continuous:
                    # only move past what we really saw, so messages that arrive
                    # later inside this id window are still picked up
                    current = max(m.id for m in valid) + 1
                else:
                    current = ids[-1] + 1
                continue

            # nothing in this window
            if continuous:
                await asyncio.sleep(10)
                continue
            empty_batches += 1
            current = ids[-1] + 1
            if limit >= UNBOUNDED and empty_batches >= 25:
                return                  # ran off the end of the chat

   #
   FwdBot.iter_messages = iter_messages
   return FwdBot

class CLIENT: 
  def __init__(self):
     self.api_id = Config.API_ID
     self.api_hash = Config.API_HASH
    
  def client(self, data, user=None):
     if user == None and data.get('is_bot') == False:
        return Client("USERBOT", self.api_id, self.api_hash, session_string=data.get('session'))
     elif user == True:
        return Client("USERBOT", self.api_id, self.api_hash, session_string=data)
     elif user != False:
        data = data.get('token')
     return Client("BOT", self.api_id, self.api_hash, bot_token=data, in_memory=True)
  
  async def add_bot(self, bot, message):
     user_id = int(message.from_user.id)
     msg = await bot.ask(chat_id=user_id, text=BOT_TOKEN_TEXT)
     if msg.text=='/cancel':
        return await msg.reply('<b>process cancelled !</b>')
     elif not msg.forward_date:
       return await msg.reply_text("<b>This is not a forward message</b>")
     elif not msg.forward_from or str(msg.forward_from.id) != "93372553":
       return await msg.reply_text("<b>This message was not forward from bot father</b>")
     bot_token = re.findall(r'\d[0-9]{8,10}:[0-9A-Za-z_-]{35}', msg.text, re.IGNORECASE)
     bot_token = bot_token[0] if bot_token else None
     if not bot_token:
       return await msg.reply_text("<b>There is no bot token in that message</b>")
     try:
       _client = await start_clone_bot(self.client(bot_token, False), True)
     except Exception as e:
       return await msg.reply_text(f"<b>BOT ERROR:</b> `{e}`")
     _bot = _client.me
     try:
       await _client.stop()
     except Exception:
       pass
     details = {
       'id': _bot.id,
       'is_bot': True,
       'user_id': user_id,
       'name': _bot.first_name,
       'token': bot_token,
       'username': _bot.username 
     }
     await db.add_bot(details)
     return True
    
  async def add_session(self, bot, message):
     user_id = int(message.from_user.id)
     text = "<b>⚠️ DISCLAIMER ⚠️</b>\n\n<code>you can use your session for forward message from private chat to another chat.\nPlease add your pyrogram session with your own risk. Their is a chance to ban your account. My developer is not responsible if your account may get banned.</code>"
     await bot.send_message(user_id, text=text)
     msg = await bot.ask(chat_id=user_id, text="<b>send your pyrogram session.\nGet it from trusted sources.\n\n/cancel - cancel the process</b>")
     if msg.text=='/cancel':
        return await msg.reply('<b>process cancelled !</b>')
     elif len(msg.text) < SESSION_STRING_SIZE:
        return await msg.reply('<b>invalid session sring</b>')
     try:
       client = await start_clone_bot(self.client(msg.text, True), True)
     except Exception as e:
       return await msg.reply_text(f"<b>USER BOT ERROR:</b> `{e}`")
     user = client.me
     try:
       await client.stop()
     except Exception:
       pass
     details = {
       'id': user.id,
       'is_bot': False,
       'user_id': user_id,
       'name': user.first_name,
       'session': msg.text,
       'username': user.username
     }
     await db.add_bot(details)
     return True

  
async def get_configs(user_id):
  #configs = temp.CONFIGS.get(user_id)
  #if not configs:
  configs = await db.get_configs(user_id)
  #temp.CONFIGS[user_id] = configs 
  return configs

                          
async def update_configs(user_id, key, value):
  current = await db.get_configs(user_id)
  if key in ['caption', 'duplicate', 'db_uri', 'forward_tag', 'protect', 'file_size', 'size_limit', 'extension', 'keywords', 'movie_whitelist', 'movie_whitelist_exact', 'series_whitelist', 'series_whitelist_exact', 'caption_first_line', 'remove_caption_link', 'button']:
     current[key] = value
  else: 
     current['filters'][key] = value
 # temp.CONFIGS[user_id] = value
  await db.update_configs(user_id, current)

    
def parse_buttons(text, markup=True):
    buttons = []
    for match in BTN_URL_REGEX.finditer(text):
        n_escapes = 0
        to_check = match.start(1) - 1
        while to_check > 0 and text[to_check] == "\\":
            n_escapes += 1
            to_check -= 1

        if n_escapes % 2 == 0:
            if bool(match.group(4)) and buttons:
                buttons[-1].append(InlineKeyboardButton(
                    text=match.group(2),
                    url=match.group(3).replace(" ", "")))
            else:
                buttons.append([InlineKeyboardButton(
                    text=match.group(2),
                    url=match.group(3).replace(" ", ""))])
    if markup and buttons:
       buttons = InlineKeyboardMarkup(buttons)
    return buttons if buttons else None

# shared instance used by the /unequify and /settings code below.
# (the class itself stays named CLIENT: handlers/forward.py and handlers/autosort.py import it and make their own instance)
CLIENT_HELPER = CLIENT()


# ======================================================================
# Helper for /diagnose_media
# ======================================================================

# ============================================================
# /diagnose_media — reply to any forwarded message with this
# command to see exactly what pyrogram reports for it: whether
# it's message.video or message.document, its mime_type,
# whether it has a thumbnail, size, filename, etc.
#
# This exists because identify.py's video-vs-document routing
# depends on pyrogram's own message.video/message.document
# split, which doesn't always match what a play-button preview
# in the Telegram app visually suggests (see plugins/identify.py's
# note above `is_files_variant = ...`). Rather than keep guessing
# at heuristics, reply to a specific problem message with this
# command and share the output - that's real ground truth instead
# of another guess.
# ============================================================



def _dump_attrs(obj, fields):
    lines = []
    for f in fields:
        val = getattr(obj, f, "<missing>")
        lines.append(f"  {f}: {val!r}")
    return "\n".join(lines)


# ======================================================================
# Buttons used by /unequify
# ======================================================================

#from pyropatch.utils import unpack_new_file_id

COMPLETED_BTN = InlineKeyboardMarkup(
   [
      [InlineKeyboardButton('⚡ Support', url='https://t.me/dev_gagan')],
      [InlineKeyboardButton('📢 Updates', url='https://t.me/dev_gagan')]
   ]
)

CANCEL_BTN = InlineKeyboardMarkup([[InlineKeyboardButton('• ᴄᴀɴᴄᴇʟ', 'terminate_frwd')]])


# ======================================================================
# /settings controls: key checks, AniList test, button builders, constants
# ======================================================================

# categories from plugins/identify.py that /autosort can route to a channel
AUTOSORT_CATEGORIES = [
    ("movie", "Movies (Video)"),
    ("movie_files", "Movies (Files)"),
    ("series", "Series (Video)"),
    ("series_files", "Series (Files)"),
    ("anime", "Anime (Video)"),
    ("anime_files", "Anime (Files)"),
    ("education", "Education (Video)"),
    ("education_files", "Education (Files)"),
    ("pdf", "PDF"),
    ("image", "Image"),
    ("audio", "Audio"),
    ("files", "Files (Unidentified)"),
    ("other", "Misc / Other"),
    ("duplicate", "Duplicate"),
]

# Two independent name-whitelists (see plugins/channels.py's check_filters()
# and plugins/identify.py's title_in_whitelist()) - "movie" also covers
# anime (both use identify.py's movie-ish categories), "series" is fully
# separate so a user can restrict series without touching movies or
# vice versa. Keyed by the callback-data/kind name used throughout the
# settings handlers below.
WHITELIST_KINDS = {
    'movie': {
        'list_key': 'movie_whitelist',
        'exact_key': 'movie_whitelist_exact',
        'label': 'MOVIE / ANIME WHITELIST',
        'scope': 'movie/anime',
        'prompt': 'movie/anime name(s)',
    },
    'series': {
        'list_key': 'series_whitelist',
        'exact_key': 'series_whitelist_exact',
        'label': 'SERIES WHITELIST',
        'scope': 'series',
        'prompt': 'series name(s) - e.g. "Money Heist" covers every season/episode of it',
    },
}

async def _verify_tmdb_key(key: str):
    """Hits TMDB's own auth-check endpoint so we can tell the user
    definitively whether their key works, instead of them having to
    guess from /autosort or /fwd's classification behaviour later."""
    if not key:
        return False, "No key given."
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.themoviedb.org/3/authentication/token/new",
                params={"api_key": key},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("success"):
                    return True, "TMDB accepted this key."
                return False, data.get("status_message", f"HTTP {resp.status}")
    except Exception as e:
        return False, f"Could not reach TMDB: {e}"

async def _verify_openai_key(key: str):
    """Sends one tiny chat request - the same kind /autosort makes - so the
    result covers the key, its chat permission and the configured model
    (OPENAI_MODEL, default gpt-4o-mini). Costs a fraction of a cent."""
    if not key:
        return False, "No key given."
    try:
        from plugins.identify import OPENAI_MODEL, OPENAI_URL
    except Exception:
        OPENAI_MODEL, OPENAI_URL = "gpt-4o-mini", "https://api.openai.com/v1/chat/completions"
    body = {
        "model": OPENAI_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": 'Reply with this JSON and nothing else: {"ok": true}'}],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENAI_URL, json=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = {}
                if resp.status == 200:
                    return True, f"OpenAI accepted this key (model: {OPENAI_MODEL})."
                err = data.get("error") if isinstance(data, dict) else None
                msg = (err or {}).get("message") if isinstance(err, dict) else None
                msg = msg or f"HTTP {resp.status}"
                if resp.status in (403, 404):
                    msg += f"\n(model: {OPENAI_MODEL} - change it with the OPENAI_MODEL env var if this key can't use it)"
                return False, msg
    except Exception as e:
        return False, f"Could not reach OpenAI: {e}"

async def _verify_tpdb_key(key: str):
    """One real ThePornDB request - the same /scenes lookup plugins/identify.py
    makes - so the result covers the token and the API being reachable.
    200 = token accepted; 401/403 = rejected."""
    if not key:
        return False, "No key given."
    try:
        from plugins.identify import TPDB_URL, TPDB_TIMEOUT_SECONDS
    except Exception:
        TPDB_URL, TPDB_TIMEOUT_SECONDS = "https://api.theporndb.net", 15
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{TPDB_URL}/scenes", params={"parse": "test"},
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=TPDB_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status == 200:
                    return True, "ThePornDB accepted this token."
                if resp.status in (401, 403):
                    return False, f"ThePornDB rejected this token (HTTP {resp.status})."
                if resp.status == 429:
                    return False, "ThePornDB rate limit hit (HTTP 429) - try again in a minute."
                return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, f"Could not reach ThePornDB: {e}"

async def _verify_anilist_key(key: str):
    """Asks AniList who the token belongs to (Viewer query). A valid token
    returns the account name; a bad one comes back as an error."""
    if not key:
        return False, "No key given."
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://graphql.anilist.co",
                json={"query": "query { Viewer { name } }"},
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json",
                         "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    return False, f"HTTP {resp.status}"
                viewer = ((data or {}).get("data") or {}).get("Viewer")
                if resp.status == 200 and viewer:
                    return True, f"AniList accepted this token (account: {viewer.get('name')})."
                errs = (data or {}).get("errors") or []
                msg = errs[0].get("message") if errs else f"HTTP {resp.status}"
                return False, msg
    except Exception as e:
        return False, f"Could not reach AniList: {e}"

async def _test_anilist(key: str = ""):
    """Live check of AniList's GraphQL API: one real lookup, so the user sees
    both that the API is reachable and what a match looks like. Uses the
    user's token too, if one is set."""
    from plugins.identify import anilist_lookup
    try:
        status, media = await anilist_lookup("Naruto", anilist_key=key)
    except Exception as e:
        return False, f"Could not reach AniList: {e}"
    if status == "error":
        return False, "AniList did not answer (network error, rate limit or API down)."
    if status == "none":
        return True, "AniList is reachable, but the sample lookup (Naruto) found nothing."
    return True, "AniList is reachable.\n\n" + _fmt_anilist(media)

def _fmt_anilist(media):
    t = media.get("title") or {}
    name = t.get("english") or t.get("romaji") or t.get("native") or "?"
    return (f"Match: {name}\n"
            f"Format: {media.get('format')}  |  Origin: {media.get('countryOfOrigin')}\n"
            f"Year: {media.get('seasonYear') or '-'}  |  Popularity: {media.get('popularity')}")

# Invisible padding (Braille blanks) to widen the bubble so it matches the
# width of the button grid below it — doesn't affect the buttons themselves.
_PAD = "⠀" * 30

SETTINGS_TXT = f"<b>change your settings as your wish</b>{_PAD}"

      
def main_buttons():
  buttons = [[
       InlineKeyboardButton('ʙᴏᴛs',
                    callback_data=f'settings#bots'),
       InlineKeyboardButton('ᴄʜᴀɴɴᴇʟs',
                    callback_data=f'settings#channels')
       ],[
       InlineKeyboardButton('ᴄᴀᴘᴛɪᴏɴ',
                    callback_data=f'settings#caption'),
       InlineKeyboardButton('ᴍᴏɴɢᴏᴅʙ',
                    callback_data=f'settings#database')
       ],[
       InlineKeyboardButton('ꜰɪʟᴛᴇʀs',
                    callback_data=f'settings#filters'),
       InlineKeyboardButton('ʙᴜᴛᴛᴏɴ',
                    callback_data=f'settings#button')
       ],[
       InlineKeyboardButton('ᴇxᴛʀᴀ sᴇᴛᴛɪɴɢs',
                    callback_data='settings#nextfilters')
       ],[
       InlineKeyboardButton('📁 ᴘʀɪᴠᴀᴛᴇ ꜰɪʟᴇs',
                    callback_data='settings#privatefiles')
       ],[      
       InlineKeyboardButton('ʙᴀᴄᴋ', callback_data='back')
       ]]
  return InlineKeyboardMarkup(buttons)

def size_limit(limit):
   if str(limit) == "None":
      return None, ""
   elif str(limit) == "True":
      return True, "more than"
   else:
      return False, "less than"

def extract_btn(datas):
    i = 0
    btn = []
    if datas:
       for data in datas:
         if i >= 5:
            i = 0
         if i == 0:
            btn.append([InlineKeyboardButton(data, f'settings#alert_{data}')])
            i += 1
            continue
         elif i > 0:
            btn[-1].append(InlineKeyboardButton(data, f'settings#alert_{data}'))
            i += 1
    return btn 

def whitelist_text(kind: str) -> str:
    info = WHITELIST_KINDS[kind]
    return (
        f'<b><u>{info["label"]}</u></b>\n\n'
        f'**Only files whose detected {info["scope"]} title matches one of '
        'these names will forward (in /fwd and /autosort). Add a name once and '
        'every season/episode or release quality of it matches - resolution, '
        'year and (for series) season/episode numbers are parsed separately '
        'and never affect the name match. Only applies to content already '
        f'detected as {info["scope"]} - Education/PDF/Audio/etc. are never '
        'touched by this list, including a file that accidentally LOOKS like '
        'one of these (e.g. a lecture with stray season/episode-style '
        'numbering), since it still won\'t match a real name here. '
        'Empty list = no restriction. Tap a name below to remove it.**\n\n'
        '**Mode:**\n'
        '🌊 Loose - whitelist entry just needs to appear as a whole word in the '
        'detected title ("Pathaan" also matches "Pathaan Returns").\n'
        '🎯 Exact - detected title must equal the whitelist entry exactly. '
        'Needs guessit/anitopy to be extracting a clean title to be useful - '
        'if title parsing fails for a file, exact mode blocks it rather than '
        'risking a wrong match.'
    )

def whitelist_btn(datas, kind: str):
    """Like extract_btn(), but tapping a name removes it (index-based
    callback, since these names can contain characters/length that don't
    round-trip cleanly through callback_data the way a single keyword does)."""
    prefix = 'rmve_movie' if kind == 'movie' else 'rmve_series'
    i = 0
    btn = []
    if datas:
       for idx, data in enumerate(datas):
         label = data if len(data) <= 30 else data[:27] + '...'
         if i >= 5:
            i = 0
         if i == 0:
            btn.append([InlineKeyboardButton(label, f'settings#{prefix}-{idx}')])
            i += 1
            continue
         elif i > 0:
            btn[-1].append(InlineKeyboardButton(label, f'settings#{prefix}-{idx}'))
            i += 1
    return btn

def whitelist_menu(kind: str, whitelist, exact: bool):
    """Full whitelist screen for `kind` ('movie' or 'series'): name list
    (tap to remove), an exact/loose match mode toggle, add / remove-all / back."""
    btn = whitelist_btn(whitelist, kind)
    btn.append([
        InlineKeyboardButton(
            'Mode: ' + ('🎯 Exact' if exact else '🌊 Loose (fuzzy)'),
            f'settings#toggle_{kind}_exact')
    ])
    btn.append([InlineKeyboardButton('ᴀᴅᴅ', f'settings#add_{kind}')])
    btn.append([InlineKeyboardButton('Remove all', f'settings#rmve_all_{kind}')])
    btn.append([InlineKeyboardButton('ʙᴀᴄᴋ', 'settings#main')])
    return InlineKeyboardMarkup(btn)

def size_button(size):
  buttons = [[
       InlineKeyboardButton('+',
                    callback_data=f'settings#update_limit-True-{size}'),
       InlineKeyboardButton('=',
                    callback_data=f'settings#update_limit-None-{size}'),
       InlineKeyboardButton('-',
                    callback_data=f'settings#update_limit-False-{size}')
       ],[
       InlineKeyboardButton('+1',
                    callback_data=f'settings#update_size-{size + 1}'),
       InlineKeyboardButton('-1',
                    callback_data=f'settings#update_size-{max(size - 1, 0)}')
       ],[
       InlineKeyboardButton('+5',
                    callback_data=f'settings#update_size-{size + 5}'),
       InlineKeyboardButton('-5',
                    callback_data=f'settings#update_size-{max(size - 5, 0)}')
       ],[
       InlineKeyboardButton('+10',
                    callback_data=f'settings#update_size-{size + 10}'),
       InlineKeyboardButton('-10',
                    callback_data=f'settings#update_size-{max(size - 10, 0)}')
       ],[
       InlineKeyboardButton('+50',
                    callback_data=f'settings#update_size-{size + 50}'),
       InlineKeyboardButton('-50',
                    callback_data=f'settings#update_size-{max(size - 50, 0)}')
       ],[
       InlineKeyboardButton('+100',
                    callback_data=f'settings#update_size-{size + 100}'),
       InlineKeyboardButton('-100',
                    callback_data=f'settings#update_size-{max(size - 100, 0)}')
       ],[
       InlineKeyboardButton('ʙᴀᴄᴋ',
                    callback_data="settings#main")
     ]]
  return InlineKeyboardMarkup(buttons)

       
async def filters_buttons(user_id):
  filter = await get_configs(user_id)
  filters = filter['filters']
  buttons = [[
       InlineKeyboardButton('ꜰᴏʀᴡᴀʀᴅ ᴛᴀɢ',
                    callback_data=f'settings_#updatefilter-forward_tag-{filter["forward_tag"]}'),
       InlineKeyboardButton('✅' if filter['forward_tag'] else '❌',
                    callback_data=f'settings#updatefilter-forward_tag-{filter["forward_tag"]}')
       ],[
       InlineKeyboardButton('ᴛᴇxᴛs',
                    callback_data=f'settings_#updatefilter-text-{filters["text"]}'),
       InlineKeyboardButton('✅' if filters['text'] else '❌',
                    callback_data=f'settings#updatefilter-text-{filters["text"]}')
       ],[
       InlineKeyboardButton('ᴅᴏᴄᴜᴍᴇɴᴛs',
                    callback_data=f'settings_#updatefilter-document-{filters["document"]}'),
       InlineKeyboardButton('✅' if filters['document'] else '❌',
                    callback_data=f'settings#updatefilter-document-{filters["document"]}')
       ],[
       InlineKeyboardButton('ᴠɪᴅᴇᴏs',
                    callback_data=f'settings_#updatefilter-video-{filters["video"]}'),
       InlineKeyboardButton('✅' if filters['video'] else '❌',
                    callback_data=f'settings#updatefilter-video-{filters["video"]}')
       ],[
       InlineKeyboardButton('ᴘʜᴏᴛᴏs',
                    callback_data=f'settings_#updatefilter-photo-{filters["photo"]}'),
       InlineKeyboardButton('✅' if filters['photo'] else '❌',
                    callback_data=f'settings#updatefilter-photo-{filters["photo"]}')
       ],[
       InlineKeyboardButton('ᴀᴜᴅɪᴏs',
                    callback_data=f'settings_#updatefilter-audio-{filters["audio"]}'),
       InlineKeyboardButton('✅' if filters['audio'] else '❌',
                    callback_data=f'settings#updatefilter-audio-{filters["audio"]}')
       ],[
       InlineKeyboardButton('ᴠᴏɪᴄᴇs',
                    callback_data=f'settings_#updatefilter-voice-{filters["voice"]}'),
       InlineKeyboardButton('✅' if filters['voice'] else '❌',
                    callback_data=f'settings#updatefilter-voice-{filters["voice"]}')
       ],[
       InlineKeyboardButton('ᴀɴɪᴍᴀᴛɪᴏɴs',
                    callback_data=f'settings_#updatefilter-animation-{filters["animation"]}'),
       InlineKeyboardButton('✅' if filters['animation'] else '❌',
                    callback_data=f'settings#updatefilter-animation-{filters["animation"]}')
       ],[
       InlineKeyboardButton('sᴛɪᴄᴋᴇʀs',
                    callback_data=f'settings_#updatefilter-sticker-{filters["sticker"]}'),
       InlineKeyboardButton('✅' if filters['sticker'] else '❌',
                    callback_data=f'settings#updatefilter-sticker-{filters["sticker"]}')
       ],[
       InlineKeyboardButton('sᴋɪᴘ ᴅᴜᴘʟɪᴄᴀᴛᴇ',
                    callback_data=f'settings_#updatefilter-duplicate-{filter["duplicate"]}'),
       InlineKeyboardButton('✅' if filter['duplicate'] else '❌',
                    callback_data=f'settings#updatefilter-duplicate-{filter["duplicate"]}')
       ],[
       InlineKeyboardButton('ʙᴀᴄᴋ',
                    callback_data="settings#main")
       ]]
  return InlineKeyboardMarkup(buttons) 

async def next_filters_buttons(user_id):
  filter = await get_configs(user_id)
  filters = filter['filters']
  buttons = [[
       InlineKeyboardButton('ᴘᴏʟʟ',
                    callback_data=f'settings_#updatefilter-poll-{filters["poll"]}'),
       InlineKeyboardButton('✅' if filters['poll'] else '❌',
                    callback_data=f'settings#updatefilter-poll-{filters["poll"]}')
       ],[
       InlineKeyboardButton('sᴇᴄᴜʀᴇ ᴍᴇssᴀɢᴇ',
                    callback_data=f'settings_#updatefilter-protect-{filter["protect"]}'),
       InlineKeyboardButton('✅' if filter['protect'] else '❌',
                    callback_data=f'settings#updatefilter-protect-{filter["protect"]}')
       ],[
       InlineKeyboardButton('ʀᴇᴍᴏᴠᴇ ᴄᴀᴘᴛɪᴏɴ ʟɪɴᴋ',
                    callback_data=f'settings_#updatefilter-remove_caption_link-{filter.get("remove_caption_link", False)}'),
       InlineKeyboardButton('✅' if filter.get('remove_caption_link', False) else '❌',
                    callback_data=f'settings#updatefilter-remove_caption_link-{filter.get("remove_caption_link", False)}')
       ],[
       InlineKeyboardButton('sɪᴢᴇ ʟɪᴍɪᴛ',
                    callback_data='settings#file_size')
       ],[
       InlineKeyboardButton('ᴇxᴛᴇɴsɪᴏɴ',
                    callback_data='settings#get_extension')
       ],[
       InlineKeyboardButton('ᴋᴇʏᴡᴏʀᴅs',
                    callback_data='settings#get_keyword')
       ],[
       InlineKeyboardButton('🎬 ᴍᴏᴠɪᴇ ᴡʜɪᴛᴇʟɪsᴛ',
                    callback_data='settings#get_movie')
       ],[
       InlineKeyboardButton('📺 sᴇʀɪᴇs ᴡʜɪᴛᴇʟɪsᴛ',
                    callback_data='settings#get_series')
       ],[
       InlineKeyboardButton('🗂 ᴀᴜᴛᴏsᴏʀᴛ',
                    callback_data='settings#asmenu')
       ],[
       InlineKeyboardButton('ʙᴀᴄᴋ', 
                    callback_data="settings#main")
       ]]
  return InlineKeyboardMarkup(buttons) 


# ======================================================================
# Started / Completed / Cancelled messages: the 🥀 links to the target channel
# ======================================================================

# target channel -> link of the 🥀 in the Started / Completed / Cancelled messages
_TARGET_LINKS = {}

async def target_link(client, to):
   """t.me link of the target channel: https://t.me/<username> for a public one,
   https://t.me/c/<id>/1 for a private one (opens for its members/admins).
   None when it can't be worked out - the message then just has a plain 🥀."""
   key = str(to)
   if key in _TARGET_LINKS:
      return _TARGET_LINKS[key]
   link = None
   try:
      chat = await client.get_chat(to)
      if chat.username:
         link = f"https://t.me/{chat.username}"
      elif str(chat.id).startswith("-100"):
         link = f"https://t.me/c/{str(chat.id)[4:]}/1"
   except Exception:
      t = key.strip()
      if t.startswith("-100") and t[4:].isdigit():
         link = f"https://t.me/c/{t[4:]}/1"
      elif t and not t.lstrip("-").isdigit():
         link = f"https://t.me/{t.lstrip('@')}"
   if link:
      _TARGET_LINKS[key] = link
   return link

def frwd_status_text(text, link):
   mark = f'<a href="{link}">🥀</a>' if link else "🥀"
   return f"<b>{text} {mark}</b>"
