import os
import re
import time as tm
from core.database import db 
from .controls import parse_buttons
from .classify import title_in_whitelist, classify, get_filename

STATUS = {}


# ============================================================
# Caption cleaning - keep only the media/movie name line
# ============================================================
# Channel posts carry the real name buried between promo lines, e.g.
#     F I L E N A M E                      <- label (fancy letters)
#     Jai Ganesh (2025) (Hindi + Malayalam) Dual Audio ... ESub.mkv 📝
#     J O I N 👇  /  https://t.me/+xxxx ✅  /  ⚠ Download Karne Se Pehle ...
# With the toggle on, the forwarded copy's caption becomes just the name
# line: fancy Unicode letters are turned into plain text, emoji / links /
# @handles are removed, and everything else is dropped. Controlled per user
# by /settings -> Extra Settings -> "Caption: Filename Only" (config key
# `caption_first_line`, default True). Doesn't apply to "forward with tag"
# mode: Telegram can't edit captions on real forwards.
import html as _html
import unicodedata

_CAP_EXT = (
    "mkv|mp4|avi|webm|mov|m4v|wmv|flv|mp3|m4a|m4b|flac|wav|ogg|opus|aac|"
    "pdf|epub|zip|rar|7z|apk|srt|ass"
)
_CAP_EXT_RE = re.compile(rf"\.(?:{_CAP_EXT})(?![A-Za-z0-9])", re.IGNORECASE)
_CAP_URL_RE = re.compile(r"(?:https?://|t\.me/|www\.)\S+", re.IGNORECASE)
_CAP_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{3,}")
_CAP_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u23FF\u2460-\u24FF\u25A0-\u27BF"
    "\u2900-\u297F\u2B00-\u2BFF\u2000-\u200F\u2028-\u202F\u2060-\u206F\uFE00-\uFE0F"
    "\uFEFF\u3030\u303D\u3297\u3299]"
)
_CAP_MARKER_RE = re.compile(r"(?<![A-Za-z0-9])(?:s\d{1,2}e\d{1,3}|(?:19|20)\d{2})(?![A-Za-z0-9])", re.IGNORECASE)
_CAP_QUALITY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{3,4}p|4k|uhd|blu-?ray|bdrip|brrip|dvdrip|hdrip|hdtv|web-?dl|web-?rip|"
    r"remux|x26[45]|h\.?26[45]|hevc|av1|camrip|hdcam)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CAP_LABEL_PREFIX_RE = re.compile(r"^\W*(?:file\s*name|filename|name|title|movie)\s*[:=\-–—]+\s*", re.IGNORECASE)
# compact (letters only) lines that are labels/promo, not a title
_CAP_LABELS = {
    "filename", "filenames", "name", "title", "movie", "join", "joinnow", "download", "downloadnow",
    "size", "quality", "language", "audio", "subs", "subtitle", "subtitles", "info", "note", "links",
}
_CAP_PROMO_PREFIXES = (
    "join", "download", "watch", "support", "share", "subscribe", "warning", "note", "powered",
    "uploadedby", "channel", "backup", "request", "clickhere", "forbackup",
)
_SMALLCAPS = {
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g", "ʜ": "h", "ɪ": "i",
    "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ǫ": "q", "ʀ": "r",
    "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
}
_SMALLCAPS_TABLE = str.maketrans(_SMALLCAPS)


def _plain_text(line: str) -> str:
    """Fancy Unicode -> plain text: math/bold/italic letters (NFKC),
    regional-indicator "flag letters", small caps; strip emoji, zero-width
    characters, links and @handles; tidy spaces."""
    line = "".join(
        chr(ord(ch) - 0x1F1E6 + 65) if 0x1F1E6 <= ord(ch) <= 0x1F1FF else ch for ch in line
    )
    line = unicodedata.normalize("NFKC", line).translate(_SMALLCAPS_TABLE)
    line = _CAP_URL_RE.sub(" ", line)
    line = _CAP_HANDLE_RE.sub(" ", line)
    line = _CAP_EMOJI_RE.sub(" ", line)
    line = re.sub(r"\s{2,}", " ", line).strip()
    return re.sub(r"^[\s\-–—_:|~•·]+|[\s\-–—_:|~•·]+$", "", line)


def _compact(line: str) -> str:
    return re.sub(r"[\W_]+", "", line).lower()


