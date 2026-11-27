"""plugins/channels.py - caption/filter helpers for /fwd + /autosort and the Auto-Sort channel creator.

Two parts, merged from the old plugins/utils.py and plugins/autosort_create.py:

1. Caption cleaning, /settings -> Filters (check_filters) and the STS task-status class.
   Used by handlers/forward.py (/fwd), handlers/autosort.py (/autosort), handlers/xsetting.py
   and plugins/analysis.py (/autosortai).

2. /settings -> Extra Settings -> AUTOSORT. Creates one PRIVATE Telegram channel (or supergroup)
   for every Auto-Sort category that has no channel yet, makes the forwarding identity an admin
   there, and saves each new chat id with db.set_category_channel() - the SAME mapping /autosort
   already reads.

   A Telegram BOT cannot create chats, so this uses a separate user-account STRING SESSION that
   is kept in the user's configs under "autosort_session" (core/database.py). That session is used
   ONLY here: it is never added to /settings -> Bots, never used by /autosort, and is never
   logged, printed or written to a file (in_memory client, scrubbed error texts).

   Plain functions only - the callback branches live in handlers/xsetting.py ("asmenu",
   "asback", "assession", "assessionset", "assessionreset", "assessiontest", "ascreate",
   "ascreatetype_channel" / "ascreatetype_group", "asgo", "ascancel").
"""

import asyncio
import html
import html as _html
import logging
import os
import random
import re
import time
import time as tm
import unicodedata

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.types import ChatPrivileges, InlineKeyboardButton, InlineKeyboardMarkup

from core.config import Config
from core.database import db
from plugins.controls import AUTOSORT_CATEGORIES, SESSION_STRING_SIZE, parse_buttons
from plugins.identify import title_in_whitelist, classify, get_filename

try:
    from pyromod.listen.listen import ListenerCanceled
except Exception:                                 # pyromod missing: never raised
    class ListenerCanceled(Exception):
        pass


# ======================================================================
# PART 1 - captions, /settings -> Filters, task status (was plugins/utils.py)
# ======================================================================
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


def _pick_name_line(message):
    """Plain (not HTML-escaped) name line of a message: the caption line that holds the media / movie
    name, else the media's own file name, else None. Line choice, best first:
      1. a line ending in a file extension (".mkv", ".mp4", ".pdf" ...)
      2. a line with a year/SxxExx marker AND a quality tag (720p, WEB-DL...)
      3. the first line that isn't a label ("FILENAME"), link or promo
      4. the media's own file name
    """
    caption = getattr(message, "caption", None)
    lines = []
    if caption:
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
    return chosen or None


def clean_caption(message, enabled=True):
    """Cleaned, HTML-safe caption text (the media/movie name line), or None
    when the toggle is off, there is no caption, or the caption is already
    just that line. See _pick_name_line() for how the line is chosen."""
    if not enabled:
        return None
    caption = getattr(message, "caption", None)
    if not caption:
        return None
    chosen = _pick_name_line(message)
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
_LINK_TLDS = (
    r"(?:com|net|org|in|me|co|cc|to|ws|tv|xyz|club|site|info|biz|link|pro|lol|ink|ltd|vip|top|fun|live|"
    r"store|app|cloud|tech|mobi|online|art|one|tf|lat|cam|rest|bar|mov|wtf|life|fit|ai|io|pw|gg|fyi|red|"
    r"nl|uk|us|ru|cx|mx|pm|ag|ac|bz|vc|sh|so|ist|zip|ly|gl|ph|dog|page|run|dev|ge|is|gd|cutt\.ly)"
)
# a link written as text: scheme://..., tg://..., t.me/..., www.x.y, and a domain WITH a path (bit.ly/abc,
# example.com/page). A bare "name.com" with no path is NOT touched here: it is usually part of a file name
# ("...-4kHdHub.Com.mkv"); when Telegram itself made it a link it arrives as an <a> tag and goes below.
_CAP_BARE_URL_RE = re.compile(
    r"(?:(?:https?|tg|ftp)://|www\.|t\.me/|telegram\.(?:me|dog)/|telegra\.ph/)[^\s<>\"']+"
    r"|(?<![\w@./-])(?:[A-Za-z0-9-]+\.)+" + _LINK_TLDS + r"/[^\s<>\"']*",
    re.I,
)
_LINK_MARK = "\x00"
_TIDY_EXT = r"(?:mkv|mp4|avi|webm|mov|m4v|wmv|flv|mp3|m4a|flac|pdf|zip|rar|7z|srt|ass)"


def _norm_url(u: str) -> str:
    return re.sub(r"^(https?://)?(www\.)?", "", u.strip().lower()).rstrip("/")


