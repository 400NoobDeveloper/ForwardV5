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
    r"(?<![A-Za-z0-9])S(\d{1,2})[ ]?E(\d{1,4})(?:[ ]?[-–][ ]?E?(\d{1,4})|E(\d{1,4}))?(?![A-Za-z0-9])", re.I)
_EP_X_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})x(\d{2,3})(?![A-Za-z0-9])", re.I)
_EP_WORD_RE = re.compile(r"(?<![A-Za-z0-9])(?:ep|episode)[ ]?(\d{1,4})(?![A-Za-z0-9])", re.I)
_EP_ABS_RE = re.compile(r"\s-\s(\d{1,4})(?:v\d)?(?=\s|$)")
_SEASON_RE = re.compile(r"(?<![A-Za-z0-9])(?:season[ ]?(\d{1,2})|S(\d{1,2}))(?![A-Za-z0-9])", re.I)
_YEAR_PAREN_RE = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
_YEAR_RE = re.compile(r"(?<![A-Za-z0-9])((?:19|20)\d{2})(?![A-Za-z0-9])")
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


def _spaced(text: str) -> str:
    """Release-style text -> readable text: "Blue.Eye.Samurai.S01E03.720p" -> "Blue Eye Samurai S01E03 720p".
    A dot between two digits stays ("AAC5.1", "DDP2.0", "H.265" keeps its form through _CODEC_RE)."""
    text = _CAP_EXT_RE.sub("", text) if _CAP_EXT_RE.search(text[-6:]) else text
    text = _NAME_DOMAIN_RE.sub(" ", text)                          # site tags: 4kHdHub.Com, 1TamilMV.tf
    text = re.sub(r"(?<=\D)\.(?=\S)|(?<=\S)\.(?=\D)|_", " ", text)   # separators, but not 5.1 / 2.0
    text = re.sub(r"(?<=[A-Za-z])-(?=\d{3,4}p)", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _parse_media_name(raw: str, episodic: bool):
    """Pull name / year / season-episode / episode title / tags out of one release-style line (a caption
    line or a file name). Returns a dict, or None when no usable name is there."""
    text = _spaced(_plain_text(raw))
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
        ep = f"S{season:02d}E{episode:02d}" + (f"-E{int(last):02d}" if last else "")
        ep_pos = (ep_m.start(), ep_m.end())
    else:
        xm = _EP_X_RE.search(text)
        if xm:
            season, episode = int(xm.group(1)), int(xm.group(2))
            ep, ep_pos = f"S{season:02d}E{episode:02d}", (xm.start(), xm.end())
        elif episodic:
            wm = _EP_WORD_RE.search(text)
            if wm:
                ep, ep_pos = f"E{int(wm.group(1)):02d}", (wm.start(), wm.end())
            else:
                am = _EP_ABS_RE.search(text)
                if am:
                    ep, ep_pos = f"E{int(am.group(1)):02d}", (am.start(), am.end())
    season_pos = None
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