def _is_label_or_promo(line: str) -> bool:
    c = _compact(line)
    return (not c) or c in _CAP_LABELS or c.startswith(_CAP_PROMO_PREFIXES)


def clean_caption(message, enabled=True):
    """Cleaned, HTML-safe caption text (the media/movie name line), or None
    when the toggle is off, there is no caption, or the caption is already
    just that line. Line choice, best first:
      1. a line ending in a file extension (".mkv", ".mp4", ".pdf" ...)
      2. a line with a year/SxxExx marker AND a quality tag (720p, WEB-DL...)
      3. the first line that isn't a label ("FILENAME"), link or promo
      4. the media's own file name
    """
    if not enabled:
        return None
    caption = getattr(message, "caption", None)
    if not caption:
        return None
    raw_lines = [ln for ln in str(caption).splitlines() if ln.strip()]
    lines = [t for t in (_plain_text(ln) for ln in raw_lines) if len(re.findall(r"\w", t)) >= 2]

    chosen = None
    for t in lines:                                   # 1. file extension
        exts = list(_CAP_EXT_RE.finditer(t))
        if exts:
            chosen = t[: exts[-1].end()]
            break
    if chosen is None:                                # 2. year/episode + quality
        for t in lines:
            if _CAP_MARKER_RE.search(t) and _CAP_QUALITY_RE.search(t) and not _is_label_or_promo(t):
                chosen = t
                break
    if chosen is None:                                # 3. first non-label line
        for t in lines:
            if not _is_label_or_promo(t):
                chosen = t
                break
    if chosen is None:                                # 4. the media's file name
        chosen = get_filename(message)
    if not chosen:
        return None

    chosen = _CAP_LABEL_PREFIX_RE.sub("", chosen).strip()
    if len(re.findall(r"\w", chosen)) < 2:
        return None
    if chosen == str(caption).strip():
        return None
    return _html.escape(chosen, quote=False)


_CAP_ANCHOR_RE = re.compile(r"<a\s+([^>]*)>(.*?)</a>", re.S | re.I)
_CAP_HREF_RE = re.compile(r'href="([^"]*)"', re.I)
_CAP_BARE_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/|telegram\.me/)[^\s<>\"']+", re.I)
_LINK_MARK = "\x00"


def _norm_url(u: str) -> str:
    return re.sub(r"^(https?://)?(www\.)?", "", u.strip().lower()).rstrip("/")


def remove_links_html(html_text: str) -> str:
    """Delete every link from a caption / message text (HTML) and keep
    everything else exactly as it is:
      * a hidden link ("<a href=..>Movie Name</a>") keeps its words;
      * a link that shows its own address (a visible URL) is removed;
      * bare URLs / t.me links in plain text are removed;
      * a line that held nothing but links disappears.
    Returns "" when nothing but links was there."""
    def _anchor(m):
        href = (_CAP_HREF_RE.search(m.group(1)) or [None, ""])[1]
        inner = m.group(2)
        plain = re.sub(r"<[^>]+>", "", inner).strip()
        shows_url = (
            _norm_url(plain) == _norm_url(href)
            or re.match(r"^(https?://|www\.|t\.me/)\S+$", plain, re.I)
        )
        return _LINK_MARK if shows_url else inner

    s = _CAP_ANCHOR_RE.sub(_anchor, html_text)
    # bare URLs in the text (never inside a tag)
    s = re.sub(
        r"(<[^>]*>)|(" + _CAP_BARE_URL_RE.pattern + r")",
        lambda m: m.group(1) or _LINK_MARK,
        s,
        flags=re.I,
    )
    out = []
    for line in s.split("\n"):
        if _LINK_MARK in line:
            line = re.sub(r"[ \t]{2,}", " ", line.replace(_LINK_MARK, "")).rstrip()
            if not line.strip():
                continue                      # that line was only a link
        out.append(line)
    return "\n".join(out).strip()


def _without_links(text_obj):
    """None = nothing to change (empty, or no links); else the text with
    links removed ("" = it was only links)."""
    if not text_obj:
        return None
    original = getattr(text_obj, "html", None) or str(text_obj)
    stripped = remove_links_html(original)
    return stripped if stripped != original.strip() else None