def remove_links_html(html_text: str) -> str:
    """Delete every link from a caption / message text (HTML) and keep
    everything else exactly as it is:
      * a hidden link ("<a href=..>Movie Name</a>") keeps its words (only the link goes);
      * a link that shows its own address (a visible URL) is removed;
      * bare URLs (https://, tg://, t.me/, www., domain/path) in plain text are removed;
      * a line that held nothing but links disappears;
      * what a removed link leaves behind is tidied ("Join :" -> "Join", "AV1-.mkv" -> "AV1.mkv").
    Returns "" when nothing but links was there."""
    def _anchor(m):
        href = (_CAP_HREF_RE.search(m.group(1)) or [None, ""])[1]
        inner = m.group(2)
        plain = re.sub(r"<[^>]+>", "", inner).strip()
        shows_url = (
            _norm_url(plain) == _norm_url(href)
            or re.match(r"^(?:(?:https?|tg|ftp)://|www\.|t\.me/|telegram\.(?:me|dog)/)\S+$", plain, re.I)
            or re.match(r"^(?:[A-Za-z0-9-]+\.)+" + _LINK_TLDS + r"(?:/\S*)?$", plain, re.I)
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
            line = line.replace(_LINK_MARK, "")
            line = re.sub(r"[ \t]{2,}", " ", line)
            line = re.sub(r"(?i)[-_.]+(\.%s)(?![A-Za-z0-9])" % _TIDY_EXT, r"\1", line)   # "AV1-.mkv" -> "AV1.mkv"
            line = re.sub(r"(?:\s*[:\-–—|~•>»➤➜]+)+\s*$", "", line).rstrip()              # dangling "Join :" / "Link -"
            line = re.sub(r"^\s*(?:[:\-–—|~•>»➤➜]+\s*)+", "", line)                         # dangling start
            line = re.sub(r"(?:\(\s*\)|\[\s*\]|\{\s*\})", "", line).strip()                  # empty brackets
            if not re.sub(r"<[^>]+>", "", line).strip():
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
# /autosort caption
# ============================================================
# /autosort rewrites the caption of a MOVIE / SERIES / ANIME file (and their "_files" document variants)
# into a short, uniform caption built from the name found in the caption (the file name is the fallback):
#     movie            Sahasam (2025)
#                      1080p • WEB-DL • Malayalam
#     series / anime   My Hero Academia S06E15 - Tartarus
#                      1080p • WEB-DL • NF • HEVC • 10bit • Hindi + English • DDP5.1 • ESub
# (plain text: no bold or other formatting)
# A video the classifier left in "other" but whose name is clearly a release (year / SxxExx + BluRay / WEB-DL / x264 ...)
# gets the same caption, built from its file name.
# Every other kind of file (education, pdf, files, audio, image, other ...) keeps its caption exactly as it is:
# only the LINKS are removed from it, the words stay. The rule is decided by the category the classifier gave
# the file (before the Movie / Series / Anime gates), so a real movie that a gate sent to Other is still captioned.
# When no usable name can be found, nothing is rebuilt: the original caption (links removed) is used.
#
# Round 3 (Step 1 of the caption): when the classifier IDENTIFIED the file (info["ident"], plugins/identify.py), the caption
# is rebuilt from that record instead of from the raw release name - see ident_caption():
#     movie   Title (Year)                      + quality tags
#     series  Title (Year) S01E05 | S01 | S01-S03 + quality tags
#     anime   AniList title (Year) S01E05        + quality tags
# The title / year are the canonical TMDB / AniList ones; the season / episode come from the FILE NAME first and from the
# caption lines only for series and anime (never for a movie). parse_edited_caption() reads such a caption back (Step 2's
# consistency check in handlers/autosort.py). Not identified: the file-name caption above is used, exactly as before.
CAPTION_FROM_IDENT = os.environ.get("AUTOSORT_CAPTION_FROM_IDENT", "1").strip().lower() not in ("0", "false", "no", "off")
CAPTION_SEASON_RANGE = os.environ.get("CAPTION_SEASON_RANGE", "1").strip().lower() not in ("0", "false", "no", "off")
CAPTION_BARE_EPISODE = os.environ.get("CAPTION_BARE_EPISODE", "1").strip().lower() not in ("0", "false", "no", "off")
# CAPTION_DOMAIN_STRICT=1 (default, /autosort captions only): a "site tag" is at most name.tld ("4kHdHub.Com", "www.1TamilMV.tf"); a
# dotted title that happens to end in ".In" / ".To" ("13th.Some.Lessons.Arent.Taught.In.Classrooms") is no longer cut off. 0 = old.
CAPTION_DOMAIN_STRICT = os.environ.get("CAPTION_DOMAIN_STRICT", "1").strip().lower() not in ("0", "false", "no", "off")
# CAPTION_EP_VERSION=1 (default): "S01E07v2" (a fansub version suffix) is read as S01E07v2, and the caption keeps "v2". 0 = old.
CAPTION_EP_VERSION = os.environ.get("CAPTION_EP_VERSION", "1").strip().lower() not in ("0", "false", "no", "off")
# CAPTION_GLUED_HANDLE=1 (default): "@MovierockerzsSpider_Man_2002_..." keeps "Spider_Man_2002_..." (the handle swallowed the
# title's first word: it ends at the last lowercase -> UPPERCASE boundary); before, the whole run was removed as one @handle.
CAPTION_GLUED_HANDLE = os.environ.get("CAPTION_GLUED_HANDLE", "1").strip().lower() not in ("0", "false", "no", "off")
_CAP_GLUED_RE = re.compile(r"^\s*@([A-Za-z0-9]{3,})(?=[A-Za-z0-9]*_)")


def _unglue_handle(text: str) -> str:
    """CAPTION_GLUED_HANDLE (used by the /autosort caption parser only). Never raises."""
    try:
        if not CAPTION_GLUED_HANDLE or not text:
            return text
        m = _CAP_GLUED_RE.match(text)
        if not m:
            return text
        word = m.group(1)
        cuts = [i for i in range(3, len(word)) if word[i - 1].islower() and word[i].isupper()]
        return (word[cuts[-1]:] + text[m.end():]) if cuts else text
    except Exception:
        return text
_MEDIA_MOVIE = ("movie", "movie_files")
_MEDIA_EPISODIC = ("series", "series_files", "anime", "anime_files")

_LANGS = (
    "Hindi", "English", "Tamil", "Telugu", "Malayalam", "Kannada", "Bengali", "Bangla", "Marathi", "Punjabi",
    "Gujarati", "Urdu", "Bhojpuri", "Odia", "Japanese", "Korean", "Chinese", "Mandarin", "Cantonese", "Spanish",
    "French", "German", "Russian", "Thai", "Arabic", "Turkish", "Italian", "Indonesian", "Portuguese",
)
_LANG_ALT = "|".join(_LANGS)
_LANG_RE = re.compile(rf"(?<![A-Za-z0-9])({_LANG_ALT})(?![A-Za-z0-9])", re.I)
_LANG_TAIL_RE = re.compile(
    rf"[\(\[]\s*((?:{_LANG_ALT})(?:\s*[+&,/-]\s*(?:{_LANG_ALT}))*)\s*[\)\]]\s*$", re.I)
_RES_RE = re.compile(r"(?<![A-Za-z0-9])(2160p|1440p|1080p|720p|576p|540p|480p|360p|4k|uhd)(?![A-Za-z])", re.I)
_SRC_RE = re.compile(
    r"(?<![A-Za-z0-9])(web[ -]?dl|web[ -]?rip|blu[ -]?ray|bd[ -]?rip|br[ -]?rip|hd[ -]?rip|dvd[ -]?rip|hdtv|"
    r"hd[ -]?cam|cam[ -]?rip|hdts|remux)(?![A-Za-z0-9])", re.I)
_PLATFORM_RE = re.compile(
    r"(?<![A-Za-z0-9])(nf|amzn|dsnp|zee5|jiohs|sonyliv|sliv|hulu|atvp|hmax|pcok|pmtp|altb|sunnxt)(?![A-Za-z0-9])", re.I)
_CODEC_RE = re.compile(r"(?<![A-Za-z0-9])(x\.?265|h[ .]?265|hevc|x\.?264|h[ .]?264|avc|av1)(?![A-Za-z0-9])", re.I)
_BIT_RE = re.compile(r"(?<![A-Za-z0-9])10[ -]?bit(?![A-Za-z0-9])", re.I)
_HDR_RE = re.compile(r"(?<![A-Za-z0-9])(hdr10\+?|hdr|dolby[ ]?vision)(?![A-Za-z0-9])", re.I)
_AUDIO_RE = re.compile(
    r"(?<![A-Za-z0-9])(ddp|dd\+|dd|eac3|ac3|aac|dts-hd|dts|truehd|atmos|flac|opus)[ .]?(\d\.\d)?(?![A-Za-z])", re.I)
_SUB_RE = re.compile(r"(?<![A-Za-z0-9])(e-?subs?|m-?subs?|multi[ ]?subs?|hard[ ]?subs?|subbed)(?![A-Za-z0-9])", re.I)
_DUB_RE = re.compile(r"(?<![A-Za-z0-9])(dual[ ]audio|multi[ ]audio|tri[ ]audio|dubbed)(?![A-Za-z0-9])", re.I)
_EP_RE = re.compile(
    r"(?<![A-Za-z0-9])S(\d{1,2})[ ]?E(\d{1,4})(?:[ ]?[-–][ ]?E?(\d{1,4})|E(\d{1,4}))?"
    + (r"(v\d)?" if CAPTION_EP_VERSION else r"()") + r"(?![A-Za-z0-9])", re.I)
_EP_X_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})x(\d{2,3})(?![A-Za-z0-9])", re.I)
_EP_WORD_RE = re.compile(r"(?<![A-Za-z0-9])(?:ep|episode)[ ]?(\d{1,4})(?![A-Za-z0-9])", re.I)
_EP_ABS_RE = re.compile(r"\s-\s(\d{1,4})(?:v\d)?(?=\s|$)")
_EP_BARE_RE = re.compile(r"(?<![A-Za-z0-9])E(\d{2,4})(?![A-Za-z0-9])", re.I)          # "One Piece E1090", "Attack on Titan E05"
_SEASON_RE = re.compile(r"(?<![A-Za-z0-9])(?:season[ ]?(\d{1,2})|S(\d{1,2}))(?![A-Za-z0-9])", re.I)
_YEAR_PAREN_RE = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
_YEAR_RE = re.compile(r"(?<![A-Za-z0-9])((?:19|20)\d{2})(?![A-Za-z0-9])")
# a season-pack RANGE: "S01-S03", "S01 to S03", "Season 1-3", "Seasons 1 & 2" (the second part needs an S / season word or
# comes after the word "season", so "S01-720p" and "S01-05" are not ranges)
_SEASON_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:seasons?[ ]?(\d{1,2})[ ]?(?:-|–|to|&)[ ]?(?:s(?:eason)?[ ]?)?(\d{1,2})"
    r"|S(\d{1,2})[ ]?(?:-|–|to)[ ]?S(\d{1,2}))(?![A-Za-z0-9])", re.I)
