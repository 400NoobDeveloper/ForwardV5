"""handlers/settings.py - all the commands: /fwd /forward /settings /unequify /diagnose_media /reset /resetall.

The helper code lives in plugins/controls.py.
"""
import asyncio
import re
from pyrogram.errors.exceptions.bad_request_400 import ChannelInvalid, ChannelPrivate, UsernameInvalid, UsernameNotModified
from pyrogram import Client, enums, filters
from core.config import Config, temp
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from pyrogram.errors.exceptions.not_acceptable_406 import ChannelPrivate as PrivateChat
from plugins.utils import STS
from core.database import db
from handlers import botsettings as _bs          # shared Add Me To Group state
from plugins import autosort_create                 # /settings -> Extra Settings -> AUTOSORT
try:
    from pyromod.listen.listen import ListenerCanceled
except Exception:                                 # pyromod missing: never raised
    class ListenerCanceled(Exception):
        pass
from plugins.controls import (
    AUTOSORT_CATEGORIES,
    CANCEL_BTN,
    CLIENT_HELPER,
    COMPLETED_BTN,
    _dump_attrs,
    extract_btn,
    filters_buttons,
    _fmt_anilist,
    get_configs,
    main_buttons,
    next_filters_buttons,
    parse_buttons,
    SETTINGS_TXT,
    size_button,
    size_limit,
    start_clone_bot,
    _test_anilist,
    Translation,
    update_configs,
    _verify_anilist_key,
    _verify_openai_key,
    _verify_tmdb_key,
    _verify_tpdb_key,
    WHITELIST_KINDS,
    whitelist_menu,
    whitelist_text,
)


    
@Client.on_message(filters.private & filters.command('reset'))
async def forward_tag(bot, m):
   default = await db.get_configs("01")
   await db.update_configs(m.from_user.id, default)
   await m.reply("successfully settings reseted ✔️")


@Client.on_message(filters.command('resetall') & filters.user(Config.BOT_OWNER_ID))
async def resetall(bot, message):
  users = await db.get_all_users()
  sts = await message.reply("**processing**")
  TEXT = "total: {}\nsuccess: {}\nfailed: {}\nexcept: {}"
  total = success = failed = already = 0
  ERRORS = []
  async for user in users:
      user_id = user['id']
      default = await get_configs(user_id)
      default['db_uri'] = None
      total += 1
      if total %10 == 0:
         await sts.edit(TEXT.format(total, success, failed, already))
      try: 
         await db.update_configs(user_id, default)
         success += 1
      except Exception as e:
         ERRORS.append(e)
         failed += 1
  if ERRORS:
     await message.reply(ERRORS[:100])
  await sts.edit("completed\n" + TEXT.format(total, success, failed, already))


@Client.on_message(filters.private & filters.command("diagnose_media") & filters.reply)
async def diagnose_media(bot: Client, message):
    target = message.reply_to_message
    if not target:
        return await message.reply_text("Reply to a message (the one with the media you want checked) with this command.")

    lines = ["<b>Media diagnosis</b>\n"]

    lines.append(f"message.empty: {target.empty}")
    lines.append(f"message.service: {bool(target.service)}")
    lines.append(f"message.media: {target.media}")
    lines.append("")

    has_video = getattr(target, "video", None) is not None
    has_document = getattr(target, "document", None) is not None
    lines.append(f"message.video is not None: {has_video}")
    lines.append(f"message.document is not None: {has_document}")
    lines.append("")

    if has_video:
        lines.append("<b>message.video fields:</b>")
        lines.append(_dump_attrs(target.video, [
            "file_name", "mime_type", "duration", "width", "height",
            "file_size", "file_unique_id", "supports_streaming", "thumbs",
        ]))
        lines.append("")

    if has_document:
        lines.append("<b>message.document fields:</b>")
        lines.append(_dump_attrs(target.document, [
            "file_name", "mime_type", "file_size", "file_unique_id", "thumbs",
        ]))
        lines.append("")

    for attr in ("photo", "audio", "animation", "voice", "sticker"):
        media = getattr(target, attr, None)
        if media is not None:
            lines.append(f"message.{attr} is also set (unexpected alongside the above)")

    await message.reply_text("\n".join(lines))


 
#===================Run Function===================#