def caption_for_copy(message, remove_links: bool = False):
    """Caption to pass to copy_message: a string (HTML), or None = leave the
    original caption as it is.

    remove_links ON  -> the caption stays exactly the same, only the links
                        are removed from it.
    remove_links OFF -> clean_caption() (just the media/movie name line)."""
    if remove_links:
        return _without_links(getattr(message, "caption", None))
    return clean_caption(message, True)


def text_without_links(message):
    """Same for plain text messages: None = copy as is, str = send this text
    instead ("" = the message was only links)."""
    return _without_links(getattr(message, "text", None))


# ============================================================
# Real message filtering (message type / extension / keywords /
# file size / in-run duplicate detection).
# ============================================================
# These configs (see plugins/settings.py's Filters menu) were being saved
# to the database correctly, but nothing ever actually read them back
# during a forward - plugins/regix.py only unpacked the dict and never
# used it, and the "DUPLICATE"/"FILTERED" sentinel checks it had were
# dead code, since client.iter_messages() (plugins/test.py) never
# produces those strings. check_filters() below is what plugins/regix.py
# and plugins/autosort.py now actually call per-message so every toggle
# in that menu has a real effect, in both /fwd and /autosort.
_MESSAGE_TYPE_KEYS = ("text", "photo", "video", "audio", "voice", "document", "animation", "sticker", "poll")
_NAMED_MEDIA_ATTRS = ("document", "video", "audio", "animation")
_SIZED_MEDIA_ATTRS = ("document", "video", "audio", "animation", "photo", "voice")


def _message_type(message):
    for key in _MESSAGE_TYPE_KEYS:
        if getattr(message, key, None):
            return key
    return None


def _media_filename(message):
    for attr in _NAMED_MEDIA_ATTRS:
        media = getattr(message, attr, None)
        if media is not None:
            return getattr(media, "file_name", None)
    return None


def _media_size_bytes(message):
    for attr in _SIZED_MEDIA_ATTRS:
        media = getattr(message, attr, None)
        if media is not None:
            return getattr(media, "file_size", 0) or 0
    return 0


def _media_unique_id(message):
    for attr in _SIZED_MEDIA_ATTRS + ("sticker",):
        media = getattr(message, attr, None)
        if media is not None:
            return getattr(media, "file_unique_id", None)
    return None


# The whitelists (see below) only ever apply to content classify.py has
# actually identified as that kind of release - never to
# education/pdf/image/audio/files/other. This matters for two reasons:
#   1. It stops a lecture/course video from silently being blocked just
#      because its title (correctly) isn't in a movie/series whitelist.
#   2. It ALSO catches the reverse and more important case: education
#      content that classify() mistakenly promotes to "series" (a lecture
#      filename with a stray "S01E02"-looking number plus a quality tag
#      can trip guessit's episode detection - see classify.py's
#      _has_release_signal). Since that file is now *categorized* as
#      "series", the series whitelist DOES apply to it, and it gets
#      blocked unless its guessed title happens to equal a real
#      whitelisted show name - which is exactly what stops it from
#      reaching the series destination channel in /autosort.
#
# Movie and series are two INDEPENDENT lists (plugins/settings.py's
# "Movie Whitelist" and "Series Whitelist" screens, each with their own
# exact/loose mode) - a user can restrict one without touching the other.
# Anime rides along with the movie list since classify.py doesn't split
# anime out as its own /autosort category pairing the way series is.
_MOVIE_WHITELIST_CATEGORIES = {"movie", "movie_files", "anime", "anime_files"}
_SERIES_WHITELIST_CATEGORIES = {"series", "series_files"}