_NAME_DOMAIN_RE = re.compile(
    r"(?<![\w])(?:www\.)?\w+(?:\.\w+)*?\.(?:com|net|org|in|me|co|cc|to|tv|xyz|club|site|info|biz|link|pro|vip|top|"
    r"live|cloud|app|io|ws|lol|tf|ink|cam|mov|ai|pw|gg|life|fun|online|store|tech|world|one)(?![A-Za-z0-9])", re.I)
_LEAD_TAG_RE = re.compile(r"^\s*[\[\(【「][^\]\)】」]{1,40}[\]\)】」]\s*(?=\S)")
_AUDIO_NORM = {"ddp": "DDP", "dd+": "DD+", "dd": "DD", "eac3": "EAC3", "ac3": "AC3", "aac": "AAC",
               "dts-hd": "DTS-HD", "dts": "DTS", "truehd": "TrueHD", "atmos": "Atmos", "flac": "FLAC", "opus": "Opus"}


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        k = it.lower()
        if it and k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _domain_repl(m) -> str:
    """CAPTION_DOMAIN_STRICT: remove a site tag only when it is name.tld (www. not counted); longer dotted runs are a title."""
    try:
        if CAPTION_DOMAIN_STRICT:
            body = re.sub(r"^www\.", "", m.group(0), flags=re.I)
            if body.count(".") > 1:
                return m.group(0)
    except Exception:
        pass
    return " "


