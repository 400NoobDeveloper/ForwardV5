"""
Media category classifier.

Given a Pyrogram message, works out which high-level category it belongs
to: anime, anime_files, series, series_files, movie, movie_files,
education, education_files, pdf, image, audio, files, or other. Used by
/forward's category filter (plugins/public.py + plugins/regix.py) and by
/autosort (plugins/autosort.py).

HOW MOVIE / SERIES / ANIME ARE DECIDED (TMDB is the authority)
--------------------------------------------------------------
Filename parsing alone (guessit / anitopy) is only used to pull out a
*title guess*. A video is only routed to movie / series / anime when
TMDB confirms that title exists. Any media that isn't confirmed as a movie,
series or anime (unknown title, sample/trailer, unparseable name, no
caption/filename) is classified "other", so it lands in your Other channel.
The one exception is "tmdb_error" (TMDB unreachable after retries): that
file is skipped rather than guessed, so a real movie isn't misfiled during
an outage.

Steps for a video file:
  1. Build candidate names: the filename first, then the first lines of
     the caption. Native Telegram videos frequently have NO file_name at
     all (or a useless one like "VID_2023.mp4") with the real title only
     in the caption - those used to fall straight into "other".
  2. Clean each candidate: strip "@channel" handles, website tags
     ("www.1TamilMV.tf - ", "[MoviesMod.com]"), emoji, leftover empty
     brackets, split-part markers. These "add-ons" wrecked title
     parsing and made bracketed site tags look like anime fansub tags.
  3. Skip extras (sample / trailer / teaser / featurette / making-of).
  4. Parse title/year/episode info with anitopy + guessit.
  5. Search TMDB (movie and/or tv) and accept a result only if its
     title matches the parsed title closely (numbers must match, year
     within +-1 for movies). Names with no release signal at all (no
     year / quality / group) need an exact title match on a title with
     real votes, so lecture/tutorial videos don't match random films.
  5b. Romaji filenames ("Boku no Hero Academia") don't equal TMDB's titles
     ("My Hero Academia"). If the normal match fails, animated Asian-origin
     candidates are verified against TMDB's alternative titles. When a
     live-action and an anime show share a title and the filename looks
     like anime (fansub tag / episode number without a season), the anime
     entry is preferred.
  6. Animation genre (+ Asian origin language, movies and TV) -> anime. Western-made
     animated TV that is clearly anime-style (e.g. Blue Eye Samurai: English
     original language, Japan setting) is caught by a TMDB keyword check
     (anime / manga / japan / samurai / ...). Western-made animated MOVIES get
     the same check but only with the explicit anime words (anime / manga /
     isekai / ...), never "japan" or "ninja", so e.g. Ninja Turtles stays a
     movie. Set ANIME_ALL_ANIMATION=1 to treat every animated title as anime
     instead.
  7. Optional AniList second opinion (per-user toggle in /settings ->
     AniList, no API key needed). It is asked ONLY when TMDB is unsure:
     TMDB found an animated series that isn't anime-language, or TMDB found
     nothing / was unreachable. A live-action show TMDB already identified
     (e.g. the live-action One Piece) is never overridden by AniList.

  8. Optional OpenAI fallback. Runs ONLY when the caller passes a key:
     /autosortai does (per-user key from /settings -> Private Files -> OpenAI),
     plain /autosort doesn't. Used ONLY for videos
     that would otherwise land in "other" (TMDB answered "not found", or
     the name couldn't be parsed). The cleaned filename/caption (plus
     duration and size) is sent to OpenAI, which answers movie / series /
     anime / education / other with a confidence. Anything below
     OPENAI_MIN_CONFIDENCE, any error, or no key = "other", exactly as
     before. Samples/trailers and TMDB outages never reach OpenAI, and a
     title TMDB already confirmed is never overridden. This is what lets
     course/lecture videos ("5.1.3 Ad Basics - (Tag).mp4") reach the
     education category.

  9. Series-detection hardening (TV serials / regional shows):
     a. Downloader noise like "1080p25.0fps 4399" (resolution + fps +
        random id) is normalised to "1080p" first. Left alone, guessit
        read "4399" as Season 43 Episode 99 and lost the resolution.
     b. Pipe-separated YouTube-style names
        ("Episode headline | Show Name | Full Episode | Episode 29")
        are split into segments; the show name is searched before the
        headline, and "Full Episode" / "Episode N" are never titles.
     c. An explicit "Episode N" / "Ep N" / "S01E02" marker lowers the
        TMDB vote requirement for TV (EPISODE_MIN_VOTES, default 3):
        small Hindi/regional serials rarely have 50 votes on TMDB.
     d. Shows TMDB doesn't know at all: a video with an explicit episode
        marker and a usable show title is filed as "series" instead of
        "other" (set SERIES_EPISODE_FALLBACK=0 to turn this off). Course
        / lecture names are excluded. With /autosortai, OpenAI gets the
        first say, this fallback only applies if OpenAI says "other".

 10. Series-like names never become movies: if ANY candidate name of a file
     carries "Episode N" / "S01E02" / "Season N" / "S01", every candidate of
     that file is looked up as TV only (see "series_like" in _parse_name()).

Non-video routing (pdf / image / audio / files / other) is extension
based, exactly as before.

/autosort's Movie channel has extra gates that are NOT part of
classify_async() (so /fwd behaves as before): movie_thumb_gate() (thumbnail
aspect ratio), movie_duration_minutes() + MOVIE_MIN_MINUTES, and
tmdb_movie_runtime() + runtime_mismatch(). handlers/autosort.py applies them
after classify_async() and sends a failing file to Other. See the
"/autosort Movie-channel gates" section at the end of this file.

NSFW: nsfw_category() is a separate,
keyword-based check on the RAW filename + caption. /autosort and
/autosortai call it first, and only when an NSFW channel is mapped in
/settings -> Channels -> Auto-Sort Channels. It runs before TMDB/OpenAI,
so adult files never reach them. Videos/photos -> "nsfw", files sent as
documents -> "nsfw_files". Optional second pass: tpdb_category() asks
ThePornDB about video files that still ended up in "other".
"""

import os
import re
import json
import math
import asyncio
import datetime
import logging
import unicodedata
from collections import OrderedDict
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# Fallback TMDB key for self-hosted single-owner deployments. The primary
# source is per-user (set via /settings -> TMDB, see core/database.py's
# get_tmdb_key) and is passed in explicitly by the caller.
ENV_TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
_ENV_TMDB_API_KEY = ENV_TMDB_API_KEY  # backwards-compatible alias


try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import anitopy
except ImportError:
    anitopy = None
    logger.warning("anitopy not installed - anime detection disabled (pip install anitopy)")

try:
    from guessit import guessit
except ImportError:
    guessit = None
    logger.warning("guessit not installed - movie/series detection disabled (pip install guessit)")

CATEGORIES = (
    "anime", "anime_files",
    "series", "series_files",
    "movie", "movie_files",
    "education", "education_files",
    "pdf", "image", "audio",
    "nsfw", "nsfw_files",
    "files", "other",
)

# Categories that depend on TMDB to be decided (used to require a key).
VIDEO_CATEGORIES = (
    "anime", "anime_files",
    "series", "series_files",
    "movie", "movie_files",
    "education", "education_files",
)

# Never routable - these mean "skip this file".
UNIDENTIFIED = "unidentified"      # (no longer returned - unconfirmed media goes to "other")
TMDB_UNAVAILABLE = "tmdb_error"    # TMDB couldn't be reached after retries
SKIP_CATEGORIES = (UNIDENTIFIED, TMDB_UNAVAILABLE)

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".tiff"}
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus", ".aac"}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".wmv", ".ts", ".m4v", ".flv"}

# Split/multi-part archive fragments - "Title.part001.mkv", "archive.7z.002",
# old-style "archive.r00" RAR volumes.
_SPLIT_PART_PATTERNS = (
    re.compile(r"\.part\d+\.", re.IGNORECASE),
    re.compile(r"\.\d{3}$", re.IGNORECASE),
    re.compile(r"\.r\d{2}$", re.IGNORECASE),
)


def _is_split_part(filename: str) -> bool:
    return any(p.search(filename) for p in _SPLIT_PART_PATTERNS)


def _strip_split_suffix(filename: str) -> str:
    filename = re.sub(r"\.part\d+(?=\.)", "", filename, flags=re.IGNORECASE)
    filename = re.sub(r"\.\d{3}$", "", filename)
    filename = re.sub(r"\.r\d{2}$", "", filename)
    return filename


def get_filename(message) -> "str | None":
    """Best-effort filename extraction from a Pyrogram message."""
    for attr in ("document", "video", "audio", "animation", "voice"):
        media = getattr(message, attr, None)
        if media is not None:
            name = getattr(media, "file_name", None)
            if name:
                return name
    return None


# ============================================================
# Name cleaning ("add-on" junk removal)
# ============================================================
# Fansub releases carry a bracketed group tag - "[SubsPlease] ...",
# or full-width "【Ani-Mix】". This is only a *hint* now; TMDB decides.
_GROUP_TAG = re.compile(r"\[[^\[\]]+\]|【[^【】]+】|「[^「」]+」")

