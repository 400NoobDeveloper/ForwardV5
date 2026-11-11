import asyncio
import os
import logging
import logging.config
import time
from types import SimpleNamespace
import aiohttp
from aiohttp import web
import pyromod.listen  # adds Client.ask() / Client.listen() used by /fwd, /settings, ...
from pyrogram import Client, idle, __version__
from pyrogram.raw.all import layer
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait
from core.database import db
from core.config import Config

# pyrofork 2.3.x removed Message.forward_from_chat / forward_from / forward_date /
# forward_from_message_id (it only has Message.forward_origin now). The code in
# handlers/ still reads the old names (set target chat, /fwd source,
# /autosort, bot-token forward ...), so give Message those names back.
from pyrogram.types import Message as _Message

def _fwd_origin(m):
    return getattr(m, "forward_origin", None)

if not hasattr(_Message, "forward_date"):
    _Message.forward_date = property(lambda m: getattr(_fwd_origin(m), "date", None))
if not hasattr(_Message, "forward_from_chat"):
    _Message.forward_from_chat = property(
        lambda m: getattr(_fwd_origin(m), "chat", None) or getattr(_fwd_origin(m), "sender_chat", None))
if not hasattr(_Message, "forward_from_message_id"):
    _Message.forward_from_message_id = property(lambda m: getattr(_fwd_origin(m), "message_id", None))
if not hasattr(_Message, "forward_from"):
    _Message.forward_from = property(lambda m: getattr(_fwd_origin(m), "sender_user", None))

logging.config.fileConfig(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core', 'logging.conf'))
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)