@Client.on_message(filters.private & filters.command(["fwd", "forward"]))
async def run(bot, message):
    buttons = []
    btn_data = {}
    user_id = message.from_user.id
    _bot = await db.get_bot(user_id)
    if not _bot:
      return await message.reply("<code>You didn't added any bot. Please add a bot using /settings !</code>")
    channels = await db.get_user_channels(user_id)
    if not channels:
       return await message.reply_text("please set a to channel in /settings before forwarding")
    if len(channels) > 1:
       for channel in channels:
          buttons.append([KeyboardButton(f"{channel['title']}")])
          btn_data[channel['title']] = channel['chat_id']
       buttons.append([KeyboardButton("cancel")]) 
       _toid = await bot.ask(message.chat.id, Translation.TO_MSG.format(_bot['name'], _bot['username']), reply_markup=ReplyKeyboardMarkup(buttons, one_time_keyboard=True, resize_keyboard=True))
       if _toid.text.startswith(('/', 'cancel')):
          return await message.reply_text(Translation.CANCEL, reply_markup=ReplyKeyboardRemove())
       to_title = _toid.text
       toid = btn_data.get(to_title)
       if not toid:
          return await message.reply_text("wrong channel choosen !", reply_markup=ReplyKeyboardRemove())
    else:
       toid = channels[0]['chat_id']
       to_title = channels[0]['title']
    fromid = await bot.ask(message.chat.id, Translation.FROM_MSG, reply_markup=ReplyKeyboardRemove())
    if fromid.text and fromid.text.startswith('/'):
        await message.reply(Translation.CANCEL)
        return 

    continuous = False

    # Handle "Saved Messages" input
    if fromid.text and fromid.text.lower() in ["me", "saved"]:
        if _bot.get('is_bot'):
            return await message.reply("<b>You cannot forward from Saved Messages using a Bot. Please add a Userbot session via /settings to use this feature.</b>")

        chat_id = "me"
        title = "Saved Messages"

        # Ask for mode: Batch vs Live
        mode_msg = await bot.ask(message.chat.id, Translation.SAVED_MSG_MODE)
        if mode_msg.text.startswith('/'):
             await message.reply(Translation.CANCEL)
             return

        if "live" in mode_msg.text.lower() or "2" in mode_msg.text:
            continuous = True
            last_msg_id = 1000000 # Just a high number, but loop will be infinite/wait
        else:
            limit_msg = await bot.ask(message.chat.id, Translation.SAVED_MSG_LIMIT)
            if limit_msg.text.startswith('/'):
                 await message.reply(Translation.CANCEL)
                 return

            if limit_msg.text.lower() == "all":
                 last_msg_id = 0 # 0 usually means no limit in some contexts, but let's use a very high number if iter logic relies on it?
                 # iter_messages: if limit > 0, it iterates until limit.
                 # If we want ALL, we should use a high number or verify how 0 is handled.
                 # In test.py: new_diff = min(200, limit - current).
                 # If limit is 0, 0-0 = 0. new_diff <= 0. Return.
                 # So we need a high number.
                 last_msg_id = 10000000
            elif not limit_msg.text.isdigit():
                 await message.reply("Invalid number.")
                 return
            else:
                 last_msg_id = int(limit_msg.text) # Using last_msg_id as limit/count

    elif fromid.text and not fromid.forward_date:
        regex = re.compile(r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")
        match = regex.match(fromid.text.replace("?single", ""))
        if not match:
            return await message.reply('Invalid link')
        chat_id = match.group(4)
        last_msg_id = int(match.group(5))
        if chat_id.isnumeric():
            chat_id  = int(("-100" + chat_id))
    elif fromid.forward_from_chat.type in [enums.ChatType.CHANNEL]:
        last_msg_id = fromid.forward_from_message_id
        chat_id = fromid.forward_from_chat.username or fromid.forward_from_chat.id
        if last_msg_id == None:
           return await message.reply_text("**This may be a forwarded message from a group and sended by anonymous admin. instead of this please send last message link from group**")
    else:
        await message.reply_text("**invalid !**")
        return 

    if chat_id != "me":
        try:
            title = (await bot.get_chat(chat_id)).title
      #  except ChannelInvalid:
            #return await fromid.reply("**Given source chat is copyrighted channel/group. you can't forward messages from there**")
        except (PrivateChat, ChannelPrivate, ChannelInvalid):
            title = "private" if fromid.text else fromid.forward_from_chat.title
        except (UsernameInvalid, UsernameNotModified):
            return await message.reply('Invalid Link specified.')
        except Exception as e:
            return await message.reply(f'Errors - {e}')

    skipno = await bot.ask(message.chat.id, Translation.SKIP_MSG)
    if skipno.text.startswith('/'):
        await message.reply(Translation.CANCEL)
        return

    # /fwd always forwards everything (no category question anymore)
    category = "all"

    forward_id = f"{user_id}-{skipno.id}"
    buttons = [[
        InlineKeyboardButton('Yes', callback_data=f"start_public_{forward_id}"),
        InlineKeyboardButton('No', callback_data="close_btn")
    ]]
    reply_markup = InlineKeyboardMarkup(buttons)
    await message.reply_text(
        text=Translation.DOUBLE_CHECK.format(botname=_bot['name'], botuname=_bot['username'], from_chat=title, to_chat=to_title, skip=skipno.text, category=category),
        disable_web_page_preview=True,
        reply_markup=reply_markup
    )
    STS(forward_id).store(chat_id, toid, int(skipno.text), int(last_msg_id), continuous=continuous, category=category)


@Client.on_message(filters.command("unequify") & filters.private)
async def unequify(client, message):
   user_id = message.from_user.id
   temp.CANCEL[user_id] = False
   if temp.lock.get(user_id) and str(temp.lock.get(user_id))=="True":
      return await message.reply("**please wait until previous task complete**")
   _bot = await db.get_bot(user_id)
   if not _bot or _bot['is_bot']:
      return await message.reply("<b>Need userbot to do this process. Please add a userbot using /settings</b>")
   target = await client.ask(user_id, text="**Forward the last message from target chat or send last message link.**\n/cancel - `cancel this process`")
   if target.text.startswith("/"):
      return await message.reply("**process cancelled !**")
   elif target.text:
      regex = re.compile(r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")
      match = regex.match(target.text.replace("?single", ""))
      if not match:
         return await message.reply('**Invalid link**')
      chat_id = match.group(4)
      last_msg_id = int(match.group(5))
      if chat_id.isnumeric():
         chat_id  = int(("-100" + chat_id))
   elif fromid.forward_from_chat.type in ['channel', 'supergroup']:
        last_msg_id = target.forward_from_message_id
        chat_id = target.forward_from_chat.username or target.forward_from_chat.id
   else:
        return await message.reply_text("**invalid !**")
   confirm = await client.ask(user_id, text="**send /yes to start the process and /no to cancel this process**")
   if confirm.text.lower() == '/no':
      return await confirm.reply("**process cancelled !**")
   sts = await confirm.reply("`processing..`")
   try:
      bot = await start_clone_bot(CLIENT_HELPER.client(_bot))
   except Exception as e:
      return await sts.edit(e)
   try:
       k = await bot.send_message(chat_id, text="testing")
       await k.delete()
   except:
       await sts.edit(f"**please make your [userbot](t.me/{_bot['username']}) admin in target chat with full permissions**")
       return await bot.stop()
   MESSAGES = []
   DUPLICATE = []
   total=deleted=0
   temp.lock[user_id] = True
   try:
     await sts.edit(Translation.DUPLICATE_TEXT.format(total, deleted, "ᴘʀᴏɢʀᴇssɪɴɢ"), reply_markup=CANCEL_BTN)
     async for message in bot.search_messages(chat_id=chat_id, filter="document"):
        if temp.CANCEL.get(user_id) == True:
           await sts.edit(Translation.DUPLICATE_TEXT.format(total, deleted, "ᴄᴀɴᴄᴇʟʟᴇᴅ"), reply_markup=COMPLETED_BTN)
           return await bot.stop()
        file = message.document
        file_id = unpack_new_file_id(file.file_id) 
        if file_id in MESSAGES:
           DUPLICATE.append(message.id)
        else:
           MESSAGES.append(file_id)
        total += 1
        if total %10000 == 0:
           await sts.edit(Translation.DUPLICATE_TEXT.format(total, deleted, "ᴘʀᴏɢʀᴇssɪɴɢ"), reply_markup=CANCEL_BTN)
        if len(DUPLICATE) >= 100:
           await bot.delete_messages(chat_id, DUPLICATE)
           deleted += 100
           await sts.edit(Translation.DUPLICATE_TEXT.format(total, deleted, "ᴘʀᴏɢʀᴇssɪɴɢ"), reply_markup=CANCEL_BTN)
           DUPLICATE = []
     if DUPLICATE:
        await bot.delete_messages(chat_id, DUPLICATE)
        deleted += len(DUPLICATE)
   except Exception as e:
       temp.lock[user_id] = False 
       await sts.edit(f"**ERROR**\n`{e}`")
       return await bot.stop()
   temp.lock[user_id] = False
   await sts.edit(Translation.DUPLICATE_TEXT.format(total, deleted, "ᴄᴏᴍᴘʟᴇᴛᴇᴅ"), reply_markup=COMPLETED_BTN)
   await bot.stop()


@Client.on_message(filters.command('settings'))
async def settings(client, message):
   await message.delete()
   await message.reply_text(
     SETTINGS_TXT,
     reply_markup=main_buttons()
     )


    
async def _autosort_prompt_markup(bot, user_id, category):
  """Auto-Sort 'SET' prompt buttons: Add Me To Group (-> Verify once the bot was added) + Back."""
  if user_id in _bs.ADDED_GROUPS:
     first = InlineKeyboardButton('ᴠᴇʀɪꜰʏ', callback_data=f"settings#verifyautosort_{category}")
  else:
     me = getattr(bot, "me", None) or await bot.get_me()
     first = InlineKeyboardButton('ᴀᴅᴅ ᴍᴇ ᴛᴏ ɢʀᴏᴜᴘ', url=f"https://t.me/{me.username}?startgroup=true")
  return InlineKeyboardMarkup([[first], [InlineKeyboardButton('ʙᴀᴄᴋ', callback_data=f"settings#editautosort_{category}")]])


@Client.on_callback_query(filters.regex(r'^settings'))
async def settings_query(bot, query):
  user_id = query.from_user.id
  i, type = query.data.split("#")
  if not type.startswith("verifyautosort"):
     _bs.GROUP_PROMPTS.pop(user_id, None)      # leaving the Auto-Sort prompt screen
  buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#main")]]
  
  if type=="main":
     await query.message.edit_text(
       SETTINGS_TXT,
       reply_markup=main_buttons())
       
  elif type=="bots":
     buttons = [] 
     _bot = await db.get_bot(user_id)
     if _bot is not None:
        buttons.append([InlineKeyboardButton(_bot['name'],
                         callback_data=f"settings#editbot")])
     else:
        buttons.append([InlineKeyboardButton('ᴀᴅᴅ ʙᴏᴛ', 
                         callback_data="settings#addbot")])
        buttons.append([InlineKeyboardButton('ᴀᴅᴅ ᴜsᴇʀ ʙᴏᴛ', 
                         callback_data="settings#adduserbot")])
     buttons.append([InlineKeyboardButton('ʙᴀᴄᴋ', 
                      callback_data="settings#main")])
     await query.message.edit_text(
       "<b><u>My Bots</b></u>\n\n<b>You can manage your bots in here</b>",
       reply_markup=InlineKeyboardMarkup(buttons))
  
  elif type=="addbot":
     await query.message.delete()
     bot = await CLIENT_HELPER.add_bot(bot, query)
     if bot != True: return
     await query.message.reply_text(
        "<b>bot token successfully added to db</b>",
        reply_markup=InlineKeyboardMarkup(buttons))
  
  elif type=="adduserbot":
     await query.message.delete()
     user = await CLIENT_HELPER.add_session(bot, query)
     if user != True: return
     await query.message.reply_text(
        "<b>session successfully added to db</b>",
        reply_markup=InlineKeyboardMarkup(buttons))
      
  elif type=="channels":
     buttons = []
     channels = await db.get_user_channels(user_id)
     for channel in channels:
        buttons.append([InlineKeyboardButton(f"{channel['title']}",
                         callback_data=f"settings#editchannels_{channel['chat_id']}")])
     buttons.append([InlineKeyboardButton('ᴀᴅᴅ ᴄʜᴀɴɴᴇʟ', 
                      callback_data="settings#addchannel")])
     buttons.append([InlineKeyboardButton('ᴀᴜᴛᴏ-sᴏʀᴛ ᴄʜᴀɴɴᴇʟs',
                      callback_data="settings#autosortchannels")])
     buttons.append([InlineKeyboardButton('ʙᴀᴄᴋ', 
                      callback_data="settings#main")])
     await query.message.edit_text( 
       "<b><u>My Channels</b></u>\n\n<b>you can manage your target chats in here</b>",
       reply_markup=InlineKeyboardMarkup(buttons))

  elif type=="autosortchannels":
     cat_channels = await db.get_category_channels(user_id)
     buttons = []
     for key, label in AUTOSORT_CATEGORIES:
        value = cat_channels.get(key)
        status = f"{value}" if value else "not set"
        buttons.append([InlineKeyboardButton(f"{label}: {status}",
                         callback_data=f"settings#editautosort_{key}")])
     buttons.append([InlineKeyboardButton('ʙᴀᴄᴋ',
                      callback_data="settings#channels")])
     await query.message.edit_text(
       "<b><u>Auto-Sort Channels</b></u>\n\n"
       "<b>Point each category at a different channel. During /autosort, every file "
       "is classified then copied straight to its matching channel — categories left "
       "\"not set\" are skipped.</b>",
       reply_markup=InlineKeyboardMarkup(buttons))

  elif type.startswith("editautosort"):
     category = type.split('_', 1)[1]
     label = dict(AUTOSORT_CATEGORIES).get(category, category)
     cat_channels = await db.get_category_channels(user_id)
     value = cat_channels.get(category, "not set")
     buttons = [[InlineKeyboardButton('sᴇᴛ',
                  callback_data=f"settings#setautosort_{category}")
               ],[
               InlineKeyboardButton('ʀᴇsᴇᴛ',
                  callback_data=f"settings#resetautosort_{category}")
               ],[
               InlineKeyboardButton('ʙᴀᴄᴋ',
                  callback_data="settings#autosortchannels")]]
     await query.message.edit_text(
        f"<b><u>{label}</b></u>\n\nCurrent value: <code>{value}</code>",
        reply_markup=InlineKeyboardMarkup(buttons))

  elif type.startswith("setautosort"):
     category = type.split('_', 1)[1]
     label = dict(AUTOSORT_CATEGORIES).get(category, category)
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data=f"settings#editautosort_{category}")]]
     await query.message.delete()
     _bs.ADDED_GROUPS.pop(user_id, None)         # fresh prompt: start from Add Me To Group
     show_verify = None
     try:
         text = await bot.send_message(
            user_id,
            f"<b>❪ SET {label.upper()} ❫</b>\n\n"
            "<b>Forward a message from the target channel, or send its numeric ID "
            "(e.g. <code>-1001234567890</code>).</b>\n"
            "/cancel - cancel this process",
            reply_markup=await _autosort_prompt_markup(bot, user_id, category)
         )

         async def show_verify(client, _m=text, _c=category, _u=user_id):
            # bot was added to a group -> swap Add Me To Group for Verify
            await client.edit_message_reply_markup(
               _m.chat.id, _m.id, await _autosort_prompt_markup(client, _u, _c))
         _bs.GROUP_PROMPTS[user_id] = show_verify
         reply = await bot.listen(chat_id=user_id, timeout=300)
         if reply.text == "/cancel":
            await reply.delete()
            return await text.edit_text("<b>process canceled</b>", reply_markup=InlineKeyboardMarkup(back_buttons))
         elif reply.forward_date and reply.forward_from_chat:
            chat_id = reply.forward_from_chat.id
         elif reply.text and reply.text.strip().lstrip('-').isdigit():
            chat_id = int(reply.text.strip())
         else:
            await reply.delete()
            return await text.edit_text(
               "<b>Invalid. Forward a message from the channel, or send its numeric ID.</b>",
               reply_markup=InlineKeyboardMarkup(back_buttons))
         await db.set_category_channel(user_id, category, chat_id)
         await reply.delete()
         await text.edit_text("<b>Successfully updated</b>", reply_markup=InlineKeyboardMarkup(back_buttons))
     except asyncio.exceptions.TimeoutError:
         await text.edit_text('Process has been automatically cancelled', reply_markup=InlineKeyboardMarkup(back_buttons))
     except ListenerCanceled:                     # Verify already saved the channel
         return
     finally:
         if show_verify and _bs.GROUP_PROMPTS.get(user_id) is show_verify:
            _bs.GROUP_PROMPTS.pop(user_id, None)

  elif type.startswith("verifyautosort"):
     category = type.split('_', 1)[1]
     if category not in dict(AUTOSORT_CATEGORIES):
        return await query.answer()
     grp = _bs.ADDED_GROUPS.get(user_id)
     if not grp or not await _bs._bot_in_group(bot, grp["id"]):
        _bs.ADDED_GROUPS.pop(user_id, None)
        try:
           await query.message.edit_reply_markup(await _autosort_prompt_markup(bot, user_id, category))
        except MessageNotModified:
           pass
        return await query.answer("Bot is not in a group. Tap Add Me To Group and add it again.", show_alert=True)
     await db.set_category_channel(user_id, category, grp["id"])      # verified -> group becomes the channel
     _bs.ADDED_GROUPS.pop(user_id, None)
     _bs.GROUP_PROMPTS.pop(user_id, None)
     bot.cancel_listener(user_id)                 # stop the "send the channel" listener
     await query.message.edit_text(
        "<b>Successfully updated</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data=f"settings#editautosort_{category}")]]))
     return await query.answer("Verified.")        # top toast

  elif type.startswith("resetautosort"):
     category = type.split('_', 1)[1]
     await db.remove_category_channel(user_id, category)
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#autosortchannels")]]
     await query.message.edit_text(
        "<b>successfully updated</b>",
        reply_markup=InlineKeyboardMarkup(back_buttons))

  # ── Extra Settings -> AUTOSORT (logic lives in plugins/autosort_create.py) ──
  elif type=="asmenu":
     autosort_create.PENDING.pop(user_id, None)
     text, markup = await autosort_create.menu_page(user_id)
     await autosort_create.safe_edit(query.message, text, markup, final=True)

  elif type=="asback":
     # back to Extra Settings: the AUTOSORT pages replaced the message text, so put it back too
     await query.message.edit_text(
        "<b><u>💠 CUSTOM FILTERS 💠</b></u>\n\n**configure the type of messages which you want forward**",
        reply_markup=await next_filters_buttons(user_id))

  elif type=="assession":
     text, markup = await autosort_create.session_page(user_id)
     await autosort_create.safe_edit(query.message, text, markup, final=True)

  elif type=="assessionset":
     await query.message.delete()
     await autosort_create.set_session_flow(bot, user_id)

  elif type=="assessionreset":
     await db.remove_autosort_session(user_id)
     text, markup = await autosort_create.session_page(user_id)
     await autosort_create.safe_edit(query.message, text, markup, final=True)

  elif type=="assessiontest":
     await autosort_create.test_session(user_id, query.message)

  elif type=="ascreate":
     if user_id in autosort_create.RUNNING:
        return await query.answer("A creation run is already in progress.", show_alert=True)
     if not await db.get_autosort_session(user_id):
        text, markup = autosort_create.no_session_page()
     elif not await autosort_create.unset_categories(user_id):
        text, markup = autosort_create.all_set_page()
     else:
        text, markup = autosort_create.type_page()
     await autosort_create.safe_edit(query.message, text, markup, final=True)

  elif type.startswith("ascreatetype_"):
     kind = type.split('_', 1)[1]
     if kind not in ("channel", "group"):
        return await query.answer()
     if user_id in autosort_create.RUNNING:
        return await query.answer("A creation run is already in progress.", show_alert=True)
     if not await db.get_autosort_session(user_id):
        text, markup = autosort_create.no_session_page()
        return await autosort_create.safe_edit(query.message, text, markup, final=True)
     await query.message.delete()
     await autosort_create.prefix_flow(bot, user_id, kind)

  elif type=="asgo":
     cfg, error = autosort_create.claim_run(user_id)       # takes the lock (no await inside)
     if error:
        return await query.answer(error, show_alert=True)
     try:
        await query.answer()
     except Exception:
        pass
     await autosort_create.run_creation(user_id, bot, query.message)

  elif type=="ascancel":
     if autosort_create.request_cancel(user_id):
        await query.answer("Stopping after the current chat...", show_alert=True)
     else:
        await query.answer("No creation run is going.", show_alert=True)

  elif type=="privatefiles":
     buttons = [[InlineKeyboardButton('TMDB',
                  callback_data="settings#tmdb"),
               InlineKeyboardButton('AniList',
                  callback_data="settings#anilist")
               ],[
               InlineKeyboardButton('OpenAI',
                  callback_data="settings#openai"),
               InlineKeyboardButton('ThePornDB',
                  callback_data="settings#tpdb")
               ],[
               InlineKeyboardButton('ʙᴀᴄᴋ',
                  callback_data="settings#main")]]
     await query.message.edit_text(
        "<b><u>📁 ᴘʀɪᴠᴀᴛᴇ ꜰɪʟᴇs</b></u>\n\n"
        "<b>Your private keys and detection services used by /autosort and /fwd.</b>",
        reply_markup=InlineKeyboardMarkup(buttons))

  elif type=="tmdb":
     key = await db.get_tmdb_key(user_id)
     shown = (key[:4] + "…" + key[-2:]) if key and len(key) > 8 else ("••••" if key else "not set")
     buttons = [[InlineKeyboardButton('sᴇᴛ',
                  callback_data="settings#settmdb")
               ],[
               InlineKeyboardButton('ᴛᴇsᴛ',
                  callback_data="settings#testtmdb")
               ],[
               InlineKeyboardButton('ʀᴇsᴇᴛ',
                  callback_data="settings#resettmdb")
               ],[
               InlineKeyboardButton('ʙᴀᴄᴋ',
                  callback_data="settings#privatefiles")]]
     await query.message.edit_text(
        f"<b><u>TMDB</b></u>\n\n"
        f"<b>Used to double-check movie/series/anime detection during /autosort and "
        f"/fwd's category filter.</b>\n\nCurrent value: <code>{shown}</code>",
        reply_markup=InlineKeyboardMarkup(buttons))

  elif type=="testtmdb":
     key = await db.get_tmdb_key(user_id)
     if not key:
        return await query.answer("No TMDB key set yet.", show_alert=True)
     await query.answer("Checking with TMDB...")
     ok, detail = await _verify_tmdb_key(key)
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#tmdb")]]
     icon = "✅" if ok else "❌"
     await query.message.edit_text(
        f"<b>{icon} {'Working' if ok else 'Not working'}</b>\n\n<code>{detail}</code>",
        reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="settmdb":
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#tmdb")]]
     await query.message.delete()
     try:
         text = await bot.send_message(
            user_id,
            "<b>❪ SET TMDB API KEY ❫</b>\n\n"
            "<b>Send your TMDB API key (v3 auth).</b>\n"
            "/cancel - cancel this process"
         )
         reply = await bot.listen(chat_id=user_id, timeout=300)
         if reply.text == "/cancel":
            await reply.delete()
            return await text.edit_text("<b>process canceled</b>", reply_markup=InlineKeyboardMarkup(back_buttons))
         key = reply.text.strip()
         await reply.delete()
         await text.edit_text("<code>Verifying key with TMDB...</code>")
         ok, detail = await _verify_tmdb_key(key)
         if not ok:
            return await text.edit_text(
               f"<b>❌ TMDB rejected this key:</b>\n<code>{detail}</code>\n\nNot saved — try /settings again with a correct key.",
               reply_markup=InlineKeyboardMarkup(back_buttons))
         await db.set_tmdb_key(user_id, key)
         await text.edit_text(f"<b>✅ Key verified and saved.</b>\n<code>{detail}</code>", reply_markup=InlineKeyboardMarkup(back_buttons))
     except asyncio.exceptions.TimeoutError:
         await text.edit_text('Process has been automatically cancelled', reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="resettmdb":
     await db.remove_tmdb_key(user_id)
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#privatefiles")]]
     await query.message.edit_text(
        "<b>successfully updated</b>",
        reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="tpdb":
     key = await db.get_tpdb_key(user_id)
     shown = (key[:4] + "…" + key[-2:]) if key and len(key) > 8 else ("••••" if key else "not set")
     buttons = [[InlineKeyboardButton('sᴇᴛ',
                  callback_data="settings#settpdb")
               ],[
               InlineKeyboardButton('ᴛᴇsᴛ',
                  callback_data="settings#testtpdb")
               ],[
               InlineKeyboardButton('ʀᴇsᴇᴛ',
                  callback_data="settings#resettpdb")
               ],[
               InlineKeyboardButton('ʙᴀᴄᴋ',
                  callback_data="settings#privatefiles")]]
     await query.message.edit_text(
        f"<b><u>ThePornDB</b></u>\n\n"
        f"<b>Optional. Used by /autosort and /autosortai only for videos that would otherwise "
        f"land in Other, and only when an NSFW channel is set: if ThePornDB recognises the file "
        f"name as a scene, it goes to the NSFW channel.</b>\n\nCurrent value: <code>{shown}</code>",
        reply_markup=InlineKeyboardMarkup(buttons))

  elif type=="testtpdb":
     key = await db.get_tpdb_key(user_id)
     if not key:
        return await query.answer("No ThePornDB token set yet.", show_alert=True)
     await query.answer("Checking with ThePornDB...")
     ok, detail = await _verify_tpdb_key(key)
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#tpdb")]]
     icon = "✅" if ok else "❌"
     await query.message.edit_text(
        f"<b>{icon} {'Working' if ok else 'Not working'}</b>\n\n<code>{detail}</code>",
        reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="settpdb":
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#tpdb")]]
     await query.message.delete()
     try:
         text = await bot.send_message(
            user_id,
            "<b>❪ SET THEPORNDB TOKEN ❫</b>\n\n"
            "<b>Send your ThePornDB API token (theporndb.net → API tokens).</b>\n"
            "/cancel - cancel this process"
         )
         reply = await bot.listen(chat_id=user_id, timeout=300)
         if reply.text == "/cancel":
            await reply.delete()
            return await text.edit_text("<b>process canceled</b>", reply_markup=InlineKeyboardMarkup(back_buttons))
         key = reply.text.strip()
         await reply.delete()
         await text.edit_text("<code>Verifying token with ThePornDB...</code>")
         ok, detail = await _verify_tpdb_key(key)
         if not ok:
            return await text.edit_text(
               f"<b>❌ ThePornDB rejected this token:</b>\n<code>{detail}</code>\n\nNot saved — try /settings again with a correct token.",
               reply_markup=InlineKeyboardMarkup(back_buttons))
         await db.set_tpdb_key(user_id, key)
         await text.edit_text(f"<b>✅ Token verified and saved.</b>\n<code>{detail}</code>", reply_markup=InlineKeyboardMarkup(back_buttons))
     except asyncio.exceptions.TimeoutError:
         await text.edit_text('Process has been automatically cancelled', reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="resettpdb":
     await db.remove_tpdb_key(user_id)
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#privatefiles")]]
     await query.message.edit_text(
        "<b>successfully updated</b>",
        reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="openai":
     key = await db.get_openai_key(user_id)
     shown = (key[:4] + "…" + key[-2:]) if key and len(key) > 8 else ("••••" if key else "not set")
     buttons = [[InlineKeyboardButton('sᴇᴛ',
                  callback_data="settings#setopenai")
               ],[
               InlineKeyboardButton('ᴛᴇsᴛ',
                  callback_data="settings#testopenai")
               ],[
               InlineKeyboardButton('ʀᴇsᴇᴛ',
                  callback_data="settings#resetopenai")
               ],[
               InlineKeyboardButton('ʙᴀᴄᴋ',
                  callback_data="settings#privatefiles")]]
     await query.message.edit_text(
        f"<b><u>OpenAI</b></u>\n\n"
        f"<b>Optional. Used by /autosort and /fwd's category filter only for videos TMDB/AniList "
        f"couldn't identify (e.g. course/lecture videos), so they reach Education instead of Other. "
        f"Only the file name/caption text is sent to OpenAI.</b>\n\nCurrent value: <code>{shown}</code>",
        reply_markup=InlineKeyboardMarkup(buttons))

  elif type=="testopenai":
     key = await db.get_openai_key(user_id)
     if not key:
        return await query.answer("No OpenAI key set yet.", show_alert=True)
     await query.answer("Checking with OpenAI...")
     ok, detail = await _verify_openai_key(key)
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#openai")]]
     icon = "✅" if ok else "❌"
     await query.message.edit_text(
        f"<b>{icon} {'Working' if ok else 'Not working'}</b>\n\n<code>{detail}</code>",
        reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="setopenai":
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#openai")]]
     await query.message.delete()
     try:
         text = await bot.send_message(
            user_id,
            "<b>❪ SET OPENAI API KEY ❫</b>\n\n"
            "<b>Send your OpenAI API key (starts with sk-).</b>\n"
            "/cancel - cancel this process"
         )
         reply = await bot.listen(chat_id=user_id, timeout=300)
         if reply.text == "/cancel":
            await reply.delete()
            return await text.edit_text("<b>process canceled</b>", reply_markup=InlineKeyboardMarkup(back_buttons))
         key = reply.text.strip()
         await reply.delete()
         await text.edit_text("<code>Verifying key with OpenAI...</code>")
         ok, detail = await _verify_openai_key(key)
         if not ok:
            return await text.edit_text(
               f"<b>❌ OpenAI rejected this key:</b>\n<code>{detail}</code>\n\nNot saved — try /settings again with a correct key.",
               reply_markup=InlineKeyboardMarkup(back_buttons))
         await db.set_openai_key(user_id, key)
         await text.edit_text(f"<b>✅ Key verified and saved.</b>\n<code>{detail}</code>", reply_markup=InlineKeyboardMarkup(back_buttons))
     except asyncio.exceptions.TimeoutError:
         await text.edit_text('Process has been automatically cancelled', reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="resetopenai":
     await db.remove_openai_key(user_id)
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#privatefiles")]]
     await query.message.edit_text(
        "<b>successfully updated</b>",
        reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="anilist":
     enabled = await db.get_anilist_enabled(user_id)
     key = await db.get_anilist_key(user_id)
     shown = (key[:4] + "…" + key[-2:]) if key and len(key) > 8 else ("••••" if key else "not set")
     buttons = [[InlineKeyboardButton('ᴅɪsᴀʙʟᴇ' if enabled else 'ᴇɴᴀʙʟᴇ',
                  callback_data="settings#toganilist")
               ],[
               InlineKeyboardButton('sᴇᴛ ᴋᴇʏ',
                  callback_data="settings#setanilist"),
               InlineKeyboardButton('ʀᴇsᴇᴛ ᴋᴇʏ',
                  callback_data="settings#resetanilist")
               ],[
               InlineKeyboardButton('ᴛᴇsᴛ',
                  callback_data="settings#testanilist")
               ],[
               InlineKeyboardButton('ᴄʜᴇᴄᴋ ᴛɪᴛʟᴇ',
                  callback_data="settings#checkanilist")
               ],[
               InlineKeyboardButton('ʙᴀᴄᴋ',
                  callback_data="settings#privatefiles")]]
     await query.message.edit_text(
        f"<b><u>AniList</b></u>\n\n"
        f"<b>Second opinion for anime detection in /autosort and /fwd's category filter. "
        f"Used only when TMDB is unsure (title not found, TMDB down, or an animated series "
        f"TMDB can't call anime). No API key needed - the access token is optional.</b>\n\n"
        f"Status: <code>{'ON' if enabled else 'OFF'}</code>\n"
        f"Key: <code>{shown}</code>",
        reply_markup=InlineKeyboardMarkup(buttons))

  elif type=="setanilist":
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#anilist")]]
     await query.message.delete()
     try:
         text = await bot.send_message(
            user_id,
            "<b>❪ SET ANILIST KEY ❫</b>\n\n"
            "<b>Send your AniList access token.</b>\n"
            "/cancel - cancel this process"
         )
         reply = await bot.listen(chat_id=user_id, timeout=300)
         if not reply.text or reply.text == "/cancel":
            await reply.delete()
            return await text.edit_text("<b>process canceled</b>", reply_markup=InlineKeyboardMarkup(back_buttons))
         key = reply.text.strip()
         await reply.delete()
         await text.edit_text("<code>Verifying key with AniList...</code>")
         ok, detail = await _verify_anilist_key(key)
         if not ok:
            return await text.edit_text(
               f"<b>❌ AniList rejected this key:</b>\n<code>{detail}</code>\n\nNot saved — try again with a correct key.",
               reply_markup=InlineKeyboardMarkup(back_buttons))
         await db.set_anilist_key(user_id, key)
         await text.edit_text(f"<b>✅ Key verified and saved.</b>\n<code>{detail}</code>", reply_markup=InlineKeyboardMarkup(back_buttons))
     except asyncio.exceptions.TimeoutError:
         await text.edit_text('Process has been automatically cancelled', reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="resetanilist":
     await db.remove_anilist_key(user_id)
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#anilist")]]
     await query.message.edit_text(
        "<b>successfully updated</b>",
        reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="toganilist":
     enabled = await db.get_anilist_enabled(user_id)
     await db.set_anilist_enabled(user_id, not enabled)
     await query.answer(f"AniList {'disabled' if enabled else 'enabled'}.")
     buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#anilist")]]
     await query.message.edit_text(
        f"<b>✅ AniList is now {'OFF' if enabled else 'ON'}.</b>",
        reply_markup=InlineKeyboardMarkup(buttons))

  elif type=="testanilist":
     await query.answer("Checking with AniList...")
     ok, detail = await _test_anilist(await db.get_anilist_key(user_id))
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#anilist")]]
     icon = "✅" if ok else "❌"
     await query.message.edit_text(
        f"<b>{icon} {'Working' if ok else 'Not working'}</b>\n\n<code>{detail}</code>",
        reply_markup=InlineKeyboardMarkup(back_buttons))

  elif type=="checkanilist":
     from plugins.classify import anilist_lookup
     back_buttons = [[InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#anilist")]]
     await query.message.delete()
     try:
         text = await bot.send_message(
            user_id,
            "<b>❪ CHECK TITLE ON ANILIST ❫</b>\n\n"
            "<b>Send a title or a file name</b> (e.g. <code>Frieren</code> or "
            "<code>Blue.Eye.Samurai.S01E01.720p.mkv</code>).\n"
            "/cancel - cancel this process"
         )
         reply = await bot.listen(chat_id=user_id, timeout=300)
         if not reply.text or reply.text == "/cancel":
            await reply.delete()
            return await text.edit_text("<b>process canceled</b>", reply_markup=InlineKeyboardMarkup(back_buttons))
         title = reply.text.strip()
         await reply.delete()
         await text.edit_text("<code>Asking AniList...</code>")
         status, media = await anilist_lookup(title, anilist_key=await db.get_anilist_key(user_id))
         if status == "found":
            msg = f"<b>✅ Anime on AniList</b>\n\n<code>{_fmt_anilist(media)}</code>"
         elif status == "none":
            msg = ("<b>❌ Not matched on AniList</b>\n\n"
                   "<code>No close, Asian-origin anime title found - this would not be "
                   "treated as anime by AniList.</code>")
         else:
            msg = "<b>⚠️ AniList did not answer</b>\n\n<code>Network error, rate limit or API down.</code>"
         await text.edit_text(msg, reply_markup=InlineKeyboardMarkup(back_buttons))
     except asyncio.exceptions.TimeoutError:
         await text.edit_text('Process has been automatically cancelled', reply_markup=InlineKeyboardMarkup(back_buttons))
   
  elif type=="addchannel":  
     await query.message.delete()
     try:
         text = await bot.send_message(user_id, "<b>❪ SET TARGET CHAT ❫\n\nForward a message from Your target chat\n/cancel - cancel this process</b>")
         chat_ids = await bot.listen(chat_id=user_id, timeout=300)
         if chat_ids.text=="/cancel":
            await chat_ids.delete()
            return await text.edit_text(
                  "<b>process canceled</b>",
                  reply_markup=InlineKeyboardMarkup(buttons))
         elif not chat_ids.forward_date:
            await chat_ids.delete()
            return await text.edit_text("**This is not a forward message**")
         else:
            chat_id = chat_ids.forward_from_chat.id
            title = chat_ids.forward_from_chat.title
            username = chat_ids.forward_from_chat.username
            username = "@" + username if username else "private"
         chat = await db.add_channel(user_id, chat_id, title, username)
         await chat_ids.delete()
         await text.edit_text(
            "<b>Successfully updated</b>" if chat else "<b>This channel already added</b>",
            reply_markup=InlineKeyboardMarkup(buttons))
     except asyncio.exceptions.TimeoutError:
         await text.edit_text('Process has been automatically cancelled', reply_markup=InlineKeyboardMarkup(buttons))
  
  elif type=="editbot": 
     bot = await db.get_bot(user_id)
     TEXT = Translation.BOT_DETAILS if bot['is_bot'] else Translation.USER_DETAILS
     buttons = [[InlineKeyboardButton('ʀᴇᴍᴏᴠᴇ', callback_data=f"settings#removebot")
               ],
               [InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#bots")]]
     await query.message.edit_text(
        TEXT.format(bot['name'], bot['id'], bot['username']),
        reply_markup=InlineKeyboardMarkup(buttons))
                                             
  elif type=="removebot":
     await db.remove_bot(user_id)
     await query.message.edit_text(
        "<b>successfully updated</b>",
        reply_markup=InlineKeyboardMarkup(buttons))
                                             
  elif type.startswith("editchannels"): 
     chat_id = type.split('_')[1]
     chat = await db.get_channel_details(user_id, chat_id)
     buttons = [[InlineKeyboardButton('ʀᴇᴍᴏᴠᴇ', callback_data=f"settings#removechannel_{chat_id}")
               ],
               [InlineKeyboardButton('ʙᴀᴄᴋ', callback_data="settings#channels")]]
     await query.message.edit_text(
        f"<b><u>📄 CHANNEL DETAILS</b></u>\n\n<b>- TITLE:</b> <code>{chat['title']}</code>\n<b>- CHANNEL ID: </b> <code>{chat['chat_id']}</code>\n<b>- USERNAME:</b> {chat['username']}",
        reply_markup=InlineKeyboardMarkup(buttons))
                                             
  elif type.startswith("removechannel"):
     chat_id = type.split('_')[1]
     await db.remove_channel(user_id, chat_id)
     await query.message.edit_text(
        "<b>successfully updated</b>",
        reply_markup=InlineKeyboardMarkup(buttons))
                               
  elif type=="caption":
     buttons = []
     data = await get_configs(user_id)
     caption = data['caption']
     if caption is None:
        buttons.append([InlineKeyboardButton('ᴀᴅᴅ ᴄᴀᴘᴛɪᴏɴ', 
                      callback_data="settings#addcaption")])
     else:
        buttons.append([InlineKeyboardButton('sᴇᴇ ᴄᴀᴘᴛɪᴏɴ', 
                      callback_data="settings#seecaption")])
        buttons[-1].append(InlineKeyboardButton('ᴅᴇʟᴇᴛᴇ ᴄᴀᴘᴛɪᴏɴ', 
                      callback_data="settings#deletecaption"))
     buttons.append([InlineKeyboardButton('ʙᴀᴄᴋ', 
                      callback_data="settings#main")])
     await query.message.edit_text(
        "<b><u>CUSTOM CAPTION</b></u>\n\n<b>You can set a custom caption to videos and documents. Normaly use its default caption</b>\n\n<b><u>AVAILABLE FILLINGS:</b></u>\n- <code>{filename}</code> : Filename\n- <code>{size}</code> : File size\n- <code>{caption}</code> : original caption cleaned to the file name only",
        reply_markup=InlineKeyboardMarkup(buttons))
                               
  elif type=="seecaption":   
     data = await get_configs(user_id)
     buttons = [[InlineKeyboardButton('ᴇᴅɪᴛ ᴄᴀᴘᴛɪᴏɴ', 
                  callback_data="settings#addcaption")
               ],[
               InlineKeyboardButton('ʙᴀᴄᴋ', 
                 callback_data="settings#caption")]]
     await query.message.edit_text(
        f"<b><u>YOUR CUSTOM CAPTION</b></u>\n\n<code>{data['caption']}</code>",
        reply_markup=InlineKeyboardMarkup(buttons))
    
  elif type=="deletecaption":
     await update_configs(user_id, 'caption', None)
     await query.message.edit_text(
        "<b>successfully updated</b>",
        reply_markup=InlineKeyboardMarkup(buttons))
                              
  elif type=="addcaption":
     await query.message.delete()
     try:
         text = await bot.send_message(query.message.chat.id, "Send your custom caption\n/cancel - <code>cancel this process</code>")
         caption = await bot.listen(chat_id=user_id, timeout=300)
         if caption.text=="/cancel":
            await caption.delete()
            return await text.edit_text(
                  "<b>process canceled !</b>",
                  reply_markup=InlineKeyboardMarkup(buttons))
         try:
            caption.text.format(filename='', size='', caption='')
         except KeyError as e:
            await caption.delete()
            return await text.edit_text(
               f"<b>wrong filling {e} used in your caption. change it</b>",
               reply_markup=InlineKeyboardMarkup(buttons))
         await update_configs(user_id, 'caption', caption.text)
         await caption.delete()
         await text.edit_text(
            "<b>successfully updated</b>",
            reply_markup=InlineKeyboardMarkup(buttons))
     except asyncio.exceptions.TimeoutError:
         await text.edit_text('Process has been automatically cancelled', reply_markup=InlineKeyboardMarkup(buttons))
  
  elif type=="button":
     buttons = []
     button = (await get_configs(user_id))['button']
     if button is None:
        buttons.append([InlineKeyboardButton('ᴀᴅᴅ ʙᴜᴛᴛᴏɴ', 
                      callback_data="settings#addbutton")])
     else:
        buttons.append([InlineKeyboardButton('sᴇᴇ ʙᴜᴛᴛᴏɴ', 
                      callback_data="settings#seebutton")])
        buttons[-1].append(InlineKeyboardButton('ʀᴇᴍᴏᴠᴇ ʙᴜᴛᴛᴏɴ', 
                      callback_data="settings#deletebutton"))
     buttons.append([InlineKeyboardButton('ʙᴀᴄᴋ', 
                      callback_data="settings#main")])
     await query.message.edit_text(
        "<b><u>CUSTOM BUTTON</b></u>\n\n<b>You can set a inline button to messages.</b>\n\n<b><u>FORMAT:</b></u>\n`[Forward bot][buttonurl:https://t.me/devgaganbot]`\n",
        reply_markup=InlineKeyboardMarkup(buttons))
  
  elif type=="addbutton":
     await query.message.delete()
     try:
         txt = await bot.send_message(user_id, text="**Send your custom button.\n\nFORMAT:**\n`[forward bot][buttonurl:https://t.me/devgaganbot]`\n")
         ask = await bot.listen(chat_id=user_id, timeout=300)
         button = parse_buttons(ask.text.html)
         if not button:
            await ask.delete()
            return await txt.edit_text("**INVALID BUTTON**")
         await update_configs(user_id, 'button', ask.text.html)
         await ask.delete()
         await txt.edit_text("**Successfully button added**",
            reply_markup=InlineKeyboardMarkup(buttons))
     except asyncio.exceptions.TimeoutError:
         await txt.edit_text('Process has been automatically cancelled', reply_markup=InlineKeyboardMarkup(buttons))
  
  elif type=="seebutton":
      button = (await get_configs(user_id))['button']
      button = parse_buttons(button, markup=False)
      button.append([InlineKeyboardButton("ʙᴀᴄᴋ", "settings#button")])
      await query.message.edit_text(
         "**YOUR CUSTOM BUTTON**",
         reply_markup=InlineKeyboardMarkup(button))
      
  elif type=="deletebutton":
     await update_configs(user_id, 'button', None)
     await query.message.edit_text(
        "**Successfully button deleted**",
        reply_markup=InlineKeyboardMarkup(buttons))
   
  elif type=="database":
     buttons = []
     db_uri = (await get_configs(user_id))['db_uri']
     if db_uri is None:
        buttons.append([InlineKeyboardButton('ᴀᴅᴅ ᴜʀʟ', 
                      callback_data="settings#addurl")])
     else:
        buttons.append([InlineKeyboardButton('sᴇᴇ ᴜʀʟ', 
                      callback_data="settings#seeurl")])
        buttons[-1].append(InlineKeyboardButton('ʀᴇᴍᴏᴠᴇ ᴜʀʟ', 
                      callback_data="settings#deleteurl"))
     buttons.append([InlineKeyboardButton('ʙᴀᴄᴋ', 
                      callback_data="settings#main")])
     await query.message.edit_text(
        "<b><u>DATABASE</u>\n\nDatabase is required for store your duplicate messages permenant. other wise stored duplicate media may be disappeared when after bot restart.</b>",
        reply_markup=InlineKeyboardMarkup(buttons))

  elif type=="addurl":
     await query.message.delete()
     uri = await bot.ask(user_id, "<b>please send your mongodb url.</b>\n\n<i>get your Mongodb url from [here](https://mongodb.com)</i>", disable_web_page_preview=True)
     if uri.text=="/cancel":
        return await uri.reply_text(
                  "<b>process canceled !</b>",
                  reply_markup=InlineKeyboardMarkup(buttons))
     if not uri.text.startswith("mongodb+srv://") and not uri.text.endswith("majority"):
        return await uri.reply("<b>Invalid Mongodb Url</b>",
                   reply_markup=InlineKeyboardMarkup(buttons))
     await update_configs(user_id, 'db_uri', uri.text)
     await uri.reply("**Successfully database url added**",
             reply_markup=InlineKeyboardMarkup(buttons))
  
  elif type=="seeurl":
     db_uri = (await get_configs(user_id))['db_uri']
     await query.answer(f"DATABASE URL: {db_uri}", show_alert=True)
  
  elif type=="deleteurl":
     await update_configs(user_id, 'db_uri', None)
     await query.message.edit_text(
        "**Successfully your database url deleted**",
        reply_markup=InlineKeyboardMarkup(buttons))
      
  elif type=="filters":
     await query.message.edit_text(
        "<b><u>💠 CUSTOM FILTERS 💠</b></u>\n\n**configure the type of messages which you want forward**",
        reply_markup=await filters_buttons(user_id))
  
  elif type=="nextfilters":
     await query.edit_message_reply_markup( 
        reply_markup=await next_filters_buttons(user_id))
   
  elif type.startswith("updatefilter"):
     i, key, value = type.split('-')
     if value=="True":
        await update_configs(user_id, key, False)
     else:
        await update_configs(user_id, key, True)
     if key in ['poll', 'protect', 'caption_first_line', 'remove_caption_link']:
        return await query.edit_message_reply_markup(
           reply_markup=await next_filters_buttons(user_id)) 
     await query.edit_message_reply_markup(
        reply_markup=await filters_buttons(user_id))
   
  elif type.startswith("file_size"):
    settings = await get_configs(user_id)
    size = settings.get('file_size', 0)
    i, limit = size_limit(settings['size_limit'])
    await query.message.edit_text(
       f'<b><u>SIZE LIMIT</b></u><b>\n\nyou can set file size limit to forward\n\nStatus: files with {limit} `{size} MB` will forward</b>',
       reply_markup=size_button(size))
  
  elif type.startswith("update_size"):
    size = int(query.data.split('-')[1])
    if size > 2000 or size < 0:
      return await query.answer("size limit exceeded", show_alert=True)
    await update_configs(user_id, 'file_size', size)
    i, limit = size_limit((await get_configs(user_id))['size_limit'])
    await query.message.edit_text(
       f'<b><u>SIZE LIMIT</b></u><b>\n\nyou can set file size limit to forward\n\nStatus: files with {limit} `{size} MB` will forward</b>',
       reply_markup=size_button(size))
  
  elif type.startswith('update_limit'):
    i, limit, size = type.split('-')
    limit, sts = size_limit(limit)
    await update_configs(user_id, 'size_limit', limit) 
    await query.message.edit_text(
       f'<b><u>SIZE LIMIT</b></u><b>\n\nyou can set file size limit to forward\n\nStatus: files with {sts} `{size} MB` will forward</b>',
       reply_markup=size_button(int(size)))
      
  elif type == "add_extension":
    await query.message.delete() 
    ext = await bot.ask(user_id, text="**please send your extensions (seperete by space)**")
    if ext.text == '/cancel':
       return await ext.reply_text(
                  "<b>process canceled</b>",
                  reply_markup=InlineKeyboardMarkup(buttons))
    extensions = ext.text.split(" ")
    extension = (await get_configs(user_id))['extension']
    if extension:
        for extn in extensions:
            extension.append(extn)
    else:
        extension = extensions
    await update_configs(user_id, 'extension', extension)
    await ext.reply_text(
        f"**successfully updated**",
        reply_markup=InlineKeyboardMarkup(buttons))
      
  elif type == "get_extension":
    extensions = (await get_configs(user_id))['extension']
    btn = extract_btn(extensions)
    btn.append([InlineKeyboardButton('ᴀᴅᴅ', 'settings#add_extension')])
    btn.append([InlineKeyboardButton('Remove all', 'settings#rmve_all_extension')])
    btn.append([InlineKeyboardButton('ʙᴀᴄᴋ', 'settings#main')])
    await query.message.edit_text(
        text='<b><u>EXTENSIONS</u></b>\n\n**Files with these extiontions will not forward**',
        reply_markup=InlineKeyboardMarkup(btn))
  
  elif type == "rmve_all_extension":
    await update_configs(user_id, 'extension', None)
    await query.message.edit_text(text="**successfully deleted**",
                                   reply_markup=InlineKeyboardMarkup(buttons))
  elif type == "add_keyword":
    await query.message.delete()
    ask = await bot.ask(user_id, text="**please send the keywords (seperete by space)**")
    if ask.text == '/cancel':
       return await ask.reply_text(
                  "<b>process canceled</b>",
                  reply_markup=InlineKeyboardMarkup(buttons))
    keywords = ask.text.split(" ")
    keyword = (await get_configs(user_id))['keywords']
    if keyword:
        for word in keywords:
            keyword.append(word)
    else:
        keyword = keywords
    await update_configs(user_id, 'keywords', keyword)
    await ask.reply_text(
        f"**successfully updated**",
        reply_markup=InlineKeyboardMarkup(buttons))
  
  elif type == "get_keyword":
    keywords = (await get_configs(user_id))['keywords']
    btn = extract_btn(keywords)
    btn.append([InlineKeyboardButton('ᴀᴅᴅ', 'settings#add_keyword')])
    btn.append([InlineKeyboardButton('Remove all', 'settings#rmve_all_keyword')])
    btn.append([InlineKeyboardButton('ʙᴀᴄᴋ', 'settings#main')])
    await query.message.edit_text(
        text='<b><u>KEYWORDS</u></b>\n\n**File with these keywords in file name will forwad**',
        reply_markup=InlineKeyboardMarkup(btn))
      
  elif type == "rmve_all_keyword":
    await update_configs(user_id, 'keywords', None)
    await query.message.edit_text(text="**successfully deleted**",
                                   reply_markup=InlineKeyboardMarkup(buttons))

  elif type in ("add_movie", "add_series"):
    kind = 'movie' if type == 'add_movie' else 'series'
    info = WHITELIST_KINDS[kind]
    await query.message.delete()
    ask = await bot.ask(user_id, text=f"**please send the {info['prompt']}, one per line or comma-separated**")
    if ask.text == '/cancel':
       return await ask.reply_text(
                  "<b>process canceled</b>",
                  reply_markup=InlineKeyboardMarkup(buttons))
    # comma AND newline separated - unlike keywords/extensions above,
    # these names routinely contain spaces, so they can't be split on " ".
    names = [n.strip() for n in ask.text.replace("\n", ",").split(",") if n.strip()]
    whitelist = (await get_configs(user_id))[info['list_key']]
    existing_lower = {n.lower() for n in whitelist} if whitelist else set()
    if not whitelist:
        whitelist = []
    for name in names:
        if name.lower() not in existing_lower:
            whitelist.append(name)
            existing_lower.add(name.lower())
    await update_configs(user_id, info['list_key'], whitelist)
    await ask.reply_text(
        f"**successfully updated**",
        reply_markup=InlineKeyboardMarkup(buttons))

  elif type in ("get_movie", "get_series"):
    kind = 'movie' if type == 'get_movie' else 'series'
    info = WHITELIST_KINDS[kind]
    settings_ = await get_configs(user_id)
    await query.message.edit_text(
        text=whitelist_text(kind),
        reply_markup=whitelist_menu(kind, settings_[info['list_key']], settings_[info['exact_key']]))

  elif type.startswith("rmve_movie-") or type.startswith("rmve_series-"):
    kind = 'movie' if type.startswith("rmve_movie-") else 'series'
    info = WHITELIST_KINDS[kind]
    index = int(type.split('-', 1)[1])
    settings_ = await get_configs(user_id)
    whitelist = settings_[info['list_key']] or []
    if 0 <= index < len(whitelist):
        whitelist.pop(index)
    await update_configs(user_id, info['list_key'], whitelist or None)
    await query.message.edit_text(
        text=whitelist_text(kind),
        reply_markup=whitelist_menu(kind, whitelist, settings_[info['exact_key']]))

  elif type in ("toggle_movie_exact", "toggle_series_exact"):
    kind = 'movie' if type == 'toggle_movie_exact' else 'series'
    info = WHITELIST_KINDS[kind]
    settings_ = await get_configs(user_id)
    exact = not settings_[info['exact_key']]
    await update_configs(user_id, info['exact_key'], exact)
    await query.message.edit_text(
        text=whitelist_text(kind),
        reply_markup=whitelist_menu(kind, settings_[info['list_key']], exact))

  elif type in ("rmve_all_movie", "rmve_all_series"):
    kind = 'movie' if type == 'rmve_all_movie' else 'series'
    info = WHITELIST_KINDS[kind]
    await update_configs(user_id, info['list_key'], None)
    await query.message.edit_text(text="**successfully deleted**",
                                   reply_markup=InlineKeyboardMarkup(buttons))

  elif type.startswith("alert"):
    alert = type.split('_', 1)[1]
    await query.answer(alert, show_alert=True)