def check_filters(message, configs: dict, disabled_types, seen_file_ids=None):
    """Real implementation of the /settings -> Filters menu.

    Returns (allowed, reason). reason is 'filtered' or 'duplicate' when
    allowed is False, matching the stat keys plugins/utils.py's STS
    already tracks (sts.add('filtered') / sts.add('duplicate')).

    disabled_types: set/list of message-type keys the user unchecked
    (from `await db.get_filters(user_id)`).
    seen_file_ids: pass a set() to enable duplicate detection for this
    run (skips a file if its file_unique_id was already seen earlier in
    the SAME run) when the user has "Skip Duplicate" turned on; pass
    None to skip this check entirely. This is in-run only - there's no
    persistent store of what was forwarded in a *previous* run.
    """
    mtype = _message_type(message)
    if mtype and mtype in disabled_types:
        return False, "filtered"

    filename = _media_filename(message)
    if filename:
        ext = os.path.splitext(filename)[1].lower().lstrip(".")
        excluded_exts = configs.get("extension") or []
        if excluded_exts and ext in {e.lower().lstrip(".") for e in excluded_exts}:
            return False, "filtered"

        keywords = configs.get("keywords") or []
        if keywords and not any(kw.lower() in filename.lower() for kw in keywords):
            return False, "filtered"

        # Movie/anime and series name whitelists (see plugins/classify.py's
        # title_in_whitelist() and /settings -> Filters -> Movie/Series
        # Whitelist). Unlike "keywords" above (raw filename substring),
        # these compare against the guessit/anitopy-parsed title, so they
        # survive messy release names (including "S01E01"/"S02"-style TV
        # naming - the season/episode numbers live in separate guessit
        # fields and never end up inside the extracted title, so one
        # series whitelist entry like "Money Heist" matches every season/
        # episode of that show). Two independent lists, each with its own
        # exact/loose mode - see _MOVIE_WHITELIST_CATEGORIES /
        # _SERIES_WHITELIST_CATEGORIES above for why education/pdf/audio/
        # etc. are never touched by either.
        movie_whitelist = configs.get("movie_whitelist") or []
        series_whitelist = configs.get("series_whitelist") or []
        if movie_whitelist or series_whitelist:
            category = classify(message)
            if category in _MOVIE_WHITELIST_CATEGORIES and movie_whitelist:
                if not title_in_whitelist(
                    filename, movie_whitelist, exact=bool(configs.get("movie_whitelist_exact"))
                ):
                    return False, "filtered"
            elif category in _SERIES_WHITELIST_CATEGORIES and series_whitelist:
                if not title_in_whitelist(
                    filename, series_whitelist, exact=bool(configs.get("series_whitelist_exact"))
                ):
                    return False, "filtered"

    size_limit = configs.get("size_limit")
    if size_limit is not None:
        limit_mb = configs.get("file_size") or 0
        size_mb = _media_size_bytes(message) / (1024 * 1024)
        if size_limit is True and size_mb <= limit_mb:
            return False, "filtered"
        if size_limit is False and size_mb >= limit_mb:
            return False, "filtered"

    if seen_file_ids is not None:
        file_unique_id = _media_unique_id(message)
        if file_unique_id:
            if file_unique_id in seen_file_ids:
                return False, "duplicate"
            seen_file_ids.add(file_unique_id)

    return True, None


class STS:
    def __init__(self, id):
        self.id = id
        self.data = STATUS
    
    def verify(self):
        return self.data.get(self.id)
    
    def store(self, From, to,  skip, limit, continuous=False, category='all'):
        self.data[self.id] = {"FROM": From, 'TO': to, 'total_files': 0, 'skip': skip, 'limit': limit,
                      'fetched': skip, 'filtered': 0, 'deleted': 0, 'duplicate': 0, 'total': limit, 'start': 0, 'continuous': continuous, 'category': category}
        self.get(full=True)
        return STS(self.id)
        
    def get(self, value=None, full=False):
        values = self.data.get(self.id)
        if not full:
           return values.get(value)
        for k, v in values.items():
            setattr(self, k, v)
        return self

    def add(self, key=None, value=1, time=False):
        if time:
          return self.data[self.id].update({'start': tm.time()})
        self.data[self.id].update({key: self.get(key) + value}) 
    
    def divide(self, no, by):
       by = 1 if int(by) == 0 else by 
       return int(no) / by 
    
    async def get_data(self, user_id):
        bot = await db.get_bot(user_id)
        k, filters = self, await db.get_filters(user_id)
        size, configs = None, await db.get_configs(user_id)
        if configs['duplicate']:
           duplicate = [configs['db_uri'], self.TO]
        else:
           duplicate = False
        button = parse_buttons(configs['button'] if configs['button'] else '')
        if configs['file_size'] != 0:
            size = [configs['file_size'], configs['size_limit']]
        return bot, configs['caption'], configs['forward_tag'], {'chat_id': k.FROM, 'limit': k.limit, 'offset': k.skip, 'filters': filters,
                'keywords': configs['keywords'], 'media_size': size, 'extensions': configs['extension'], 'skip_duplicate': duplicate}, configs['protect'], button