_TLDS = (
    r"(?:com|net|org|in|me|co|cc|to|ws|tv|xyz|club|site|info|biz|link|pro|lol|ink|"
    r"ltd|vip|top|fun|live|store|app|cloud|tech|mobi|online|art|one|tf|lat|cam|rest|"
    r"bar|mov|wtf|life|fit|ai|io|pw|gg|fyi|red|nl|uk|us|ru|cx|mx|pm|ag|ac|bz|vc|"
    r"sh|so|ist|zip)"
)
_DOMAIN = rf"(?:https?://)?(?:www\.)?[\w-]+(?:\.[\w-]+)*?\.{_TLDS}(?![\w])"
_URL_RE = re.compile(r"(?:https?://|t\.me/)\S+", re.IGNORECASE)
_WWW_RE = re.compile(rf"www\.[\w-]+(?:\.[\w-]+)*?\.{_TLDS}(?![\w])", re.IGNORECASE)
_BRACKETED_DOMAIN_RE = re.compile(rf"[\[\(【「]\s*{_DOMAIN}\s*[\]\)】」]", re.IGNORECASE)
_LEADING_DOMAIN_RE = re.compile(
    rf"^\s*{_DOMAIN}(?:\s+[-–—_:|~]+\s*|\s*[-–—_:|~]+\s+)", re.IGNORECASE
)
_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{3,}")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200d\u2069\u2068]")
_JUNK_BRACKET_RE = re.compile(
    r"[\[\(【「](?:[^\w\[\]\(\)【】「」]|telegram|tg|join|via|by|uploaded|upload|"
    r"channel|subscribe|share|forwarded)*[\]\)】」]",
    re.IGNORECASE,
)
_EDGE_JUNK_RE = re.compile(r"^[\s\-–—_:|~.]+|[\s\-–—_:|~]+$")

# Sample / trailer / behind-the-scenes clips that ride along with a movie
# or show release. They're not the movie - never forward them as one.
_EXTRA_RE = re.compile(
    r"(?<![A-Za-z0-9])(sample|trailer|teaser|featurette|behind[ ._-]the[ ._-]scenes|"
    r"making[ ._-]of|deleted[ ._-]scenes?)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# Caption lines that are metadata, not a title.
_CAPTION_META_RE = re.compile(
    r"^\W*(size|quality|language|audio|subtitles?|format|duration|resolution|file|"
    r"download|join|support|channel|genre|rating|imdb|note|source|cast|director|"
    r"runtime|file ?name|codec|links?|share|report)\b",
    re.IGNORECASE,
)
_CAPTION_LABEL_RE = re.compile(r"^\W*(?:movie|title|name|film|series|anime|show)\s*[:\-–—]\s*", re.IGNORECASE)


def _clean_name(name: str) -> str:
    """Strip site tags, @handles, URLs, emoji and empty brackets so the
    title parser sees the actual release name."""
    name = _URL_RE.sub(" ", name)
    name = _BRACKETED_DOMAIN_RE.sub(" ", name)
    name = _LEADING_DOMAIN_RE.sub("", name)
    name = _WWW_RE.sub(" ", name)
    name = _HANDLE_RE.sub(" ", name)
    name = _EMOJI_RE.sub(" ", name)
    name = _JUNK_BRACKET_RE.sub(" ", name)
    name = re.sub(r"\s{2,}", " ", name)
    return _EDGE_JUNK_RE.sub("", name)


# ------------------------------------------------------------
# Release-noise normalisation, pipe-separated names, episode markers
# ------------------------------------------------------------
# Some downloader bots name files "<title> 1080p25.0fps 4399.mp4"
# (resolution + fps glued together + a random numeric id). guessit can't
# read "1080p25.0fps" and treats "4399" as Season 43 Episode 99.
_FPS_GLUED_RE = re.compile(r"(?<![0-9])(\d{3,4}[pi])\s*\d{2,3}(?:\.\d+)?\s*fps\b", re.IGNORECASE)
_FPS_BARE_RE = re.compile(r"(?<![0-9])\d{2,3}(?:\.\d+)?\s*fps\b", re.IGNORECASE)
_RES_THEN_ID_RE = re.compile(
    r"(?<![0-9])(\d{3,4}[pi])\s+\d{3,6}(?=\s*(?:\.[A-Za-z0-9]{2,4})?\s*$)", re.IGNORECASE
)
_VIDEO_EXT_TAIL_RE = re.compile(r"\s*\.(?:mkv|mp4|avi|webm|mov|wmv|ts|m4v|flv)\s*$", re.IGNORECASE)


def _strip_release_noise(name: str) -> str:
    name = _FPS_GLUED_RE.sub(r"\1", name)
    name = _FPS_BARE_RE.sub(" ", name)
    name = _RES_THEN_ID_RE.sub(r"\1", name)
    return re.sub(r"\s{2,}", " ", name).strip()


# An explicit episode marker: "Episode 29", "Ep.6", "Epi 12", "S01E05".
_EP_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:episodes?|ep|epi|एपिसोड)\.?[\s._#:-]*0*\d{1,4}(?![0-9])"
    r"|(?<![A-Za-z0-9])s\d{1,2}[ ._-]?e\d{1,4}(?![0-9])",
    re.IGNORECASE,
)

# "Season 2" / "S02" (a season pack). Together with _EP_MARKER_RE this is what
# "series_like" means in _parse_name(): series naming even if guessit missed it.
_SEASON_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:seasons?[\s._-]*0*\d{1,2}|s0*\d{1,2})(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# Course / lecture words: such names must never be promoted to "series"
# by the episode-marker rules ("Python Tutorial Episode 5").
_EDU_RE = re.compile(
    r"(?<![A-Za-z])(?:lectures?|lec|tutorials?|courses?|class(?:es)?|lessons?|modules?|"
    r"chapters?|bootcamp|training|masterclass|seminar|webinar|syllabus|udemy|coursera)(?![A-Za-z])",
    re.IGNORECASE,
)

# Env switch: file explicit-episode videos TMDB doesn't know as "series".
SERIES_EPISODE_FALLBACK = os.environ.get("SERIES_EPISODE_FALLBACK", "1").strip().lower() not in ("0", "false", "no")

_PIPE_RE = re.compile(r"\s[|｜]\s|\s[|｜]|[|｜]\s")
# Pieces of a segment that are never part of a show title.
_SEG_NOISE_RE = re.compile(
    r"(?:\b(?:full|latest|new|complete|whole)\s+)?\b(?:episodes?|ep|epi)\b\.?\s*#?\s*0*\d{0,4}"
    r"|\b\d{3,4}[pi]\b|\bfull\s+(?:hd|movie)\b|\b(?:hd|uhd|official)\b",
    re.IGNORECASE,
)


def _pipe_titles(name: str) -> list:
    """Title candidates from a pipe-separated name, best first.

    "Arjun ne kyun kiya... | Kkoi Dil Mein Hai | Full Episode | Episode 29"
    -> ["Kkoi Dil Mein Hai", "Arjun ne kyun kiya..."]

    The show name is usually NOT the first segment (that is the episode
    headline), so later, short, comma-free segments are tried first; cast
    lists ("A, B") and long headlines go last.
    """
    stem = _VIDEO_EXT_TAIL_RE.sub("", name)
    segments = []
    for idx, seg in enumerate(re.split(r"\s*[|｜]\s*", stem)):
        seg = _SEG_NOISE_RE.sub(" ", seg)
        seg = re.sub(r"\s{2,}", " ", seg)
        seg = _EDGE_JUNK_RE.sub("", seg).strip()
        if len(re.findall(r"\w", seg)) < 2:
            continue
        segments.append((idx, seg[:80]))
    segments.sort(key=lambda t: (
        ("," in t[1]) or ("&" in t[1]),
        len(t[1].split()) > 7,
        t[0] == 0,
        t[0],
    ))
    out = []
    for _idx, seg in segments:
        if seg not in out:
            out.append(seg)
    return out


def _episode_fallback_ok(p: dict, raw_names) -> bool:
    """Explicit episode marker + usable title + not a course -> 'series'
    even if TMDB has never heard of the show."""
    if not SERIES_EPISODE_FALLBACK or not p or not p.get("strong_episode"):
        return False
    if not p.get("titles") or p.get("extra"):
        return False
    return not any(_EDU_RE.search(n or "") for n in raw_names)


# ------------------------------------------------------------
# NSFW detection (keyword based, whole words only)
# ------------------------------------------------------------
# Matched against the RAW filename + start of the caption (before
# _clean_name() strips site tags - "ManyVids - clip.mp4" is exactly the
# signal wanted).
#
# Two lists:
#   STRONG - one hit is enough: adult site / studio names and explicit words.
#   WEAK   - suggestive words that also appear in normal titles ("Sex
#            Education", "Step Brothers", "Naked Gun", "Kinky Boots"), so at
#            least NSFW_WEAK_MIN (default 2) DIFFERENT ones must appear
#            together ("sexy" + "stepdaughter").
# Add your own words with NSFW_KEYWORDS (comma separated, strong list);
# NSFW_DETECTION=0 turns everything off.
_NSFW_TERMS = [
    # generic markers
    "nsfw", "porn", "porno", "pornstars?", "camgirls?", "hentai", "nudes?",
    # sites / studios / platforms
    "pornhub", "xvideos", "xnxx", "xhamster", "redtube", "youporn", "spankbang", "eporner",
    "brazzers", "bangbros", "realitykings", "naughtyamerica", "mofos", "teamskeet", "nubiles",
    "onlyfans", "fansly", "manyvids", "clips4sale", "iwantclips", "modelhub", "justforfans",
    "chaturbate", "stripchat", "camwhores", "erome", "fapello", "thothub", "coomer", "pornpics",
    "bellesa", "kooku", "nuefliks", "primeshots", "cineprime", "besharams", "electecity",
    # explicit words
    "pussy", "squirt(?:s|ing)?", "cumshots?", "anal", "dildos?", "gangbangs?", "threesomes?",
    "orgy", "bdsm", "footjobs?", "deepthroat(?:ing)?", "bukkake", "cuckold(?:ing)?",
    "orgasms?", "masturbat(?:e|es|ing|ion)", "blowjobs?", "handjobs?", "creampies?", "milf",
    "titfuck", "boobjob", "rimjob", "fisting",
]
_NSFW_TERMS += [re.escape(w.strip()) for w in os.environ.get("NSFW_KEYWORDS", "").split(",") if w.strip()]
_NSFW_WEAK_TERMS = [
    "sex", "sexy", "sexual", "naked", "topless", "lingerie", "horny", "naughty", "slut", "slutty",
    "busty", "kinky", "fetish", "seduces?", "seduced", "stripper", "striptease", "pov",
    r"step[ ._-]?(?:mom|mother|sis|sister|brother|bro|dad|father|daughter|son)s?",
]
NSFW_DETECTION = os.environ.get("NSFW_DETECTION", "1").strip().lower() not in ("0", "false", "no")
try:
    NSFW_WEAK_MIN = max(1, int(os.environ.get("NSFW_WEAK_MIN", "2")))
except ValueError:
    NSFW_WEAK_MIN = 2
_NSFW_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(_NSFW_TERMS) + r")(?![A-Za-z0-9])", re.IGNORECASE
)
_NSFW_WEAK_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(_NSFW_WEAK_TERMS) + r")(?![A-Za-z0-9])", re.IGNORECASE
)
# "XXX" only in all-caps / all-lowercase, so the film "xXx" isn't caught.
_NSFW_XXX_RE = re.compile(r"(?<![A-Za-z0-9])(?:XXX|xxx)(?![A-Za-z0-9])")
_MEDIA_ATTRS = ("photo", "video", "document", "animation", "audio", "voice", "video_note")