def _spaced(text: str) -> str:
    """Release-style text -> readable text: "Blue.Eye.Samurai.S01E03.720p" -> "Blue Eye Samurai S01E03 720p".
    A dot between two digits stays ("AAC5.1", "DDP2.0", "H.265" keeps its form through _CODEC_RE)."""
    text = _CAP_EXT_RE.sub("", text) if _CAP_EXT_RE.search(text[-6:]) else text
    text = _NAME_DOMAIN_RE.sub(_domain_repl, text)                 # site tags: 4kHdHub.Com, 1TamilMV.tf
    text = re.sub(r"(?<=\D)\.(?=\S)|(?<=\S)\.(?=\D)|_", " ", text)   # separators, but not 5.1 / 2.0
    text = re.sub(r"(?<=[A-Za-z])-(?=\d{3,4}p)", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _parse_media_name(raw: str, episodic: bool):
    """Pull name / year / season-episode / episode title / tags out of one release-style line (a caption
    line or a file name). Returns a dict, or None when no usable name is there."""
    text = _spaced(_plain_text(_unglue_handle(raw)))
    text = _CAP_LABEL_PREFIX_RE.sub("", text).strip()
    for _ in range(3):                                              # leading fansub / uploader tag: [SubsPlease]
        stripped = _LEAD_TAG_RE.sub("", text)
        if stripped == text or len(re.findall(r"[A-Za-z]", stripped)) < 2:
            break
        text = stripped
    text = re.sub(r"^\s*[\-–—:|~]+\s*", "", text)
    if not text:
        return None

    # ---- episode markers
    ep = season = episode = None
    ep_m = _EP_RE.search(text)
    ep_pos = None
    if ep_m:
        season, episode = int(ep_m.group(1)), int(ep_m.group(2))
        last = ep_m.group(3) or ep_m.group(4)
        ep = f"S{season:02d}E{episode:02d}" + (f"-E{int(last):02d}" if last else "") + (ep_m.group(5) or "").lower()
        ep_pos = (ep_m.start(), ep_m.end())
    else:
        xm = _EP_X_RE.search(text)
        if xm:
            season, episode = int(xm.group(1)), int(xm.group(2))
            ep, ep_pos = f"S{season:02d}E{episode:02d}", (xm.start(), xm.end())
        elif episodic:
            wm = _EP_WORD_RE.search(text)
            bm = _EP_BARE_RE.search(text) if (CAPTION_BARE_EPISODE and not wm) else None
            if wm:
                ep, ep_pos = f"E{int(wm.group(1)):02d}", (wm.start(), wm.end())
            elif bm:
                ep, ep_pos = f"E{int(bm.group(1)):02d}", (bm.start(), bm.end())
            else:
                am = _EP_ABS_RE.search(text)
                if am:
                    ep, ep_pos = f"E{int(am.group(1)):02d}", (am.start(), am.end())
    season_pos = None
    if ep is None and episodic and CAPTION_SEASON_RANGE:
        rm = _SEASON_RANGE_RE.search(text)
        if rm:
            lo, hi = int(rm.group(1) or rm.group(3)), int(rm.group(2) or rm.group(4))
            if 1 <= lo < hi <= 99:
                ep, season_pos = f"S{lo:02d}-S{hi:02d}", (rm.start(), rm.end())
    if ep is None and episodic:
        sm = _SEASON_RE.search(text)
        if sm:
            ep = f"S{int(sm.group(1) or sm.group(2)):02d}"
            season_pos = (sm.start(), sm.end())

    # ---- year
    year, year_pos = None, None
    pm = _YEAR_PAREN_RE.search(text)
    if pm and pm.start() > 0:
        year, year_pos = pm.group(1), (pm.start(), pm.end())
    else:
        bare = [m for m in _YEAR_RE.finditer(text) if m.start() > 0]
        if bare and not episodic:
            m = bare[-1]
            year, year_pos = m.group(1), (m.start(), m.end())
        elif bare:
            m = bare[0]
            year, year_pos = m.group(1), (m.start(), m.end())

    # ---- where the name stops: the first marker or tag
    cuts = []
    for pos in (ep_pos, season_pos, year_pos):
        if pos:
            cuts.append(pos[0])
    for rx in (_RES_RE, _SRC_RE, _CODEC_RE, _BIT_RE, _HDR_RE, _SUB_RE, _DUB_RE):
        m = rx.search(text)
        if m and m.start() > 0:
            cuts.append(m.start())
    cut = min(cuts) if cuts else len(text)
    name = text[:cut]
    tail = text[cut:]

    langs = []
    tm = _LANG_TAIL_RE.search(name)                                  # "Name (Hindi + Malayalam) 2025"
    if tm:
        langs += _LANG_RE.findall(tm.group(1))
        name = name[: tm.start()]
    name = re.sub(r"[\s\-–—_:|~.,(\[{【「]+$", "", name)
    name = re.sub(r"^[\s\-–—_:|~.,)\]}】」]+", "", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    if len(re.findall(r"\w", name)) < 2:
        return None

    # ---- episode title (series / anime): text between the episode marker and the first tag
    ep_title = None
    if episodic and ep_pos:
        rest = text[ep_pos[1]:]
        stops = [m.start() for rx in (_RES_RE, _SRC_RE, _CODEC_RE, _BIT_RE, _HDR_RE, _SUB_RE, _DUB_RE, _AUDIO_RE,
                                      _PLATFORM_RE, _LANG_RE)
                 for m in [rx.search(rest)] if m]
        stops += [i for i in (rest.find("["), rest.find("("), rest.find("{")) if i != -1]
        rest = rest[: min(stops)] if stops else rest
        rest = re.sub(r"^[\s\-–—_:|~.,]+|[\s\-–—_:|~.,]+$", "", rest)
        if len(re.findall(r"[A-Za-z]", rest)) >= 2 and len(rest) <= 70:
            ep_title = rest

    # ---- tags (only after the name)
    res = _RES_RE.search(tail)
    res = (res.group(1).upper() if res.group(1).lower() in ("4k", "uhd") else res.group(1).lower()) if res else None
    src = _SRC_RE.search(tail)
    if src:
        k = re.sub(r"[ -]", "", src.group(1).lower())
        src = {"webdl": "WEB-DL", "webrip": "WEBRip", "bluray": "BluRay", "bdrip": "BDRip", "brrip": "BRRip",
               "hdrip": "HDRip", "dvdrip": "DVDRip", "hdtv": "HDTV", "hdcam": "HDCAM", "camrip": "CAMRip",
               "hdts": "HDTS", "remux": "Remux"}.get(k, src.group(1))
    plat = _dedupe([m.upper() for m in _PLATFORM_RE.findall(tail)])[:1]
    codecs = []
    for c in _CODEC_RE.findall(tail):
        c = re.sub(r"[ .]", "", c.lower())
        codecs.append("HEVC" if c in ("x265", "h265", "hevc") else "AV1" if c == "av1" else "x264")
    codecs = _dedupe(codecs)
    bit = "10bit" if _BIT_RE.search(tail) else None
    hdr = _HDR_RE.search(tail)
    hdr = ("Dolby Vision" if hdr.group(1).lower().startswith("dolby") else hdr.group(1).upper()) if hdr else None
    audio = _dedupe([_AUDIO_NORM.get(a.lower(), a) + (q or "") for a, q in _AUDIO_RE.findall(tail)])
    langs += _LANG_RE.findall(tail)
    langs = _dedupe([l.capitalize() for l in langs])
    dub = _DUB_RE.search(tail)
    dub = re.sub(r"\s+", " ", dub.group(1)).title() if dub else None
    sub = "ESub" if re.search(r"e-?subs?", tail, re.I) else ("MSub" if re.search(r"m-?subs?|multi[ ]?subs?", tail, re.I)
                                                              else None)

    tags = [res, src] + plat + codecs + [bit, hdr]
    lang_txt = " + ".join(langs) if langs else None
    if dub and not (dub.lower().endswith("audio") and len(langs) >= 2):
        lang_txt = f"{lang_txt} ({dub})" if lang_txt else dub
    tags += [lang_txt, " + ".join(audio[:3]) if audio else None, sub]
    tags = [t for t in tags if t]
    return {"name": name, "year": year, "ep": ep, "ep_title": ep_title, "tags": tags,
            "tech": bool(src or codecs),
            "score": bool(year) + 2 * bool(ep) + len(tags)}


_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v", ".wmv", ".flv", ".ts")


def media_caption(message, classified):
    """Plain-text caption (HTML-escaped, no tags) for a movie / series / anime file, or None when it cannot be built.
    classified = movie / series / anime (+ "_files"): the category decides the layout.
    classified = "other": a VIDEO file the classifier could not place (TMDB had no match ...) is still captioned from its
    file name when the name is clearly a release - a year or SxxExx AND a source / codec tag (BluRay, WEB-DL, x264 ...);
    SxxExx -> series layout, otherwise movie layout. A lecture / random video has no such name and is left alone."""
    auto = classified == "other"
    if not auto and classified not in _MEDIA_MOVIE + _MEDIA_EPISODIC:
        return None
    fn = get_filename(message)
    if auto and not (getattr(message, "video", None) is not None or (fn and fn.lower().endswith(_VIDEO_EXTS))):
        return None
    episodic = classified in _MEDIA_EPISODIC
    candidates = []
    caption = getattr(message, "caption", None)
    if caption:                                                     # 1. the name in the caption
        for ln in str(caption).splitlines():
            t = _plain_text(ln)
            if len(re.findall(r"\w", t)) >= 3 and not _is_label_or_promo(t):
                candidates.append(t)
    if fn:                                                          # 2. the file name
        candidates.append(fn)
    best, best_src = None, None
    for c in candidates[:12]:
        info = _parse_media_name(c, episodic)
        if auto and info and not (info["tech"] and (info["year"] or info["ep"])):
            continue                                                # not clearly a release name
        if info and (best is None or info["score"] > best["score"]):   # a tie keeps the earlier (caption) line
            best, best_src = info, c
    if not best or best["score"] <= 0:
        return None
    if auto:
        episodic = bool(best["ep"])
        if episodic:                                                # parse again with the episode-title rules on
            best = _parse_media_name(best_src, True) or best
    esc = lambda x: _html.escape(x, quote=False)
    if episodic:
        head = esc(best["name"])
        if best["ep"]:
            head += " " + best["ep"]
            if best["ep_title"]:
                head += " - " + esc(best["ep_title"])
        elif best["year"]:
            head += f" ({best['year']})"
    else:
        head = esc(best["name"]) + (f" ({best['year']})" if best["year"] else "")
    out = head                                  # plain text, no bold / formatting tags
    if best["tags"]:
        out += "\n" + esc(" • ".join(best["tags"]))
    return out


def autosort_caption(message, classified=None, remove_links: bool = True):
    """Caption /autosort passes to copy_message: an HTML string, or None = keep the original caption.
      movie / series / anime (+ "_files")  -> media_caption(): name, year or season-episode, quality ...
      everything else                      -> the caption as it is, only the links removed
    A caption that cannot be rebuilt falls back to the second line. remove_links=False leaves links alone."""
    built = media_caption(message, classified) if classified else None
    if built:
        return built
    return _without_links(getattr(message, "caption", None)) if remove_links else None


_IDENT_KINDS = ("movie", "series", "anime")


def _ep_numbers(ep):
    """"S01E05" -> (1, 5); "S01E05-E06" -> (1, 5); "S01" -> (1, None); "S01-S03" -> (1, None); "E05" -> (None, 5); None -> (None, None)."""
    if not ep:
        return None, None
    m = re.match(r"S(\d+)(?:E(\d+))?", ep, re.I) or re.match(r"()E(\d+)", ep, re.I)
    if not m:
        return None, None
    return (int(m.group(1)) if m.group(1) else None), (int(m.group(2)) if m.group(2) else None)


def ident_caption(message, classified, ident):
    """Step 1 of the /autosort caption. -> (caption, facts), or (None, None) when nothing can be built (then the caller keeps the
    file-name caption of autosort_caption()). `ident` is info["ident"] of plugins/identify.py: {kind, title, year, source, id
    [, season]}. The title and year are the CANONICAL ones of the identified TMDB / AniList record; the quality tags (resolution,
    source, platform, codec, HDR, language, audio, subs) come from the name; the season / episode come from the file name first,
    then - for series and anime only, NEVER for a movie - from the caption lines. A year the record lacks is taken from the name.
    Plain text (no bold, no links), HTML-escaped. facts = {kind, title, year, ep, season, episode} is what Step 2 checks the
    caption against. Never raises."""
    try:
        if not CAPTION_FROM_IDENT or not ident or not classified:
            return None, None
        base = classified[:-6] if classified.endswith("_files") else classified
        if base not in _IDENT_KINDS or ident.get("kind") != base:
            return None, None
        title = re.sub(r"\s+", " ", str(ident.get("title") or "")).strip()
        if len(re.findall(r"\w", title)) < 2:
            return None, None
        episodic = base != "movie"
        names = []
        fn = get_filename(message)
        if fn:
            names.append(fn)                                          # 1. the file name
        caption = getattr(message, "caption", None)
        if caption:                                                   # 2. the caption lines
            for ln in str(caption).splitlines():
                t = _plain_text(ln)
                if len(re.findall(r"\w", t)) >= 3 and not _is_label_or_promo(t):
                    names.append(t)
        parses = [p for p in (_parse_media_name(n, episodic) for n in names[:13]) if p]
        if not parses:
            return None, None
        tags_src = max(enumerate(parses), key=lambda ip: (len(ip[1]["tags"]), -ip[0]))[1]   # most tags, the earlier name wins a tie
        year = ident.get("year") if isinstance(ident.get("year"), int) else None
        if year is None:
            year = next((int(p["year"]) for p in parses if p["year"]), None)
        ep = None
        if episodic:
            ep = next((p["ep"] for p in parses if p["ep"]), None)
            if ep and re.fullmatch(r"E\d+", ep) and isinstance(ident.get("season"), int):
                ep = f"S{ident['season']:02d}{ep}"                      # anime: a bare "E05" + the season the AniList title names
        head = title + (f" ({year})" if year else "") + (f" {ep}" if ep else "")
        out = _html.escape(head, quote=False)
        if tags_src["tags"]:
            out += "\n" + _html.escape(" • ".join(tags_src["tags"]), quote=False)
        season, episode = _ep_numbers(ep)
        return out, {"kind": base, "title": title, "year": year, "ep": ep, "season": season, "episode": episode}
    except Exception:
        return None, None


def parse_edited_caption(caption):
    """Step 2 helper: read the FIRST line of an edited caption (ident_caption() output) back with the same parser the caption
    rules use -> {"year": int | None, "ep": str | None, "season": int | None, "episode": int | None, "episodic": bool}, or None
    when it cannot be read. "episodic" = the caption carries an episode / season marker (so it is not a movie caption)."""
    try:
        first = _html.unescape(str(caption or "").split("\n", 1)[0])
        p = _parse_media_name(first, True)
        if not p:
            return None
        season, episode = _ep_numbers(p["ep"])
        return {"year": int(p["year"]) if p["year"] else None, "ep": p["ep"], "season": season, "episode": episode,
                "episodic": bool(p["ep"])}
    except Exception:
        return None


# ============================================================
# Real message filtering (message type / extension / keywords /
# file size / in-run duplicate detection).
# ============================================================
# These configs (see plugins/settings.py's Filters menu) were being saved
# to the database correctly, but nothing ever actually read them back
# during a forward - handlers/forward.py only unpacked the dict and never
# used it, and the "DUPLICATE"/"FILTERED" sentinel checks it had were
# dead code, since client.iter_messages() (plugins/test.py) never
# produces those strings. check_filters() below is what handlers/forward.py
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


# The whitelists (see below) only ever apply to content identify.py has
# actually identified as that kind of release - never to
# education/pdf/image/audio/files/other. This matters for two reasons:
#   1. It stops a lecture/course video from silently being blocked just
#      because its title (correctly) isn't in a movie/series whitelist.
#   2. It ALSO catches the reverse and more important case: education
#      content that classify() mistakenly promotes to "series" (a lecture
#      filename with a stray "S01E02"-looking number plus a quality tag
#      can trip guessit's episode detection - see identify.py's
#      _has_release_signal). Since that file is now *categorized* as
#      "series", the series whitelist DOES apply to it, and it gets
#      blocked unless its guessed title happens to equal a real
#      whitelisted show name - which is exactly what stops it from
#      reaching the series destination channel in /autosort.
#
# Movie and series are two INDEPENDENT lists (plugins/settings.py's
# "Movie Whitelist" and "Series Whitelist" screens, each with their own
# exact/loose mode) - a user can restrict one without touching the other.
# Anime rides along with the movie list since identify.py doesn't split
# anime out as its own /autosort category pairing the way series is.
_MOVIE_WHITELIST_CATEGORIES = {"movie", "movie_files", "anime", "anime_files"}
_SERIES_WHITELIST_CATEGORIES = {"series", "series_files"}


def check_filters(message, configs: dict, disabled_types, seen_file_ids=None):
    """Real implementation of the /settings -> Filters menu.

    Returns (allowed, reason). reason is 'filtered' or 'duplicate' when
    allowed is False, matching the stat keys plugins/channels.py's STS
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

        # Movie/anime and series name whitelists (see plugins/identify.py's
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



# ======================================================================
# PART 2 - Auto-Sort channel creator (was plugins/autosort_create.py)
# ======================================================================
logger = logging.getLogger(__name__)

AUTOSORT_CREATE_DELAY = 5          # seconds between two chat creations (a random 80-120 % of it is used)
TITLE_LIMIT = 128                  # Telegram: chat title 1-128 characters
ABOUT_LIMIT = 255                  # Telegram: chat description up to 255 characters
PREFIX_LIMIT = 60                  # longest name prefix accepted
MIN_EDIT_GAP = 1.0                 # progress message is edited at most about once a second
LONG_FLOOD = 60                    # FloodWait below this: sleep and retry once; at or above: stop the run

# user_id -> {"cancel": bool, "cfg": {...}} while that user's creation run is going
RUNNING: dict = {}
# user_id -> {"kind": "channel" | "group", "prefix": str} between "type / prefix" and "Confirm"
PENDING: dict = {}

# errors after which creating more chats cannot work: stop the run, keep what was done
_STOP_IDS = {
    "CHANNELS_TOO_MUCH": "this account is already in too many channels / groups (CHANNELS_TOO_MUCH)",
    "USER_RESTRICTED": "Telegram has restricted this account (USER_RESTRICTED)",
    "PEER_FLOOD": "Telegram is limiting this account right now (PEER_FLOOD)",
    "USER_DEACTIVATED": "this account is deactivated (USER_DEACTIVATED)",
    "USER_DEACTIVATED_BAN": "this account is banned (USER_DEACTIVATED_BAN)",
    "AUTH_KEY_UNREGISTERED": "the session is no longer valid - RESET it and set a new one",
    "AUTH_KEY_DUPLICATED": "the session is being used somewhere else - RESET it and set a new one",
    "SESSION_REVOKED": "the session was revoked - RESET it and set a new one",
    "SESSION_EXPIRED": "the session expired - RESET it and set a new one",
}

_MARKS = {"wait": "⏳", "make": "🛠", "ok": "✅", "admin": "⚠️", "fail": "❌"}


# ======================================================================
# small helpers
# ======================================================================
def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _back(data: str = "settings#asmenu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn('ʙᴀᴄᴋ', data)]])


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _fmt_wait(seconds) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    parts = ([f"{h}h"] if h else []) + ([f"{m}m"] if m else []) + ([f"{s}s"] if s or not (h or m) else [])
    return " ".join(parts)


def _error_id(e) -> str:
    """Telegram's error name (CHANNELS_TOO_MUCH ...) - class attribute ID, else found in the text,
    else the class name. Never contains anything but an error name."""
    eid = getattr(e, "ID", None)
    if isinstance(eid, str) and eid:
        return eid
    m = re.search(r"[A-Z][A-Z0-9_]{4,}", str(e))
    return m.group(0) if m else type(e).__name__


def _scrub(err, secret: str = "") -> str:
    """Error text that is safe to show: the string session is cut out and the text is short."""
    text = f"{type(err).__name__}: {err}"
    if secret:
        text = text.replace(secret, "***")
    return text[:200]


def looks_like_session(text) -> bool:
    """Same idea as CLIENT.add_session(): a Pyrogram string session is a long url-safe base64 text
    (at least SESSION_STRING_SIZE characters, no spaces). The real proof is get_me() later."""
    s = (text or "").strip()
    return len(s) >= SESSION_STRING_SIZE and re.fullmatch(r"[A-Za-z0-9_\-]+={0,2}", s) is not None


def make_client(user_id: int, session: str) -> Client:
    """The creation client: unique name, nothing written to disk, no update handling."""
    return Client(
        f"AUTOSORT_{user_id}",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        session_string=session,
        in_memory=True,
        no_updates=True,
    )


async def _stop(client) -> None:
    try:
        await client.stop()
    except Exception:
        pass


async def check_session(user_id: int, session: str):
    """Start the session, read the account, stop it. -> (me, None) or (None, error text).
    Never raises; the error text never contains the string."""
    client = make_client(user_id, session)
    try:
        await client.start()
        me = await client.get_me()
        if getattr(me, "is_bot", False):
            return None, "this is a BOT session - use the string session of a normal user account"
        return me, None
    except Exception as e:
        return None, _scrub(e, session)
    finally:
        await _stop(client)


def _who(me) -> str:
    name = " ".join(p for p in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if p) or "-"
    user = getattr(me, "username", None)
    return f"{_esc(name)}" + (f" (@{_esc(user)})" if user else "")


async def safe_edit(message, text: str, markup=None, final: bool = False):
    """edit_text that never raises. MessageNotModified is ignored; a FloodWait skips a progress
    edit (the next one carries the news) but is waited out for a final edit."""
    for _ in range(2):
        try:
            return await message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except MessageNotModified:
            return None
        except FloodWait as e:
            if not final or e.value > 30:
                return None
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            logger.debug("autosort create: edit failed: %s", type(e).__name__)
            return None
    return None


async def unset_categories(user_id: int) -> list:
    """[(key, label)] of the AUTOSORT_CATEGORIES that have no channel yet, in list order."""
    mapping = await db.get_category_channels(user_id)
    return [(key, label) for key, label in AUTOSORT_CATEGORIES if not mapping.get(key)]


def chat_title(prefix: str, label: str) -> str:
    prefix = (prefix or "").strip()
    return (f"{prefix} • {label}" if prefix else label)[:TITLE_LIMIT]


# ======================================================================
# pages (text, markup)
# ======================================================================
async def menu_page(user_id: int):
    has_session = bool(await db.get_autosort_session(user_id))
    mapping = await db.get_category_channels(user_id)
    done = sum(1 for key, _label in AUTOSORT_CATEGORIES if mapping.get(key))
    text = (
        "<b><u>🗂 AUTOSORT</u></b>\n\n"
        "<b>Creates a private channel (or group) for every Auto-Sort category that has none yet, "
        "makes your bot an admin there and saves it in Auto-Sort Channels - so /autosort works "
        "without any manual step.\n"
        "Telegram bots cannot create chats, so this needs the string session of your own user "
        "account (only used here).</b>\n\n"
        f"<b>String session:</b> {'set ✅' if has_session else 'not set ❌'}\n"
        f"<b>Categories with a channel:</b> {done}/{len(AUTOSORT_CATEGORIES)}"
    )
    markup = InlineKeyboardMarkup([
        [_btn('sᴇssɪᴏɴ', 'settings#assession'), _btn('ᴄʀᴇᴀᴛᴇ', 'settings#ascreate')],
        [_btn('ʙᴀᴄᴋ', 'settings#asback')],
    ])
    return text, markup


async def session_page(user_id: int):
    has_session = bool(await db.get_autosort_session(user_id))
    if has_session:
        text = ("<b><u>🗂 AUTOSORT - SESSION</u></b>\n\n"
                "<b>String session:</b> set ✅ <code>••••••••</code>\n"
                "<b>RESET deletes it completely. TEST starts it once and shows the account name.</b>")
        rows = [[_btn('ʀᴇsᴇᴛ', 'settings#assessionreset'), _btn('ᴛᴇsᴛ', 'settings#assessiontest')]]
    else:
        text = ("<b><u>🗂 AUTOSORT - SESSION</u></b>\n\n"
                "<b>String session:</b> not set ❌\n"
                "<b>Tap SET and send the Pyrogram string session of your own user account.</b>")
        rows = [[_btn('sᴇᴛ', 'settings#assessionset')]]
    rows.append([_btn('ʙᴀᴄᴋ', 'settings#asmenu')])
    return text, InlineKeyboardMarkup(rows)


def no_session_page():
    text = ("<b><u>🗂 AUTOSORT</u></b>\n\n"
            "<b>No string session is saved yet. Set one first - it is needed to create the chats.</b>")
    return text, InlineKeyboardMarkup([[_btn('sᴇᴛ', 'settings#assessionset')], [_btn('ʙᴀᴄᴋ', 'settings#asmenu')]])


def type_page():
    text = ("<b><u>🗂 AUTOSORT - CREATE</u></b>\n\n"
            "<b>Channels or Groups?</b>\n"
            "Both work as /autosort destinations. They are created private (no username).")
    return text, InlineKeyboardMarkup([
        [_btn('📢 ᴄʜᴀɴɴᴇʟs', 'settings#ascreatetype_channel'), _btn('👥 ɢʀᴏᴜᴘs', 'settings#ascreatetype_group')],
        [_btn('ʙᴀᴄᴋ', 'settings#asmenu')],
    ])


def all_set_page():
    return ("<b><u>🗂 AUTOSORT</u></b>\n\n<b>Everything is already set - every category has a channel.</b>",
            _back())


def confirm_page(kind: str, prefix: str, todo: list):
    lines = "\n".join(f"• {_esc(chat_title(prefix, label))}" for _key, label in todo)
    text = (
        "<b><u>🗂 AUTOSORT - CONFIRM</u></b>\n\n"
        f"<b>Type:</b> {'Channels' if kind == 'channel' else 'Groups'} (private)\n"
        f"<b>Prefix:</b> {('<code>' + _esc(prefix) + '</code>') if prefix else 'none'}\n"
        f"<b>To create ({len(todo)}):</b>\n{lines}\n\n"
        "<i>Categories that already have a channel are left untouched. "
        "Each chat is made with a pause of a few seconds to stay clear of Telegram limits.</i>"
    )
    return text, InlineKeyboardMarkup([[_btn('ᴄᴏɴꜰɪʀᴍ', 'settings#asgo'), _btn('ᴄᴀɴᴄᴇʟ', 'settings#asmenu')]])


# ======================================================================
# SET / TEST the string session
# ======================================================================
async def set_session_flow(bot, user_id: int):
    """Warning -> wait for the string -> delete the user's message at once -> validate -> save.
    Every outcome is shown on the prompt message; the string itself is never shown or logged."""
    back = _back("settings#assession")
    prompt = await bot.send_message(
        user_id,
        "<b>❪ SET AUTOSORT STRING SESSION ❫</b>\n\n"
        "<b>⚠️ WARNING ⚠️</b>\n"
        "<code>A string session is FULL ACCESS to your Telegram account. Use your OWN account only. "
        "There is a chance that Telegram limits or bans an account that creates many chats. "
        "It is used ONLY to create the Auto-Sort chats, is not shared with /autosort, and you can "
        "delete it any time with RESET.</code>\n\n"
        "<b>Send the Pyrogram string session now.</b>\n"
        "/cancel - cancel this process",
        parse_mode=ParseMode.HTML,
    )
    try:
        reply = await bot.listen(chat_id=user_id, timeout=300)
    except asyncio.TimeoutError:
        return await safe_edit(prompt, "Process has been automatically cancelled", back, final=True)
    except ListenerCanceled:
        return
    secret = (reply.text or "").strip()
    try:                                  # the message holds the secret: delete it before anything else
        await reply.delete()
    except Exception:
        pass
    if secret == "/cancel":
        return await safe_edit(prompt, "<b>process canceled</b>", back, final=True)
    if not looks_like_session(secret):
        return await safe_edit(
            prompt, "<b>❌ That does not look like a Pyrogram string session.</b>\n\nNot saved.", back, final=True)
    await safe_edit(prompt, "<code>Checking the session...</code>")
    me, error = await check_session(user_id, secret)
    if error:
        return await safe_edit(prompt, f"<b>❌ Session rejected:</b>\n<code>{_esc(error)}</code>\n\nNot saved.",
                               back, final=True)
    await db.set_autosort_session(user_id, secret)
    secret = ""
    await safe_edit(prompt, f"<b>✅ Session saved.</b>\nAccount: <b>{_who(me)}</b>", back, final=True)


async def test_session(user_id: int, message):
    session = await db.get_autosort_session(user_id)
    back = _back("settings#assession")
    if not session:
        return await safe_edit(message, "<b>No string session is saved.</b>", back, final=True)
    await safe_edit(message, "<code>Starting the session...</code>")
    me, error = await check_session(user_id, session)
    if error:
        return await safe_edit(message, f"<b>❌ Session not working:</b>\n<code>{_esc(error)}</code>", back, final=True)
    await safe_edit(message, f"<b>✅ Session works.</b>\nAccount: <b>{_who(me)}</b>", back, final=True)


# ======================================================================
# CREATE: type -> prefix -> confirm
# ======================================================================
async def prefix_flow(bot, user_id: int, kind: str):
    """Ask for the optional name prefix, then show the confirmation (only the categories that
    are not set yet). The chosen type and prefix wait in PENDING until Confirm."""
    PENDING.pop(user_id, None)
    back = _back()
    if not await unset_categories(user_id):
        text, markup = all_set_page()
        return await bot.send_message(user_id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
    prompt = await bot.send_message(
        user_id,
        "<b>❪ AUTOSORT NAME PREFIX ❫</b>\n\n"
        "<b>Send a prefix for the chat names, e.g. <code>My Library</code> - the chats are then called "
        "<code>My Library • Movies (Video)</code> and so on.</b>\n"
        "/skip - no prefix\n"
        "/cancel - cancel this process",
        parse_mode=ParseMode.HTML,
    )
    try:
        reply = await bot.listen(chat_id=user_id, timeout=300)
    except asyncio.TimeoutError:
        return await safe_edit(prompt, "Process has been automatically cancelled", back, final=True)
    except ListenerCanceled:
        return
    text = (reply.text or "").strip()
    try:
        await reply.delete()
    except Exception:
        pass
    if text == "/cancel":
        return await safe_edit(prompt, "<b>process canceled</b>", back, final=True)
    if text.lower() == "/skip":
        prefix = ""
    elif text and not text.startswith("/"):
        prefix = text[:PREFIX_LIMIT]
    else:
        return await safe_edit(prompt, "<b>Invalid. Send a plain text prefix, or /skip.</b>", back, final=True)
    todo = await unset_categories(user_id)
    if not todo:
        page, markup = all_set_page()
        return await safe_edit(prompt, page, markup, final=True)
    PENDING[user_id] = {"kind": kind, "prefix": prefix}
    page, markup = confirm_page(kind, prefix, todo)
    await safe_edit(prompt, page, markup, final=True)


def claim_run(user_id: int):
    """Called by the Confirm button. NO await in here: the lock is taken before anything else can
    run, so a second tap can never start a second run. -> (cfg | None, error text | None)."""
    if user_id in RUNNING:
        return None, "A creation run is already in progress."
    cfg = PENDING.pop(user_id, None)
    if not cfg:
        return None, "This button has expired. Open AUTOSORT -> CREATE again."
    RUNNING[user_id] = {"cancel": False, "cfg": cfg}
    return cfg, None


def request_cancel(user_id: int) -> bool:
    state = RUNNING.get(user_id)
    if not state:
        return False
    state["cancel"] = True
    return True


# ======================================================================
# creation run
# ======================================================================
async def _flood_retry(make_call):
    """await make_call(); a FloodWait below LONG_FLOOD is slept out and the call tried ONCE more.
    A long FloodWait (or a second one) is raised to the caller."""
    try:
        return await make_call()
    except FloodWait as e:
        if e.value >= LONG_FLOOD:
            raise
        await asyncio.sleep(e.value + 1)
        return await make_call()


async def _create_chat(client, kind: str, title: str, about: str):
    if kind == "channel":
        return await client.create_channel(title, about)
    return await client.create_supergroup(title, about)


def _privileges(kind: str) -> ChatPrivileges:
    if kind == "channel":
        return ChatPrivileges(can_manage_chat=True, can_post_messages=True, can_edit_messages=True,
                              can_delete_messages=True, can_invite_users=True)
    return ChatPrivileges(can_manage_chat=True, can_delete_messages=True, can_invite_users=True,
                          can_pin_messages=True)


async def promote(client, chat_id, ident: dict, kind: str):
    """Make ident ({"id", "username"}) an admin of chat_id. -> (ok, reason).

    The creation client is a fresh in-memory session that has never seen the bot, so a bare
    numeric id cannot be resolved; the @username is tried first (that lookup also teaches the
    client the peer), the id second. Direct promote first, then add + promote."""
    targets = []
    if ident.get("username"):
        targets.append("@" + str(ident["username"]).lstrip("@"))
    if ident.get("id"):
        targets.append(int(ident["id"]))
    if not targets:
        return False, "no id / username saved"
    privileges = _privileges(kind)
    reason = "unknown error"
    for target in targets:
        try:
            await _flood_retry(lambda: client.promote_chat_member(chat_id, target, privileges=privileges))
            return True, None
        except FloodWait as e:
            return False, f"FloodWait {_fmt_wait(e.value)}"
        except Exception as e:
            reason = _error_id(e)
        try:
            await _flood_retry(lambda: client.add_chat_members(chat_id, target))
            await _flood_retry(lambda: client.promote_chat_member(chat_id, target, privileges=privileges))
            return True, None
        except FloodWait as e:
            return False, f"FloodWait {_fmt_wait(e.value)}"
        except Exception as e:
            reason = _error_id(e)
    return False, reason


async def forwarding_identities(user_id: int, creator_id, ui_me):
    """Who has to be admin in the new chats. -> (primary, extra, note)

    primary - the identity /autosort posts with (db.get_bot): a bot token -> that bot; a userbot
              session -> that account, unless it is the SAME account as the creation session
              (then it already owns the chat and nothing is needed).
    extra   - the bot that runs this settings UI, when it is a different one.
    note    - "no_forwarder" (nothing saved in /settings -> Bots), "same_account", or None."""
    saved = await db.get_bot(user_id)
    saved_id = saved.get("id") if saved else None
    primary, note = None, None
    if not saved:
        note = "no_forwarder"
    elif saved.get("is_bot") == False and saved_id == creator_id:       # same test as CLIENT.client()
        note = "same_account"
    else:
        primary = {"id": saved_id, "username": saved.get("username")}
    extra = None
    ui_id = getattr(ui_me, "id", None)
    if ui_id and ui_id != saved_id and ui_id != creator_id:
        extra = {"id": ui_id, "username": getattr(ui_me, "username", None)}
    return primary, extra, note


async def _pause(state: dict, seconds: float) -> None:
    """Sleep in one-second steps so the Cancel button is noticed quickly."""
    end = time.monotonic() + seconds
    while not state["cancel"]:
        left = end - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(1.0, left))


def _render(title: str, order: list, rows: dict, labels: dict, footer: str = "") -> str:
    lines = []
    for key in order:
        if key not in rows:
            continue
        mark, detail = rows[key]
        line = f"{_MARKS[mark]} {_esc(labels[key])}"
        if detail:
            line += f" - <i>{_esc(detail[:90])}</i>"
        lines.append(line)
    text = f"<b><u>🗂 AUTOSORT - {title}</u></b>\n\n" + "\n".join(lines)
    if footer:
        text += "\n\n" + footer
    else:
        text += "\n\n<i>⏳ waiting · 🛠 creating · ✅ done · ⚠️ admin missing · ❌ failed</i>"
    return text


async def run_creation(user_id: int, bot, message):
    """The whole run for a user who holds the RUNNING lock (claim_run). `message` is edited as the
    ONE progress message; `bot` is the client that runs the settings UI (its own account is
    promoted too when it differs from the saved forwarding identity). Never raises."""
    state = RUNNING.get(user_id)
    if state is None:
        return
    kind, prefix = state["cfg"]["kind"], state["cfg"]["prefix"]
    labels = dict(AUTOSORT_CATEGORIES)
    cancel_markup = InlineKeyboardMarkup([[_btn('• ᴄᴀɴᴄᴇʟ', 'settings#ascancel')]])
    order, rows = [], {}
    created = admin_problems = failed = 0
    stop, note = None, None
    client = None
    last_edit = 0.0

    async def show(force: bool = False):
        nonlocal last_edit
        now = time.monotonic()
        if not force and now - last_edit < MIN_EDIT_GAP:
            return
        last_edit = now
        await safe_edit(message, _render("CREATING", order, rows, labels), cancel_markup)

    async def finish(title: str, footer: str):
        await safe_edit(message, _render(title, order, rows, labels, footer), _back(), final=True)

    try:
        session = await db.get_autosort_session(user_id)
        if not session:
            text, markup = no_session_page()
            return await safe_edit(message, text, markup, final=True)
        todo = await unset_categories(user_id)
        if not todo:
            text, markup = all_set_page()
            return await safe_edit(message, text, markup, final=True)
        order = [key for key, _label in todo]
        rows = {key: ["wait", ""] for key in order}
        await show(force=True)

        client = make_client(user_id, session)
        try:
            await client.start()
            me = await client.get_me()
        except Exception as e:
            return await safe_edit(
                message, f"<b>❌ Could not start the session:</b>\n<code>{_esc(_scrub(e, session))}</code>",
                _back(), final=True)
        if getattr(me, "is_bot", False):
            return await safe_edit(message, "<b>❌ The saved session belongs to a bot. RESET it and set a user "
                                            "account's string session.</b>", _back(), final=True)
        ui_me = getattr(bot, "me", None)
        if ui_me is None:
            try:
                ui_me = await bot.get_me()
            except Exception:
                ui_me = None
        primary, extra, note = await forwarding_identities(user_id, getattr(me, "id", None), ui_me)

        for index, key in enumerate(list(order)):
            if state["cancel"]:
                stop = "cancelled by you"
                break
            if (await db.get_category_channels(user_id)).get(key):
                rows.pop(key, None)                  # set by hand meanwhile: never overwrite it
                continue
            rows[key] = ["make", ""]
            await show()
            title = chat_title(prefix, labels[key])
            about = f"Auto-Sort: {labels[key]}"[:ABOUT_LIMIT]
            try:
                chat = await _flood_retry(lambda: _create_chat(client, kind, title, about))
            except FloodWait as e:
                rows[key] = ["wait", f"Telegram asks to wait {_fmt_wait(e.value)}"]
                stop = f"Telegram asks to wait {_fmt_wait(e.value)} before more chats can be created"
                break
            except RPCError as e:
                eid = _error_id(e)
                failed += 1
                rows[key] = ["fail", eid]
                if eid in _STOP_IDS:
                    stop = _STOP_IDS[eid]
                    break
                logger.warning("autosort create: %s failed: %s", key, eid)
                await show()
                if index < len(order) - 1:
                    await _pause(state, AUTOSORT_CREATE_DELAY * random.uniform(0.8, 1.2))
                continue
            except Exception as e:
                failed += 1
                rows[key] = ["fail", type(e).__name__]
                stop = f"unexpected error ({type(e).__name__})"
                logger.warning("autosort create: %s failed: %s", key, type(e).__name__)
                break

            chat_id = chat.id
            try:                                     # saved the moment the chat exists
                await db.set_category_channel(user_id, key, chat_id)
            except Exception as e:
                failed += 1
                rows[key] = ["fail", f"created (id {chat_id}) but not saved"]
                stop = f"the database refused to save a chat ({type(e).__name__})"
                break
            created += 1
            rows[key] = ["ok", ""]
            if primary:
                ok, why = await promote(client, chat_id, primary, kind)
                if not ok:
                    admin_problems += 1
                    rows[key] = ["admin", f"bot is not admin ({why}) - add it by hand"]
            if extra:
                await promote(client, chat_id, extra, kind)      # the settings bot: best effort
            await show()
            if index < len(order) - 1:
                await _pause(state, AUTOSORT_CREATE_DELAY * random.uniform(0.8, 1.2))

        reached = [k for k in order if k in rows]
        left = sum(1 for k in reached if rows[k][0] in ("wait", "make"))
        footer = [f"<b>Created:</b> {created}   <b>Bot-admin problems:</b> {admin_problems}   <b>Failed:</b> {failed}"]
        if stop:
            footer.append(f"<b>Stopped:</b> {_esc(stop)}")
        if left:
            footer.append(f"<b>Not created yet:</b> {left} - tap CREATE again to continue where it stopped.")
        if admin_problems:
            footer.append("<i>Those chats are saved already; add the bot as admin there by hand.</i>")
        if note == "no_forwarder":
            footer.append("<i>No bot / userbot is saved in /settings -> Bots, so nobody was made admin for "
                          "/autosort. Add one there and make it admin in the new chats.</i>")
        await finish("STOPPED" if stop else "FINISHED", "\n".join(footer))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("autosort create: run failed: %s", type(e).__name__)
        await safe_edit(message, f"<b>❌ The run stopped:</b> <code>{_esc(type(e).__name__)}</code>\n"
                                 "Everything created so far is saved; tap CREATE again to continue.",
                        _back(), final=True)
    finally:
        if client is not None:
            await _stop(client)
        RUNNING.pop(user_id, None)
