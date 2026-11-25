import os
import re
import sys 
import math
import time
import asyncio 
import logging
from plugins.channels import STS, check_filters, clean_caption, _MESSAGE_TYPE_KEYS
from core.database import db 
from plugins.controls import CLIENT , start_clone_bot, target_link, frwd_status_text
from plugins.classify import matches_async as category_matches_async

try:
    import aiohttp
except ImportError:
    aiohttp = None
from core.config import Config, temp
from plugins.controls import Translation
from pyrogram import Client, filters 
#from pyropatch.utils import unpack_new_file_id
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message 

CLIENT = CLIENT()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
TEXT = Translation.TEXT
# /settings -> Filters (audio / video / photo ... toggles, extension, keyword, size) are applied to /fwd.
# Set the environment variable FWD_APPLY_FILTERS=0 to make /fwd forward everything again.
APPLY_FILTERS = os.environ.get("FWD_APPLY_FILTERS", "1") != "0"

_ANCHOR_RE = re.compile(r'<a\s+href="[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_LINK_TEXT_RE = re.compile(r'^(https?://|t\.me/|www\.)\S+$', re.I)

def strip_caption_links(html_text):
  """Remove links from a caption (HTML): a hidden link keeps its words
  ("<a href=..>Movie Name</a>" -> "Movie Name"); a link whose visible text is
  itself a URL is dropped completely."""
  def _repl(m):
    inner = m.group(1)
    plain = re.sub(r'<[^>]+>', '', inner).strip()
    return '' if _LINK_TEXT_RE.match(plain) else inner
  return _ANCHOR_RE.sub(_repl, html_text).strip()

@Client.on_callback_query(filters.regex(r'^start_public'))
async def pub_(bot, message):
    user = message.from_user.id
    frwd_id = message.data.split("_")[2]
    if temp.lock.get(user) and str(temp.lock.get(user))=="True":
      if temp.CANCEL.get(user):
         # the previous task was cancelled and is just finishing its current message
         await message.answer("Previous task is stopping, your new task starts in a moment...")
         for _ in range(150):
            await asyncio.sleep(1)
            if not temp.lock.get(user):
               break
      if temp.lock.get(user) and str(temp.lock.get(user))=="True":
         return await message.answer("please wait until previous task complete", show_alert=True)
    temp.CANCEL[user] = False
    sts = STS(frwd_id)
    if not sts.verify():
      await message.answer("your are clicking on my old button", show_alert=True)
      return await message.message.delete()
    i = sts.get(full=True)
    if i.TO in temp.IS_FRWD_CHAT:
      return await message.answer("In Target chat a task is progressing. please wait until task complete", show_alert=True)
    m = await msg_edit(message.message, "<code>verifying your data's, please wait.</code>")
    _bot, caption, forward_tag, data, protect, button = await sts.get_data(user)
    if not _bot:
      return await msg_edit(m, "<code>You didn't added any bot. Please add a bot using /settings !</code>", wait=True)
    try:
      client = await start_clone_bot(CLIENT.client(_bot))
    except Exception as e:  
      return await m.edit(f"<b>Failed to start your bot/userbot:</b>\n<code>{e}</code>")
    await msg_edit(m, "<code>processing..</code>")
    try: 
       # Just check if we can access messages. If continuous, limit might be huge.
       await client.get_messages(sts.get("FROM"), 1)
    except:
       await msg_edit(m, f"**Source chat may be a private channel / group. Use userbot (user must be member over there) or  if Make Your [Bot](t.me/{_bot['username']}) an admin over there**", retry_btn(frwd_id), True)
       return await stop(client, user)
    try:
       k = await client.send_message(i.TO, "Testing")
       await k.delete()
    except:
       await msg_edit(m, f"**Please Make Your [UserBot / Bot](t.me/{_bot['username']}) Admin In Target Channel With Full Permissions**", retry_btn(frwd_id), True)
       return await stop(client, user)
    temp.forwardings += 1
    await db.add_frwd(user)
    await send(client, user, frwd_status_text("Forwarding Started", await target_link(client, i.TO)))
    sts.add(time=True)
    # Save this job so that, if the bot restarts, it continues from the last forwarded message
    _old_job = await db.get_job(user)
    _last = await db.get_last_run(user)
    _skip_now = int(sts.get('skip') or 0)
    base = 0
    _prev_fwd = 0
    _continued_cancelled = False
    if _old_job and _old_job.get('resume_id') == frwd_id:
       base = int(_old_job.get('forwarded', 0))           # automatic resume after a restart
       _prev_fwd = int(_old_job.get('last_fwd_id') or 0)
    elif continues_last_run(_last, sts.get('FROM'), sts.get('TO'), _skip_now):
       base = int(_last.get('forwarded', 0))              # new task continues a cancelled one
       _prev_fwd = int(_last.get('last_fwd_id') or 0)
       _continued_cancelled = True
    await db.clear_last_run(user)
    if _continued_cancelled:
       await send(client, user, f"<b>♻️ ᴄᴏɴᴛɪɴᴜɪɴɢ ᴛʜᴇ ᴄᴀɴᴄᴇʟʟᴇᴅ ᴛᴀsᴋ - ᴀʟʀᴇᴀᴅʏ ꜰᴡᴅ :</b> <code>{base}</code>")
    elif _last and _skip_now > 0 and str(_last.get('FROM')) == str(sts.get('FROM')) and str(_last.get('TO')) == str(sts.get('TO')):
       _pos = max(int(_last.get('last_id') or 0), int(_last.get('fetched') or 0))
       await send(client, user, f"<b>ℹ️ ᴄᴏᴜɴᴛ sᴛᴀʀᴛs ꜰʀᴏᴍ 0 : ʏᴏᴜʀ sᴋɪᴘ ɴᴜᴍʙᴇʀ <code>{_skip_now}</code> ɪs ɴᴏᴛ ɴᴇᴀʀ ᴛʜᴇ ʟᴀsᴛ sᴛᴏᴘᴘᴇᴅ ᴍsɢ ɴᴏ <code>{_pos}</code></b>")
    if base:
       sts.data[frwd_id]['total_files'] = base            # status counts on from there, not from 0
    # which message types /settings -> Filters lets through (None = everything), shown after a restart
    _disabled_now = set(await db.get_filters(user)) if APPLY_FILTERS else set()
    _filter_types = [t for t in _MESSAGE_TYPE_KEYS if t not in _disabled_now] if _disabled_now else None
    await db.save_job(user, {'FROM': sts.get('FROM'), 'TO': sts.get('TO'), 'skip': sts.get('skip'),
                             'limit': sts.get('limit'), 'continuous': bool(sts.get('continuous')),
                             'category': sts.get('category') or 'all',
                             'last_id': int(sts.get('skip') or 0), 'forwarded': base,
                             'last_fwd_id': _prev_fwd, 'filter_types': _filter_types,
                             # the 'Forwarded Status' message, so a restart can edit it
                             'status_chat': message.message.chat.id, 'status_msg': message.message.id})
    sleep = 1 if _bot['is_bot'] else 10
    await msg_edit(m, "<code>Processing...</code>") 
    temp.IS_FRWD_CHAT.append(i.TO)
    temp.lock[user] = locked = True
    if locked:
        try:
          MSG = []
          pling=0
          await edit(m, 'Progressing', 10, sts)
          print(f"Starting Forwarding Process... From :{sts.get('FROM')} To: {sts.get('TO')} Totel: {sts.get('limit')} stats : {sts.get('skip')})")

          # Use getattr to safely check for 'continuous'/'category' attributes since old STS objects might not have them
          is_continuous = getattr(sts, 'continuous', False)
          wanted_category = getattr(sts, 'category', 'all')
          # Per-user TMDB key (see /settings -> TMDB, core/database.py)
          tmdb_key = await db.get_tmdb_key(user)
          use_anilist = await db.get_anilist_enabled(user)
          anilist_key = await db.get_anilist_key(user)
          openai_key = await db.get_openai_key(user)
          # Real /settings -> Filters enforcement (message type toggles,
          # extension exclude-list, keyword include-list, file size limit,
          # skip-duplicate) - see plugins/channels.py's check_filters().
          configs = await db.get_configs(user)
          disabled_types = set(await db.get_filters(user))
          seen_file_ids = set() if configs.get('duplicate') else None
          # One shared session for the whole run's TMDB lookups (classify.py),
          # instead of opening a new connection per file across 1000s of messages.
          tmdb_session = aiohttp.ClientSession() if aiohttp else None

          async for message in client.iter_messages(
            client,
            chat_id=sts.get('FROM'), 
            limit=int(sts.get('limit')), 
            offset=int(sts.get('skip')) if sts.get('skip') else 0,
            continuous=is_continuous
            ):
                await wait_if_paused(user, m, sts)
                if await is_cancelled(client, user, m, sts):
                   if tmdb_session:
                      await tmdb_session.close()
                   return
                if pling %20 == 0: 
                   await edit(m, 'Progressing', 10, sts)
                   if not forward_tag:
                      await db.update_job(user, message.id - 1, sts.get('total_files'))
                pling += 1
                sts.add('fetched')
                if message.empty or message.service:
                   sts.add('deleted')
                   continue
                # /fwd forwards EVERYTHING: /settings filters (duplicate, type,
                # extension, keyword, size, whitelist) are intentionally not applied here.
                if not await category_matches_async(message, wanted_category, tmdb_session, tmdb_key, use_anilist, anilist_key, openai_key):
                   sts.add('filtered')
                   continue
                if APPLY_FILTERS:
                   allowed, reason = check_filters(message, configs, disabled_types, seen_file_ids)
                   if not allowed:
                      sts.add(reason)          # 'filtered' or 'duplicate'
                      continue
                if forward_tag:
                   MSG.append(message.id)
                   notcompleted = len(MSG)
                   completed = sts.get('total') - sts.get('fetched')
                   if ( notcompleted >= 100 
                        or completed <= 100): 
                      ok = await forward(client, MSG, m, sts, protect)
                      if ok:
                         sts.add('total_files', notcompleted)
                      await db.update_job(user, max(MSG), sts.get('total_files'), last_fwd_id=max(MSG) if ok else None)
                      await asyncio.sleep(10)
                      MSG = []
                else:
                   new_caption = custom_caption(message, caption, bool(configs.get('remove_caption_link')))
                   details = {"msg_id": message.id, "media": media(message), "caption": new_caption, 'button': button, "protect": protect}
                   ok = await copy(client, details, m, sts)
                   if ok:                       # only a really forwarded media is counted
                      sts.add('total_files')
                   await db.update_job(user, message.id, sts.get('total_files'), last_fwd_id=message.id if ok else None)
                   await asyncio.sleep(sleep) 
          # flush the last (<100) batch of "forward with tag" messages
          if MSG:
             ok = await forward(client, MSG, m, sts, protect)
             if ok:
                sts.add('total_files', len(MSG))
             await db.update_job(user, max(MSG), sts.get('total_files'), last_fwd_id=max(MSG) if ok else None)
             MSG = []
        except Exception as e:
            if tmdb_session:
                await tmdb_session.close()
            await msg_edit(m, f'<b>ERROR:</b>\n<code>{e}</code>', wait=True)
            temp.IS_FRWD_CHAT.remove(sts.TO)
            await remember_run(user)
            return await stop(client, user)
        if tmdb_session:
            await tmdb_session.close()
        temp.IS_FRWD_CHAT.remove(sts.TO)
        await send(client, user, frwd_status_text("Forwarding Completed", await target_link(client, sts.TO)))
        await edit(m, 'Completed', "completed", sts) 
        await stop(client, user)
            
async def copy(bot, msg, m, sts):
   try:                                  
     if msg.get("media") and msg.get("caption"):
        await bot.send_cached_media(
              chat_id=sts.get('TO'),
              file_id=msg.get("media"),
              caption=msg.get("caption"),
              reply_markup=msg.get('button'),
              protect_content=msg.get("protect"))
     else:
        await bot.copy_message(
              chat_id=sts.get('TO'),
              from_chat_id=sts.get('FROM'),    
              caption=msg.get("caption"),
              message_id=msg.get("msg_id"),
              reply_markup=msg.get('button'),
              protect_content=msg.get("protect"))
     return True
   except FloodWait as e:
     await edit(m, 'Progressing', e.value, sts)
     await asyncio.sleep(e.value)
     await edit(m, 'Progressing', 10, sts)
     return await copy(bot, msg, m, sts)
   except Exception as e:
     # Improved error logging to debug "Not Forwarding" issues
     print(f"Failed to copy message {msg.get('msg_id')}: {e}")
     sts.add('deleted')
     return False
        
async def forward(bot, msg, m, sts, protect):
   try:                             
     await bot.forward_messages(
           chat_id=sts.get('TO'),
           from_chat_id=sts.get('FROM'), 
           protect_content=protect,
           message_ids=msg)
     return True
   except FloodWait as e:
     await edit(m, 'Progressing', e.value, sts)
     await asyncio.sleep(e.value)
     await edit(m, 'Progressing', 10, sts)
     return await forward(bot, msg, m, sts, protect)
   except Exception as e:
      print(f"Failed to forward messages {msg}: {e}")
      sts.add('deleted', len(msg))
      return False

PROGRESS = """
📈 Percetage: {0} %

♻️ Feched: {1}

♻️ Fowarded: {2}

♻️ Remaining: {3}

♻️ Stataus: {4}

⏳️ ETA: {5}
"""

async def msg_edit(msg, text, button=None, wait=None):
    try:
        return await msg.edit(text, reply_markup=button)
    except MessageNotModified:
        pass 
    except FloodWait as e:
        if wait:
           await asyncio.sleep(e.value)
           return await msg_edit(msg, text, button, wait)
        
async def edit(msg, title, status, sts):
   i = sts.get(full=True)
   status = 'Forwarding' if status == 10 else f"Sleeping {status} s" if str(status).isnumeric() else status
   finished = status in ["cancelled", "completed"]
   # Handle division by zero if total is 0 (which happens if infinite/continuous without known total)
   total = float(i.total) if float(i.total) > 0 else 1.0
   fetched = float(i.fetched)
   if status == "completed" and title != "Cancelled":
      pct = 100.0
   else:
      pct = min(max(fetched * 100 / total, 0.0), 100.0)
   # one decimal, so a big channel does not sit at "0 %" for hours
   percentage = "{:.1f}".format(pct)

   now = time.time()
   diff = max(int(now - i.start), 1)
   done_now = max(fetched - float(i.skip or 0), 0.0)   # messages handled in THIS run (skip/resume start excluded)
   speed = done_now / diff                              # messages per second (float, can be < 1)
   remaining = max(total - fetched, 0.0)
   eta_ms = 0 if (finished or speed <= 0) else int(min(remaining / speed, 999999) * 1000)
   progress = "◉{0}{1}".format(
       ''.join(["◉" for _ in range(math.floor(pct / 10))]),
       ''.join(["◎" for _ in range(10 - math.floor(pct / 10))]))
   # keep callback_data short (Telegram limit: 64 bytes) - a long "paused ... left" text is only for display
   cb_status = 'paused' if str(status).startswith('paused') else status
   button =  [[InlineKeyboardButton(title, f'fwrdstatus#{cb_status}#{eta_ms}#{percentage}#{i.id}')]]
   estimated_total_time = TimeFormatter(milliseconds=eta_ms)
   estimated_total_time = estimated_total_time if estimated_total_time != '' else '0 s'

   text = TEXT.format(i.fetched, i.total_files, i.duplicate, i.deleted, i.skip, status, percentage, estimated_total_time, progress)
   if not finished:
      button.extend(control_rows())
      try:
         # saved so that, after a bot restart, this same status can be shown with "Bot Restarted"
         await db.update_job_status(msg.chat.id, {
            'fetched': i.fetched, 'total_files': i.total_files, 'duplicate': i.duplicate,
            'deleted': i.deleted, 'skip': i.skip, 'filtered': i.filtered,
            'percentage': percentage, 'progress': progress})
      except Exception:
         pass
   await msg_edit(msg, text, InlineKeyboardMarkup(button))
   
# ============================================================
# Pause mode: the Pause button on the status message pauses the
# running /fwd for a chosen time (5 min ... 2 hours). It continues
# by itself when the time is over, or earlier with the Resume button.
# Cancel still works while paused.
# ============================================================
# user_id -> time (epoch seconds) until which forwarding is paused (float("inf") = until Resume is pressed)
PAUSE = {}
PAUSE_OPTIONS = [(300, "5 min"), (900, "15 min"), (1800, "30 min"), (3600, "1 hour"), (7200, "2 hours"), (0, "Until Resume")]

def is_paused(user):
    return time.time() < PAUSE.get(user, 0)

def control_rows():
    """Pause and Resume are always shown together, Cancel below them."""
    return [[InlineKeyboardButton('⏸ ᴘᴀᴜsᴇ', 'pause_frwd'), InlineKeyboardButton('▶️ ʀᴇsᴜᴍᴇ', 'resume_frwd')],
            [InlineKeyboardButton('• ᴄᴀɴᴄᴇʟ', 'terminate_frwd')]]

async def wait_if_paused(user, m, sts):
    """Called before every message: blocks while the user's task is paused."""
    if not is_paused(user):
        PAUSE.pop(user, None)
        return
    started = time.time()
    last_shown = 0
    while is_paused(user) and not temp.CANCEL.get(user):
        now = time.time()
        if now - last_shown >= 15:
            if PAUSE[user] == float('inf'):
                shown = "paused ⏸ (press Resume)"
            else:
                shown = f"paused ⏸ {TimeFormatter(milliseconds=int(max(PAUSE[user] - now, 0)) * 1000)} left"
            await edit(m, 'Paused', shown, sts)
            last_shown = now
        await asyncio.sleep(1)
    PAUSE.pop(user, None)
    # the paused time must not count as forwarding time (speed / ETA)
    sts.data[sts.id]['start'] += time.time() - started
    if not temp.CANCEL.get(user):
        await edit(m, 'Progressing', 10, sts)

async def set_buttons(query, paused, menu=None):
    """Change only the buttons of the status message (first row = status button)."""
    rows = list(query.message.reply_markup.inline_keyboard[:1])
    rows += menu if menu else control_rows()
    try:
        await query.message.edit_reply_markup(InlineKeyboardMarkup(rows))
    except MessageNotModified:
        pass
    except FloodWait:
        pass

def task_running(user):
    return str(temp.lock.get(user)) == "True" and not temp.CANCEL.get(user)

@Client.on_callback_query(filters.regex(r'^pause_frwd$'))
async def pause_menu(bot, q):
    if not task_running(q.from_user.id):
        return await q.answer("No forwarding task is running.", show_alert=True)
    btns = [InlineKeyboardButton(label, f"pause_set_{secs}") for secs, label in PAUSE_OPTIONS]
    await q.answer("Choose how long to pause")
    await set_buttons(q, False, menu=[btns[:3], btns[3:5], [btns[5]], [InlineKeyboardButton('⬅️ ʙᴀᴄᴋ', 'pause_back')]])

@Client.on_callback_query(filters.regex(r'^pause_back$'))
async def pause_back(bot, q):
    await q.answer()
    await set_buttons(q, is_paused(q.from_user.id))

@Client.on_callback_query(filters.regex(r'^pause_set_(\d+)$'))
async def pause_set(bot, q):
    user = q.from_user.id
    if not task_running(user):
        return await q.answer("No forwarding task is running.", show_alert=True)
    secs = int(q.matches[0].group(1))
    if secs not in dict(PAUSE_OPTIONS):
        return await q.answer()
    PAUSE[user] = time.time() + secs if secs else float('inf')   # 0 = until the Resume button is pressed
    await q.answer(f"⏸ Forwarding paused ({dict(PAUSE_OPTIONS)[secs]}). Press Resume to continue.", show_alert=True)
    await set_buttons(q, True)

@Client.on_callback_query(filters.regex(r'^resume_frwd$'))
async def resume_frwd(bot, q):
    user = q.from_user.id
    if not is_paused(user):
        await q.answer("Forwarding is not paused.")
        return await set_buttons(q, False)
    PAUSE.pop(user, None)
    await q.answer("▶️ Forwarding resumed")
    await set_buttons(q, False)

async def is_cancelled(client, user, msg, sts):
   if temp.CANCEL.get(user)==True:
      temp.IS_FRWD_CHAT.remove(sts.TO)
      await edit(msg, "Cancelled", "cancelled", sts)
      await send(client, user, frwd_status_text("Forwarding Process Cancelled", await target_link(client, sts.TO)))
      await remember_run(user)
      await stop(client, user)
      return True 
   return False 

async def remember_run(user):
   # keep the position of a cancelled/failed run, so the next task can continue from it
   job = await db.get_job(user)
   if job:
      snap = job.get('snap') or {}
      await db.save_last_run(user, {'FROM': job.get('FROM'), 'TO': job.get('TO'),
                                    'last_id': job.get('last_id', 0), 'forwarded': job.get('forwarded', 0),
                                    'start_skip': int(job.get('skip') or 0),      # where that run started
                                    'fetched': int(snap.get('fetched') or 0),     # last position shown on its status
                                    'last_fwd_id': job.get('last_fwd_id', 0)})

def continues_last_run(last, from_chat, to_chat, skip_now, tolerance=100):
   """True when a new /fwd (skip number = skip_now) carries on a cancelled/failed run, so its
   'successfully fwd' count keeps counting from the old total instead of 0.
   The skip number only has to be near where the old run stopped (the user usually types the
   'Fetched' number of the cancelled status, which can differ from the saved position by a few)."""
   if not last or int(skip_now or 0) <= 0:
      return False
   if str(last.get('FROM')) != str(from_chat) or str(last.get('TO')) != str(to_chat):
      return False
   pos = max(int(last.get('last_id') or 0), int(last.get('fetched') or 0))
   start = int(last.get('start_skip') or 0)
   return start <= int(skip_now) <= pos + tolerance

async def stop(client, user):
   try:
     await client.stop()
   except:
     pass 
   await db.rmve_frwd(user)
   await db.remove_job(user)
   temp.forwardings -= 1
   PAUSE.pop(user, None)
   temp.lock[user] = False 
    
async def send(bot, user, text):
   try:
      await bot.send_message(user, text=text)
   except:
      pass 
     
def custom_caption(msg, caption, remove_links=False):
  if msg.media:
    if (msg.video or msg.document or msg.audio or msg.photo):
      media = getattr(msg, msg.media.value, None)
      if media:
        file_name = getattr(media, 'file_name', '')
        file_size = getattr(media, 'file_size', '')
        fcaption = getattr(msg, 'caption', '')
        if fcaption:
          # keep only the media/movie name line; see utils.clean_caption
          fcaption = clean_caption(msg, True) or fcaption.html
          if remove_links:
            fcaption = strip_caption_links(fcaption)
        if caption:
          return caption.format(filename=file_name, size=get_size(file_size), caption=fcaption)
        return fcaption
  return None

def get_size(size):
  units = ["Bytes", "KB", "MB", "GB", "TB", "PB", "EB"]
  size = float(size)
  i = 0
  while size >= 1024.0 and i < len(units):
     i += 1
     size /= 1024.0
  return "%.2f %s" % (size, units[i]) 

def media(msg):
  if msg.media:
     media = getattr(msg, msg.media.value, None)
     if media:
        return getattr(media, 'file_id', None)
  return None 

def TimeFormatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d, ") if days else "") + \
        ((str(hours) + "h, ") if hours else "") + \
        ((str(minutes) + "m, ") if minutes else "") + \
        ((str(seconds) + "s, ") if seconds else "") + \
        ((str(milliseconds) + "ms, ") if milliseconds else "")
    return tmp[:-2]

def retry_btn(id):
    return InlineKeyboardMarkup([[InlineKeyboardButton('♻️ RETRY ♻️', f"start_public_{id}")]])

@Client.on_callback_query(filters.regex(r'^terminate_frwd$'))
async def terminate_frwding(bot, m):
    user_id = m.from_user.id 
    temp.CANCEL[user_id] = True 
    await m.answer("Forwarding cancelled !", show_alert=True)
          
@Client.on_callback_query(filters.regex(r'^fwrdstatus'))
async def status_msg(bot, msg):
    _, status, est_time, percentage, frwd_id = msg.data.split("#")
    sts = STS(frwd_id)
    if not sts.verify():
       fetched = forwarded = remaining = 0
    else:
       fetched, forwarded = sts.get('fetched'), sts.get('total_files')
       remaining = fetched - forwarded 
    est_time = TimeFormatter(milliseconds=est_time)
    est_time = est_time if (est_time != '' or status not in ['completed', 'cancelled']) else '0 s'
    return await msg.answer(PROGRESS.format(percentage, fetched, forwarded, remaining, status, est_time), show_alert=True)
                  
@Client.on_callback_query(filters.regex(r'^close_btn$'))
async def close(bot, update):
    await update.answer()
    await update.message.delete()