def is_nsfw(message) -> bool:
    """True if the media message's filename/caption carries adult-content
    markers (see the STRONG / WEAK lists above). Text-only messages are
    never NSFW here."""
    if not NSFW_DETECTION:
        return False
    if not any(getattr(message, a, None) for a in _MEDIA_ATTRS):
        return False
    text = f"{get_filename(message) or ''}\n{(getattr(message, 'caption', None) or '')[:400]}"
    if _NSFW_RE.search(text) or _NSFW_XXX_RE.search(text):
        return True
    weak = {re.sub(r"[^a-z0-9]", "", m.group(0).lower()) for m in _NSFW_WEAK_RE.finditer(text)}
    return len(weak) >= NSFW_WEAK_MIN


def _nsfw_target(message, mapping=None):
    """Which NSFW channel category this message would go to: 'nsfw_files' for
    documents, else 'nsfw'. With `mapping`, limited to categories that have a
    channel (one set channel serves both); None when neither is set."""
    want = "nsfw_files" if getattr(message, "document", None) is not None else "nsfw"
    if mapping is None or want in mapping:
        return want
    other = "nsfw" if want == "nsfw_files" else "nsfw_files"
    return other if other in mapping else None


def nsfw_category(message, mapping=None):
    """'nsfw' (video/photo/etc.) or 'nsfw_files' (sent as a document), or
    None when the message isn't NSFW.

    With `mapping` (the user's {category: channel} dict) the answer is
    limited to categories that actually have a channel - if only one of
    the two NSFW channels is set, it is used for both, and with neither
    set this returns None so nothing changes for that user."""
    if not is_nsfw(message):
        return None
    return _nsfw_target(message, mapping)


def _candidate_names(message) -> list:
    """Filename first, then up to two usable caption lines."""
    names = []
    fn = get_filename(message)
    if fn:
        names.append(fn)
    caption = getattr(message, "caption", None) or ""
    added = 0
    for line in caption.splitlines():
        line = _CAPTION_LABEL_RE.sub("", line.strip())
        if not line or _CAPTION_META_RE.match(line):
            continue
        cleaned = _clean_name(line)
        if len(re.findall(r"\w", cleaned)) < 2:
            continue
        names.append(line)
        added += 1
        if added >= 2:
            break
    return names


# ============================================================
# Parsing
# ============================================================
_RELEASE_SIGNAL_FIELDS = ("year", "screen_size", "source", "video_codec", "audio_codec", "release_group")


