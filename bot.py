# Don't Remove Credit @VJ_Bots
# Subscribe YouTube Channel For Amazing Bot @Tech_VJ
# Ask Doubt on telegram @KingVJ01

# Clone Code Credit : YT - @Tech_VJ / TG - @VJ_Bots / GitHub - @VJBots

import sys, glob, importlib, logging, logging.config, pytz, asyncio
from pathlib import Path

# Get logging configurations
logging.config.fileConfig('core/logging.conf')
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("cinemagoer").setLevel(logging.ERROR)

from pyrogram import Client, idle
from plugins.database.users_chats_db import db
from core.info import *
from core.utils import temp
from typing import Union, Optional, AsyncGenerator
from core.script import script 
from datetime import date, datetime 
from aiohttp import web
from deploy import web_server
from plugins.clone.clone import restart_bots

from core.clients import TechVJBot
from deploy.keepalive import ping_server
from core.clients import initialize_clients

ppath = "plugins/*/*.py"
files = [f for f in glob.glob(ppath) if Path(f).parent.name not in ("database", "clone_bot")]
# Pyrogram's own loader, one root ("plugins"). The `include` list sets the load order:
# tools/, rename/, stream/, auto_approve/ first, then main/, manage/ and clone/, and search/ last.
# Inside a group the first matching handler wins, and
# pm_text / give_filter / cb_handler (plugins/search/pm_filter.py) would swallow private commands,
# group commands and other callbacks if they were registered before them.
# plugins/database/ and plugins/clone_bot/ are not in the list, so the main bot never loads them
# (clone_bot handlers are for the clone bots only, see plugins/clone/clone.py).
# `exclude` is not used: Pyrogram's exclude unloads modules that were already loaded.
PLUGIN_FOLDERS = ["tools", "rename", "stream", "auto_approve", "main", "manage", "clone", "search"]
TechVJBot.plugins = {
    "root": "plugins",
    "include": [
        "{}.{}".format(folder, f.stem)
        for folder in PLUGIN_FOLDERS
        for f in sorted(Path("plugins", folder).glob("*.py"))
        if f.stem != "__init__"
    ],
}
TechVJBot.start()
loop = asyncio.get_event_loop()


async def start():
    print('\n')
    print('Initalizing Your Bot')
    bot_info = await TechVJBot.get_me()
    await initialize_clients()
    for name in files:
        with open(name) as a:
            patt = Path(a.name)
            plugin_name = patt.stem.replace(".py", "")
            plugin_folder = patt.parent.name
            plugins_dir = Path(f"plugins/{plugin_folder}/{plugin_name}.py")
            import_path = "plugins.{}.{}".format(plugin_folder, plugin_name)
            spec = importlib.util.spec_from_file_location(import_path, plugins_dir)
            load = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(load)
            sys.modules[import_path] = load
            print("Tech VJ Imported => " + plugin_name)
    if ON_HEROKU:
        asyncio.create_task(ping_server())
    b_users, b_chats = await db.get_banned()
    temp.BANNED_USERS = b_users
    temp.BANNED_CHATS = b_chats
    me = await TechVJBot.get_me()
    temp.BOT = TechVJBot
    temp.ME = me.id
    temp.U_NAME = me.username
    temp.B_NAME = me.first_name
    logging.info(script.LOGO)
    tz = pytz.timezone('Asia/Kolkata')
    today = date.today()
    now = datetime.now(tz)
    time = now.strftime("%H:%M:%S %p")
    try:
        await TechVJBot.send_message(chat_id=LOG_CHANNEL, text=script.RESTART_TXT.format(today, time))
    except:
        print("Make Your Bot Admin In Log Channel With Full Rights")
    for ch in CHANNELS:
        try:
            k = await TechVJBot.send_message(chat_id=ch, text="**Bot Restarted**")
            await k.delete()
        except:
            print("Make Your Bot Admin In File Channels With Full Rights")
    try:
        k = await TechVJBot.send_message(chat_id=AUTH_CHANNEL, text="**Bot Restarted**")
        await k.delete()
    except:
        print("Make Your Bot Admin In Force Subscribe Channel With Full Rights")
    if CLONE_MODE == True:
        print("Restarting All Clone Bots.......")
        await restart_bots()
        print("Restarted All Clone Bots.")
    app = web.AppRunner(await web_server())
    await app.setup()
    bind_address = "0.0.0.0"
    await web.TCPSite(app, bind_address, PORT).start()
    await idle()


if __name__ == '__main__':
    try:
        loop.run_until_complete(start())
    except KeyboardInterrupt:
        logging.info('Service Stopped Bye 👋')