class Bot(Client): 
    def __init__(self):
        super().__init__(
            Config.BOT_SESSION,
            api_hash=Config.API_HASH,
            api_id=Config.API_ID,
            plugins={
                "root": "handlers"
            },
            workers=50,
            bot_token=Config.BOT_TOKEN
        )
        self.log = logging

    def load_plugins(self):
        """Pyrogram loads only one folder, but the handlers are split between
        handlers/ and plugins/ (regix.py), so load both."""
        for root in ("handlers", "plugins"):
            self.plugins = {"root": root}
            super().load_plugins()

    def patch_start_text(self):
        """/start text: say "update channel" instead of "news channel", whatever
        version of handlers/start.py is deployed."""
        try:
            import handlers.start as _start
            _original = _start._build_start_text

            async def _build_start_text(client):
                text = await _original(client)
                return text.replace("news channel", "update channel")

            _start._build_start_text = _build_start_text
        except Exception as e:
            logging.error(f"Could not patch /start text: {e}")

    async def start(self):
        await super().start()
        self.patch_start_text()
        me = await self.get_me()
        logging.info(f"{me.first_name} with for pyrogram v{__version__} (Layer {layer}) started on @{me.username}.")
        self.id = me.id
        self.username = me.username
        self.first_name = me.first_name
        self.set_parse_mode(ParseMode.DEFAULT)
        text = "**๏[-ิ_•ิ]๏ bot restarted !**"
        logging.info(text)

        # Check if database URI is default broken one
        if "mongodb+srv://chhjgjkkjhkjhkjh@cluster0.xowzpr4.mongodb.net/" in Config.DATABASE_URI:
             logging.error("You have not set the DATABASE environment variable. The bot will not function correctly.")
             return

        try:
            success = failed = 0
            # users whose /fwd was interrupted get a richer message + automatic resume (below)
            jobs = [job async for job in await db.get_all_jobs()]
            job_users = {job['user_id'] for job in jobs}
            users = await db.get_all_frwd()
            async for user in users:
               chat_id = user['user_id']
               if chat_id in job_users:
                  continue
               try:
                  await self.send_message(chat_id, text)
                  success += 1
               except FloodWait as e:
                  await asyncio.sleep(e.value + 1)
                  await self.send_message(chat_id, text)
                  success += 1
               except Exception:
                  failed += 1

            if (success + failed) != 0 or jobs:
               await db.rmve_frwd(all=True)
               logging.info(f"Restart message status"
                     f"success: {success}"
                     f"failed: {failed}")
            if jobs:
               await self.resume_jobs(jobs)
        except Exception as e:
            logging.error(f"Failed to send restart messages or connect to DB: {e}")

    async def show_restart_status(self, job):
        """Show the usual 'Forwarded Status' message with 'Bot Restarted' as its
        status. The old status message (the one with the stale Cancel button) is
        edited; if it can't be found a new one is sent."""
        from plugins.controls import Translation
        try:
            from core.restart_info import build_restart_info
        except ModuleNotFoundError:
            from plugins.restart_info import build_restart_info
        user = job['user_id']
        snap = job.get('snap') or {}
        forwarded = int(job.get('forwarded') or 0)
        last_id = int(job.get('last_id') or 0)
        limit = int(job.get('limit') or 0)
        fetched = int(snap.get('fetched') or last_id)
        pct = snap.get('percentage')
        if pct is None:
            pct = "{:.1f}".format(min(fetched * 100 / limit, 100.0) if limit > 0 else 0.0)
        progress = snap.get('progress')
        if not progress:
            n = int(float(pct) // 10)
            progress = "◉" + "◉" * n + "◎" * (10 - n)
        text = Translation.TEXT.format(
            fetched, forwarded, int(snap.get('duplicate') or 0), int(snap.get('deleted') or 0),
            int(snap.get('skip') or 0), "ʙᴏᴛ ʀᴇsᴛᴀʀᴛᴇᴅ ♻️", pct, "Resuming...", progress)
        # + continue-from message number, last forwarded number and the active filters
        text += build_restart_info(job, snap)
        chat, msg_id = job.get('status_chat'), job.get('status_msg')
        if chat and msg_id:
            try:
                await self.edit_message_text(chat, msg_id, text)   # no buttons -> old Cancel button is removed
                return
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                try:
                    await self.edit_message_text(chat, msg_id, text)
                    return
                except Exception:
                    pass
            except Exception as e:
                logging.error(f"Could not edit old status message for {user}: {e}")
        await self.send_message(user, text)

    async def resume_jobs(self, jobs):
        """Tell each interrupted user how much was already forwarded and continue
        from the last forwarded message (not from the beginning)."""
        from plugins.regix import pub_
        from plugins.utils import STS

        async def _noop(*args, **kwargs):
            return None

        for job in jobs:
            user = job['user_id']
            try:
                last_id = int(job.get('last_id') or 0)
                forwarded = int(job.get('forwarded') or 0)
                # 1) the usual "Forwarded Status" message, with "Bot Restarted" on it
                await self.show_restart_status(job)
                # 2) a fresh message that the resumed task will use for its live progress
                text = (f"**๏[-ิ_•ิ]๏ bot restarted !**\n\n"
                        f"♻️ Already forwarded: `{forwarded}`\n"
                        f"▶️ Resuming from message: `{last_id + 1}`")
                m = await self.send_message(user, text)
                forward_id = f"{user}-r{int(time.time()) % 100000}"
                STS(forward_id).store(job['FROM'], job['TO'], last_id, int(job['limit']),
                                      continuous=bool(job.get('continuous')),
                                      category=job.get('category') or 'all')
                await db.set_job_resume(user, forward_id)
                # same code path as pressing the "Yes" button of /fwd
                fake_click = SimpleNamespace(from_user=SimpleNamespace(id=user),
                                             data=f"start_public_{forward_id}",
                                             message=m, answer=_noop)
                asyncio.create_task(pub_(self, fake_click))
                logging.info(f"Resuming /fwd for {user} from message {last_id + 1} ({forwarded} already forwarded)")
            except FloodWait as e:
                logging.error(f"FloodWait while resuming job for {user}: {e.value}s")
            except Exception as e:
                logging.error(f"Could not resume job for {user}: {e}", exc_info=True)

    async def stop(self, *args):
        msg = f"@{self.username} stopped. Bye."
        await super().stop()
        logging.info(msg)

# Calculate uptime
START_TIME = time.time()

def get_uptime():
    elapsed = time.time() - START_TIME
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"

async def web_server():
    async def handle(request):
        uptime = get_uptime()
        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Bot Status</title>
            <style>
                body {{
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    background-color: #f0f2f5;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                }}
                .container {{
                    background-color: white;
                    padding: 40px;
                    border-radius: 12px;
                    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
                    text-align: center;
                    max-width: 400px;
                    width: 100%;
                }}
                h1 {{
                    color: #1a73e8;
                    margin-bottom: 20px;
                }}
                p {{
                    color: #555;
                    font-size: 18px;
                    margin: 10px 0;
                }}
                .status-active {{
                    color: #28a745;
                    font-weight: bold;
                }}
                .footer {{
                    margin-top: 30px;
                    font-size: 14px;
                    color: #888;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Bot is Running</h1>
                <p>Status: <span class="status-active">Active</span></p>
                <p>Uptime: {uptime}</p>
                <div class="footer">
                    Powered by Auto Forward Bot V2
                </div>
            </div>
        </body>
        </html>
        """
        return web.Response(text=html_content, content_type='text/html')

    app = web.Application()
    app.add_routes([web.get('/', handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Web server started on port {port}")

async def ping_server():
    while True:
        await asyncio.sleep(300) # Ping every 5 minutes
        try:
            port = int(os.environ.get('PORT', 8080))
            url = f'http://127.0.0.1:{port}'
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    logging.info(f"Self-ping to {url}: Status {resp.status}")
        except Exception as e:
            logging.error(f"Self-ping failed: {e}")

async def main():
    # pyromod 1.5 grabs the event loop when it is imported, but asyncio.run() creates a
    # new one, so bot.listen() / bot.ask() failed with "attached to a different loop"
    # (set target chat, add caption, /fwd questions ... never answered). Point pyromod
    # at the loop that is really running.
    import sys
    sys.modules["pyromod.listen.listen"].loop = asyncio.get_running_loop()

    # Start web server first so the host sees the port open immediately
    await web_server()

    bot = Bot()
    await bot.start()

    # Start self-ping task
    asyncio.create_task(ping_server())

    await idle()
    await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