def _first(value):
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _as_int(value):
    value = _first(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_name(raw: str, check_extra: bool = False):
    """Parse one candidate name. Returns a dict or None if no usable title.

    keys: titles (best first), year, is_episode, gtype, weak, anime_tag,
          has_movie_signal, extra, series_like
    """
    if not raw:
        return None
    name = _strip_split_suffix(raw) if _is_split_part(raw) else raw
    name = _strip_release_noise(_clean_name(name))
    if not name:
        return None

    anime_title, anime_episode = None, False
    if anitopy is not None and _GROUP_TAG.search(name):
        try:
            a = anitopy.parse(name)
        except Exception as e:
            logger.debug("anitopy failed on %r: %s", name, e)
            a = None
        if a:
            anime_title = _first(a.get("anime_title"))
            anime_episode = bool(a.get("episode_number"))

    g = {}
    if guessit is not None:
        try:
            g = guessit(name) or {}
        except Exception as e:
            logger.debug("guessit failed on %r: %s", name, e)
            g = {}

    strong_episode = bool(_EP_MARKER_RE.search(name))

    titles = []
    max_titles = 3
    if _PIPE_RE.search(name):
        # "Headline | Show | Full Episode | Episode N": guessit's title is
        # the headline, so build candidates from the segments instead.
        titles = _pipe_titles(name)
        max_titles = 4
    if not titles:
        for t in (anime_title, g.get("title"), g.get("alternative_title")):
            for item in (t if isinstance(t, (list, tuple)) else [t]):
                if isinstance(item, str):
                    item = item.strip()
                    if len(item) >= 2 and item not in titles:
                        titles.append(item)
    titles = titles[:max_titles]
    if not titles:
        return None

    gtype = g.get("type")
    is_episode = gtype == "episode" or anime_episode or strong_episode
    # Anime-style naming: fansub group tag, or an episode number with no
    # season ("One.Piece.E1090", "Naruto Shippuden - 123").
    anime_hint = bool(anime_title) or (bool(is_episode) and not g.get("season"))
    weak = not any(g.get(f) for f in _RELEASE_SIGNAL_FIELDS) and not anime_title

    extra = False
    if check_extra:
        stem = os.path.splitext(name)[0]
        m = _EXTRA_RE.search(stem)
        if m and not is_episode and (m.start() > 0 or len(stem) <= len(m.group(0)) + 3):
            extra = True

    return {
        "titles": titles,
        "year": _as_int(g.get("year")),
        "is_episode": bool(is_episode),
        "gtype": gtype,
        "weak": bool(weak),
        "anime_tag": bool(anime_title),
        "anime_hint": bool(anime_hint),
        "has_movie_signal": bool(g.get("year")) or bool(g.get("release_group")),
        "strong_episode": strong_episode,
        "extra": extra,
        # explicit "Episode N" / "S01E02" / "Season N" / "S01" in this name
        "series_like": bool(strong_episode or _SEASON_RE.search(name)),
    }


def _detect_type(name: str) -> "str | None":
    """Filename-only *guess* of 'anime' / 'series' / 'movie' (no TMDB).
    Used by the sync classify() (whitelists) and as the no-TMDB-key
    fallback. Stricter than before about bare anime tags: a site tag no
    longer counts, because _clean_name() removes those first."""
    p = _parse_name(name)
    if not p:
        return None
    if p["anime_tag"]:
        return "anime"
    if p["gtype"] == "movie" and p["has_movie_signal"]:
        return "movie"
    if p["gtype"] == "episode" and not p["weak"]:
        return "series"
    if _episode_fallback_ok(p, [name]):
        return "series"
    return None


# ============================================================
# Non-TMDB routing
# ============================================================
def _early_category(message):
    """(category, names, is_files_variant). category is None for videos
    that still need title-based identification."""
    if getattr(message, "photo", None):
        return "image", [], False

    filename = get_filename(message)
    if not filename:
        if getattr(message, "voice", None) or getattr(message, "audio", None):
            return "audio", [], False
        # Native video uploads often have no file_name; the title is
        # usually in the caption.
        if getattr(message, "video", None) is not None:
            names = _candidate_names(message)
            if names:
                return None, names, False
        return "other", [], False

    ext = os.path.splitext(filename)[1].lower()
    if ext in PDF_EXTENSIONS:
        return "pdf", [], False
    if ext in IMAGE_EXTENSIONS:
        return "image", [], False
    if ext in AUDIO_EXTENSIONS:
        return "audio", [], False

    is_split = _is_split_part(filename)
    is_video_ext = ext in VIDEO_EXTENSIONS
    if not is_video_ext and not is_split:
        return "other", [], False
    if is_split and not is_video_ext:
        return "files", [], False

    # A video-extension file: "_files" variant only if it arrived as a
    # genuine document upload (see /diagnose_media).
    is_files_variant = getattr(message, "document", None) is not None
    return None, _candidate_names(message), is_files_variant


def classify(message) -> str:
    """Heuristic (no TMDB) category. Kept synchronous for the filename
    whitelists in plugins/utils.py. Movie/series/anime routing decisions
    for forwarding go through classify_async()."""
    early, names, is_files = _early_category(message)
    if early is not None:
        return early
    found = None
    for name in names:
        found = _detect_type(name)
        if found:
            break
    if found:
        return f"{found}_files" if is_files else found
    return "other"


def matches(message, wanted: str) -> bool:
    if not wanted or wanted == "all":
        return True
    return classify(message) == wanted


# ============================================================
# Movie/series/anime name whitelist
# ============================================================
# Set via /settings -> Filters and enforced by plugins/utils.py's
# check_filters() for both /fwd and /autosort.

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def extract_title(filename: str) -> "str | None":
    """Best-effort clean title from a filename ("Jawan.2023.1080p...mkv"
    -> "Jawan")."""
    p = _parse_name(filename)
    return p["titles"][0] if p else None


def _normalize_title(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def _contains_whole(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def title_in_whitelist(filename: str, whitelist, exact: bool = False) -> bool:
    """True if `filename`'s parsed title matches one of the whitelist
    entries (loose = whole-word containment either way, exact = equal).
    Empty whitelist = no restriction."""
    if not whitelist:
        return True

    title = extract_title(filename) or filename
    norm_title = _normalize_title(title)
    if not norm_title:
        return False

    for entry in whitelist:
        norm_entry = _normalize_title(entry)
        if not norm_entry:
            continue
        if exact:
            if norm_entry == norm_title:
                return True
        elif _contains_whole(norm_title, norm_entry) or _contains_whole(norm_entry, norm_title):
            return True
    return False


# ============================================================
# TMDB identification
# ============================================================
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_ANIMATION_GENRE_ID = 16
TMDB_TIMEOUT_SECONDS = 8
TMDB_RETRIES = 3

MIN_TITLE_SCORE = 0.88      # normal names: fuzzy title similarity
STRICT_MIN_VOTES = 50       # weak names: exact title AND this many TMDB votes
try:
    # Weak names WITH an explicit "Episode N" marker: small regional serials
    # rarely reach 50 TMDB votes, so TV needs far fewer.
    EPISODE_MIN_VOTES = int(os.environ.get("EPISODE_MIN_VOTES", "3"))
except ValueError:
    EPISODE_MIN_VOTES = 3

_TMDB_ERROR = object()

# (media_type, normalized query, year) -> results list. A 5,000-episode
# series would otherwise hit TMDB 5,000 times for the same title.
_SEARCH_CACHE: "OrderedDict" = OrderedDict()
_CACHE_MAX = 5000

_ANIME_ORIGIN_LANGUAGES = {"ja", "zh", "ko"}

# Animated TV made outside Asia (original_language "en", "fr", ...) is only
# called anime when TMDB's keywords for the show say so. Blue Eye Samurai,
# for example, is tagged japan / samurai / edo period.
_ANIME_KEYWORDS = {
    "anime", "manga", "based on manga", "based on light novel", "isekai",
    "shounen", "shonen", "seinen", "shoujo", "shojo", "mecha",
    "japan", "japanese", "feudal japan", "edo period", "sengoku period",
    "samurai", "ronin", "ninja", "shogun", "yakuza", "kyoto, japan", "tokyo, japan",
}
# Animated MOVIES made outside Asia only count as anime when TMDB's keywords say
# so explicitly. The geographic words above (japan / ninja / samurai ...) are
# left out on purpose: they also tag plain Western cartoons (Ninja Turtles).
_ANIME_MOVIE_KEYWORDS = {
    "anime", "manga", "based on manga", "based on light novel", "isekai",
    "shounen", "shonen", "seinen", "shoujo", "shojo",
}
_ANIME_KEYWORD_IDS = {210024}   # TMDB keyword "anime"
# Opt-in: every animated movie/show goes to anime (cartoons included).
ANIME_ALL_ANIMATION = os.environ.get("ANIME_ALL_ANIMATION", "").strip().lower() in ("1", "true", "yes")

# (media_type, tmdb id) -> list of keyword dicts
_KEYWORD_CACHE: "OrderedDict" = OrderedDict()
_DIGITS = re.compile(r"\d+")


def _norm_cmp(text: str) -> str:
    """Unicode-aware comparison form (keeps Japanese/Chinese titles,
    folds accents, '&' -> 'and', drops a leading article)."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("&", " and ")
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE).strip()
    return re.sub(r"^(the|a|an) ", "", text)


def _title_score(query: str, cand: str) -> float:
    if not query or not cand:
        return 0.0
    if query == cand:
        return 1.0
    # "Spider-Man 2" must never match "Spider-Man".
    if _DIGITS.findall(query) != _DIGITS.findall(cand):
        return 0.0
    return SequenceMatcher(None, query, cand).ratio()


def _year_of(date):
    if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _is_anime_result(result: dict, media_type: str) -> bool:
    """Animation genre AND ja/zh/ko original language, for movies and TV
    alike, so Western cartoons (e.g. the Netflix movie "Leo") and live-action
    Asian titles never land in anime. Set ANIME_ALL_ANIMATION=1 to treat
    every animated title as anime (the old behaviour for movies)."""
    if TMDB_ANIMATION_GENRE_ID not in (result.get("genre_ids") or []):
        return False
    if ANIME_ALL_ANIMATION:
        return True
    return result.get("original_language") in _ANIME_ORIGIN_LANGUAGES


async def _tmdb_search(session, media_type: str, query: str, tmdb_key: str, year=None):
    """List of results ([] = TMDB cleanly returned nothing) or _TMDB_ERROR
    after retries (network error / 5xx / rate-limit / bad key)."""
    if not tmdb_key:
        return _TMDB_ERROR
    ck = (media_type, _norm_cmp(query), year)
    hit = _SEARCH_CACHE.get(ck)
    if hit is not None:
        _SEARCH_CACHE.move_to_end(ck)
        return hit

    params = {"api_key": tmdb_key, "query": query, "include_adult": "false"}
    if year and media_type == "movie":
        params["year"] = year

    for attempt in range(TMDB_RETRIES):
        try:
            async with session.get(
                f"{TMDB_BASE}/search/{media_type}",
                params=params,
                timeout=aiohttp.ClientTimeout(total=TMDB_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status == 429:
                    try:
                        wait = int(resp.headers.get("Retry-After", 2))
                    except (TypeError, ValueError):
                        wait = 2
                    await asyncio.sleep(min(max(wait, 1), 15))
                    continue
                if resp.status >= 500:
                    await asyncio.sleep(1 + attempt)
                    continue
                if resp.status != 200:      # 401 bad key, 404, ...: retrying won't help
                    return _TMDB_ERROR
                payload = await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("TMDB lookup failed for %r (%s): %s", query, media_type, e)
            await asyncio.sleep(1 + attempt)
            continue

        results = payload.get("results") or []
        _SEARCH_CACHE[ck] = results
        if len(_SEARCH_CACHE) > _CACHE_MAX:
            _SEARCH_CACHE.popitem(last=False)
        return results
    return _TMDB_ERROR


# ============================================================
# AniList (optional second opinion; API key/token optional)
# ============================================================
ANILIST_URL = "https://graphql.anilist.co"
ANILIST_TIMEOUT_SECONDS = 10
ANILIST_RETRIES = 3
ANILIST_MIN_INTERVAL = 0.8          # AniList allows ~90 req/min (30 when degraded)
ANILIST_STRICT_MIN_POPULARITY = 500  # weak names: exact title AND this popularity
# Only Asian-origin titles count: keeps Western cartoons in "series".
ANILIST_ORIGINS = {"JP", "CN", "KR", "TW"}

_ANILIST_QUERY = """
query ($search: String) {
  Page(perPage: 8) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      format
      seasonYear
      startDate { year }
      endDate { year }
      popularity
      countryOfOrigin
      title { romaji english native }
      synonyms
    }
  }
}
"""

_ANILIST_CACHE: "OrderedDict" = OrderedDict()   # normalized query -> results
_anilist_lock = None
_anilist_last_call = 0.0


async def _anilist_search(session, query: str, anilist_key: str = ""):
    """List of AniList media dicts ([] = nothing found) or _TMDB_ERROR."""
    global _anilist_lock, _anilist_last_call
    if aiohttp is None:
        return _TMDB_ERROR
    ck = _norm_cmp(query)
    hit = _ANILIST_CACHE.get(ck)
    if hit is not None:
        _ANILIST_CACHE.move_to_end(ck)
        return hit
    if _anilist_lock is None:
        _anilist_lock = asyncio.Lock()

    payload = {"query": _ANILIST_QUERY, "variables": {"search": query}}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if anilist_key:                       # optional per-user token (/settings -> AniList)
        headers["Authorization"] = f"Bearer {anilist_key}"
    for attempt in range(ANILIST_RETRIES):
        async with _anilist_lock:        # one request at a time + spacing
            loop = asyncio.get_event_loop()
            wait = ANILIST_MIN_INTERVAL - (loop.time() - _anilist_last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with session.post(
                    ANILIST_URL, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=ANILIST_TIMEOUT_SECONDS),
                ) as resp:
                    _anilist_last_call = loop.time()
                    if resp.status == 429:
                        try:
                            retry = int(resp.headers.get("Retry-After", 5))
                        except (TypeError, ValueError):
                            retry = 5
                        await asyncio.sleep(min(max(retry, 1), 30))
                        continue
                    if resp.status >= 500:
                        await asyncio.sleep(1 + attempt)
                        continue
                    if resp.status != 200:
                        return _TMDB_ERROR
                    data = await resp.json()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("AniList lookup failed for %r: %s", query, e)
                await asyncio.sleep(1 + attempt)
                continue
        if data.get("errors") and not data.get("data"):
            return _TMDB_ERROR
        results = (((data.get("data") or {}).get("Page") or {}).get("media")) or []
        _ANILIST_CACHE[ck] = results
        if len(_ANILIST_CACHE) > _CACHE_MAX:
            _ANILIST_CACHE.popitem(last=False)
        return results
    return _TMDB_ERROR


def _anilist_best_match(results, queries, year, strict: bool):
    """Pick the AniList entry whose title genuinely matches, or None."""
    best, best_key = None, None
    for r in (results or [])[:8]:
        if r.get("format") == "MUSIC":
            continue
        if (r.get("countryOfOrigin") or "JP") not in ANILIST_ORIGINS:
            continue
        t = r.get("title") or {}
        names = [t.get("romaji"), t.get("english"), t.get("native")] + list(r.get("synonyms") or [])
        score = max(
            (_title_score(q, _norm_cmp(n)) for q in queries for n in names if n),
            default=0.0,
        )
        if score < (1.0 if strict else MIN_TITLE_SCORE):
            continue
        pop = r.get("popularity") or 0
        if strict and pop < ANILIST_STRICT_MIN_POPULARITY:
            continue
        if year:
            if r.get("format") == "MOVIE":
                ry = r.get("seasonYear") or ((r.get("startDate") or {}).get("year"))
                if ry and abs(ry - year) > 1:
                    continue
            else:
                # TV / OVA / ONA / SPECIAL: the filename year must fall inside
                # the years the show actually aired (+-1). Before this only
                # MOVIE entries were year-checked, so a 2023 movie could match
                # an older anime series with the same title and be filed as anime.
                sy = (r.get("startDate") or {}).get("year") or r.get("seasonYear")
                ey = (r.get("endDate") or {}).get("year") or (datetime.date.today().year + 1)
                if sy and (year < sy - 1 or year > ey + 1):
                    continue
        key = (score, pop)
        if best is None or key > best_key:
            best, best_key = r, key
    return best


async def anilist_identify(session, p: dict, anilist_key: str = ""):
    """-> ("found", media) | ("none", None) | ("error", None)."""
    queries = [_norm_cmp(t) for t in p["titles"]]
    errored = False
    for title in p["titles"]:
        results = await _anilist_search(session, title, anilist_key)
        if results is _TMDB_ERROR:
            errored = True
            continue
        match = _anilist_best_match(results, queries, p.get("year"), p.get("weak", False))
        if match:
            return "found", match
    return ("error" if errored else "none"), None


async def anilist_lookup(title: str, session=None, anilist_key: str = ""):
    """For the /settings -> AniList test buttons. Cleans the text like a
    filename, then returns (status, media) from anilist_identify()."""
    p = _parse_name(title)
    if not p:
        p = {"titles": [title.strip()], "year": None, "weak": False}
    owns = session is None
    if owns:
        session = aiohttp.ClientSession()
    try:
        return await anilist_identify(session, p, anilist_key)
    finally:
        if owns:
            await session.close()


async def _tmdb_keywords(session, media_type: str, tmdb_id, tmdb_key: str):
    """TMDB keyword list for one title, or _TMDB_ERROR."""
    ck = (media_type, tmdb_id)
    hit = _KEYWORD_CACHE.get(ck)
    if hit is not None:
        _KEYWORD_CACHE.move_to_end(ck)
        return hit
    for attempt in range(TMDB_RETRIES):
        try:
            async with session.get(
                f"{TMDB_BASE}/{media_type}/{tmdb_id}/keywords",
                params={"api_key": tmdb_key},
                timeout=aiohttp.ClientTimeout(total=TMDB_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status == 429:
                    try:
                        wait = int(resp.headers.get("Retry-After", 2))
                    except (TypeError, ValueError):
                        wait = 2
                    await asyncio.sleep(min(max(wait, 1), 15))
                    continue
                if resp.status >= 500:
                    await asyncio.sleep(1 + attempt)
                    continue
                if resp.status != 200:
                    return _TMDB_ERROR
                payload = await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("TMDB keywords failed for %s/%s: %s", media_type, tmdb_id, e)
            await asyncio.sleep(1 + attempt)
            continue
        # movies: {"keywords": [...]}, tv: {"results": [...]}
        kws = payload.get("results") or payload.get("keywords") or []
        _KEYWORD_CACHE[ck] = kws
        if len(_KEYWORD_CACHE) > _CACHE_MAX:
            _KEYWORD_CACHE.popitem(last=False)
        return kws
    return _TMDB_ERROR


_RUNTIME_CACHE: "OrderedDict" = OrderedDict()     # tmdb movie id -> runtime in minutes (0 = TMDB has none)


async def _tmdb_runtime(session, tmdb_id, tmdb_key: str):
    """Runtime in minutes of one TMDB movie (0 = TMDB lists none), or
    _TMDB_ERROR. Same session / retries / timeout as the other TMDB calls."""
    hit = _RUNTIME_CACHE.get(tmdb_id)
    if hit is not None:
        _RUNTIME_CACHE.move_to_end(tmdb_id)
        return hit
    for attempt in range(TMDB_RETRIES):
        try:
            async with session.get(
                f"{TMDB_BASE}/movie/{tmdb_id}",
                params={"api_key": tmdb_key},
                timeout=aiohttp.ClientTimeout(total=TMDB_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status == 429:
                    try:
                        wait = int(resp.headers.get("Retry-After", 2))
                    except (TypeError, ValueError):
                        wait = 2
                    await asyncio.sleep(min(max(wait, 1), 15))
                    continue
                if resp.status >= 500:
                    await asyncio.sleep(1 + attempt)
                    continue
                if resp.status != 200:
                    return _TMDB_ERROR
                payload = await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("TMDB runtime failed for movie/%s: %s", tmdb_id, e)
            await asyncio.sleep(1 + attempt)
            continue
        runtime = payload.get("runtime") if isinstance(payload, dict) else None
        runtime = runtime if isinstance(runtime, int) and not isinstance(runtime, bool) and runtime > 0 else 0
        _RUNTIME_CACHE[tmdb_id] = runtime
        if len(_RUNTIME_CACHE) > _CACHE_MAX:
            _RUNTIME_CACHE.popitem(last=False)
        return runtime
    return _TMDB_ERROR


_ALT_TITLE_CACHE: "OrderedDict" = OrderedDict()   # (media_type, tmdb id) -> list of title strings


async def _tmdb_alt_titles(session, media_type: str, tmdb_id, tmdb_key: str):
    """TMDB alternative titles for one title (romaji lives here), or
    _TMDB_ERROR."""
    ck = (media_type, tmdb_id)
    hit = _ALT_TITLE_CACHE.get(ck)
    if hit is not None:
        _ALT_TITLE_CACHE.move_to_end(ck)
        return hit
    for attempt in range(TMDB_RETRIES):
        try:
            async with session.get(
                f"{TMDB_BASE}/{media_type}/{tmdb_id}/alternative_titles",
                params={"api_key": tmdb_key},
                timeout=aiohttp.ClientTimeout(total=TMDB_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status == 429:
                    try:
                        wait = int(resp.headers.get("Retry-After", 2))
                    except (TypeError, ValueError):
                        wait = 2
                    await asyncio.sleep(min(max(wait, 1), 15))
                    continue
                if resp.status >= 500:
                    await asyncio.sleep(1 + attempt)
                    continue
                if resp.status != 200:
                    return _TMDB_ERROR
                payload = await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("TMDB alt titles failed for %s/%s: %s", media_type, tmdb_id, e)
            await asyncio.sleep(1 + attempt)
            continue
        # movies: {"titles": [...]}, tv: {"results": [...]}
        rows = payload.get("results") or payload.get("titles") or []
        titles = [r.get("title") for r in rows if r.get("title")]
        _ALT_TITLE_CACHE[ck] = titles
        if len(_ALT_TITLE_CACHE) > _CACHE_MAX:
            _ALT_TITLE_CACHE.popitem(last=False)
        return titles
    return _TMDB_ERROR


async def _match_by_alt_titles(session, results, media_type: str, queries, year, strict: bool, tmdb_key: str):
    """Filenames carry the romaji name ("Boku no Hero Academia") but TMDB
    lists the show as "My Hero Academia" / the Japanese title, so
    _best_match() finds nothing even though the search did return the
    right show (TMDB searches alternative titles too). Verify the top
    animated, Asian-origin candidates against their alternative titles.
    Only called when the normal title match failed. Returns a result or None."""
    cands = [r for r in (results or [])[:5] if r.get("id") and _is_anime_result(r, media_type)]
    for r in cands[:3]:
        alts = await _tmdb_alt_titles(session, media_type, r["id"], tmdb_key)
        if alts is _TMDB_ERROR:
            continue                       # can't verify -> treat as no match, don't guess
        score = max(
            (_title_score(q, _norm_cmp(n)) for q in queries for n in alts if n),
            default=0.0,
        )
        if score < (1.0 if strict else MIN_TITLE_SCORE):
            continue
        if media_type == "movie" and year:
            date = r.get("release_date")
            ry = _year_of(date)
            if ry and abs(ry - year) > 1:
                continue
        return r
    return None


def _keywords_say_anime(keywords, strict: bool = False) -> bool:
    """strict=True (movies): only the explicit anime words, see _ANIME_MOVIE_KEYWORDS."""
    words = _ANIME_MOVIE_KEYWORDS if strict else _ANIME_KEYWORDS
    for k in keywords or []:
        if k.get("id") in _ANIME_KEYWORD_IDS:
            return True
        if (k.get("name") or "").strip().lower() in words:
            return True
    return False


def _best_match(results, media_type: str, queries, year, strict: bool, prefer_anime: bool = False, min_votes: int = STRICT_MIN_VOTES):
    """Pick the TMDB result whose title genuinely matches, or None.

    prefer_anime: when two TV results match the title equally well (the
    anime "One Piece" vs the live-action "One Piece", Cowboy Bebop, Death
    Note, ...), pick the anime one if the filename looks like anime.
    Before this, the entry with more TMDB votes won, which was often the
    live-action remake -> the anime got filed as a normal series."""
    best, best_key = None, None
    for r in (results or [])[:10]:
        if media_type == "movie":
            names = (r.get("title"), r.get("original_title"))
        else:
            names = (r.get("name"), r.get("original_name"))
        score = max(
            (_title_score(q, _norm_cmp(n)) for q in queries for n in names if n),
            default=0.0,
        )
        if score < (1.0 if strict else MIN_TITLE_SCORE):
            continue
        votes = r.get("vote_count") or 0
        if strict and votes < min_votes:
            continue
        date = r.get("release_date") if media_type == "movie" else r.get("first_air_date")
        ry = _year_of(date)
        year_bonus = 0
        if year and ry:
            if media_type == "movie" and abs(ry - year) > 1:
                continue
            year_bonus = 1 if ry == year else 0
        anime_pref = 1 if (prefer_anime and media_type == "tv" and _is_anime_result(r, "tv")) else 0
        key = (score, anime_pref, year_bonus, votes)
        if best is None or key > best_key:
            best, best_key = r, key
    return best


class _Ident(tuple):
    """What _tmdb_identify_core() returns: ("found" | "none" | "error", kind, animated).

    It IS that 3-tuple, so `status, kind, animated = await _tmdb_identify_core(...)`
    keeps working for every existing caller. It also carries two attributes for
    /autosort's movie gates: .tmdb_id (id of the matched TMDB title) and
    .media_type ("movie" | "tv"); both None when nothing matched."""
    tmdb_id = None
    media_type = None

    def __new__(cls, status, kind, animated, tmdb_id=None, media_type=None):
        self = super().__new__(cls, (status, kind, animated))
        self.tmdb_id = tmdb_id
        self.media_type = media_type
        return self


async def _tmdb_identify_core(session, tmdb_key: str, p: dict):
    """-> ("found", kind, animated) | ("none", None, False) | ("error", None, False)
    (an _Ident: that tuple plus .tmdb_id / .media_type).
    `animated` = the TMDB match carries the Animation genre."""
    # Episode / season naming (this name, or any other name of the same file:
    # see "series_like" in classify_async) is only ever looked up as TV.
    order = ["tv"] if (p["is_episode"] or p.get("series_like")) else ["movie", "tv"]
    queries = [_norm_cmp(t) for t in p["titles"]]
    errored = False
    for media_type in order:
        for title in p["titles"]:
            results = await _tmdb_search(session, media_type, title, tmdb_key)
            if results is _TMDB_ERROR:
                errored = True
                continue
            hint = p.get("anime_hint", False)
            min_votes = EPISODE_MIN_VOTES if (media_type == "tv" and p.get("strong_episode")) else STRICT_MIN_VOTES
            match = _best_match(results, media_type, queries, p["year"], p["weak"], hint, min_votes)
            if not match and media_type == "movie" and p["year"]:
                # Extra page of candidates filtered by year on TMDB's side.
                results = await _tmdb_search(session, media_type, title, tmdb_key, p["year"])
                if results is _TMDB_ERROR:
                    errored = True
                    continue
                match = _best_match(results, media_type, queries, p["year"], p["weak"], hint, min_votes)
            if not match and results:
                match = await _match_by_alt_titles(
                    session, results, media_type, queries, p["year"], p["weak"], tmdb_key)
            if match:
                animated = TMDB_ANIMATION_GENRE_ID in (match.get("genre_ids") or [])
                mid = match.get("id")
                if _is_anime_result(match, media_type):
                    return _Ident("found", "anime", animated, mid, media_type)
                # Animated title whose original language isn't ja/zh/ko
                # (e.g. Blue Eye Samurai): ask TMDB's keywords. Movies use the
                # strict anime-words-only list (live-action manga adaptations
                # are not animated, so they never get here).
                if animated and mid:
                    kws = await _tmdb_keywords(session, media_type, mid, tmdb_key)
                    if kws is _TMDB_ERROR:
                        return _Ident("error", None, False)   # don't guess during an outage
                    if _keywords_say_anime(kws, strict=(media_type == "movie")):
                        return _Ident("found", "anime", animated, mid, media_type)
                return _Ident("found", ("movie" if media_type == "movie" else "series"),
                              animated, mid, media_type)
    return _Ident("error" if errored else "none", None, False)


async def _tmdb_identify(session, tmdb_key: str, p: dict, use_anilist: bool = False, anilist_key: str = "", info=None):
    """-> ("found", kind) | ("none", None) | ("error", None).

    With use_anilist, AniList gets a say ONLY when TMDB is unsure: nothing
    found, TMDB unreachable, or an animated series TMDB couldn't call anime.
    Live-action matches and TMDB-confirmed anime are never touched.

    info: optional dict that receives "tmdb_id" / "tmdb_media" of a TMDB match
    (read by /autosort; the result itself is not affected)."""
    ident = await _tmdb_identify_core(session, tmdb_key, p)
    status, kind, animated = ident
    if info is not None and status == "found":
        info["tmdb_id"] = ident.tmdb_id
        info["tmdb_media"] = ident.media_type
    if not use_anilist:
        return status, kind
    if status == "found" and not (kind in ("series", "movie") and animated):
        return status, kind           # anime already, or a live-action movie/series
    a_status, _media = await anilist_identify(session, p, anilist_key)
    if a_status == "found":
        return "found", "anime"
    return status, kind               # AniList said no / failed: keep TMDB's verdict


# ============================================================
# OpenAI fallback (only for files that would land in "other")
# ============================================================
ENV_OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "").strip() or "gpt-4o-mini"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_TIMEOUT_SECONDS = 20
OPENAI_RETRIES = 3
try:
    OPENAI_MIN_CONFIDENCE = float(os.environ.get("OPENAI_MIN_CONFIDENCE", "0.7"))
except ValueError:
    OPENAI_MIN_CONFIDENCE = 0.7
_OPENAI_KINDS = {"movie", "series", "anime", "education"}

# normalized names (digits masked) -> verdict ("other" included), so
# "Lecture 1" ... "Lecture 200" cost one request, not 200.
_OPENAI_CACHE: "OrderedDict" = OrderedDict()
# Keys OpenAI rejected with 401 - not asked again (one failed request, not
# one per file for the rest of a 1-lakh-message run).
_OPENAI_BAD_KEYS: set = set()

_OPENAI_SYSTEM = (
    "You sort Telegram video file names into one category. Reply with JSON "
    'only: {"category": "movie|series|anime|education|other", "confidence": 0.0-1.0}.\n'
    "- education: course, lecture, tutorial, bootcamp, class or training "
    "videos. Numbered module/lesson names like '5.1.3 Title' and uploader "
    "tags in brackets are typical.\n"
    "- anime: Japanese/Asian-style animated shows or films, including "
    "Hindi/English dubs.\n"
    "- series: live-action TV shows or web-series episodes.\n"
    "- movie: feature films.\n"
    "- other: anything else - personal or random videos, samples, trailers, "
    "or when you are not sure.\n"
    "Never guess a movie, series or anime title you do not recognise; "
    "answer other instead."
)


def _media_hints(message) -> str:
    media = getattr(message, "video", None) or getattr(message, "document", None)
    parts = []
    duration = getattr(media, "duration", None)
    if isinstance(duration, int) and duration > 0:
        parts.append(f"duration: {duration // 60} min")
    size = getattr(media, "file_size", None)
    if isinstance(size, int) and size > 0:
        parts.append(f"size: {size // (1024 * 1024)} MB")
    return ", ".join(parts)


async def _openai_chat(session, messages, key):
    """Content string of the model's reply, or None (no key / error)."""
    body = {
        "model": OPENAI_MODEL,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for attempt in range(OPENAI_RETRIES):
        try:
            async with session.post(
                OPENAI_URL, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=OPENAI_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status == 429:
                    try:
                        wait = int(resp.headers.get("Retry-After", 3))
                    except (TypeError, ValueError):
                        wait = 3
                    await asyncio.sleep(min(max(wait, 1), 30))
                    continue
                if resp.status >= 500:
                    await asyncio.sleep(1 + attempt)
                    continue
                if resp.status != 200:     # bad key / unknown model: retrying won't help
                    if resp.status == 401:
                        _OPENAI_BAD_KEYS.add(key)
                    logger.warning("OpenAI returned HTTP %s (check the OpenAI key in /settings -> Private Files, and OPENAI_MODEL)", resp.status)
                    return None
                data = await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("OpenAI request failed: %s", e)
            await asyncio.sleep(1 + attempt)
            continue
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
    return None


async def _openai_category(session, names, message, key):
    """'movie' | 'series' | 'anime' | 'education', or None for other /
    unsure / no key / any failure."""
    if not key or key in _OPENAI_BAD_KEYS or aiohttp is None or not names:
        return None
    texts = [t for t in (_clean_name(n) for n in names[:3]) if t]
    if not texts:
        return None

    ck = re.sub(r"\d+", "#", _norm_cmp(" | ".join(texts)))
    hit = _OPENAI_CACHE.get(ck)
    if hit is not None:
        _OPENAI_CACHE.move_to_end(ck)
        return hit or None

    lines = [f"file name / caption: {t}" for t in texts]
    hints = _media_hints(message)
    if hints:
        lines.append(hints)
    content = await _openai_chat(session, [
        {"role": "system", "content": _OPENAI_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ], key)
    if content is None:
        return None                       # failure: don't cache, try again next time

    kind = ""
    try:
        verdict = json.loads(content)
        cat = str(verdict.get("category", "")).strip().lower()
        conf = float(verdict.get("confidence", 0))
        if cat in _OPENAI_KINDS and conf >= OPENAI_MIN_CONFIDENCE:
            kind = cat
    except (ValueError, TypeError, AttributeError):
        kind = ""
    _OPENAI_CACHE[ck] = kind
    if len(_OPENAI_CACHE) > _CACHE_MAX:
        _OPENAI_CACHE.popitem(last=False)
    return kind or None


async def _other_or_openai(message, names, is_files: bool, session, key: str = "") -> str:
    """The point where classify_async() would say "other" for a video:
    give OpenAI one chance to name it, else keep "other"."""
    if not key or key in _OPENAI_BAD_KEYS or aiohttp is None or not names:
        return "other"
    owns = session is None
    if owns:
        session = aiohttp.ClientSession()
    try:
        kind = await _openai_category(session, names, message, key)
    finally:
        if owns:
            await session.close()
    if not kind:
        return "other"
    return f"{kind}_files" if is_files else kind


# ============================================================
# ThePornDB (optional: recognises adult scenes the keyword check missed)
# ============================================================
# Per-user token from /settings -> Private Files -> ThePornDB. Asked ONLY for
# video files that ended up as "other" AND only when an NSFW channel is
# mapped, so it costs nothing for everything TMDB/keywords already sorted.
# A file counts as adult only if its cleaned name (minus the site prefix)
# matches a TPDB scene title almost exactly (TPDB_MIN_SCORE, default 0.9)
# and has 2+ words, so generic one-word titles never match.
TPDB_URL = os.environ.get("TPDB_API_URL", "https://api.theporndb.net").strip().rstrip("/")
TPDB_TIMEOUT_SECONDS = 15
TPDB_RETRIES = 3
try:
    TPDB_MIN_SCORE = float(os.environ.get("TPDB_MIN_SCORE", "0.9"))
except ValueError:
    TPDB_MIN_SCORE = 0.9

_TPDB_CACHE: "OrderedDict" = OrderedDict()   # normalized query -> bool
_TPDB_BAD_KEYS: set = set()                   # tokens TPDB rejected (401/403)
_TPDB_DATE_RE = re.compile(r"\b(?:19|20)\d{2} \d{2} \d{2}\b")


def _tpdb_query(raw: str) -> str:
    """Filename/caption line -> plain text for TPDB ("ManyVids - Title")."""
    n = _strip_split_suffix(raw) if _is_split_part(raw) else raw
    n = _VIDEO_EXT_TAIL_RE.sub("", n)
    n = re.sub(r"[._]+", " ", n)
    n = _strip_release_noise(_clean_name(n))
    return re.sub(r"\s{2,}", " ", n).strip(" -")


async def _tpdb_search(session, query: str, key: str):
    """List of TPDB scene dicts ([] = nothing) or _TMDB_ERROR."""
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    for attempt in range(TPDB_RETRIES):
        try:
            async with session.get(
                f"{TPDB_URL}/scenes", params={"parse": query}, headers=headers,
                timeout=aiohttp.ClientTimeout(total=TPDB_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status == 429:
                    try:
                        wait = int(resp.headers.get("Retry-After", 3))
                    except (TypeError, ValueError):
                        wait = 3
                    await asyncio.sleep(min(max(wait, 1), 30))
                    continue
                if resp.status >= 500:
                    await asyncio.sleep(1 + attempt)
                    continue
                if resp.status in (401, 403):
                    _TPDB_BAD_KEYS.add(key)
                    logger.warning("ThePornDB rejected the token (HTTP %s) - check /settings -> Private Files -> ThePornDB", resp.status)
                    return _TMDB_ERROR
                if resp.status != 200:
                    return _TMDB_ERROR
                payload = await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("ThePornDB lookup failed for %r: %s", query, e)
            await asyncio.sleep(1 + attempt)
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            data = [data]
        return [r for r in (data or []) if isinstance(r, dict)]
    return _TMDB_ERROR


def _tpdb_match(results, query: str) -> bool:
    qn = _norm_cmp(query)
    for r in (results or [])[:10]:
        title = r.get("title")
        if not isinstance(title, str):
            continue
        tn = _norm_cmp(title)
        if len(tn.split()) < 2 or len(tn) < 8:
            continue                       # too generic to trust
        cands = [qn, _TPDB_DATE_RE.sub(" ", qn)]
        site = r.get("site")
        sname = site.get("name") if isinstance(site, dict) else (site if isinstance(site, str) else "")
        sn = _norm_cmp(sname or "")
        if sn:
            for c in list(cands):
                if c.startswith(sn + " "):
                    cands.append(re.sub(r"\s{2,}", " ", c[len(sn) + 1:]).strip())
        if any(_title_score(c, tn) >= TPDB_MIN_SCORE for c in cands if len(c.split()) >= 2):
            return True
    return False


async def tpdb_known(message, key: str, session=None) -> bool:
    """True if ThePornDB knows this message's filename/caption as a scene."""
    if not key or key in _TPDB_BAD_KEYS or aiohttp is None:
        return False
    owns = session is None
    if owns:
        session = aiohttp.ClientSession()
    try:
        for raw in _candidate_names(message)[:2]:
            q = _tpdb_query(raw)
            if len(q.split()) < 2:
                continue
            ck = _norm_cmp(q)
            hit = _TPDB_CACHE.get(ck)
            if hit is None:
                results = await _tpdb_search(session, q, key)
                if results is _TMDB_ERROR:
                    if key in _TPDB_BAD_KEYS:
                        return False
                    continue               # error: don't cache, try again later
                hit = _tpdb_match(results, q)
                _TPDB_CACHE[ck] = hit
                if len(_TPDB_CACHE) > _CACHE_MAX:
                    _TPDB_CACHE.popitem(last=False)
            else:
                _TPDB_CACHE.move_to_end(ck)
            if hit:
                return True
        return False
    finally:
        if owns:
            await session.close()


async def tpdb_category(message, mapping, key: str, session=None):
    """'nsfw' / 'nsfw_files' if ThePornDB recognises this video as an adult
    scene and an NSFW channel is mapped, else None. No network call at all
    unless both a token and an NSFW channel exist and the message is a video."""
    if not key:
        return None
    target = _nsfw_target(message, mapping)
    if target is None:
        return None
    fn = get_filename(message)
    is_video = (getattr(message, "video", None) is not None
                or getattr(message, "animation", None) is not None
                or bool(fn and os.path.splitext(fn)[1].lower() in VIDEO_EXTENSIONS))
    if not is_video:
        return None
    return target if await tpdb_known(message, key, session) else None


async def classify_async(message, session=None, tmdb_key: str = "", use_anilist: bool = False, anilist_key: str = "", openai_key: str = "", info=None) -> str:
    """Category for forwarding. Videos are routed to movie / series /
    anime ONLY when TMDB confirms the title; otherwise "unidentified"
    (or "tmdb_error") is returned, which no channel maps to -> skipped.

    Without a TMDB key (or without aiohttp/guessit) this falls back to the
    filename-only guess from classify().

    info: optional dict. When TMDB matched the file it receives "tmdb_id" and
    "tmdb_media" ("movie" | "tv"). /autosort uses them for its runtime check
    and its log line; passing it never changes the returned category.
    """
    early, names, is_files = _early_category(message)
    if early is not None:
        return early

    tmdb_key = tmdb_key or ENV_TMDB_API_KEY
    if not tmdb_key or aiohttp is None or guessit is None:
        return classify(message)

    def _finish(kind: str) -> str:
        return f"{kind}_files" if is_files else kind

    async def _unconfirmed(sess) -> str:
        """TMDB answered "not found": OpenAI (if a key was passed) first,
        then the explicit-episode-marker fallback, else "other"."""
        verdict = await _other_or_openai(message, names, is_files, sess, openai_key)
        if verdict != "other":
            return verdict
        for pp in parsed:
            if _episode_fallback_ok(pp, names):
                return _finish("series")
        return "other"

    parsed = []
    for i, raw in enumerate(names):
        # The first name (the file name, or - for native videos that have none -
        # the first caption line) is checked for sample / trailer / teaser ...
        # Before this was skipped when there was no file name, so a caption-only
        # "Movie (2025) Official Trailer" was matched to the movie on TMDB.
        p = _parse_name(raw, check_extra=(i == 0))
        if p:
            parsed.append(p)
    if not parsed:
        return await _other_or_openai(message, names, is_files, session, openai_key)
    if parsed[0]["extra"]:
        return "other"
    if any(pp.get("series_like") for pp in parsed):
        # One name says "Episode 5" / "S01E02" / "Season 2": look up ALL names of
        # this file as TV only. Before, another name (a plain caption line without
        # a marker) was still searched as a movie and could turn an episode into
        # a movie that merely shares the show's title.
        for pp in parsed:
            pp["series_like"] = True

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        errored = False
        for p in parsed:
            status, kind = await _tmdb_identify(session, tmdb_key, p, use_anilist, anilist_key, info)
            if status == "found":
                return _finish(kind)
            if status == "error":
                errored = True
        if errored:
            # TMDB unreachable: don't guess (a real movie could be misfiled
            # during an outage) - the file is skipped, re-run later.
            return TMDB_UNAVAILABLE
        # TMDB answered but this isn't a confirmed movie/series/anime:
        # OpenAI (if configured), then the episode-marker fallback, get a
        # chance before it becomes "other".
        return await _unconfirmed(session)
    finally:
        if owns_session:
            await session.close()


async def matches_async(message, wanted: str, session=None, tmdb_key: str = "", use_anilist: bool = False, anilist_key: str = "", openai_key: str = "") -> bool:
    """Async counterpart of matches() with TMDB identification."""
    if not wanted or wanted == "all":
        return True
    return await classify_async(message, session, tmdb_key, use_anilist, anilist_key, openai_key) == wanted


# ============================================================
# /autosort Movie-channel gates
# ============================================================
# Deliberately NOT inside classify_async(): /fwd shares that function and must
# keep behaving as before. handlers/autosort.py calls these helpers after
# classify_async() returned "movie" / "movie_files" and sends the file to the
# Other channel when a gate fails (nothing is ever dropped).
#
# Env vars (all optional):
#   MOVIE_THUMB_RATIO_MIN / MOVIE_THUMB_RATIO_MAX   accepted thumbnail width/height
#                                                   band, default 1.30 .. 2.40
#   MOVIE_RATIO_UNKNOWN   "other" (default) | "allow": no thumbnail size at all
#   MOVIE_MIN_MINUTES     shorter videos are not movies, default 45
#   MOVIE_RUNTIME_TOLERANCE  allowed |file - TMDB runtime| / TMDB runtime, default 0.35
_DEFAULT_THUMB_RATIO_MIN = 1.30
_DEFAULT_THUMB_RATIO_MAX = 2.40
_DEFAULT_MIN_MINUTES = 45.0
_DEFAULT_RUNTIME_TOLERANCE = 0.35


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


MOVIE_THUMB_RATIO_MIN = _env_float("MOVIE_THUMB_RATIO_MIN", _DEFAULT_THUMB_RATIO_MIN)
MOVIE_THUMB_RATIO_MAX = _env_float("MOVIE_THUMB_RATIO_MAX", _DEFAULT_THUMB_RATIO_MAX)
if not 0 < MOVIE_THUMB_RATIO_MIN < MOVIE_THUMB_RATIO_MAX:
    logger.warning(
        "MOVIE_THUMB_RATIO_MIN/MAX (%s / %s) are not a valid range - using %s / %s",
        MOVIE_THUMB_RATIO_MIN, MOVIE_THUMB_RATIO_MAX, _DEFAULT_THUMB_RATIO_MIN, _DEFAULT_THUMB_RATIO_MAX,
    )
    MOVIE_THUMB_RATIO_MIN, MOVIE_THUMB_RATIO_MAX = _DEFAULT_THUMB_RATIO_MIN, _DEFAULT_THUMB_RATIO_MAX

MOVIE_RATIO_UNKNOWN = os.environ.get("MOVIE_RATIO_UNKNOWN", "other").strip().lower()
if MOVIE_RATIO_UNKNOWN not in ("other", "allow"):
    MOVIE_RATIO_UNKNOWN = "other"

MOVIE_MIN_MINUTES = max(0.0, _env_float("MOVIE_MIN_MINUTES", _DEFAULT_MIN_MINUTES))   # 0 = guard off
MOVIE_RUNTIME_TOLERANCE = _env_float("MOVIE_RUNTIME_TOLERANCE", _DEFAULT_RUNTIME_TOLERANCE)
if MOVIE_RUNTIME_TOLERANCE <= 0:
    MOVIE_RUNTIME_TOLERANCE = _DEFAULT_RUNTIME_TOLERANCE


def _movie_media(message):
    """The Video, or the Document when it is a video-extension file; else None."""
    video = getattr(message, "video", None)
    if video is not None:
        return video
    doc = getattr(message, "document", None)
    if doc is not None:
        name = getattr(doc, "file_name", None) or ""
        if os.path.splitext(_strip_split_suffix(name))[1].lower() in VIDEO_EXTENSIONS:
            return doc
    return None


def _positive_dims(obj):
    """(width, height) as floats when both are positive numbers, else None."""
    try:
        w = float(getattr(obj, "width", None) or 0)
        h = float(getattr(obj, "height", None) or 0)
    except (TypeError, ValueError):
        return None
    return (w, h) if w > 0 and h > 0 else None


def movie_thumb_gate(message):
    """-> (ok, ratio, source): does this video look like a normal landscape
    video preview?

    Ratio (width / height) comes from, in this order:
      "thumb"   the LARGEST entry (width * height) of media.thumbs
      "video"   media.width / media.height, when there is no usable thumbnail
      "unknown" neither exists -> ratio None
    ok is True when MOVIE_THUMB_RATIO_MIN <= ratio <= MOVIE_THUMB_RATIO_MAX
    (default 1.30 .. 2.40: 4:3 up to scope-wide; square and portrait posters
    fail). With an unknown ratio ok follows MOVIE_RATIO_UNKNOWN ("other" ->
    False, "allow" -> True)."""
    media = _movie_media(message)
    ratio, source = None, "unknown"
    if media is not None:
        best_area = 0.0
        for thumb in getattr(media, "thumbs", None) or []:
            dims = _positive_dims(thumb)
            if dims and dims[0] * dims[1] > best_area:
                best_area = dims[0] * dims[1]
                ratio, source = dims[0] / dims[1], "thumb"
        if ratio is None:
            dims = _positive_dims(media)
            if dims:
                ratio, source = dims[0] / dims[1], "video"
    if ratio is None:
        return MOVIE_RATIO_UNKNOWN == "allow", None, "unknown"
    return MOVIE_THUMB_RATIO_MIN <= ratio <= MOVIE_THUMB_RATIO_MAX, ratio, source


def movie_duration_minutes(message):
    """Length of the video in minutes, or None when unknown. Documents carry no
    duration in Pyrogram, and a duration of 0 means "not set", so both give None
    (the duration guard and the runtime check are then skipped)."""
    seconds = getattr(_movie_media(message), "duration", None)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return seconds / 60.0


async def tmdb_movie_runtime(session, tmdb_id, tmdb_key: str = "") -> int:
    """TMDB runtime in minutes for a movie id; 0 when TMDB has none or the
    lookup failed (so it can never block a movie). Cached by TMDB id."""
    tmdb_key = tmdb_key or ENV_TMDB_API_KEY
    if not tmdb_id or not tmdb_key or aiohttp is None:
        return 0
    owns = session is None
    if owns:
        session = aiohttp.ClientSession()
    try:
        runtime = await _tmdb_runtime(session, tmdb_id, tmdb_key)
    finally:
        if owns:
            await session.close()
    return 0 if runtime is _TMDB_ERROR else runtime


def runtime_mismatch(file_minutes, runtime_minutes) -> bool:
    """True when both lengths are known and the file differs from TMDB's runtime
    by more than MOVIE_RUNTIME_TOLERANCE (default 35 %) of that runtime."""
    if not file_minutes or not runtime_minutes or file_minutes <= 0 or runtime_minutes <= 0:
        return False
    return abs(file_minutes - runtime_minutes) / runtime_minutes > MOVIE_RUNTIME_TOLERANCE
