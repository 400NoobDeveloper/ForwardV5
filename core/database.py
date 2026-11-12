from os import environ 
from core.config import Config
import motor.motor_asyncio
from pymongo import MongoClient
import sys

async def mongodb_version():
    x = MongoClient(Config.DATABASE_URI)
    mongodb_version = x.server_info()['version']
    return mongodb_version

class Database:
    
    def __init__(self, uri, database_name):
        if not uri:
            print("ERROR: DATABASE environment variable is not set! Please set your MongoDB URI in Render environment variables.", flush=True)
            sys.exit(1)
        if "@" in uri and ":" not in uri.split("@")[0].split("//")[-1]:
            print("ERROR: MongoDB URI is missing password! Format should be: mongodb+srv://username:password@cluster.mongodb.net/", flush=True)
            sys.exit(1)
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.bot = self.db.bots
        self.col = self.db.users
        self.nfy = self.db.notify
        self.chl = self.db.channels 
        self.job = self.db.fwd_jobs   # running /fwd jobs, so they can resume after a restart
        self.lastrun = self.db.fwd_lastrun   # where the last cancelled/failed /fwd stopped
        
    def new_user(self, id, name):
        return dict(
            id = id,
            name = name,
            ban_status=dict(
                is_banned=False,
                ban_reason="",
            ),
        )
      
    async def add_user(self, id, name):
        user = self.new_user(id, name)
        await self.col.insert_one(user)
    
    async def is_user_exist(self, id):
        user = await self.col.find_one({'id':int(id)})
        return bool(user)
    
    async def total_users_bots_count(self):
        bcount = await self.bot.count_documents({})
        count = await self.col.count_documents({})
        return count, bcount

    async def total_channels(self):
        count = await self.chl.count_documents({})
        return count
    
    async def remove_ban(self, id):
        ban_status = dict(
            is_banned=False,
            ban_reason=''
        )
        await self.col.update_one({'id': id}, {'$set': {'ban_status': ban_status}})
    
    async def ban_user(self, user_id, ban_reason="No Reason"):
        ban_status = dict(
            is_banned=True,
            ban_reason=ban_reason
        )
        await self.col.update_one({'id': user_id}, {'$set': {'ban_status': ban_status}})

    async def get_ban_status(self, id):
        default = dict(
            is_banned=False,
            ban_reason=''
        )
        user = await self.col.find_one({'id':int(id)})
        if not user:
            return default
        return user.get('ban_status', default)

    async def get_all_users(self):
        return self.col.find({})
    
    async def delete_user(self, user_id):
        await self.col.delete_many({'id': int(user_id)})
 
    async def get_banned(self):
        users = self.col.find({'ban_status.is_banned': True})
        b_users = [user['id'] async for user in users]
        return b_users

    async def update_configs(self, id, configs):
        await self.col.update_one({'id': int(id)}, {'$set': {'configs': configs}})
         
    async def get_configs(self, id):
        default = {
            'caption': None,
            'duplicate': True,
            'caption_first_line': True,
            'remove_caption_link': False,
            'forward_tag': False,
            'file_size': 0,
            'size_limit': None,
            'extension': None,
            'keywords': None,
            'movie_whitelist': None,
            'movie_whitelist_exact': False,
            'series_whitelist': None,
            'series_whitelist_exact': False,
            'protect': None,
            'button': None,
            'db_uri': None,
            'category_channels': {},
            'tmdb_key': None,
            'tpdb_key': None,
            'anilist_enabled': True,
            'anilist_key': None,
            'openai_key': None,
            'filters': {
               'poll': True,
               'text': True,
               'audio': True,
               'voice': True,
               'video': True,
               'photo': True,
               'document': True,
               'animation': True,
               'sticker': True
            }
        }
        user = await self.col.find_one({'id':int(id)})
        if user:
            return user.get('configs', default)
        return default 
       
    async def add_bot(self, datas):
       if not await self.is_bot_exist(datas['user_id']):
          await self.bot.insert_one(datas)
    
    async def remove_bot(self, user_id):
       await self.bot.delete_many({'user_id': int(user_id)})
      
    async def get_bot(self, user_id: int):
       bot = await self.bot.find_one({'user_id': user_id})
       return bot if bot else None
                                          
    async def is_bot_exist(self, user_id):
       bot = await self.bot.find_one({'user_id': user_id})
       return bool(bot)
                                          
    async def in_channel(self, user_id: int, chat_id: int) -> bool:
       channel = await self.chl.find_one({"user_id": int(user_id), "chat_id": int(chat_id)})
       return bool(channel)
    
    async def add_channel(self, user_id: int, chat_id: int, title, username):
       channel = await self.in_channel(user_id, chat_id)
       if channel:
         return False
       return await self.chl.insert_one({"user_id": user_id, "chat_id": chat_id, "title": title, "username": username})
    
    async def remove_channel(self, user_id: int, chat_id: int):
       channel = await self.in_channel(user_id, chat_id )
       if not channel:
         return False
       return await self.chl.delete_many({"user_id": int(user_id), "chat_id": int(chat_id)})
    
    async def get_channel_details(self, user_id: int, chat_id: int):
       return await self.chl.find_one({"user_id": int(user_id), "chat_id": int(chat_id)})
       
    async def get_user_channels(self, user_id: int):
       channels = self.chl.find({"user_id": int(user_id)})
       return [channel async for channel in channels]
     
    async def get_filters(self, user_id):
       filters = []
       filter = (await self.get_configs(user_id))['filters']
       for k, v in filter.items():
          if v == False:
            filters.append(str(k))
       return filters

    async def get_category_channels(self, user_id: int) -> dict:
       """{category: chat_id} for every auto-sort category this user has
       configured a destination channel for (see plugins/settings.py's
       'Auto-Sort Channels' section and plugins/autosort.py)."""
       configs = await self.get_configs(user_id)
       return configs.get('category_channels') or {}

    async def set_category_channel(self, user_id: int, category: str, chat_id: int):
       current = await self.get_configs(user_id)
       cat_channels = current.get('category_channels') or {}
       cat_channels[category] = int(chat_id)
       current['category_channels'] = cat_channels
       await self.update_configs(user_id, current)

    async def remove_category_channel(self, user_id: int, category: str):
       current = await self.get_configs(user_id)
       cat_channels = current.get('category_channels') or {}
       cat_channels.pop(category, None)
       current['category_channels'] = cat_channels
       await self.update_configs(user_id, current)

    async def get_tmdb_key(self, user_id: int) -> str:
       """Per-user TMDB API key, set via /settings -> TMDB (see plugins/settings.py).
       Used by plugins/classify.py to refine movie/series/anime detection."""
       configs = await self.get_configs(user_id)
       return configs.get('tmdb_key') or ''

    async def set_tmdb_key(self, user_id: int, key: str):
       current = await self.get_configs(user_id)
       current['tmdb_key'] = key
       await self.update_configs(user_id, current)

    async def remove_tmdb_key(self, user_id: int):
       current = await self.get_configs(user_id)
       current['tmdb_key'] = None
       await self.update_configs(user_id, current)

    async def get_tpdb_key(self, user_id: int) -> str:
       """Per-user ThePornDB API token (/settings -> Private Files -> ThePornDB).
       Used by plugins/classify.py's tpdb_category() to recognise adult scenes
       that the NSFW keyword check missed."""
       configs = await self.get_configs(user_id)
       return configs.get('tpdb_key') or ''

    async def set_tpdb_key(self, user_id: int, key: str):
       current = await self.get_configs(user_id)
       current['tpdb_key'] = key
       await self.update_configs(user_id, current)

    async def remove_tpdb_key(self, user_id: int):
       current = await self.get_configs(user_id)
       current['tpdb_key'] = None
       await self.update_configs(user_id, current)

    async def get_anilist_enabled(self, user_id: int) -> bool:
       """Per-user AniList toggle (/settings -> AniList). No API key needed.
       ON unless the user switched it off (also for users with older configs)."""
       configs = await self.get_configs(user_id)
       value = configs.get('anilist_enabled')
       return True if value is None else bool(value)

    async def set_anilist_enabled(self, user_id: int, enabled: bool):
       current = await self.get_configs(user_id)
       current['anilist_enabled'] = bool(enabled)
       await self.update_configs(user_id, current)

    async def get_anilist_key(self, user_id: int) -> str:
       """Optional per-user AniList access token (/settings -> AniList -> SET KEY).
       AniList works without it; when set it is sent as a Bearer token."""
       configs = await self.get_configs(user_id)
       return configs.get('anilist_key') or ''

    async def set_anilist_key(self, user_id: int, key: str):
       current = await self.get_configs(user_id)
       current['anilist_key'] = key
       await self.update_configs(user_id, current)

    async def remove_anilist_key(self, user_id: int):
       current = await self.get_configs(user_id)
       current['anilist_key'] = None
       await self.update_configs(user_id, current)

    async def get_openai_key(self, user_id: int) -> str:
       """Per-user OpenAI API key, set via /settings -> Private Files -> OpenAI.
       Used by plugins/classify.py only for videos that would otherwise land in Other."""
       configs = await self.get_configs(user_id)
       return configs.get('openai_key') or ''

    async def set_openai_key(self, user_id: int, key: str):
       current = await self.get_configs(user_id)
       current['openai_key'] = key
       await self.update_configs(user_id, current)

    async def remove_openai_key(self, user_id: int):
       current = await self.get_configs(user_id)
       current['openai_key'] = None
       await self.update_configs(user_id, current)
              
    async def add_frwd(self, user_id):
       return await self.nfy.insert_one({'user_id': int(user_id)})
    
    async def rmve_frwd(self, user_id=0, all=False):
       data = {} if all else {'user_id': int(user_id)}
       return await self.nfy.delete_many(data)
    
    async def get_all_frwd(self):
       return self.nfy.find({})

    # ---- resumable /fwd jobs (progress is saved so a restart continues, not starts over) ----
    async def save_job(self, user_id, data):
       data = dict(data)
       data['user_id'] = int(user_id)
       await self.job.replace_one({'user_id': int(user_id)}, data, upsert=True)

    async def update_job(self, user_id, last_id, forwarded, last_fwd_id=None):
       try:
          fields = {'last_id': int(last_id), 'forwarded': int(forwarded)}
          if last_fwd_id is not None:
             fields['last_fwd_id'] = int(last_fwd_id)      # number of the last message really forwarded
          await self.job.update_one({'user_id': int(user_id)}, {'$set': fields})
       except Exception as e:
          print(f"update_job failed: {e}", flush=True)

    async def update_job_status(self, user_id, snap):
       """Remember the numbers shown on the 'Forwarded Status' message, so after a
       restart the same message can be shown again with 'Bot Restarted' on it."""
       try:
          await self.job.update_one({'user_id': int(user_id)}, {'$set': {'snap': dict(snap)}})
       except Exception as e:
          print(f"update_job_status failed: {e}", flush=True)

    async def set_job_resume(self, user_id, resume_id):
       await self.job.update_one({'user_id': int(user_id)}, {'$set': {'resume_id': resume_id}})

    async def get_job(self, user_id):
       return await self.job.find_one({'user_id': int(user_id)})

    async def get_all_jobs(self):
       return self.job.find({})

    async def save_last_run(self, user_id, data):
       data = dict(data)
       data['user_id'] = int(user_id)
       await self.lastrun.replace_one({'user_id': int(user_id)}, data, upsert=True)

    async def get_last_run(self, user_id):
       return await self.lastrun.find_one({'user_id': int(user_id)})

    async def clear_last_run(self, user_id):
       try:
          await self.lastrun.delete_many({'user_id': int(user_id)})
       except Exception as e:
          print(f"clear_last_run failed: {e}", flush=True)

    async def remove_job(self, user_id):
       try:
          await self.job.delete_many({'user_id': int(user_id)})
       except Exception as e:
          print(f"remove_job failed: {e}", flush=True)
     
db = Database(Config.DATABASE_URI, Config.DATABASE_NAME)
