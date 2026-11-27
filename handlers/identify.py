"""
Media category classifier.

Given a Pyrogram message, works out which high-level category it belongs
to: anime, anime_files, series, series_files, movie, movie_files,
education, education_files, pdf, image, audio, files, or other. Used by
/forward's category filter (handlers/forward.py) and by
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

 11. /autosort only (deep_series=True): series identification is stricter and smarter. The
     name is first normalised (URL-decoded; "None" / "null" junk, empty / audio-bitrate
     bracket groups and uploader tags removed; platform tags such as JIOHS and language
     codes such as BBKan read out), the show title is separated from the episode title,
     and the show is searched on TMDB, then TVmaze, then Wikidata with a short list of title
     candidates. A bracket counts as an anime fansub tag only when it is a plausible group
     name at the start of the name. See the "deep SERIES identification" section at the end.

 12. /autosort only (deep_series=True), MOVIE names: the release name is cleaned much harder
     (_movie_info(): @handles, site names, release groups, quality / codec / language words, the
     year in brackets; a language word is kept as a signal), a title that is only digits ("2012")
     is allowed when the year agrees, spaced initials are joined ("M S M" -> "MSM"), titles are
     scored with rapidfuzz (difflib when it is not installed), and a film TMDB does not know is
     tried with transliteration variants ("Chaadum" / "Chadum", year + Indian language needed) and
     then on Wikidata (label + year + film class). Archives (zip, rar, 7z ...) become "files"
     instead of "other". classify_async(info=...) also says WHY a file is "other":
     info["other_reason"] / info["other_detail"] (read by handlers/autosort.py).
     See "deep MOVIE identification" in the middle of this file.

 13. /autosort only (deep_series=True): identification has to be PROVEN, not guessed.
     a. Anime: "Episode N" without a season and dub words only raise the PRIORITY of an AniList lookup. The verdict
        needs an authority - an AniList match with Asian origin, the Fribb anime list, TMDB Animation + Asian
        origin, or Jikan when AniList failed. AniList "not found" is an answer: the file stays a series / movie.
        The entry must also FIT the file: "Episode 25" is not a 2-episode OVA that merely shares the title.
     b. Pipe names "SHOW | Episode N | Original title | Dubbed in English | CODE 1080p25.0fps 3843": the show is the
        segment before "Episode N", the original title is the second candidate, junk segments never are one.
     c. Series signals: season packs ("S01 Combined / Complete"), "1of4" / "Part 1 of 4" / "S01 EP01" markers and
        TV-source tags (PDTV / HDTV / DSR / TVRip / DVBS). Broadcaster prefixes (Ch5, BBC ...) and site tags
        (MVGroup.org, torrentgalaxy.to) are cut out of the title.
     d. A film / show TMDB's own title search does not know is searched with spelling variants, then TMDB's year +
        language pool, then IMDb suggestions (OMDb with OMDB_API_KEY), then Wikidata (TVmaze for shows).
     e. Education is only a candidate: a source that knows the show wins over soft course words ("Lessons") and a
        bracketed year; with no confirmation, course signals send the file to education instead of other.

 14. /autosort only, round 2 (each rule has its own env switch, default on; 0 = the old behaviour; an unexpected
     exception in a new rule logs one WARNING and falls back to the old behaviour for that file):
     a. Alternate titles ("Ne Zha A.K.A. Nezha Mo tong jiang shi", "A / B", "A | B", "A (B)") are all candidates, the main
        title first; release tags ("TRUE", "HQ", "HDRip"), hashtags and place / institution segments are not
        (TITLE_ALT_SPLIT). "House Mates" / "Housemates" / "WALL-E" / "WALL.E" are one title (TITLE_KEY_MATCH) and the
        joined / split spellings are extra search queries (TITLE_SPACE_VARIANTS). A 2-word prefix of a longer title
        ("Blue Eye") is only a candidate when the name has a year that the result agrees with (PREFIX2_NEEDS_YEAR).
     b. Season packs ("S01", "Season 1", Complete / Batch ...) are a strong series / anime marker (SEASONPACK_AS_MARKER,
        is_season_pack()); handlers/autosort.py skips the maximum-length check for them.
     c. AniList entries of ANY country of origin count as anime when the title matches EXACTLY, the year agrees, the
        format is TV / TV_SHORT / ONA / OVA / MOVIE / SPECIAL and TMDB does not call the title live-action
        (ANILIST_ANY_ORIGIN; 0 = only JP / CN / KR / TW as before).
     d. Indian exam-prep / course videos need two independent signals (course words, exam words, subject words,
        teacher words, a "A | B | C #tag" list) to be education; a source that knows a movie / series wins
        (EDU_EXAM_RULES).
     e. The manami anime-offline-database is an offline anime authority when AniList is off or failing, loaded once in
        the background (USE_ANIME_OFFLINE_DB). The optional "parsett" parser (import PTT) cross-checks the episode
        number when guessit finds none (USE_PTT_PARSER, default off, needs Python 3.11+).
     f. A file whose name is truncated ("v .mp4") reads up to CAPTION_LINES_WHEN_NAME_USELESS caption lines.
     g. One thumbnail gate for all native-video channels (MOVIE_REQUIRE_THUMB, EDUCATION_REQUIRE_THUMB,
        EDUCATION_THUMB_RATIO_MIN / _MAX / _RATIO_UNKNOWN): see handlers/autosort.py.
     h. Fail-safe: the helpers of these rules are wrapped by _new_rule() (an exception = one WARNING a minute and the
        neutral answer = the old behaviour), and a name whose deep parse raises is parsed again with every round-2
        switch off (_parse_name_safe()). Nothing is ever dropped: a file nothing identifies goes to Other.

 15. /autosort only, round 3 (every rule has its own env switch, default on; 0 = the old behaviour; an exception in a new
     rule logs one WARNING and falls back to the old behaviour for that file):
     a. A file is a VIDEO by what it is, not only by its extension (DEEP_VIDEO_BY_MIME): a native Telegram video, a
        document whose MIME type is video/*, a container missing from VIDEO_EXTENSIONS (.mpg .3gp .m2ts ...) and a
        document with no real extension whose name is a release name ("Title.2019.1080p.BluRay.x264-GRP": the "extension"
        is ".x264-GRP") are identified; before they went to Other as "other_type".
     b. A generic file name ("VID_2023...", "video_12", "Telegram Video") reads the wide caption window, like a truncated
        one (GENERIC_NAME_USELESS).
     c. A caption line with a year or SxxExx plus a quality tag is a candidate name wherever it sits in the caption, not
        only when it is one of the first two usable lines (CAPTION_SIGNAL_LINES).
     d. A season / episode that appears only in the CAPTION never turns a movie file name (year + quality, no marker)
        into a TV-only search (CAPTION_EP_SERIES_ONLY).
     e. Strong course names (numbered lecture, "Lecture 5", "class 12 chapter 3") without a year / release tag are
        education BEFORE any TMDB / AniList call (EDU_PRECHECK). Soft course words stay candidates (a source that knows
        the show still wins).
     f. info["ident"] is the record of what was identified (kind, canonical TMDB / AniList title, year, source, id).
        handlers/autosort.py rebuilds the caption from it and checks the edited caption parses back to the same
        type / season / episode / year (IDENT_RECORD).
     g. Final check before a file stays in Other (FINAL_PASS): audio / pdf / image / archive / video signals by MIME and
        cleaned extension, then ONE more run of the deep pipeline over a wider candidate set (every caption line, the
        classic guessit titles, transliteration variants), cached per title. info["final_pass"] says what was tried;
        info["other_reason"] / ["other_detail"] stay precise.

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
import functools
import logging
import time
import unicodedata
from collections import OrderedDict
from difflib import SequenceMatcher
from urllib.parse import quote, unquote

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

try:                                    # optional: only used by the deep series matcher
    from rapidfuzz import fuzz as _rf_fuzz
except ImportError:
    _rf_fuzz = None


# ============================================================
# Round-2 switches (every new /autosort rule has its own env switch; 0 / false / no / off = the OLD behaviour)
# ============================================================
def _env_on(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _env_count(name: str, default: int, lo: int = 0, hi: int = 50) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, "").strip() or default)))
    except ValueError:
        return default


_RULE_WARNED: dict = {}


def _new_rule(default):
    """Decorator for the helpers of the round-2 rules. An unexpected exception inside one of them logs ONE WARNING (per helper,
    at most once a minute) and the helper answers `default` - the neutral value meaning "this rule says nothing", i.e. the
    OLD behaviour for that file. `default` is a callable called with the helper's own arguments."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as e:
                now = time.monotonic()
                if now - _RULE_WARNED.get(fn.__name__, -1e9) > 60:
                    _RULE_WARNED[fn.__name__] = now
                    logger.warning("autosort: new rule %s failed (%s: %s) - old behaviour for this file",
                                   fn.__name__, type(e).__name__, e)
                try:
                    return default(*a, **k)
                except Exception:
                    return None
        return wrapper
    return deco


TITLE_ALT_SPLIT = _env_on("TITLE_ALT_SPLIT")                   # 3.5  "A.K.A." / " / " / " | " / "(Alt)" alternate titles + junk candidates out
TITLE_SPACE_VARIANTS = _env_on("TITLE_SPACE_VARIANTS")         # 3.5  joined / split spelling variants in the search queries
TITLE_KEY_MATCH = _env_on("TITLE_KEY_MATCH")                   # 3.5  "WALL-E" = "WALL·E" = "House Mates" = "Housemates" in the title scorers
PREFIX2_NEEDS_YEAR = _env_on("PREFIX2_NEEDS_YEAR")             # 3.2  a 2-word prefix of a longer title ("Blue Eye") needs a year that agrees
SEASONPACK_AS_MARKER = _env_on("SEASONPACK_AS_MARKER")         # 3.4  a season pack ("S01", "Season 1", "Complete") is a strong series / anime marker
EDU_EXAM_RULES = _env_on("EDU_EXAM_RULES")                     # 3.6  Indian exam-prep / course names: two independent signals = education
ANILIST_ANY_ORIGIN = _env_on("ANILIST_ANY_ORIGIN")             # 3.2  anime-style Western animation listed by AniList counts as anime
USE_ANIME_OFFLINE_DB = _env_on("USE_ANIME_OFFLINE_DB")         # 3.7  manami anime-offline-database as an offline anime authority
USE_PTT_PARSER = _env_on("USE_PTT_PARSER", False)              # 3.7  optional second parser ("parsett", import name PTT); needs Python 3.11+
CAPTION_LINES_WHEN_NAME_USELESS = _env_count("CAPTION_LINES_WHEN_NAME_USELESS", 6, 2, 12)   # caption lines read when the file name is truncated

# ---- round 3 (identification coverage, Step 1 / Step 2 records, the final check before Other). Same convention as round 2:
# default on, 0 / false / no / off = the OLD behaviour; a helper that raises logs ONE WARNING (see _new_rule) and answers
# its neutral value. All of them are /autosort only (deep mode): /fwd never sees them.
DEEP_VIDEO_BY_MIME = _env_on("DEEP_VIDEO_BY_MIME")             # 15a  video by MIME type / known container / extension-less release name
GENERIC_NAME_USELESS = _env_on("GENERIC_NAME_USELESS")         # 15b  "VID_2023", "video_12", "Telegram Video" read the wide caption window
CAPTION_SIGNAL_LINES = _env_on("CAPTION_SIGNAL_LINES")         # 15c  a caption line with year / SxxExx + quality is a candidate wherever it sits
CAPTION_EP_SERIES_ONLY = _env_on("CAPTION_EP_SERIES_ONLY")     # 15d  a caption-only season / episode never makes a movie file name TV-only
EDU_PRECHECK = _env_on("EDU_PRECHECK")                         # 15e  strong course names are education before any TMDB / AniList call
IDENT_RECORD = _env_on("IDENT_RECORD")                         # 15f  info["ident"]: canonical title / year of the identified record
FINAL_PASS = _env_on("FINAL_PASS")                             # 15g  one full re-check before a file stays in Other

# ---- round 4 (/autosort only, deep mode). Same convention: default on, 0 / false / no / off = the OLD behaviour; every helper is
# wrapped with _new_rule (an exception = ONE WARNING + the old behaviour for that file).
AUTOSORT_VIDEO_FIRST = _env_on("AUTOSORT_VIDEO_FIRST")         # 4a  a video / release name ending in ".AAC" / ".DTS" is a VIDEO, not audio
AUTOSORT_ALL_CAPTION_LINES = _env_on("AUTOSORT_ALL_CAPTION_LINES")   # 4b  every usable caption line is a candidate (promo lines out)
AUTOSORT_CAPTION_PRIMARY = _env_on("AUTOSORT_CAPTION_PRIMARY") # 4c  a useless / generic file name goes AFTER the caption lines

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
# Only /autosort (classify_async(deep_series=True)) knows these audio formats too, so a ".ac3" / ".dts" soundtrack goes to
# Audio instead of Other; /fwd and its category filters keep AUDIO_EXTENSIONS exactly as before.
DEEP_AUDIO_EXTENSIONS = AUDIO_EXTENSIONS | {".ac3", ".eac3", ".dts", ".mka", ".aiff", ".wma", ".amr", ".ape", ".wv", ".m4b"}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".wmv", ".ts", ".m4v", ".flv"}
# Only /autosort (DEEP_VIDEO_BY_MIME) also knows these containers as video; /fwd keeps VIDEO_EXTENSIONS exactly as before.
_DEEP_EXTRA_VIDEO_EXTENSIONS = {".mpg", ".mpeg", ".3gp", ".m2ts", ".mts", ".vob", ".divx", ".ogv", ".rmvb", ".asf", ".f4v", ".ogm"}
# Archives. Only /autosort (classify_async(deep_series=True)) files them under "files"
# ("Files (Unidentified)"); everywhere else they stay "other" exactly as before. Split-archive
# forms (.7z.001, .part1.rar, .r00) are already caught by _SPLIT_PART_PATTERNS below.
ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso"}

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


# Deep pipe names ("SHOW | Episode 25 | Original Title | Dubbed in English | GK11 1080p25.0fps 3843.mp4"): a segment made
# only of these words, a number or a channel code ("GK11") is never a title. PIPE_JUNK_WORDS adds more, comma separated.
_PIPE_JUNK_WORDS = {
    "dubbed", "dub", "subbed", "sub", "subs", "subtitles", "subtitle", "english", "eng", "hindi", "urdu", "tamil", "telugu",
    "malayalam", "kannada", "bengali", "bangla", "punjabi", "marathi", "gujarati", "official", "full", "episode", "episodes",
    "ep", "part", "parts", "new", "latest", "complete", "whole", "hd", "uhd", "series", "serial", "show", "web", "live",
    "moments", "moment", "drama", "dramas", "awards", "award", "performances", "performance", "highlights", "highlight",
    "promo", "scenes", "scene", "clips", "clip", "songs", "song", "interview", "interviews", "video", "videos", "channel",
    "audio", "in", "with", "and", "the", "of", "a", "to",
    *[w.strip().lower() for w in os.environ.get("PIPE_JUNK_WORDS", "").split(",") if w.strip()],
}
_PIPE_CODE_RE = re.compile(r"^[A-Za-z]{1,4}\d{1,4}$")
_EP_SEG_RE = re.compile(r"^\W*(?:(?:full|latest|new|complete|whole)\s+)?(?:episodes?|ep|epi)\b\.?\s*#?\s*0*\d{0,4}\W*$",
                        re.IGNORECASE)


def _pipe_junk(seg: str) -> bool:
    words = re.findall(r"[^\W_]+", seg or "", re.UNICODE)
    if not words or (len(words) == 1 and _PIPE_CODE_RE.match(words[0])):
        return True
    if TITLE_ALT_SPLIT and (seg or "").lstrip().startswith("#"):
        return True                                           # a hashtag ("#pareeksir") is a channel tag, never a title
    return all(w.lower() in _PIPE_JUNK_WORDS or w.isdigit() for w in words)


# ---- junk title candidates (deep mode, TITLE_ALT_SPLIT) ------------------------------------------------------------
# A release / quality tag ("TRUE", "HQ", "HDRip", "TRUE WEB-DL"), a hashtag, or - in a "A | B | C" list - a segment that is
# only places / institutions / exam names ("Haryana CET Mains & Group D", "Delhi Police", "Railway Special") is never a
# title. The MAIN title of a name is never removed by these rules; they only prune the EXTRA candidates.
_RELEASE_JUNK_WORDS = {
    "true", "hq", "hd", "uhd", "sd", "fhd", "4k", "2k", "hdrip", "webrip", "webdl", "web", "dl", "bluray", "bdrip", "brrip",
    "dvdrip", "dvdscr", "hdtv", "hdcam", "camrip", "cam", "predvd", "pre", "dvd", "proper", "repack", "remux", "org",
    "original", "dubbed", "dub", "dual", "multi", "audio", "esub", "esubs", "hsub", "hsubs", "msub", "msubs", "sub", "subs",
    "hevc", "x264", "x265", "h264", "h265", "avc", "av1", "aac", "ac3", "eac3", "ddp", "dd", "dts", "hdr", "sdr", "10bit",
    "8bit", "nf", "amzn", "dsnp", "hs", "zee5", "complete", "full", "movie", "film", "line", "extended", "uncut", "unrated",
    "imax", "remastered", "theatrical", "edition", "version", "rip", "print", "clean", "hindi", "english", "tamil", "telugu",
    "malayalam", "kannada", "bengali", "punjabi", "marathi", "gujarati", "urdu", "eng", "hin", "tam", "tel", "mal", "kan",
    "chi", "jpn", "kor", "fre", "ita", "spa", "ger", "rus", "vff", "vf", "vostfr", "multi", "mkv", "mp4", "x", "kbps",
}
# Exam / board words (also the education signal "board and exam words", see _edu_signals()).
_EDU_EXAM_WORDS = {
    "ncert", "cbse", "icse", "ssc", "cet", "upsc", "neet", "jee", "gate", "railway", "railways", "rrb", "ntpc", "police",
    "banking", "tet", "ctet", "htet", "uptet", "reet", "ibps", "sbi", "rbi", "nda", "cds", "afcat", "constable", "patwari",
    "mains", "prelims", "pyq", "ias", "ips", "cuet", "clat", "net", "jrf", "bpsc", "uppsc", "mppsc", "rpsc", "hssc", "hpsc",
    *[w.strip().lower() for w in os.environ.get("EDU_EXAM_WORDS", "").split(",") if w.strip()],
}
_PLACE_INST_WORDS = {
    "andhra", "assam", "bihar", "goa", "gujarat", "haryana", "himachal", "jharkhand", "karnataka", "kerala", "madhya",
    "maharashtra", "manipur", "odisha", "orissa", "punjab", "rajasthan", "sikkim", "tamil", "nadu", "telangana", "tripura",
    "uttar", "pradesh", "uttarakhand", "bengal", "delhi", "mumbai", "kolkata", "chennai", "bangalore", "bengaluru",
    "hyderabad", "pune", "lucknow", "jaipur", "patna", "bhopal", "nagpur", "india", "indian", "up", "mp", "hp", "jammu",
    "kashmir", "ladakh", "chandigarh", "army", "navy", "airforce", "defence", "defense", "bank", "board", "commission",
    "department", "ministry", "court", "judiciary", "teacher", "teaching", "exam", "exams", "examination", "recruitment",
    "vacancy", "notification", "govt", "government", "sarkari", "job", "jobs", "group", "special", "batch", "live", "gd",
    "clerk", "po", "si", "forest", "revenue", "state", "central", "national", "all", "and", "for", "the", "of", "in", "a",
    "b", "c", "d", "e",
}


@_new_rule(lambda *a, **k: [])
def _title_words(text: str) -> list:
    return [w.lower() for w in re.findall(r"[^\W_]+", text or "", re.UNICODE)]


@_new_rule(lambda *a, **k: False)
def _only_words(text: str, *sets) -> bool:
    """Every word of `text` is a digit or belongs to one of the sets (and there is at least one word)."""
    words = _title_words(text)
    return bool(words) and all(w.isdigit() or any(w in s for s in sets) for w in words)


@_new_rule(lambda *a, **k: False)
def _extra_title_junk(text: str, pipe_list: bool = True) -> bool:
    """True for an EXTRA title candidate that is certainly not a title: a hashtag, nothing but release / quality /
    language tags ("TRUE", "HQ HDRip", "TRUE WEB-DL"), or - with pipe_list - only places / institutions / exam names /
    lesson-list words ("Concept", "Theory", "Numericals")."""
    t = (text or "").strip()
    if not t or t.startswith("#"):
        return True
    if _only_words(t, _RELEASE_JUNK_WORDS):
        return True
    return bool(pipe_list and _only_words(t, _PLACE_INST_WORDS, _EDU_EXAM_WORDS, _RELEASE_JUNK_WORDS, _EDU_LIST_WORDS))


def _pipe_titles_deep(name: str) -> list:
    """Deep (/autosort) title candidates of a pipe-separated name, best first. The SHOW is the last real segment
    BEFORE the "Episode N" segment, the segments AFTER it are alternate (original) titles, earlier ones are headlines;
    junk segments (see _PIPE_JUNK_WORDS) and the downloader tail ("1080p25.0fps 3843") never become titles.
    "FORBIDDEN LOVE | Episode 25 | Rabba Ishq Na Hove | Dubbed in English | GK11 1080p25.0fps 3843.mp4"
    -> ["FORBIDDEN LOVE", "Rabba Ishq Na Hove"]. A name with no "Episode N" segment keeps _pipe_titles()'s order
    (later, short, comma-free segments first) with the junk segments left out."""
    stem = _strip_release_noise(_VIDEO_EXT_TAIL_RE.sub("", name))
    raw = [s.strip() for s in re.split(r"\s*[|｜]\s*", stem)]
    ep_idx = next((i for i, s in enumerate(raw) if _EP_SEG_RE.match(s)
                   or re.fullmatch(_DEEP_EP_RE.pattern, s, re.IGNORECASE)), None)
    segs = []
    # a "Title | A | B | C" list WITHOUT an episode segment (a YouTube lecture / promo name): the segments after the
    # first that are only places / institutions / exam names ("Delhi Police", "Railway Special") are not titles
    list_mode = bool(TITLE_ALT_SPLIT and ep_idx is None and len(raw) >= 3)
    for idx, seg in enumerate(raw):
        seg = _EDGE_JUNK_RE.sub("", re.sub(r"\s{2,}", " ", _SEG_NOISE_RE.sub(" ", seg))).strip()
        if idx == ep_idx or len(re.findall(r"\w", seg)) < 2 or _pipe_junk(seg):
            continue
        if list_mode and idx > 0 and _extra_title_junk(seg):
            continue
        segs.append((idx, seg[:80]))
    if ep_idx is not None:
        before = [t for t in segs if t[0] < ep_idx]
        after = [t for t in segs if t[0] > ep_idx]
        ordered = (before[-1:] + after + before[:-1])
    else:
        ordered = sorted(segs, key=lambda t: (("," in t[1]) or ("&" in t[1]), len(t[1].split()) > 7, t[0] == 0, t[0]))
    out = []
    for _idx, seg in ordered:
        if seg not in out:
            out.append(seg)
    return out


def _pipe_titles(name: str, deep: bool = False) -> list:
    """Title candidates from a pipe-separated name, best first.

    "Arjun ne kyun kiya... | Kkoi Dil Mein Hai | Full Episode | Episode 29"
    -> ["Kkoi Dil Mein Hai", "Arjun ne kyun kiya..."]

    The show name is usually NOT the first segment (that is the episode
    headline), so later, short, comma-free segments are tried first; cast
    lists ("A, B") and long headlines go last. deep=True (/autosort): see _pipe_titles_deep().
    """
    if deep:
        return _pipe_titles_deep(name)
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


def _candidate_names(message, max_lines: int = 2, signal: bool = False) -> list:
    """Filename first, then up to `max_lines` (default two) usable caption lines.
    signal=True (/autosort's deep mode, CAPTION_SIGNAL_LINES): up to two further caption lines that carry a clear release
    signal are appended (see _signal_lines()); everything else is unchanged."""
    names = []
    fn = get_filename(message)
    deep_all = bool(signal and AUTOSORT_ALL_CAPTION_LINES)
    if deep_all:
        max_lines = max(max_lines, CAPTION_LINES_WHEN_NAME_USELESS)     # 4b: every usable caption line, not only two
    name_last = bool(fn and signal and _caption_primary(fn))              # 4c: a useless name ("v .mp4") goes last
    if fn and not name_last:
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
        if deep_all and (_promo_line(line) or line in names):
            continue
        names.append(line)
        added += 1
        if added >= max_lines:
            break
    if signal and CAPTION_SIGNAL_LINES:
        names += _signal_lines(caption, names)
    if name_last:
        names.append(fn)
    return names


# Caption lines that are only channel promotion ("Join @MoviesHub for more", "Share & Support Us", "Uploaded By ...").
_PROMO_WORDS = {
    "join", "joined", "subscribe", "follow", "share", "support", "us", "our", "for", "more", "movies", "movie", "series", "new",
    "latest", "updates", "update", "channel", "channels", "group", "telegram", "tg", "click", "here", "link", "links", "download",
    "watch", "online", "free", "request", "requests", "uploaded", "upload", "by", "powered", "backup", "main", "official",
    "and", "the", "to", "now", "daily", "all", "hd", "best", "your", "please", "via", "from", "on", "at", "of", "in",
}
_PROMO_START_RE = re.compile(r"^\W*(?:join|subscribe|follow|share|support|click|uploaded|upload(?:ed)?\s+by|powered\s+by|"
                             r"request|backup|main\s+channel)\b", re.IGNORECASE)


@_new_rule(lambda *a, **k: False)
def _promo_line(line: str) -> bool:
    """AUTOSORT_ALL_CAPTION_LINES: True for a caption line that is only channel promotion. A line with a year or an episode marker
    is never promotion (it may be the title line)."""
    cleaned = _clean_name(line or "")
    if re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", cleaned) or _EP_MARKER_RE.search(cleaned):
        return False
    words = re.findall(r"[^\W\d_]+", cleaned.lower())
    if not words:
        return False
    return bool(_PROMO_START_RE.match(cleaned)) or all(w in _PROMO_WORDS for w in words)


@_new_rule(lambda *a, **k: False)
def _caption_primary(filename) -> bool:
    """AUTOSORT_CAPTION_PRIMARY: the file name is missing, truncated or generic ("video_123.mp4", "v .mp4", "IMG_2023"), so the
    caption is the primary source - its lines are tried first, the name last."""
    if not AUTOSORT_CAPTION_PRIMARY or not filename:
        return False
    return bool(_name_is_useless(filename) or _name_is_generic(filename))


@_new_rule(lambda *a, **k: False)
def _video_first(message, filename: str, ext: str) -> bool:
    """AUTOSORT_VIDEO_FIRST: a file whose "extension" is an audio tag is a VIDEO when
      * it is a native Telegram video, or its MIME type is video/*, or
      * it is not sent as Telegram audio / voice, its MIME type is not audio/*, and the name is a release name
        (year / SxxExx + quality / codec tags): "Movie.2019.720p.WEB-DL.DDP5.1.AAC", "Show.S01E02.1080p.x264.DTS".
    Real music (".mp3" / ".flac" / ".m4a" ... sent as audio, or a document with an audio/* MIME type) stays audio."""
    if not AUTOSORT_VIDEO_FIRST or ext not in DEEP_AUDIO_EXTENSIONS:
        return False
    if getattr(message, "video", None) is not None or _doc_video_mime(message):
        return True
    if getattr(message, "voice", None) is not None:
        return False
    # Telegram guesses the MIME type from the fake extension (".AAC" -> audio/aac), so the MIME type alone proves nothing here:
    # a VIDEO quality / source / codec tag together with a year or an episode marker does. Music never carries "720p WEB-DL".
    stem = (filename or "")[: -len(ext)] if ext and (filename or "").lower().endswith(ext) else (filename or "")
    if not _VIDEO_TAG_RE.search(stem):
        return False
    return bool(re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", stem) or _EP_MARKER_RE.search(stem) or _SEASON_RE.search(stem))


_VIDEO_TAG_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{3,4}[pi]|4k|uhd|web[ ._-]?dl|web[ ._-]?rip|webrip|blu[ ._-]?ray|bdrip|brrip|hdrip|dvdrip|dvdscr|"
    r"hdtv|hdcam|camrip|pre[ ._-]?dvd|x26[45]|h[ ._]?26[45]|hevc|avc|xvid|divx|10[ ._-]?bit|hdr10?|dovi|remux|amzn|nf|dsnp|"
    r"hmax|zee5|jiohs|sonyliv)(?![A-Za-z0-9])", re.IGNORECASE)


@_new_rule(lambda *a, **k: [])
def _signal_lines(caption: str, have: list) -> list:
    """Up to two caption lines (of the first 14) that are not candidates yet and carry a clear release signal: a year or an
    SxxExx marker TOGETHER with a quality / source / codec tag. Channel posts often open with promo lines and put the real
    name on line 3 or 4; before, only the first two usable lines were ever looked at. Pure string work."""
    out = []
    seen = {_norm_cmp(_clean_name(x)) for x in have}
    for line in (caption or "").splitlines()[:14]:
        line = _CAPTION_LABEL_RE.sub("", line.strip())
        if not line or _CAPTION_META_RE.match(line) or line in have:
            continue
        cleaned = _clean_name(line)
        if len(re.findall(r"\w", cleaned)) < 2:
            continue
        key = _norm_cmp(cleaned)
        if key in seen:
            continue
        mv = _movie_info(line)
        if mv["usable"] and mv["tech"] and (mv["year"] or _EP_MARKER_RE.search(cleaned)):
            out.append(line)
            seen.add(key)
            if len(out) >= 2:
                break
    return out


@_new_rule(lambda *a, **k: False)
def _name_is_useless(filename) -> bool:
    """A truncated / empty file name ("v .mp4"): fewer than 4 letters or digits before the extension."""
    stem = os.path.splitext(filename or "")[0]
    return len(re.findall(r"[^\W_]", stem)) < 4


# Camera / downloader / messenger names carry no title ("VID_20231105_123456", "video_12", "Telegram Video", "IMG-0001-WA0001").
_GENERIC_NAME_WORDS = {
    "vid", "video", "videos", "img", "image", "file", "files", "document", "doc", "media", "movie", "clip", "screenrecord",
    "screenrecording", "screen", "recording", "record", "recorded", "telegram", "whatsapp", "wa", "mov", "mvi", "dsc", "pxl",
    "download", "downloaded", "unknown", "untitled", "new", "copy", "final", "output", "out", "tmp", "temp", "reel", "reels",
    "insta", "instagram", "fb", "facebook", "vlc", "trim", "edited", "edit", "pic", "photo", "mp", "mp4", "mkv",
}


@_new_rule(lambda *a, **k: False)
def _name_is_generic(filename) -> bool:
    """GENERIC_NAME_USELESS: the file name is only generic words and numbers ("VID_2023", "video 12", "Telegram Video"), so the
    title can only be in the caption. Like a truncated name it makes every caption line a candidate. An empty stem is no
    news (the caller handles a missing name)."""
    if not GENERIC_NAME_USELESS or not filename:
        return False
    stem = os.path.splitext(filename)[0]
    words = re.findall(r"[A-Za-z]+", stem)
    if not words:
        return bool(re.search(r"\d", stem))                  # digits only ("20231105_123456")
    return all(w.lower() in _GENERIC_NAME_WORDS for w in words)


def _doc_video_mime(message) -> bool:
    """A DOCUMENT whose MIME type says video/* (a Telegram video sent \"as a file\")."""
    doc = getattr(message, "document", None)
    mime = getattr(doc, "mime_type", None) if doc is not None else None
    return isinstance(mime, str) and mime.lower().startswith("video/")


_KNOWN_EXTS = (PDF_EXTENSIONS | IMAGE_EXTENSIONS | DEEP_AUDIO_EXTENSIONS | VIDEO_EXTENSIONS | _DEEP_EXTRA_VIDEO_EXTENSIONS
               | ARCHIVE_EXTENSIONS | {".srt", ".ass", ".ssa", ".vtt", ".sub", ".txt", ".nfo", ".apk", ".exe", ".epub", ".mobi",
                                       ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".json", ".xml", ".html"})


def _fake_ext(ext: str) -> bool:
    """True when os.path.splitext() found no real extension: the name is empty-tailed ("Title (2019) 1080p WEB-DL") or the
    last dotted piece is a release token ("Title.2019.1080p.BluRay.x264-GRP" -> ".x264-GRP", "...WEB-DL" -> ".WEB-DL")."""
    if not ext:
        return True
    if ext in _KNOWN_EXTS:
        return False
    body = ext[1:]
    return len(body) > 6 or "-" in body or body.isdigit() or bool(_MOVIE_STRONG_RE.match(body))


@_new_rule(lambda *a, **k: False)
def _release_signal(name: str) -> bool:
    """The name is clearly a release name: a year or an episode marker TOGETHER with a quality / source / codec tag."""
    mv = _movie_info(name)
    return bool(mv["usable"] and mv["tech"] and (mv["year"] or _EP_MARKER_RE.search(name or "")))


@_new_rule(lambda *a, **k: False)
def _deep_is_video(message, filename: str, ext: str) -> bool:
    """DEEP_VIDEO_BY_MIME (/autosort only). True when a file the extension test calls "not a video" really is one, so it is
    identified instead of landing in Other as "other_type":
      * a native Telegram video, whatever its file name says;
      * a document whose MIME type is video/*;
      * a container missing from VIDEO_EXTENSIONS (.mpg .mpeg .3gp .m2ts ...);
      * a document with no real extension whose name is a release name (year / SxxExx + quality tags).
    Normal video extensions and split archive names (".mkv.001", ".r00") keep their old routing (False here)."""
    if not DEEP_VIDEO_BY_MIME or ext in VIDEO_EXTENSIONS or ext in PDF_EXTENSIONS or ext in IMAGE_EXTENSIONS:
        return False
    if _is_split_part(filename):
        return False
    if ext in _DEEP_EXTRA_VIDEO_EXTENSIONS:
        return True
    videoish = getattr(message, "video", None) is not None or _doc_video_mime(message)
    if ext in DEEP_AUDIO_EXTENSIONS:
        # "Title.2019.720p.HDRip.AAC" has no extension, ".AAC" is its last tag: only a release name sent as a video says so
        return bool(videoish and _release_signal(filename))
    if ext in _KNOWN_EXTS:
        return False                                  # archives, subtitles, text / office files: the extension's verdict stands
    if videoish:
        return True
    return bool(getattr(message, "document", None) is not None and _fake_ext(ext) and _release_signal(filename))


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


# ------------------------------------------------------------
# Deep series parsing - opt-in via _parse_name(..., deep=True), which only
# /autosort's deep_series mode uses (everything else parses exactly as before).
# ------------------------------------------------------------
# ---- Knowledge tables (small and data-driven: add a line to extend) ---------------
# TV channels / OTT platforms: stripped from the title, kept as a positive series
# signal. Long names and unambiguous codes (JIOHS, DSNP, ZEE5, AMZN ...) count anywhere
# as a whole word; the two-letter codes (HS, VT, NF, JC) only right next to a
# resolution / source tag ("1080p.HS.WEB-DL").
_PLATFORM_SURE = (
    r"star\s*plus|star\s*bharat|star\s*vijay|star\s*maa|star\s*jalsha|star\s*suvarna|"
    r"zee\s*tv|zee5|zee\s*(?:kannada|tamil|telugu|marathi|bangla)|"
    r"colors\s*tv|colors\s*(?:kannada|tamil|marathi|bangla)|"
    r"sony\s*tv|sony\s*liv|sonyliv|sliv|sony\s*sab|sab\s*tv|"
    r"disney\+?\s*hotstar|jio\s*hotstar|jiohs|jhs|hotstar|dsnp|"
    r"jio\s*cinema|mx\s*player|mxp|alt\s*balaji|altt|netflix|amazon\s*prime|prime\s*video|amzn|voot|"
    r"sun\s*tv|sun\s*nxt|asianet|&\s*tv|dangal\s*tv"
)
_PLATFORM_AMBIGUOUS = r"star|zee|colors|sony|sab|amazon|aha"      # real words: only bracketed / at an edge
_PLATFORM_SURE_RE = re.compile(rf"(?<![A-Za-z0-9])(?:{_PLATFORM_SURE})(?![A-Za-z0-9])", re.IGNORECASE)
_PLATFORM_EDGE_RE = re.compile(
    rf"[\[\(]\s*(?:{_PLATFORM_AMBIGUOUS})\s*[\]\)]"
    rf"|^\s*(?:{_PLATFORM_AMBIGUOUS})\s*[-–—:|]\s*"
    rf"|\s*[-–—:|]\s*(?:{_PLATFORM_AMBIGUOUS})\s*$",
    re.IGNORECASE,
)
_PLATFORM_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:hs|vt|nf|jc)(?=[\s._-]+(?:web[\s._-]?dl|web[\s._-]?rip|web|hdrip|hdtv|blu[\s._-]?ray|brrip)(?![A-Za-z0-9]))"
    r"|(?<=\d[pi])[\s._-]+(?:hs|vt|nf|jc)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PLATFORM_CODES = {"jiohs", "jhs", "hotstar", "jiohotstar", "dsnp", "hs", "zee5", "sonyliv", "sliv", "voot", "vt",
                   "mxp", "amzn", "nf", "altt", "jc", "jiocinema", "netflix"}

# Languages. Full words may sit anywhere; the 3-letter codes only count outside the show
# title (a "Ben 10" is not Bengali) or glued to a franchise abbreviation ("BBKan").
_LANG_FULL = {"hindi": "hi", "kannada": "kn", "tamil": "ta", "telugu": "te", "malayalam": "ml", "marathi": "mr",
              "bengali": "bn", "bangla": "bn", "punjabi": "pa", "gujarati": "gu", "bhojpuri": "bho"}
_LANG_SHORT = {"hin": "hi", "kan": "kn", "tam": "ta", "tel": "te", "mal": "ml", "mar": "mr",
               "ben": "bn", "pun": "pa", "guj": "gu", "bho": "bho"}
_LANG_ANY = {**_LANG_FULL, **_LANG_SHORT}
_LANG_WORDS = {"hi": ("Hindi",), "kn": ("Kannada",), "ta": ("Tamil",), "te": ("Telugu",), "ml": ("Malayalam",),
               "mr": ("Marathi",), "bn": ("Bangla", "Bengali"), "pa": ("Punjabi",), "gu": ("Gujarati",),
               "bho": ("Bhojpuri",)}

# Franchise abbreviations -> full show name (matched whole, or glued to a language code:
# "BBKan" = BB + Kan). Unknown abbreviations are simply left alone.
_FRANCHISE_ALIASES = {
    "bb": "Bigg Boss", "biggboss": "Bigg Boss",
    "kbc": "Kaun Banega Crorepati",
    "cid": "CID",
    "tmkoc": "Taarak Mehta Ka Ooltah Chashmah",
    "yrkkh": "Yeh Rishta Kya Kehlata Hai",
    "srgmp": "Sa Re Ga Ma Pa",
}
# Franchises that exist in many regional versions (the language decides which show it is).
_REGIONAL_FRANCHISES = {
    "bigg boss": "Bigg Boss", "kaun banega crorepati": "Kaun Banega Crorepati",
    "indian idol": "Indian Idol", "sa re ga ma pa": "Sa Re Ga Ma Pa",
}

# Titles that are never a show name (also: a single letter / number, a bare language).
_GENERIC_TITLES = {"episode", "episodes", "ep", "full episode", "part", "season", "video", "videos", "movie",
                   "film", "clip", "clips", "full movie", "full", "new", "latest", "hd", "untitled", "unknown"}
_GENERIC_WORDS = {"episode", "episodes", "ep", "full", "part", "season", "video", "videos", "movie", "film", "clip",
                  "clips", "new", "latest", "hd", "untitled", "unknown"}
_NOISE_TITLES = {"episode", "episodes", "ep", "full episode", "part", "season", "e"}   # (kept for reference)
_RELEASE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{3,4}[pi]|[248]k|uhd|hdr\d*|web[\s-]?dl|web[\s-]?rip|hdrip|hdtv|blu[\s-]?ray|brrip|dvdrip|"
    r"x26[45]|h[\s.]?26[45]|hevc|avc|av1|10bit|aac|e?ac3|dts|flac)(?![A-Za-z0-9])", re.IGNORECASE)


def _generic_title(title) -> bool:
    """Never a show name: "Episode", "Video", "Part", "Movie", a single letter / number, a bare
    language, generic words plus a number ("Episode 5", "Part 2"), or nothing but an episode
    marker / resolution / codec ("S01E01 720p", "E105")."""
    n = _norm_cmp(title or "")
    if len(re.sub(r"[\W_]+", "", n, flags=re.UNICODE)) < 2 or n.isdigit():
        return True
    if n in _GENERIC_TITLES or n in _LANG_FULL:
        return True
    words = n.split()
    if words and all(w in _GENERIC_WORDS or w.isdigit() for w in words):
        return True
    rest = _RELEASE_TOKEN_RE.sub(" ", _TITLE_MARKER_RE.sub(" ", title or ""))
    return not re.search(r"\w", rest, re.UNICODE)


# Scraper bots write Python's None / null / NaN into file names ("Bigg.Boss.None.S12E01").
_JUNK_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[Nn]one|NONE|[Nn]ull|NULL|NaN|NAN|nan|[Uu]ndefined|UNDEFINED|[Nn]/[Aa])(?![A-Za-z0-9])"
)
# Bracket / paren groups: "[.(AAC.2.0.-.64Kbps)]", "[]", "()" are junk, not a fansub group.
_BRACKET_GROUP_RE = re.compile(r"[\[\(（【「]([^\[\]\(\)（）【】「」]*)[\]\)）】」]")
_AUDIO_FRAG_RE = re.compile(
    r"^(?:aac|ac3|eac3|e-ac3|dd\+?|ddp?\d?|dts(?:-?hd)?|truehd|atmos|flac|mp[234]|opus|ogg|"
    r"\d|\d+ch|\d{2,4}k(?:bps)?|kbps|mbps|stereo|mono|dual|multi|audio)$", re.IGNORECASE)
_BITRATE_RE = re.compile(r"(?<![A-Za-z0-9])\d{2,4}\s?kbps(?![A-Za-z0-9])", re.IGNORECASE)
# Uploader tags: "_Esub_YourShivay", a configurable list, "-Group" right after a codec / source tag.
_ESUB_TAIL_RE = re.compile(r"(?<![A-Za-z0-9])(?:esubs?|hsubs?|msubs?)[\s._-]+([A-Za-z][A-Za-z0-9]{2,24})\s*$", re.IGNORECASE)
_ESUB_RE = re.compile(r"(?<![A-Za-z0-9])(?:esubs?|hsubs?|msubs?)(?![A-Za-z0-9])", re.IGNORECASE)
_UPLOADER_TAGS = [t.strip() for t in os.environ.get("SERIES_UPLOADER_TAGS", "YourShivay").split(",") if t.strip()]
_UPLOADER_RE = (re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(t) for t in _UPLOADER_TAGS) + r")(?![A-Za-z0-9])",
                           re.IGNORECASE) if _UPLOADER_TAGS else None)
_TRAILING_GROUP_RE = re.compile(
    r"((?:x26[45]|h[\s.]?26[45]|hevc|avc|av1|aac(?:[\s._]?\d\.\d)?|e?ac3|ddp?[\s._]?\d\.\d|web[\s._-]?dl|"
    r"web[\s._-]?rip|hdrip|hdtv|blu[\s._-]?ray|brrip|dvdrip)[\s._]*)-([A-Za-z0-9]{2,20})\s*$", re.IGNORECASE)
_VIDEO_EXT_END_RE = re.compile(r"\.(?:mkv|mp4|avi|webm|mov|wmv|ts|m4v|flv)\s*$", re.IGNORECASE)

# Where the technical tail of a release name starts, and where the show title ends.
_TECH_START_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{3,4}[pi]|[248]k|uhd|hdr\d*|web[\s-]?dl|web[\s-]?rip|hdrip|hdtv|blu[\s-]?ray|brrip|dvdrip|"
    r"x26[45]|h[\s.]?26[45]|hevc|avc|av1|10bit|aac|e?ac3|dd[p+]?\s?\d|dts|flac|(?:19|20)\d{2})(?![A-Za-z0-9])",
    re.IGNORECASE)
_TAG_NOISE_WORDS = {"full", "episode", "episodes", "ep", "hd", "uhd", "new", "latest", "official", "complete", "watch",
                    "online", "free", "download", "web", "dl", "webrip", "hdrip", "hdtv", "dubbed", "dub", "sub",
                    "subs", "subbed", "audio", "dual", "multi", "org", "original", "and", "&"}

# A fansub tag must look like a group NAME: letters, at the very START of the name,
# not audio / codec / bitrate / resolution / platform / language text.
_FANSUB_LEAD_RE = re.compile(r"^\s*[\[【「]\s*([^\[\]【】「」]{2,40}?)\s*[\]】」]")
_TECH_TOKEN_RE = re.compile(
    r"^(?:\d{3,4}[pi]|[248]k|uhd|hdr\d*|web|dl|webrip|hdrip|hdtv|bluray|brrip|dvdrip|x26[45]|h26[45]|hevc|avc|av1|"
    r"10bit|8bit|aac|e?ac3|ddp?\d?|dts|flac|mp3|\d+k(?:bps)?|(?:19|20)\d{2}|multi|dual|esubs?|hsubs?|msubs?|subs?|"
    r"dubbed|hd|official|complete|batch)$", re.IGNORECASE)


def _url_decode(s: str) -> str:
    """%5B -> [ , %20 -> space ... Only when the result is valid UTF-8 (a name like
    "100%adventure" stays as it is). Never raises."""
    for _ in range(2):
        if not re.search(r"%[0-9A-Fa-f]{2}", s):
            break
        try:
            decoded = unquote(s, errors="strict")
        except Exception:
            break
        if decoded == s:
            break
        s = decoded
    return s


def _fansub_group(name: str):
    """The fansub group name when `name` STARTS with a plausible one ("[SubsPlease] ..."), else
    None. Junk groups such as "[.(AAC.2.0.-.64Kbps)]", resolution / codec / language / platform
    tags and groups in the middle or at the end of the name do not count."""
    m = _FANSUB_LEAD_RE.match(name or "")
    if not m:
        return None
    group = m.group(1).strip()
    if not re.match(r"[A-Za-z]", group) or len(re.findall(r"[A-Za-z]", group)) < 2 or re.search(r"[()]", group):
        return None
    tokens = [t for t in re.split(r"[\s._\-+,]+", group) if t]
    for t in tokens:
        low = re.sub(r"[^a-z0-9]", "", t.lower())
        if not (_TECH_TOKEN_RE.match(t) or low in _LANG_ANY or low in _PLATFORM_CODES or _PLATFORM_SURE_RE.fullmatch(t)):
            return group                      # at least one real word: a plausible group name
    return None


def _normalize_release_name(name: str) -> dict:
    """Deep-mode clean-up of a release name BEFORE guessit / anitopy see it (order matters):
      1. (URL-decoding happens one step earlier, in _parse_name, on the raw string)
      2. junk tokens (None / null / NaN / undefined / N/A) are deleted
      3. empty or garbage bracket groups and bitrate fragments are deleted
      4. uploader / channel tags: "_Esub_<Name>", the SERIES_UPLOADER_TAGS list, a "-Group"
         right after a codec / source tag
    Returns {"name", "junk_removed", "uploaders", "group_removed"}. `name` keeps the original
    separators and the file extension (guessit uses it)."""
    ext, stem = "", name
    m = _VIDEO_EXT_END_RE.search(name)
    if m:
        stem, ext = name[:m.start()], m.group(0).strip()

    junk = False
    cleaned = _JUNK_TOKEN_RE.sub(" ", stem)
    if cleaned != stem and re.search(r"[^\W_]", cleaned):
        junk, stem = True, cleaned

    def _group(mm):
        toks = [t for t in re.split(r"[\s._\-+,|/:;]+", mm.group(1)) if t]
        return " " if all(_AUDIO_FRAG_RE.match(t) for t in toks) else mm.group(0)

    for _ in range(4):                                   # inner groups first, then the shell left behind
        new = _BRACKET_GROUP_RE.sub(_group, stem)
        if new == stem:
            break
        stem = new
    stem = _BITRATE_RE.sub(" ", stem)

    uploaders = []
    m = _ESUB_TAIL_RE.search(stem)
    if m and m.group(1).lower() not in _LANG_ANY and not _TECH_TOKEN_RE.match(m.group(1)):
        uploaders.append(m.group(1))
        stem = stem[:m.start()]
    stem = _ESUB_RE.sub(" ", stem)
    if _UPLOADER_RE is not None:
        uploaders += [um.group(0) for um in _UPLOADER_RE.finditer(stem)]
        stem = _UPLOADER_RE.sub(" ", stem)

    group_removed = False
    stem = re.sub(r"[\s._\-–—:|~]+$", "", stem)
    m = _TRAILING_GROUP_RE.search(stem)
    if m and m.group(2).lower() not in _LANG_ANY:
        stem, group_removed = stem[:m.start()] + m.group(1), True

    stem = re.sub(r"([._\s])[._\s]+", r"\1", stem)       # runs of separators -> one
    stem = re.sub(r"^[\s._\-–—:|~]+|[\s._\-–—:|~]+$", "", stem)
    return {"name": stem + ext, "junk_removed": junk, "uploaders": uploaders, "group_removed": group_removed}


# Extra episode formats: "E105", "1x05" (plus the "Episode N" / "Ep N" / "S01E02" ones)
_DEEP_EP_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?:episodes?|ep|epi|एपिसोड)\.?[\s._#:-]*0*\d{1,4}"
    r"|s\d{1,2}[ ._-]?e\d{1,4}"
    r"|e0*\d{2,4}"
    r"|(?!(?:16x9|4x3|21x9)(?![A-Za-z0-9]))\d{1,2}x0*\d{1,3}"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PART_RE = re.compile(r"(?<![A-Za-z0-9])part[\s._-]*0*\d{1,3}(?![A-Za-z0-9])", re.IGNORECASE)
_TITLE_MARKER_RE = re.compile(_DEEP_EP_RE.pattern + "|" + _SEASON_RE.pattern, re.IGNORECASE)

# sample / trailer / promo / recap ... also for EPISODES (the shared _EXTRA_RE skips those)
_EXTRA_DEEP_RE = re.compile(
    r"(?<![A-Za-z0-9])(sample|trailer|teaser|promos?|recap|featurette|behind[ ._-]the[ ._-]scenes|"
    r"making[ ._-]of|deleted[ ._-]scenes?)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# "5.1.3 Intro to Python": numbered lecture / module names
_EDU_NUMBERED_RE = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2}){1,3}[\s._-]+[A-Za-z]")

_MONTHS = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
_MONTH_NUM = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_DATE_PATTERNS = (
    ("ymd", re.compile(r"(?<![0-9])((?:19|20)\d{2})[-._/](\d{1,2})[-._/](\d{1,2})(?![0-9])")),
    ("dmy_w", re.compile(rf"(?<![A-Za-z0-9])(\d{{1,2}})(?:st|nd|rd|th)?[\s._,-]*({_MONTHS})(?![A-Za-z])[\s._,-]+((?:19|20)\d{{2}})(?![0-9])", re.IGNORECASE)),
    ("mdy_w", re.compile(rf"(?<![A-Za-z0-9])({_MONTHS})(?![A-Za-z])[\s._-]+(\d{{1,2}})(?:st|nd|rd|th)?[\s._,-]+((?:19|20)\d{{2}})(?![0-9])", re.IGNORECASE)),
    ("num", re.compile(r"(?<![0-9])(\d{1,2})[-._/](\d{1,2})[-._/]((?:19|20)\d{2})(?![0-9])")),
)


def _date_from(kind: str, g):
    try:
        if kind == "ymd":
            return datetime.date(int(g[0]), int(g[1]), int(g[2]))
        if kind == "dmy_w":
            return datetime.date(int(g[2]), _MONTH_NUM[g[1][:3].lower()], int(g[0]))
        if kind == "mdy_w":
            return datetime.date(int(g[2]), _MONTH_NUM[g[0][:3].lower()], int(g[1]))
        a, b, y = int(g[0]), int(g[1]), int(g[2])            # numeric: day-first (India), else month-first
        try:
            return datetime.date(y, b, a)
        except ValueError:
            return datetime.date(y, a, b)
    except (ValueError, KeyError):
        return None


def _extract_air_date(name: str):
    """(date | None, name without the date text): "12th Nov 2026", "2026-11-12", "12.11.2026"."""
    latest = datetime.date.today().year + 1
    for kind, rx in _DATE_PATTERNS:
        for m in rx.finditer(name):
            d = _date_from(kind, m.groups())
            if d and 1980 <= d.year <= latest:
                return d, name[:m.start()] + " " + name[m.end():]
    return None, name


def _deep_prepare(name: str) -> dict:
    """Pre-clean for deep parsing: platform tags out (remembered), air date out (remembered)."""
    platforms = []

    def _grab(m):
        tag = re.sub(r"[^a-z0-9&+]", "", m.group(0).lower())
        if tag and tag not in platforms:
            platforms.append(tag)
        return " "

    name = _PLATFORM_SURE_RE.sub(_grab, name)
    name = _PLATFORM_CODE_RE.sub(_grab, name)
    name = _PLATFORM_EDGE_RE.sub(_grab, name)
    air_date, name = _extract_air_date(name)
    name = re.sub(r"\(\s*\)|\[\s*\]", " ", name)
    name = re.sub(r"([._\s])[._\s]+", r"\1", name)          # "1080p. .WEB-DL" (a removed tag) -> "1080p.WEB-DL"
    return {"name": _EDGE_JUNK_RE.sub("", name), "date": air_date, "platforms": platforms}


# ---- deep parse gaps (/autosort only): counted episodes, season packs, TV-source rips, broadcaster / site tags ----
#   "1of4" / "1 of 4" / "Part 1 of 4" / "Ep 1/4"   -> "Episode 1": a normal episode marker for everything downstream
#   "S01 Combined" / "Complete Season 1" / ...      -> a SEASON PACK: strong series marker, the pack words leave the title
#   PDTV / HDTV / DSR / TVRip / DVBS                -> TV-source rip: a positive series signal, out of the title
#   leading "Ch5." / "BBC " / "ITV " / "NatGeo "   -> out of the title (BROADCASTER_PREFIXES adds more, comma separated)
#   "...AAC.MVGroup.org", "(2025) (2025)"           -> site tag / duplicate year out of the name
# A counted episode, a TV-source rip or a broadcaster prefix also means "not anime-style naming" (see "serial" below).
_BROADCASTERS = ["ch5", "ch4", "c5", "c4", "channel 5", "channel 4", "channel5", "channel4", "bbc", "bbc one", "bbc two",
                 "bbc three", "bbc four", "bbc1", "bbc2", "bbc3", "bbc4", "itv", "itv1", "itv2", "itv3", "itv4", "itvx",
                 "natgeo", "nat geo", "natgeo wild", "national geographic", "pbs", "cnbc", "more4", "stv"]
_BROADCASTERS += [b.strip().lower() for b in os.environ.get("BROADCASTER_PREFIXES", "").split(",") if b.strip()]
_BCAST_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(b).replace(r"\ ", r"[\s._-]?") for b in sorted(set(_BROADCASTERS), key=len, reverse=True))
    + r")[\s._-]+(?=\S)", re.IGNORECASE)
_TV_SRC_RE = re.compile(r"(?<![A-Za-z0-9])(?:pdtv|dsr(?:ip)?|tv[\s._-]?rip|dvb[\s._-]?[st]|sat[\s._-]?rip|hdtv)(?![A-Za-z0-9])",
                        re.IGNORECASE)
_EP_OF_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?P<lead>part|pt|episode|ep)\.?[\s._-]*)?0*(?P<n>\d{1,2})(?:[\s._-]*of[\s._-]*|(?P<sl>/))0*(?P<m>\d{1,2})"
    r"(?![A-Za-z0-9/])", re.IGNORECASE)
_PACK_WORD_RE = re.compile(r"(?<![A-Za-z0-9])(?:combined|complete|full|entire|all[\s._-]*(?:episodes?|eps?))(?![A-Za-z0-9])",
                           re.IGNORECASE)
_SITE_TAIL_RE = re.compile(
    r"(?<=[\s._-])[A-Za-z0-9][A-Za-z0-9-]{1,30}\.(?:org|com|net|to|me|cc|co|in|xyz|ws|lol|vip|site|info|biz|club|link|fun|top|ink)"
    r"(?![A-Za-z0-9])", re.IGNORECASE)
_DUP_YEAR_RE = re.compile(r"([\(\[]\s*(?:19|20)\d{2}\s*[\)\]])(?:[\s._-]*\1)+")


def _deep_pre(name: str):
    """-> (name, flags): the deep clean-up above. flags = {"broadcaster", "tv_source", "ep_of", "pack", "serial"}.
    Runs BEFORE the movie / series name machinery sees the name. Pure string work; never raises."""
    flags = {"broadcaster": None, "tv_source": False, "ep_of": None, "pack": False, "serial": False}
    try:
        stem = _clean_name(_strip_release_noise(name))
        m = _VIDEO_EXT_END_RE.search(stem)
        stem, ext = (stem[:m.start()], m.group(0)) if m else (stem, "")
        bm = _BCAST_RE.match(stem)
        if bm and len(re.findall(r"[^\W_]", stem[bm.end():])) >= 2:
            flags["broadcaster"], stem = re.sub(r"[\s._-]+", "", bm.group(1).lower()), stem[bm.end():]
        if _TV_SRC_RE.search(stem):
            flags["tv_source"], stem = True, _TV_SRC_RE.sub(" ", stem)
        for mm in _EP_OF_RE.finditer(stem):
            n, total = int(mm.group("n")), int(mm.group("m"))
            if mm.group("sl") and not mm.group("lead"):
                continue                                          # "1/4" alone is a fraction / date, not an episode
            if 1 <= n <= total <= 99 and total >= 2:
                flags["ep_of"] = (n, total)
                stem = stem[:mm.start()] + f" Episode {n} " + stem[mm.end():]
                break
        if (_SEASON_RE.search(stem) and _PACK_WORD_RE.search(stem) and not _DEEP_EP_RE.search(stem)
                and not flags["ep_of"]):
            flags["pack"], stem = True, _PACK_WORD_RE.sub(" ", stem)
        stem = _DUP_YEAR_RE.sub(r"\1", stem)
        tech = _TECH_START_RE.search(stem)                         # a site tag only counts after the technical tail starts
        if tech:
            stem = stem[:tech.start()] + _SITE_TAIL_RE.sub(" ", stem[tech.start():])
        stem = re.sub(r"([._\s])[._\s]+", r"\1", stem)
        stem = re.sub(r"^[\s._\-–—:|~]+|[\s._\-–—:|~]+$", "", stem)
        flags["serial"] = bool(flags["broadcaster"] or flags["tv_source"] or flags["ep_of"])
        return (stem + ext) if stem else name, flags
    except Exception as e:                                        # never raise from an identification layer
        logger.debug("deep pre-clean failed on %r: %s", name, e)
        return name, {"broadcaster": None, "tv_source": False, "ep_of": None, "pack": False, "serial": False}


def _deep_extra(stem: str) -> bool:
    """sample / trailer / promo / recap ... anywhere but as the FIRST word of a title
    ("Trailer Park Boys S01E01" is fine; "Promo - Show Ep 5" is not)."""
    for m in _EXTRA_DEEP_RE.finditer(stem):
        if m.start() > 0 or re.match(r"\s*[-–—:|]", stem[m.end():]):
            return True
    return False


def _episode_numbers(name: str, g: dict):
    """(season, episode) as ints where the name or guessit gives them."""
    season, episode = _as_int(g.get("season")), _as_int(g.get("episode"))
    m = (re.search(r"(?<![A-Za-z0-9])s0*(\d{1,2})[ ._-]?e0*(\d{1,4})(?![0-9])", name, re.IGNORECASE)
         or re.search(r"(?<![A-Za-z0-9])(\d{1,2})x0*(\d{1,3})(?![A-Za-z0-9])", name, re.IGNORECASE))
    if m:
        season, episode = season or int(m.group(1)), episode or int(m.group(2))
    if season is None:
        m = re.search(r"(?<![A-Za-z0-9])seasons?[\s._-]*0*(\d{1,2})(?![0-9])", name, re.IGNORECASE)
        if m:
            season = int(m.group(1))
    if episode is None:
        m = re.search(r"(?<![A-Za-z0-9])(?:(?:episodes?|ep|epi)\.?[\s._#:-]*0*(\d{1,4})|e0*(\d{2,4}))(?![A-Za-z0-9])",
                      name, re.IGNORECASE)
        if m:
            episode = int(m.group(1) or m.group(2))
    return season, episode


# ---- show / episode-title / language derivation (deep mode, names with an episode marker) ----
def _spaced(s: str) -> str:
    """Dots and underscores -> spaces, except the dot between two digits ("2.0", "5.1.3")."""
    return re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s.replace("_", " "))


def _tokens(text: str) -> list:
    return [t for t in re.split(r"[\s,;/|]+", text or "") if t]


def _alnum(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _glued_alias_lang(token: str):
    """("bb", "kn") for "BBKan" / "BBKannada"; None for anything else."""
    t = _alnum(token)
    for alias in sorted(_FRANCHISE_ALIASES, key=len, reverse=True):
        if t.startswith(alias) and t[len(alias):] in _LANG_ANY:
            return alias, _LANG_ANY[t[len(alias):]]
    return None


def _lang_codes(tokens, short: bool) -> list:
    out = []
    for tok in tokens:
        t = _alnum(tok)
        code = _LANG_FULL.get(t) or (_LANG_SHORT.get(t) if short else None)
        if code and code not in out:
            out.append(code)
    return out


def _clean_piece(text: str) -> str:
    """A name fragment without its technical tail, bracket groups and edge junk."""
    text = _BRACKET_GROUP_RE.sub(" ", text or "")
    m = _TECH_START_RE.search(text)
    if m:
        text = text[:m.start()]
    text = re.sub(r"\s{2,}", " ", text)
    return re.sub(r"^[\s\-–—_:|~.,]+|[\s\-–—_:|~.,]+$", "", text)


def _piece_is_tag(tokens) -> bool:
    """Nothing but language / franchise-abbreviation / noise words: not an episode title."""
    for tok in tokens:
        t = _alnum(tok)
        if not t or t in _TAG_NOISE_WORDS or t in _LANG_ANY or t in _FRANCHISE_ALIASES or t in _PLATFORM_CODES:
            continue
        if _glued_alias_lang(tok):
            continue
        return False
    return True


def _derive_shape(name: str) -> dict:
    """Split a normalised name into SHOW, EPISODE TITLE, language and franchise.

    "Bigg Boss S12E01 - BBKan - Grand Opening 1080p WEB-DL H264"
      -> show "Bigg Boss", episode title "Grand Opening", language kn (from "BBKan"),
         franchise "Bigg Boss" (regional) -> clean_title "Bigg Boss Kannada"

    Segments are split on " - ". The show is the text before the first episode / season
    marker; what follows (up to the technical tail) is the episode title unless it is only
    a language / abbreviation tag. Returns {} when no show text can be found."""
    stem = _VIDEO_EXT_TAIL_RE.sub("", name)
    group_tokens = [t for g in _BRACKET_GROUP_RE.findall(stem) for t in _tokens(g)]
    spaced = re.sub(r"\s{2,}", " ", _spaced(_BRACKET_GROUP_RE.sub(" ", stem))).strip()
    segs = [s.strip() for s in re.split(r"\s+[-–—]\s+", spaced) if s.strip()]
    if not segs:
        return {}

    k, before, after, found = 0, segs[0], "", False
    for i, seg in enumerate(segs):
        m = _TITLE_MARKER_RE.search(seg)
        if not m:
            continue
        k, before, rest, found = i, seg[:m.start()], seg[m.end():], True
        while True:                                       # "Season 1 Episode 56", "S01E05 Part 2" ...
            rest = rest.lstrip(" ,")
            mm = _TITLE_MARKER_RE.match(rest)
            if not mm:
                break
            rest = rest[mm.end():]
        after = rest
        break
    head = segs[:k]
    tail_pieces = ([after] + segs[k + 1:]) if found else segs[1:]

    show = _clean_piece(before)
    headline = head
    if len(re.findall(r"\w", show)) < 2:                  # marker first: the show is the segment before it
        show, headline = (_clean_piece(head[-1]), head[:-1]) if head else ("", [])

    texts, tag_tokens = [], []
    for piece in tail_pieces:
        cp = _clean_piece(piece)
        if not cp:
            continue
        toks = _tokens(cp)
        if _piece_is_tag(toks):
            tag_tokens += toks
        else:
            texts.append(cp)
    if not show and texts:
        show = texts.pop(0)
    if len(re.findall(r"\w", show)) < 2:
        return {}
    episode_title = texts[0] if texts else (_clean_piece(headline[0]) if headline else "")
    if _generic_title(episode_title) or _piece_is_tag(_tokens(episode_title)):
        episode_title = ""

    # language: glued to an abbreviation ("BBKan") > a full word in the title > exactly one
    # code in the tags / brackets (several = a multi-audio release: unknown)
    glued = next((g for g in (_glued_alias_lang(t) for t in _tokens(spaced)) if g), None)
    title_langs = _lang_codes(_tokens(show), short=False)
    other_langs = _lang_codes(tag_tokens + group_tokens, short=True)
    if glued:
        language = glued[1]
    elif title_langs:
        language = title_langs[0]
    else:
        language = other_langs[0] if len(other_langs) == 1 else None

    # franchise / abbreviation expansion of the show text
    base, franchise, lang_in_title = show, None, bool(title_langs)
    toks = _tokens(show)
    nolang = [t for t in toks if _alnum(t) not in _LANG_FULL]
    one = _glued_alias_lang(show.replace(" ", "")) if toks else None
    if one:                                               # the whole show is "BBKan"
        base, franchise, lang_in_title = _FRANCHISE_ALIASES[one[0]], _FRANCHISE_ALIASES[one[0]], False
    elif nolang and _alnum("".join(nolang)) in _FRANCHISE_ALIASES:      # "BB" / "KBC" / "BB Hindi"
        base = franchise = _FRANCHISE_ALIASES[_alnum("".join(nolang))]
        lang_in_title = False
    else:
        low = _norm_cmp(show)
        for key, display in _REGIONAL_FRANCHISES.items():
            if low == key or (low.startswith(key + " ") and all(
                    _alnum(t) in _LANG_FULL or t.isdigit() for t in _tokens(low[len(key):]))):
                franchise = display
                break
    regional = bool(franchise) and _norm_cmp(franchise) in _REGIONAL_FRANCHISES
    clean_title = base
    if regional and language and not lang_in_title:
        clean_title = base if language == "hi" else f"{base} {_LANG_WORDS[language][0]}"
    return {"base": base, "clean_title": clean_title, "episode_title": episode_title, "language": language,
            "franchise": franchise, "regional": regional, "lang_in_title": lang_in_title}


_STOP_ENDINGS = {"ka", "ki", "ke", "ko", "se", "mein", "me", "hai", "aur", "and", "the", "of", "in", "a", "an", "to",
                 "for", "with", "na", "ek"}


def _build_candidates(shape: dict, extra_titles=(), year=None, alt_titles=()) -> list:
    """Search candidates in priority order, each {"title", "dropped", "kind"}:
      1. the cleaned full title (for a regional franchise with a known language that is the
         language form: "Bigg Boss Kannada"; for Hindi / unknown the plain name comes first)
      2. title + language word
      3. progressive shortening, one word at a time (1 or 2 words, never fewer than one
         word of 5+ letters, never ending on a stop word); each dropped word costs
         _SHORTEN_PENALTY, so a shortened title only matches when the show name is exact.
         A 2-word PREFIX of a longer title ("Blue Eye" of "Blue Eye Samurai") is only a candidate when the
         name has a year (PREFIX2_NEEDS_YEAR), and a result then also has to agree with that year (kind "prefix2")
      4. extra titles (guessit's own idea of the title)
    At most SERIES_MAX_SEARCHES entries, so one show costs at most that many searches per source."""
    limit = max(1, SERIES_MAX_SEARCHES)
    out, seen = [], set()

    def add(title, dropped=0, kind="title"):
        title = re.sub(r"\s{2,}", " ", (title or "").strip())
        key = _norm_cmp(title)
        if not key or key in seen or _generic_title(title) or len(out) >= limit:
            return
        seen.add(key)
        out.append({"title": title, "dropped": dropped, "kind": kind})

    base, clean, language = shape["base"], shape["clean_title"], shape["language"]
    lang_titles = []
    if language and not shape.get("lang_in_title"):
        lang_titles = [f"{base} {w}" for w in _LANG_WORDS[language]]
    if shape["regional"] and language and language != "hi":
        add(clean, 0, "full+lang")
        add(base, 0, "full")
    else:
        add(clean, 0, "full")
        for t in lang_titles:
            add(t, 0, "full+lang")
    for t in alt_titles:                                      # "Show A.K.A. Other": every part is a full candidate
        add(t, 0, "alt")
    words = base.split()
    for drop in (1, 2):
        kept = words[:-drop]
        if not kept or kept[-1].lower() in _STOP_ENDINGS:
            continue
        if len(kept) == 1 and len(kept[0]) < 5:
            continue
        kind = "short"
        if PREFIX2_NEEDS_YEAR and len(kept) == 2 and len(words) >= 3:
            if not year:
                continue                                      # "Blue Eye": no year to agree with = not a candidate
            kind = "prefix2"
        add(" ".join(kept), drop, kind)
    for t in extra_titles:
        add(t, 0, "guessit")
    if TITLE_SPACE_VARIANTS:                                  # "House Mates" -> "Housemates": only while there is room
        for v in _space_variants(base)[:2]:
            add(v, 0, "spacevar")
    return out


# ------------------------------------------------------------
# Deep MOVIE name preparation - opt-in via _parse_name(..., deep=True), i.e. /autosort only
# ------------------------------------------------------------
# A film release name carries a lot that is not the title: "@handle - " / "[handle]" prefixes, quality /
# source / audio / platform words, language words and codes, release groups ("-ATH", "(HmaDi007) -
# (HmaDi007)"). _movie_info() cuts the TITLE out of one name (pure string work, no network) and records what
# it saw. The title ends at the first technical token (quality, source, codec, audio, ...), at the release
# year, or at a run of language / platform words - never at the very first word, so a film called "Cam" or
# "2012" survives. Everything after the cut is only inspected for language signals and release groups.
#   Mal, Hin, Malayalam, FR EN, ita eng, sub ita eng, DUAL-AUDIO, JPN-ENG ...  -> "languages" (never in the title)
#   TS, HDTS, CAM, NF, MMAX, DS4K, WEBRip, x265, DDP5.1, DTS-HD.MA, 6CH ...    -> removed
# Env: MOVIE_RELEASE_TAGS = extra release-group / uploader tags, comma separated.
_MOVIE_TAGS_NORM = {re.sub(r"[^a-z0-9]", "", t.lower()) for t in (
    "ATH", "Saon", "FraMeSToR", "HmaDi007", "Licdom", "PrimeFix", "PSA", "YourShivay", "RARBG", "YTS", "YIFY",
    "GalaxyRG", "TGx", "MeGusta", "NTb", "CMRG", "EVO", "Pahe", "Tigole", "QxR",
    *_UPLOADER_TAGS, *os.environ.get("MOVIE_RELEASE_TAGS", "").split(","))} - {""}

# One token that is certainly technical (a title never contains it): resolution, source, codec, audio, channels.
_MOVIE_STRONG_RE = re.compile(
    r"^(?:\d{3,4}[pi]|[248]k|uhd|hdr\d*|"
    r"web(?:[-.]?dl|[-.]?rip)?|blu[-.]?ray|bd[-.]?rip|br[-.]?rip|dvd[-.]?rip|dvd[-.]?scr|hd[-.]?rip|hd[-.]?tv|"
    r"hd[-.]?ts|hd[-.]?cam|pre[-.]?dvd|remux|ds4k|"
    r"x26[45]|h[-.]?26[45]|hevc|avc|av1|xvid|divx|10[-.]?bit|8[-.]?bit|"
    r"aac\d*(?:\.\d)?|e?[-.]?ac[-.]?3|dd[p+]\d*(?:\.\d)?|dd\d(?:\.\d)?|dts(?:[-.]?hd)?(?:[-.]?ma)?|truehd|atmos|"
    r"flac|mp3|opus|\d{1,2}ch|\d\.\d)"
    r"(?:[-.+][A-Za-z0-9.+-]*)?$", re.IGNORECASE)
# Words that are technical / language tags but could also be a title word ("Cam", "Mal"): they only end the
# title when a tag, a technical word or the year follows (or they close the name).
_MOVIE_WEAK = {
    "ts", "cam", "scr", "nf", "amzn", "dsnp", "jiohs", "jhs", "mmax", "hs", "zee5", "sonyliv", "sliv", "hotstar",
    "jiohotstar", "netflix", "dual", "audio", "multi", "sub", "subs", "esub", "esubs", "msub", "msubs", "hsub",
    "hsubs", "dubbed", "dub", "org", "ma", "hd", "fr", "en", "eng", "ita", "jpn", "jap", "spa", "ger", "rus", "kor",
    "chi", "fre", "hin", "mal", "tam", "tel", "kan", "english", "french", "italian", "japanese", "spanish",
    "german", "russian", "korean", "chinese", *_LANG_FULL,
}
_MOVIE_LANG_WORDS = _MOVIE_WEAK - {"ts", "cam", "scr", "nf", "amzn", "dsnp", "jiohs", "jhs", "mmax", "hs", "zee5",
                                   "sonyliv", "sliv", "hotstar", "jiohotstar", "netflix", "sub", "subs", "esub",
                                   "esubs", "msub", "msubs", "hsub", "hsubs", "dubbed", "dub", "org", "ma", "hd",
                                   "dual", "audio", "multi"}
_MOVIE_BRACKET_YEAR_RE = re.compile(r"[\(\[]\s*((?:19|20)\d{2})\s*[\)\]]")
_MOVIE_BRACKET_HANDLE_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)\s*\]\s*[-–—:|]*\s*")
_TRANSLIT_HINT_RE = re.compile(r"aa|ii|uu|dh|bh|kh|gh|zh|chh", re.IGNORECASE)


def _movie_tok(tok: str) -> str:
    """'year' | 'strong' | 'weak' | '' for one token of a release name (edge punctuation ignored)."""
    t = tok.strip("-–—:,;|~.'\"!?*")
    if not t:
        return ""
    if re.fullmatch(r"(?:19|20)\d{2}", t):
        return "year" if int(t) <= datetime.date.today().year + 1 else ""
    if _MOVIE_STRONG_RE.match(t):
        return "strong"
    parts = [x for x in re.split(r"[-+/&]+", t.lower()) if x]
    return "weak" if parts and all(x in _MOVIE_WEAK for x in parts) else ""


def _case_like(src: str, repl: str) -> str:
    if len(src) > 1 and src.isupper():
        return repl.upper()
    return repl[:1].upper() + repl[1:] if src[:1].isupper() else repl


_TR_VOWELS_PLAIN = ((r"aa+", "a"), (r"ee+", "e"), (r"oo+", "o"), (r"ii+", "i"), (r"uu+", "u"))
_TR_VOWELS_INDIC = ((r"aa+", "a"), (r"ee+", "i"), (r"oo+", "u"), (r"ii+", "i"), (r"uu+", "u"))
_TR_CONSONANTS = ((r"([bcdfgjklmnpqrstvwxyz])\1+", None),)           # doubled consonant -> one
_TR_ASPIRATES = ((r"th", "t"), (r"dh", "d"))                          # th / dh -> t / d
_TR_COMBOS = (
    (_TR_VOWELS_PLAIN,), (_TR_VOWELS_INDIC,),
    (_TR_VOWELS_PLAIN, _TR_CONSONANTS), (_TR_VOWELS_INDIC, _TR_CONSONANTS), (_TR_CONSONANTS,),
    (_TR_VOWELS_PLAIN, _TR_CONSONANTS, _TR_ASPIRATES), (_TR_VOWELS_INDIC, _TR_CONSONANTS, _TR_ASPIRATES),
    (_TR_ASPIRATES,),
)


def _translit_variants(title: str, limit=None) -> list:
    """Spelling variants of an Indian-language title, most likely first, never the title itself:
    "Chaadum" -> "Chadum" (aa -> a, ee -> e / i, oo -> o / u), doubled consonants collapsed, th / dh -> t / d."""
    seen, out = {_norm_cmp(title)}, []
    for combo in _TR_COMBOS:
        text = title
        for rules in combo:
            for pat, repl in rules:
                text = re.sub(pat, (lambda m, r=repl: _case_like(m.group(0), r if r is not None else m.group(1))),
                              text, flags=re.IGNORECASE)
        key = _norm_cmp(text)
        if key and key not in seen:
            seen.add(key)
            out.append(text)
    return out if limit is None else out[:limit]


def _translit_keys(text: str) -> set:
    """Spacing-free forms of a title and all its variants: two spellings of one title share a key."""
    return {_norm_cmp(v).replace(" ", "") for v in [text, *_translit_variants(text)] if v}


def _translit_expand(title: str) -> list:
    """The OTHER direction of _translit_variants(): a -> aa, i -> ee, u -> oo (the last single vowel, then every one),
    t -> th, d -> dh, v <-> w, and the last single consonant between vowels doubled. "Madharasi" -> "Madharaasi"."""
    out = []

    def last(pat, repl):
        ms = list(re.finditer(pat, title, flags=re.IGNORECASE))
        if ms:
            m = ms[-1]
            out.append(title[:m.start()] + _case_like(m.group(0), repl) + title[m.end():])

    def every(pat, repl):
        new = re.sub(pat, lambda m: _case_like(m.group(0), repl), title, flags=re.IGNORECASE)
        if new != title:
            out.append(new)

    for pat, repl in ((r"(?<![aeiou])a(?![aeiou])", "aa"), (r"(?<![aeiou])i(?![aeiou])", "ee"),
                      (r"(?<![aeiou])u(?![aeiou])", "oo")):
        last(pat, repl)
        every(pat, repl)
    for pat, repl in ((r"(?<![t])t(?![thr])", "th"), (r"(?<![d])d(?![dhr])", "dh"), (r"v", "w"), (r"w", "v")):
        every(pat, repl)
    ms = list(re.finditer(r"(?<=[aeiou])([bcdfgjklmnprstyz])(?=[aeiou])", title, flags=re.IGNORECASE))
    if ms:                                                    # the last single consonant between vowels, doubled
        m = ms[-1]
        out.append(title[:m.end()] + m.group(1) + title[m.end():])
    return out


def _title_variants(title: str, translit: bool = True) -> list:
    """Query variants of a film title for the TMDB token search, most likely first, never the title itself:
      1. spaces joined between short adjacent words ("Radhe krishna" -> "Radhekrishna", "1980 lo Radhe krishna" ->
         "1980 lo Radhekrishna"); a short 2-3 word title joined whole
      2. a glued compound split ("RadheKrishna" -> "Radhe Krishna", "Kalki2898" -> "Kalki 2898")
      3. transliteration spellings (only with translit=True: the name says an Indian language or the title looks
         transliterated), shortened (aa -> a, th -> t ...) and lengthened (a -> aa, t -> th ...)
      4. without a leading number ("1980 lo Radhe krishna" -> "lo Radhe krishna")
      5. the first three words of a long title (accepted only when the FULL title still matches the result)"""
    out, seen = [], {_norm_cmp(title)}

    def add(t):
        t = re.sub(r"\s{2,}", " ", t or "").strip()
        k = _norm_cmp(t)
        if k and k not in seen and len(re.findall(r"[^\W_]", t)) >= 2:
            seen.add(k)
            out.append(t)

    words = title.split()
    for i in range(len(words) - 1):
        a, b = words[i], words[i + 1]
        if (a.isalpha() and b.isalpha() and 3 <= len(a) <= 8 and 3 <= len(b) <= 8
                and a.lower() not in _STOP_ENDINGS and b.lower() not in _STOP_ENDINGS):
            add(" ".join(words[:i] + [a + b] + words[i + 2:]))
    if 2 <= len(words) <= 3 and all(w.isalpha() for w in words):
        add("".join(words))
    add(re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", title))
    if translit:
        for v in _translit_variants(title) + _translit_expand(title):
            add(v)
    if len(words) > 2 and words[0].isdigit():
        add(" ".join(words[1:]))
    if len(words) >= 5:
        add(" ".join(words[:3]))
    return out


# ---- alternate titles, spelling variants and comparison keys (deep mode) -----------------------------------------------
#   TITLE_ALT_SPLIT       "Ne Zha A.K.A. Nezha Mo tong jiang shi", "Title / Other", "Title | Other", "Title (Other Title)": every
#                         part is a candidate, the MAIN title (the first part) stays first
#   TITLE_SPACE_VARIANTS  "House Mates" -> "Housemates", "WALL-E" -> "WALLE", "HouseMates" -> "House Mates" in the search queries
#   TITLE_KEY_MATCH       the title scorers treat "WALL-E" = "WALL·E" = "walle" and "House Mates" = "Housemates" as the same title
_ALT_SEP_RE = re.compile(r"(?<![A-Za-z0-9])a[\s._]?k[\s._]?a(?![A-Za-z0-9])|\s[/|｜]\s", re.IGNORECASE)
_BRACKET_ALT_RE = re.compile(r"[\(\[]([^\(\)\[\]]{3,80})[\)\]]")
_EDITION_WORDS = {"cut", "edition", "version", "remastered", "extended", "uncut", "unrated", "imax", "director", "directors",
                  "theatrical", "final", "collectors", "anniversary", "restored", "season", "seasons", "episodes", "batch"}


@_new_rule(lambda title: [title] if title else [])
def _split_alt_titles(title: str) -> list:
    """"Ne Zha A K A Nezha Mo tong jiang shi" -> ["Ne Zha", "Nezha Mo tong jiang shi"]. Splits on A.K.A. / AKA / aka, " / "
    and " | ". The first part is the main title; a part with fewer than two letters is dropped; a name whose first part
    would be empty is not split. Never raises; always at least [title] for a non-empty title."""
    t = re.sub(r"\s{2,}", " ", title or "").strip()
    if not t:
        return []
    try:
        pieces = [re.sub(r"^[\s\-–—_:|~.,/]+|[\s\-–—_:|~.,/]+$", "", x) for x in _ALT_SEP_RE.split(t)]
        if len(pieces) < 2 or len(re.findall(r"[^\W_]", pieces[0])) < 2:
            return [t]
        # a hashtag and whatever follows it is a channel tag / download tail ("Railway Special |#pareeksir 360p"), never title text
        pieces = [pieces[0]] + [re.sub(r"\s*[|｜]?\s*#\S.*$", "", x).strip() for x in pieces[1:]]
        # "A | B | C" with three or more parts is a LIST (a YouTube lecture / promo name): a later part that is only places /
        # institutions / exam names ("Delhi Police", "Haryana CET Mains & Group D") or release tags is not a title. The
        # main (first) part is never removed here.
        list_mode = len(pieces) >= 3 and bool(re.search(r"[|｜]", t))
        out, keys = [], set()
        for i, x in enumerate(pieces):
            k = _norm_cmp(x)
            if len(re.findall(r"[^\W_]", x)) >= 2 and k and k not in keys:
                if i > 0 and list_mode and _extra_title_junk(x, pipe_list=True):
                    continue
                keys.add(k)
                out.append(x)
        return out or [t]
    except Exception as e:
        logger.debug("alternate-title split failed on %r: %s", title, e)
        return [t]


@_new_rule(lambda text: (text, []))
def _pull_bracket_alts(text: str):
    """(text with the alternate-title brackets removed, [alternate titles]). "Spirited Away (Sen to Chihiro) 2001": a
    bracket group counts as an alternate title only when it sits INSIDE the title part of the name (after some title
    text, before the year / technical tail), has two or more words and is not a year, a release / language / audio tag,
    an edition ("Director's Cut"), a season / batch word or a release group. Never raises."""
    try:
        tech = _TECH_START_RE.search(text)
        limit = tech.start() if tech else len(text)
        alts = []

        def repl(m):
            inner = re.sub(r"\s{2,}", " ", _spaced(m.group(1))).strip()
            toks = [t for t in inner.split() if re.search(r"[^\W_]", t)]
            if (m.start() < 2 or m.start() >= limit or len(toks) < 2 or len(toks) > 8
                    or not re.match(r"[^\W\d_]", inner) or len(re.findall(r"[^\W_]", inner)) < 3):
                return m.group(0)
            if any(_movie_tok(t) in ("strong", "year") for t in toks):
                return m.group(0)
            if all(_movie_tok(t) == "weak" or re.sub(r"[^a-z0-9]", "", t.lower()) in _MOVIE_TAGS_NORM for t in toks):
                return m.group(0)
            if {w for w in _title_words(inner)} & _EDITION_WORDS or _generic_title(inner):
                return m.group(0)
            if _extra_title_junk(inner, pipe_list=False) or _only_words(inner, _PLACE_INST_WORDS, _EDU_EXAM_WORDS):
                return m.group(0)
            alts.append(inner)
            return " "

        new = _BRACKET_ALT_RE.sub(repl, text)
        return (new, alts) if alts else (text, [])
    except Exception as e:
        logger.debug("bracket alternate-title scan failed on %r: %s", text, e)
        return text, []


@_new_rule(lambda words: "".join(words))
def _join_words(words: list) -> str:
    return words[0] + "".join(w.lower() for w in words[1:])


@_new_rule(lambda *a, **k: [])
def _space_variants(title: str) -> list:
    """Spelling variants that differ only in spaces / hyphens / dots, never the title itself, most likely first:
      "House Mates" -> "Housemates", "Ne Zha" -> "Nezha"   (two plain words joined)
      "WALL-E"      -> "WALLE"                                 (a hyphen / middle dot between letters removed)
      "HouseMates"  -> "House Mates", "Kalki2898" -> "Kalki 2898"   (a glued compound split)
    Used as extra TMDB / IMDb / AniList queries only; a result still has to match the title by the scorers."""
    out, seen = [], {_norm_cmp(title or "")}

    def add(t):
        t = re.sub(r"\s{2,}", " ", t or "").strip()
        k = _norm_cmp(t)
        if k and k not in seen and len(re.findall(r"[^\W_]", t)) >= 2:
            seen.add(k)
            out.append(t)

    try:
        words = (title or "").split()
        add(re.sub(r"(?<=[^\W\d_])[-·‐‑–]+(?=[^\W\d_])", "", title or ""))
        if len(words) == 2 and all(re.fullmatch(r"[^\W_]+", w) for w in words):
            add(_join_words(words))                           # (3-word joins stay in _title_variants())
        add(re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", title or ""))
    except Exception as e:
        logger.debug("space variants failed on %r: %s", title, e)
    return out


@_new_rule(lambda *a, **k: "")
def _title_key(text: str) -> str:
    """Comparison key of a title: lower case, accents folded, punctuation / middle dots and spaces removed
    ("WALL-E" = "WALL·E" = "walle", "House Mates" = "Housemates")."""
    return _norm_cmp(text or "").replace(" ", "")


@_new_rule(lambda *a, **k: False)
def _keys_equal(a: str, b: str) -> bool:
    """TITLE_KEY_MATCH: two already-normalised titles are one title when their keys agree (min 3 characters)."""
    if not TITLE_KEY_MATCH:
        return False
    ka, kb = _title_key(a), _title_key(b)
    return len(ka) >= 3 and ka == kb


def _movie_info(raw: str) -> dict:
    """Pure clean-up of one release name for deep MOVIE identification (no network, never raises).
      title        the cut-out title ("M S M", "2012", "Kalaratri")        year   release year or None
      year_in_brackets  the year came from "(2009)" / "[2009]"
      titles       search titles: the title, then spaced initials joined ("M S M" -> "MSM", "M.S.M")
      digits       the title is only digits ("2012"): the year is REQUIRED to agree with the TMDB result
      languages    language words / codes read after the title        indian  one of them is an Indian language
      tags         release groups / uploader tags seen after the title  handle  "@handle" / "[handle]" prefix
      tech         a technical token (quality / source / codec ...) was seen
      variants     transliteration variants of the title (only with a year and an Indian-language hint)
      usable       there is something to search for (a digits-only title needs its year)"""
    out = {"title": "", "titles": [], "year": None, "year_in_brackets": False, "digits": False, "initials": False,
           "languages": [], "indian": False, "tags": [], "handle": None, "tech": False, "variants": [],
           "usable": False, "alt": []}
    try:
        stem = _VIDEO_EXT_END_RE.sub("", raw or "").strip()
        if not stem:
            return out
        hm = _HANDLE_RE.search(stem)
        out["handle"] = hm.group(0) if hm else None
        text = _clean_name(stem)                              # @handles, URLs, www.site tags, emoji, empty brackets
        m = _MOVIE_BRACKET_HANDLE_RE.match(text)              # "[VC_Movie] Title ..."
        if m:
            out["handle"] = out["handle"] or m.group(1)
            text = text[m.end():]
        max_year = datetime.date.today().year + 1
        bracket_year = []

        def _by(mm):
            if int(mm.group(1)) <= max_year:
                bracket_year.append(int(mm.group(1)))
            return f" {mm.group(1)} "

        text = _MOVIE_BRACKET_YEAR_RE.sub(_by, text)
        bracket_alts = []
        if TITLE_ALT_SPLIT:
            text, bracket_alts = _pull_bracket_alts(text)         # "Title (Other Title) 2001": the bracket is an alternate title
        text = re.sub(r"[\[\]\(\)【】「」{}]", " ", text)
        # _spaced() keeps the dot between two digits ("5.1"); "2016.1080p" / "x265.10bit" are two tokens
        text = re.sub(r"(?<=\d{3})\.(?=\d)|(?<=\d)\.(?=\d{3})", " ", text)
        text = re.sub(r"(?<![A-Za-z0-9])h[\s.]26([45])(?![0-9])", r"x26\1", text, flags=re.IGNORECASE)
        toks = re.sub(r"\s{2,}", " ", _spaced(text)).split()
        n = len(toks)
        if not n:
            return out
        kinds = [_movie_tok(t) for t in toks]
        strong_idx = next((i for i in range(1, n) if kinds[i] == "strong"), n)
        year_idx = max((i for i in range(1, strong_idx) if kinds[i] == "year"), default=None)
        weak_idx = next((i for i in range(1, n) if kinds[i] == "weak"
                         and (i == n - 1 or kinds[i + 1] in ("weak", "strong", "year"))), None)
        cut = min(strong_idx, n if year_idx is None else year_idx, n if weak_idx is None else weak_idx)
        if year_idx is None:
            year_idx = next((i for i in range(1, n) if kinds[i] == "year"), None)
        if year_idx is not None:
            out["year"] = int(toks[year_idx].strip("-–—:,;|~.'\"!?*"))
            out["year_in_brackets"] = out["year"] in bracket_year
        out["tech"] = strong_idx < n

        title_toks = toks[:cut]
        while len(title_toks) > 1 and (not re.search(r"[^\W_]", title_toks[-1])
                                       or re.sub(r"[^a-z0-9]", "", title_toks[-1].lower()) in _MOVIE_TAGS_NORM):
            title_toks.pop()                                  # a trailing "-" or release-group tag
        title = re.sub(r"^[\s\-–—_:|~.,]+|[\s\-–—_:|~.,]+$", "", " ".join(title_toks))
        alt_titles = []
        if TITLE_ALT_SPLIT and title:
            parts = _split_alt_titles(title)                      # "Ne Zha A K A Nezha Mo tong jiang shi": main first
            if len(parts) > 1:
                title, alt_titles = parts[0], parts[1:]
            for b in bracket_alts:
                if _norm_cmp(b) not in {_norm_cmp(x) for x in [title, *alt_titles]}:
                    alt_titles.append(b)
        out["title"] = title
        out["alt"] = alt_titles

        langs, tags = [], []
        for tok in toks[cut:]:
            low = tok.strip("-–—:,;|~.'\"!?*").lower()
            parts = [x for x in re.split(r"[-+/&]+", low) if x]
            for part in parts:
                if part in _MOVIE_LANG_WORDS and part not in langs:
                    langs.append(part)
                    if part in _LANG_ANY:
                        out["indian"] = True
                if re.sub(r"[^a-z0-9]", "", part) in _MOVIE_TAGS_NORM and part not in tags:
                    tags.append(part)
            if {"dual", "multi"} & set(parts) and "audio" in parts and "dual-audio" not in langs:
                langs.append("dual-audio")
        out["languages"], out["tags"] = langs, tags

        words = title.split()
        out["digits"] = bool(re.fullmatch(r"\d+", title))
        out["initials"] = len(words) >= 2 and all(re.fullmatch(r"[^\W\d_]\.?", w) for w in words)
        titles = [title] if title else []
        if out["initials"]:
            letters = [w.rstrip(".") for w in words]
            titles += ["".join(letters), ".".join(letters)]  # "MSM", "M.S.M"
        if alt_titles:
            # a name that spells its title in several ways: the main title, its joined spelling ("Ne Zha" -> "Nezha"),
            # then every alternate title ("Nezha Mo tong jiang shi")
            if TITLE_SPACE_VARIANTS:
                titles += _space_variants(title)[:1]
            titles += alt_titles
        out["titles"] = list(dict.fromkeys(t for t in titles if t))
        out["usable"] = bool(len(re.findall(r"[^\W_]", title)) >= 2 and (not out["digits"] or out["year"]))
        if out["usable"] and out["year"] and not out["digits"]:
            variants = _title_variants(title, translit=bool(out["indian"] or _TRANSLIT_HINT_RE.search(title)))
            if TITLE_SPACE_VARIANTS:                              # "House Mates" -> "Housemates" first, then the old variants
                sv = _space_variants(title)
                variants = sv + [v for v in variants if _norm_cmp(v) not in {_norm_cmp(x) for x in sv}]
            out["variants"] = variants
    except Exception as e:                                    # never raise from an identification layer
        logger.debug("movie name clean-up failed on %r: %s", raw, e)
        out["usable"] = False
    return out


def _movie_attach(p: dict, mv: dict) -> None:
    """Deep-movie part of _parse_name(): for a MOVIE-LIKE name (no episode / season marker, not a date
    episode, not a sample / trailer, not a strong anime candidate) sets p["movie"] = mv and makes the cut-out title
    (plus joined initials) the first search titles, takes the year the clean-up found when the name has it in
    brackets (guessit reads "2012 (2009)" as 2012), treats a name with a year or a technical token as having a
    release signal, and - for a number-only title - requires the year to agree. Anything else: p["movie"] stays None."""
    p["movie"] = None
    a = p.get("anime") or {}
    if not (mv and mv.get("usable") and not p["strong_episode"] and not p["series_like"] and not p["date_episode"]
            and not p["extra"] and not a.get("strong")):
        return
    if p["is_episode"] and not p["anime_tag"] and mv["year_in_brackets"]:
        # only guessit's guess said "episode" (it read "2012" as season 20, episode 12): "Title (year)" is a film
        p["is_episode"], p["gtype"], p["anime_hint"] = False, "movie", False
    titles = list(mv["titles"])
    seen = {_norm_cmp(t) for t in titles}
    for t in p["titles"]:
        k = _norm_cmp(t)
        if k and k not in seen and not _generic_title(t):
            if TITLE_ALT_SPLIT and _extra_title_junk(t, pipe_list=False):
                continue                                      # "TRUE", "HQ", "HDRip", "#hashtag": a tag, never a title
            seen.add(k)
            titles.append(t)
    p["titles"] = titles[:(6 if TITLE_ALT_SPLIT and mv.get("alt") else 4)]
    p["candidates"] = [{"title": t, "dropped": 0, "kind": "movie"} for t in p["titles"]]
    p["clean_title"] = mv["title"]
    if mv["year"] and (p["year"] is None or mv["year_in_brackets"]):
        p["year"] = mv["year"]
    if mv["year"] or mv["tech"]:
        p["weak"] = False                                     # a year / a quality tag IS a release signal
        p["has_movie_signal"] = True
    p["year_required"] = bool(mv["digits"])
    p["movie"] = mv


# ---- season packs (deep mode, SEASONPACK_AS_MARKER) --------------------------------------------------------------------
# A season pack is ONE file holding a whole season: a season number with NO episode number ("S01", "Season 1") or a batch
# word. "Batch", "Full Season" and "All Episodes" are enough alone; "Complete", "Combined", "Entire", "Whole" count only
# together with a season number or an anime tag (a film called "The Complete Guide" is no pack). A pack is a strong
# series / anime marker (like S01E02) and its length is not limited by SERIES_MAX_MINUTES / ANIME_MAX_MINUTES
# (SEASONPACK_SKIP_MAX_DURATION); the minimum length, thumbnail and ratio rules still apply.
_BATCH_ALONE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:batch|full[\s._-]*seasons?|all[\s._-]*(?:episodes?|eps))(?![A-Za-z0-9])", re.IGNORECASE)
_BATCH_SEASONLESS_RE = re.compile(r"(?<![A-Za-z0-9])(?:complete|combined|entire|whole)(?![A-Za-z0-9])", re.IGNORECASE)


@_new_rule(lambda *a, **k: False)
def _detect_pack(name: str, g: dict, pre: dict, prep: dict, anime_like: bool) -> bool:
    """True when the (deep-cleaned) name is a season pack, see the block comment above. Pure string work."""
    if pre.get("pack"):
        return True                                           # "S01 Combined": the old, narrower rule
    if prep.get("date") is not None or pre.get("ep_of"):
        return False
    probe = _BATCH_ALONE_RE.sub(" ", name)                    # "All Episodes 1080p" must not read as "Episodes 1080"
    if _EP_MARKER_RE.search(probe) or _DEEP_EP_RE.search(probe):
        return False
    if _episode_numbers(name, g)[1] is not None:
        return False
    if _SEASON_RE.search(name) or _BATCH_ALONE_RE.search(name):
        return True
    return bool(anime_like and _BATCH_SEASONLESS_RE.search(name))


@_new_rule(lambda *a, **k: False)
def is_season_pack(parsed) -> bool:
    """True when a parsed name (the dict of _parse_name(), or a list of them) is a season pack: a season number with no
    episode number ("S01", "Season 1") or a batch word (Complete, Combined, Batch, Full Season, All Episodes). Used by
    /autosort's gates: the maximum-length check is skipped for a pack. Never raises."""
    try:
        items = parsed if isinstance(parsed, (list, tuple)) else [parsed]
        return any(bool(pp and (pp.get("pack") or pp.get("season_pack"))) for pp in items)
    except Exception:
        return False


_PTT = None


@_new_rule(lambda *a, **k: (None, None))
def _ptt_episode(name: str):
    """(season, episode) from the optional "parsett" package (import name PTT), else (None, None). Only used with
    USE_PTT_PARSER=1; the package needs Python 3.11+ - on an older interpreter the import fails once and this stays off."""
    global _PTT
    if _PTT is None:
        try:
            import PTT as _mod                                # type: ignore
            _PTT = _mod
        except Exception as e:
            logger.warning("USE_PTT_PARSER is on but the parsett package (import PTT) is unavailable: %s", e)
            _PTT = False
    if not _PTT:
        return None, None
    try:
        r = _PTT.parse_title(name) or {}
        seasons, episodes = r.get("seasons") or [], r.get("episodes") or []
        return (_as_int(seasons[0]) if seasons else None), (_as_int(episodes[0]) if episodes else None)
    except Exception as e:
        logger.debug("parsett failed on %r: %s", name, e)
        return None, None


_ROUND2_SWITCHES = ("TITLE_ALT_SPLIT", "TITLE_SPACE_VARIANTS", "TITLE_KEY_MATCH", "PREFIX2_NEEDS_YEAR", "SEASONPACK_AS_MARKER",
                    "EDU_EXAM_RULES", "USE_PTT_PARSER")


def _parse_name_safe(raw: str, check_extra: bool = False, deep: bool = False, anime: bool = False):
    """_parse_name() for classify_async(). The deep parse has new rules (alternate titles, joined variants, key match, season
    packs ...). If one of them raises on a name, ONE WARNING is logged and that name is parsed again with every round-2
    switch off (this function is synchronous, so the switches cannot change under another task) = the OLD behaviour.
    A name the old code cannot parse either raises exactly as it did before."""
    try:
        return _parse_name(raw, check_extra=check_extra, deep=deep, anime=anime)
    except Exception as e:
        if not deep:
            raise
        logger.warning("autosort: the new name rules failed on %r (%s: %s) - parsing it the old way", raw, type(e).__name__, e)
        g = globals()
        saved = {k: g[k] for k in _ROUND2_SWITCHES}
        try:
            for k in saved:
                g[k] = False
            return _parse_name(raw, check_extra=check_extra, deep=deep, anime=anime)
        finally:
            g.update(saved)


def _parse_name(raw: str, check_extra: bool = False, deep: bool = False, anime: bool = False):
    """Parse one candidate name. Returns a dict or None if no usable title.

    keys: titles (best first), year, is_episode, gtype, weak, anime_tag,
          has_movie_signal, extra, series_like, and (deep mode fills them in,
          otherwise they hold neutral defaults) date_episode, air_year,
          platforms, part_weak, season, episode, signals, clean_title, language,
          franchise, regional, episode_title, junk_removed, candidates

    deep=True (only /autosort's deep_series mode): URL-decodes the name, deletes junk tokens
    (None / null ...), garbage bracket groups and uploader tags, recognises "E105" / "1x05" /
    date-named episodes, strips channel / platform tags (kept as a signal), reads the language
    and franchise out of abbreviations ("BBKan"), builds a list of search candidates, treats
    promo / recap as extras even for episodes, counts a bracket as a fansub tag only when it
    is a plausible group name at the START of the name, and records every signal.

    anime=True (only with deep=True, only /autosort's deep_anime mode): also deletes the junk suffix
    "_Remove" / "_Removed", and fills p["anime"] (see _anime_info(): cleaned title, dub / language
    signal, groups, season / part / episode, anime-candidate signals), flags NCOP / NCED / AMV ... as
    extras and, for a strong anime candidate, replaces the title candidates by the cleaned anime ones.
    Otherwise p["anime"] is None and nothing changes.

    deep=True also fills p["movie"] for a MOVIE-LIKE name (see _movie_info() / _movie_attach()): the cut-out
    title, joined initials, a bracketed year, a number-only title with a required year. Otherwise it is None.
    """
    if not raw:
        return None
    if deep:
        raw = _url_decode(raw)
    anime = bool(anime and deep)
    name = _strip_split_suffix(raw) if _is_split_part(raw) else raw
    pre = {}
    if deep:
        name, pre = _deep_pre(name)               # counted episodes, season packs, TV-source tags, broadcaster / site tags
    mv = _movie_info(name) if deep else None      # deep MOVIE clean-up (pure string work), attached at the end
    if anime:
        m_ext = _VIDEO_EXT_END_RE.search(name)
        body, ext = (name[:m_ext.start()], m_ext.group(0)) if m_ext else (name, "")
        name = _ANIME_JUNK_SUFFIX_RE.sub("", body) + ext
    name = _strip_release_noise(_clean_name(name))
    if not name:
        return None
    pre_norm = name                       # the anime cleaner needs the brackets the normaliser deletes
    norm = _normalize_release_name(name) if deep else None
    if norm:
        name = norm["name"]
        if not name:
            return None
    prep = _deep_prepare(name) if deep else None
    if prep:
        name = prep["name"]
        if not name:
            return None

    anime_title, anime_episode = None, False
    if anitopy is not None and (_fansub_group(name) if deep else _GROUP_TAG.search(name)):
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
    if prep:
        # deep: also "E105" / "1x05" and date-named episodes (\"12th Nov 2026\")
        strong_episode = strong_episode or bool(_DEEP_EP_RE.search(name)) or prep["date"] is not None
        strong_episode = strong_episode or bool(pre.get("pack"))        # "S01 Combined" is explicit series naming

    if USE_PTT_PARSER and prep and strong_episode and not _as_int(g.get("episode")):
        # optional second parser (parsett / PTT): only when guessit found no episode on a name with an obvious marker
        ptt_s, ptt_e = _ptt_episode(name)
        if ptt_e:
            g = dict(g)
            g["episode"] = ptt_e
            if ptt_s and not _as_int(g.get("season")):
                g["season"] = ptt_s

    # deep: a SEASON PACK ("S01", "Season 1", "Complete", "Batch" ... with no episode number, see is_season_pack()) is
    # explicit series / anime naming, a strong marker like "S01E02"
    pack = False
    if prep and SEASONPACK_AS_MARKER:
        try:
            pack = _detect_pack(name, g, pre, prep, bool(anime_title) or bool(_fansub_group(name)))
        except Exception as e:                                            # never raise: fall back to the old rule
            logger.warning("season-pack check failed on %r: %s - old behaviour", raw, e)
            pack = False
        if pack:
            strong_episode = True

    guess_titles = []
    for t in (anime_title, g.get("title"), g.get("alternative_title")):
        for item in (t if isinstance(t, (list, tuple)) else [t]):
            if isinstance(item, str):
                item = item.strip()
                if len(item) >= 2 and item not in guess_titles:
                    guess_titles.append(item)

    titles = []
    max_titles = 3
    shape, cands = None, None
    if prep and strong_episode and not _PIPE_RE.search(name):
        # deep: the show / episode title / language come from the name's own structure;
        # guessit's title is only an extra fallback candidate
        shape = _derive_shape(name) or None
        alt_extra = []
        if shape and TITLE_ALT_SPLIT:
            try:                                          # "Show A.K.A. Other Title S01E01": every part is a candidate
                parts = _split_alt_titles(shape["base"])
                if len(parts) > 1 and shape["clean_title"] == shape["base"]:
                    shape = dict(shape, base=parts[0], clean_title=parts[0])
                    alt_extra = parts[1:]
            except Exception as e:
                logger.warning("alternate-title split failed on %r: %s - old behaviour", raw, e)
        if shape:
            cands_year = None if (prep["date"] is not None) else (_as_int(g.get("year")) or (mv or {}).get("year"))
            cands = _build_candidates(shape, guess_titles, cands_year, alt_extra)
            titles = [c["title"] for c in cands]
            max_titles = max(1, SERIES_MAX_SEARCHES)
    if not titles and _PIPE_RE.search(name):
        # "Headline | Show | Full Episode | Episode N": guessit's title is
        # the headline, so build candidates from the segments instead.
        titles = _pipe_titles(name, deep=bool(prep))
        max_titles = 4
    if not titles:
        titles = list(guess_titles)
    if prep:
        # deep: "Episode" / "Part" / "Video" / a bare language, a single letter or number and
        # bare platform names are not titles
        titles = [t for t in titles if not _generic_title(t) and t.lower() not in prep["platforms"]]
        if TITLE_ALT_SPLIT and len(titles) > 1:
            # release / quality tags ("TRUE", "HQ", "HDRip") and hashtags are never extra titles; the main one always stays
            titles = titles[:1] + [t for t in titles[1:] if not _extra_title_junk(t, pipe_list=False)]
    if not titles and mv and mv["usable"] and mv["digits"]:
        titles = [mv["title"]]                    # "2012 (2009)": a number-only title is allowed (its year must agree)
    titles = titles[:max_titles]
    if not titles:
        return None
    if prep:
        keep = {c["title"]: c for c in (cands or [])}
        cands = [keep.get(t) or {"title": t, "dropped": 0, "kind": "title"} for t in titles]

    gtype = g.get("type")
    is_episode = gtype == "episode" or anime_episode or strong_episode
    # Anime-style naming: fansub group tag, or an episode number with no
    # season (\"One.Piece.E1090\", \"Naruto Shippuden - 123\"). Deep: a date-named
    # or channel-tagged episode (\"Ghar Ki Baat 12 Nov 2026\", \"Star Plus\") is a TV serial.
    serial_style = bool(prep and (prep["date"] or prep["platforms"] or pre.get("serial")))
    season_no, episode_no = _episode_numbers(name, g) if prep else (None, None)
    if pack and season_no is None:                            # a bare "S01" (guessit may not report it)
        m_s = re.search(r"(?<![A-Za-z0-9])s0*(\d{1,2})(?![A-Za-z0-9])", name, re.IGNORECASE)
        season_no = int(m_s.group(1)) if m_s else None
    anime_hint = bool(anime_title) or (bool(is_episode) and not (g.get("season") or season_no) and not serial_style)
    release_group = bool(g.get("release_group")) or bool(norm and norm["group_removed"])
    weak = (not (any(g.get(f) for f in _RELEASE_SIGNAL_FIELDS) or release_group) and not anime_title
            and not pre.get("tv_source"))                     # a TV-source tag (PDTV / HDTV ...) is a release signal

    extra = False
    if check_extra:
        stem = os.path.splitext(name)[0]
        m = _EXTRA_RE.search(stem)
        if m and not is_episode and (m.start() > 0 or len(stem) <= len(m.group(0)) + 3):
            extra = True
        elif prep and _deep_extra(stem):
            extra = True

    date_ep = bool(prep and prep["date"])
    part_weak = bool(prep and _PART_RE.search(name) and not (g.get("year") or release_group))
    signals = []
    if strong_episode:
        signals.append("date" if date_ep else ("pack" if (pre.get("pack") or pack) else "marker"))
    if pre.get("tv_source"):
        signals.append("tv-source")
    if pre.get("broadcaster"):
        signals.append("bcast:" + pre["broadcaster"])
    if prep and prep["platforms"]:
        signals.append("plat:" + prep["platforms"][0])
    if part_weak:
        signals.append("part")

    result = {
        "titles": titles,
        # a date-named episode's year is the AIR year, not the show's release year
        "year": None if date_ep else _as_int(g.get("year")),
        "date_episode": date_ep,
        "air_year": prep["date"].year if date_ep else None,
        "platforms": list(prep["platforms"]) if prep else [],
        "part_weak": part_weak,
        "season": season_no,
        "episode": episode_no,
        "signals": signals,
        "is_episode": bool(is_episode),
        "gtype": gtype,
        "weak": bool(weak),
        "anime_tag": bool(anime_title),
        "anime_hint": bool(anime_hint),
        "has_movie_signal": bool(g.get("year")) or release_group,
        "release_group": release_group,
        "strong_episode": strong_episode,
        "extra": extra,
        # explicit "Episode N" / "S01E02" / "Season N" / "S01" in this name
        "series_like": bool(strong_episode or _SEASON_RE.search(name)),
        # deep-only: a season pack ("S01 Combined"), a TV-source rip (PDTV / HDTV ...), "not anime-style naming"
        "season_pack": bool(pre.get("pack")),
        # deep-only: the broader season-pack test (a season number or batch word and NO episode number, is_season_pack())
        "pack": bool(pack or pre.get("pack")),
        "tv_source": bool(pre.get("tv_source")),
        "serial_style": serial_style,
        # deep-only (neutral defaults otherwise)
        "clean_title": (shape["clean_title"] if shape else (titles[0] if prep else None)),
        "language": shape["language"] if shape else None,
        "franchise": shape["franchise"] if shape else None,
        "regional": bool(shape and shape["regional"]),
        "episode_title": (shape["episode_title"] or None) if shape else None,
        "junk_removed": bool(norm and norm["junk_removed"]),
        "uploaders": list(norm["uploaders"]) if norm else [],
        "candidates": cands or [],
        "anime": None,
        "movie": None,
    }
    if anime:
        _anime_attach(result, pre_norm, check_extra)
    if mv is not None:
        _movie_attach(result, mv)
    return result


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
def _early_category(message, archives_as_files: bool = False):
    """(category, names, is_files_variant). category is None for videos
    that still need title-based identification.

    archives_as_files (only /autosort's deep mode passes True): a zip / rar / 7z / ... file is
    "files" instead of "other", and the extra audio formats (DEEP_AUDIO_EXTENSIONS: .ac3 .dts .mka ...) are
    "audio". Default False, so /fwd, the whitelists and /autosortai are unchanged."""
    if getattr(message, "photo", None):
        return "image", [], False

    filename = get_filename(message)
    # deep (/autosort) only: a missing / truncated file name ("v .mp4") makes EVERY caption line a candidate
    # (up to CAPTION_LINES_WHEN_NAME_USELESS); a normal file name keeps the old two lines.
    wide = (CAPTION_LINES_WHEN_NAME_USELESS
            if (archives_as_files and (_name_is_useless(filename) or _name_is_generic(filename))) else 2)
    if not filename:
        if getattr(message, "voice", None) or getattr(message, "audio", None):
            return "audio", [], False
        # Native video uploads often have no file_name; the title is
        # usually in the caption.
        if getattr(message, "video", None) is not None:
            names = _candidate_names(message, wide, archives_as_files)
            if names:
                return None, names, False
        # deep: a video sent as a DOCUMENT with no file name (MIME video/*): the title is in the caption as well
        if archives_as_files and DEEP_VIDEO_BY_MIME and _doc_video_mime(message):
            names = _candidate_names(message, wide, True)
            if names:
                return None, names, True
        return "other", [], False

    ext = os.path.splitext(filename)[1].lower()
    if ext in PDF_EXTENSIONS:
        return "pdf", [], False
    if ext in IMAGE_EXTENSIONS:
        return "image", [], False
    if archives_as_files and _deep_is_video(message, filename, ext):
        # deep: a video by MIME type / container / release name (see _deep_is_video); the "_files" variant is a document
        return None, _candidate_names(message, wide, True), getattr(message, "document", None) is not None
    if archives_as_files and _video_first(message, filename, ext):
        # 4a: "Movie.2019.720p.WEB-DL.DDP5.1.AAC" (no real extension) / a native video named "... .DTS": a video, not audio
        return None, _candidate_names(message, wide, True), getattr(message, "document", None) is not None
    if ext in (DEEP_AUDIO_EXTENSIONS if archives_as_files else AUDIO_EXTENSIONS):
        return "audio", [], False

    is_split = _is_split_part(filename)
    is_video_ext = ext in VIDEO_EXTENSIONS
    if not is_video_ext and not is_split:
        if archives_as_files and ext in ARCHIVE_EXTENSIONS:
            return "files", [], False
        return "other", [], False
    if is_split and not is_video_ext:
        return "files", [], False

    # A video-extension file: "_files" variant only if it arrived as a
    # genuine document upload (see /diagnose_media).
    is_files_variant = getattr(message, "document", None) is not None
    return None, _candidate_names(message, wide, archives_as_files), is_files_variant


def _other_note(info, reason: str, detail: str = "") -> None:
    """Tell /autosort WHY a file is "other" (info["other_reason"] / info["other_detail"]; the first
    reason reported wins). Log information only: it never changes the returned category."""
    if info is not None and "other_reason" not in info:
        info["other_reason"], info["other_detail"] = reason, detail


def _early_other_reason(message, early: str):
    """(reason, detail) for a message _early_category() answered "other" / "files" without looking
    at its title (see handlers/autosort.py for the reason names)."""
    if early == "files":
        return "archive", "archive or split part"
    filename = get_filename(message)
    if filename:
        ext = os.path.splitext(filename)[1].lower() or "-"
        if ext in (".srt", ".ass", ".ssa", ".vtt", ".sub"):
            return "other_type", f"subtitle file ({ext}) stays in Other"
        if ext in (".txt", ".nfo"):
            return "other_type", f"text / info file ({ext}) stays in Other"
        return "other_type", f"extension {ext} is not video / pdf / image / audio / archive"
    if getattr(message, "video", None) is not None:
        return "unidentified", "video without a file name or a usable caption"
    return "other_type", "no file name (not a video, photo, audio or document)"


def classify(message) -> str:
    """Heuristic (no TMDB) category. Kept synchronous for the filename
    whitelists in plugins/channels.py. Movie/series/anime routing decisions
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
# Set via /settings -> Filters and enforced by plugins/channels.py's
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


def _title_score_movie(query: str, cand: str) -> float:
    """_title_score() for deep MOVIE names (/autosort only): the same exact / digit rules, spacing ignored
    ("m s m" == "msm"), and rapidfuzz for the fuzzy part when it is installed - else the old _title_score()."""
    if not query or not cand:
        return 0.0
    if query == cand:
        return 1.0
    if _DIGITS.findall(query) != _DIGITS.findall(cand):
        return 0.0
    sq, sc = query.replace(" ", ""), cand.replace(" ", "")
    if len(sq) >= 3 and sq == sc:
        return 1.0
    if _rf_fuzz is not None:
        return _rf_fuzz.ratio(query, cand) / 100.0
    return _title_score(query, cand)


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
    elif year and media_type == "tv":
        params["first_air_date_year"] = year      # only the deep /autosort layers pass a year for TV

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
  Page(perPage: 10) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      idMal
      format
      seasonYear
      startDate { year }
      endDate { year }
      episodes
      duration
      status
      isAdult
      genres
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
_anilist_warn_at = 0.0            # loop time of the last AniList WARNING: repeated errors are warned once a minute


def _anilist_note(query: str, status, count, started: float) -> None:
    """ONE INFO line per AniList request that went out (query, HTTP status, number of results, milliseconds). A failed
    request (count None) is also a WARNING, but at most once a minute, so a dead AniList cannot flood the log."""
    global _anilist_warn_at
    now = asyncio.get_event_loop().time()
    logger.info("AniList request: query=%r status=%s results=%s ms=%d",
                query, status, "error" if count is None else count, int((now - started) * 1000))
    if count is None and (not _anilist_warn_at or now - _anilist_warn_at >= 60):
        _anilist_warn_at = now
        logger.warning("AniList is failing (status %s for %r): anime checks use Jikan meanwhile; "
                       "repeated AniList errors are logged once a minute", status, query)


async def _anilist_search(session, query: str, anilist_key: str = ""):
    """List of AniList media dicts ([] = nothing found) or _TMDB_ERROR. Each request is logged once (_anilist_note)."""
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
    started, status = asyncio.get_event_loop().time(), "-"
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
                    status = resp.status
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
                        _anilist_note(query, status, None, started)
                        return _TMDB_ERROR
                    data = await resp.json()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("AniList lookup failed for %r: %s", query, e)
                status = type(e).__name__
                await asyncio.sleep(1 + attempt)
                continue
        if data.get("errors") and not data.get("data"):
            _anilist_note(query, status, None, started)
            return _TMDB_ERROR
        results = (((data.get("data") or {}).get("Page") or {}).get("media")) or []
        _anilist_note(query, status, len(results), started)
        _ANILIST_CACHE[ck] = results
        if len(_ANILIST_CACHE) > _CACHE_MAX:
            _ANILIST_CACHE.popitem(last=False)
        return results
    _anilist_note(query, status, None, started)
    return _TMDB_ERROR


def _anilist_best_match(results, queries, year, strict: bool, deep=None):
    """Pick the AniList entry whose title genuinely matches, or None.

    deep: only /autosort's deep_anime mode passes it (a dict: episode / season / minutes). Then the
    scored choice of _anilist_best_match_deep() is used and the return value is (media | None, details).
    Without it (every other caller) the behaviour and the return value are exactly as before."""
    if deep is not None:
        return _anilist_best_match_deep(results, queries, year, strict, deep)
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


def _best_match(results, media_type: str, queries, year, strict: bool, prefer_anime: bool = False, min_votes: int = STRICT_MIN_VOTES,
                scorer=None, year_required: bool = False):
    """Pick the TMDB result whose title genuinely matches, or None.

    prefer_anime: when two TV results match the title equally well (the
    anime "One Piece" vs the live-action "One Piece", Cowboy Bebop, Death
    Note, ...), pick the anime one if the filename looks like anime.
    Before this, the entry with more TMDB votes won, which was often the
    live-action remake -> the anime got filed as a normal series.

    scorer / year_required: only /autosort's deep movie names pass them (scorer = _title_score_movie;
    year_required = a number-only title: a MOVIE result must carry a release year that agrees). The
    defaults keep every other caller exactly as before."""
    best, best_key = None, None
    score_fn = scorer or _title_score
    for r in (results or [])[:10]:
        if media_type == "movie":
            names = (r.get("title"), r.get("original_title"))
        else:
            names = (r.get("name"), r.get("original_name"))
        score = max(
            (score_fn(q, _norm_cmp(n)) for q in queries for n in names if n),
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
        elif year_required and media_type == "movie":
            continue                      # no release year to compare: not good enough for a number-only title
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
    # deep_anime only (see "/autosort deep ANIME identification"): the TMDB match and why it is anime
    match = None
    anime_source = None
    anime_why = ""
    animelist = None

    def __new__(cls, status, kind, animated, tmdb_id=None, media_type=None):
        self = super().__new__(cls, (status, kind, animated))
        self.tmdb_id = tmdb_id
        self.media_type = media_type
        return self


async def _tmdb_identify_core(session, tmdb_key: str, p: dict, anime_mode: bool = False):
    """-> ("found", kind, animated) | ("none", None, False) | ("error", None, False)
    (an _Ident: that tuple plus .tmdb_id / .media_type).
    `animated` = the TMDB match carries the Animation genre."""
    # Episode / season naming (this name, or any other name of the same file:
    # see "series_like" in classify_async) is only ever looked up as TV.
    order = ["tv"] if (p["is_episode"] or p.get("series_like")) else ["movie", "tv"]
    if p.get("tv_source") and order == ["movie", "tv"]:
        order = ["tv", "movie"]           # deep: PDTV / HDTV / DSR / TVRip is a TV recording - look it up as a show first
    if p.get("year_required"):
        order = ["movie"]                 # deep movie name that is only a number ("2012"): films only, year must agree
    scorer = _title_score_movie if p.get("movie") else None       # deep movie names only
    year_required = bool(p.get("year_required"))
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
            match = _best_match(results, media_type, queries, p["year"], p["weak"], hint, min_votes, scorer, year_required)
            if not match and media_type == "movie" and p["year"]:
                # Extra page of candidates filtered by year on TMDB's side.
                results = await _tmdb_search(session, media_type, title, tmdb_key, p["year"])
                if results is _TMDB_ERROR:
                    errored = True
                    continue
                match = _best_match(results, media_type, queries, p["year"], p["weak"], hint, min_votes, scorer, year_required)
            if not match and results:
                match = await _match_by_alt_titles(
                    session, results, media_type, queries, p["year"], p["weak"], tmdb_key)
            if match:
                animated = TMDB_ANIMATION_GENRE_ID in (match.get("genre_ids") or [])
                mid = match.get("id")
                if _is_anime_result(match, media_type):
                    ident = _Ident("found", "anime", animated, mid, media_type)
                    if anime_mode:
                        ident.match, ident.anime_source = match, "tmdb"
                        ident.anime_why = f"animation + {_tmdb_asian_origin(match) or 'ANIME_ALL_ANIMATION'}"
                    return ident
                if anime_mode:
                    # deep_anime: TMDB keywords (japan / samurai / edo period ...) never decide. Anime =
                    # Animation + Asian origin_country, or the id is in the anime list. Otherwise it is a
                    # series / movie (the caller may still ask AniList / Jikan).
                    ok, src, why, listed = await _tmdb_anime_confirm(session, match, media_type, mid, animated)
                    ident = _Ident("found", "anime" if ok else ("movie" if media_type == "movie" else "series"),
                                   animated, mid, media_type)
                    ident.match, ident.anime_source, ident.anime_why, ident.animelist = match, src, why, listed
                    return ident
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


@_new_rule(lambda *a, **k: None)
def _note_tmdb_match(info, match: dict, media: str) -> None:
    """IDENT_RECORD: info["tmdb_match"] = the canonical title / year of the TMDB entry the file matched (read by
    _build_ident()). Never changes a verdict."""
    if info is None or not IDENT_RECORD or not isinstance(match, dict):
        return
    is_movie = media == "movie"
    info["tmdb_match"] = {
        "media": "movie" if is_movie else "tv", "id": match.get("id"),
        "title": (match.get("title") if is_movie else match.get("name")) or match.get("name") or match.get("title"),
        "original": match.get("original_title") if is_movie else match.get("original_name"),
        "year": _year_of(match.get("release_date") if is_movie else match.get("first_air_date")),
        "animated": TMDB_ANIMATION_GENRE_ID in (match.get("genre_ids") or []),
    }


async def _tmdb_identify(session, tmdb_key: str, p: dict, use_anilist: bool = False, anilist_key: str = "", info=None,
                         anime_mode: bool = False):
    """-> ("found", kind) | ("none", None) | ("error", None).

    With use_anilist, AniList gets a say ONLY when TMDB is unsure: nothing
    found, TMDB unreachable, or an animated series TMDB couldn't call anime.
    Live-action matches and TMDB-confirmed anime are never touched.

    info: optional dict that receives "tmdb_id" / "tmdb_media" of a TMDB match
    (read by /autosort; the result itself is not affected)."""
    ident = await _tmdb_identify_core(session, tmdb_key, p, anime_mode)
    status, kind, animated = ident
    if info is not None and status == "found":
        info["tmdb_id"] = ident.tmdb_id
        info["tmdb_media"] = ident.media_type
        if anime_mode and ident.match is not None:
            _anime_tmdb_fields(info, animated, ident.match.get("original_language"),
                               ",".join(ident.match.get("origin_country") or []), ident.anime_source,
                               ident.anime_why, ident.animelist)
        if ident.match is not None:
            _note_tmdb_match(info, ident.match, ident.media_type)
    if not use_anilist:
        if anime_mode and USE_ANIME_OFFLINE_DB and (status != "found" or (kind in ("series", "movie") and animated)):
            # AniList is OFF and TMDB is unsure: the anime-offline-database may know it (exact title + year, never raises)
            res = await _anime_lookup(session, p, anilist_key, False)
            if res["status"] == "found":
                _anime_note(info, anilist="off", source=res["source"], verdict="anime",
                            **_anime_media_fields(res["media"], res["source"]))
                return "found", "anime"
        return status, kind
    if status == "found" and not (kind in ("series", "movie") and animated):
        return status, kind           # anime already, or a live-action movie/series
    if anime_mode:
        # deep_anime: AniList (Jikan when AniList failed), scored choice + verdict cache
        res = await _anime_lookup(session, p, anilist_key, True)
        _anime_note(info, anilist=res["status"])
        if res["status"] == "found":
            _anime_note(info, source=res["source"], verdict="anime", **_anime_media_fields(res["media"], res["source"]))
            return "found", "anime"
        return status, kind
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


# ============================================================
# Round 3 helpers (/autosort deep mode): caption-only markers, education pre-check, the final check before Other,
# the identification record. Every helper is wrapped (_new_rule / try): an exception = one WARNING + the old behaviour.
# ============================================================
def _rule_warn(name: str, e: Exception) -> None:
    """ONE WARNING (per helper, at most once a minute) for a round-3 step that raised outside a _new_rule helper."""
    now = time.monotonic()
    if now - _RULE_WARNED.get(name, -1e9) > 60:
        _RULE_WARNED[name] = now
        logger.warning("autosort: new rule %s failed (%s: %s) - old behaviour for this file", name, type(e).__name__, e)


@_new_rule(lambda *a, **k: False)
def _caption_only_marker(parsed: list) -> bool:
    """CAPTION_EP_SERIES_ONLY: True when the FILE NAME's parse is clearly a movie (a year and quality tags, no season /
    episode marker) and a season / episode marker exists only in caption lines."""
    if not CAPTION_EP_SERIES_ONLY:
        return False
    fn = [pp for pp in parsed if pp.get("from_name")]
    if not fn or any(pp.get("series_like") for pp in fn):
        return False
    return all(pp.get("year") and (pp.get("movie") or {}).get("tech") and not pp.get("is_episode")
               and not pp.get("anime_tag") for pp in fn)


def _propagate_series_like(parsed: list, deep: bool = False) -> None:
    """If ANY name of the file says series (Episode N / SxxExx / Season N), every name is looked up as TV only. Deep exception:
    when the marker is only in a caption line and the file name is a movie name, the file name stays a movie search (movie
    first; the caption lines are still searched as TV) - season / episode text in a CAPTION never makes a movie a series."""
    if not any(pp.get("series_like") for pp in parsed):
        return
    if deep and _caption_only_marker(parsed):
        for pp in parsed:
            if not pp.get("from_name"):
                pp["series_like"] = True
        return
    for pp in parsed:
        pp["series_like"] = True


@_new_rule(lambda *a, **k: False)
def _edu_precheck(message, names, parsed) -> bool:
    """EDU_PRECHECK: STRONG course naming (numbered lecture "5.1.3 Intro", "Lecture 5", "class 12 chapter 3", a course platform)
    in a name with no year, no release tag and no episode marker is education - no source can confirm such a name anyway, so
    nothing is sent to TMDB / AniList. Soft course words, release names and episodes are NOT decided here."""
    if not EDU_PRECHECK or not parsed:
        return False
    if any(pp.get("has_movie_signal") or pp.get("strong_episode") or pp.get("year") or pp.get("extra") for pp in parsed):
        return False
    if is_nsfw(message) or _extra_hit(parsed, names):
        return False
    return _edu_level(names) == 2


_IDENT_CACHE: "OrderedDict" = OrderedDict()     # final-pass verdict per title: (normalized title, year, is_files) -> dict
_FINAL_SKIP_REASONS = {"extra", "education_vetoed", "nsfw_unmapped"}      # deliberate "other"s: not re-checked
_ARCHIVE_MIMES = ("application/zip", "application/x-zip", "application/x-zip-compressed", "application/x-rar",
                  "application/vnd.rar", "application/x-rar-compressed", "application/x-7z-compressed", "application/x-tar",
                  "application/gzip", "application/x-gzip", "application/x-bzip2", "application/x-xz")


def _mime_of(message) -> str:
    for attr in ("document", "video", "audio", "animation", "voice"):
        media = getattr(message, attr, None)
        mt = getattr(media, "mime_type", None) if media is not None else None
        if isinstance(mt, str) and mt:
            return mt.lower()
    return ""


@_new_rule(lambda *a, **k: None)
def _signal_recheck(message):
    """pdf | image | audio | files | video | None: what the cleaned extension (trailing dots / spaces ignored) and the MIME type
    say the file is. The last look before a file stays in Other."""
    mime = _mime_of(message)
    name = (get_filename(message) or "").strip().rstrip(". _")
    ext = os.path.splitext(name)[1].lower()
    if ext in PDF_EXTENSIONS or mime == "application/pdf":
        return "pdf"
    if ext in IMAGE_EXTENSIONS or mime.startswith("image/"):
        return "image"
    if _video_first(message, name, ext):
        return "video"                                # 4a: video first - an ".AAC" / ".DTS" release tag is not an audio file
    if ext in DEEP_AUDIO_EXTENSIONS or mime.startswith("audio/"):
        return "audio"
    if ext in ARCHIVE_EXTENSIONS or mime in _ARCHIVE_MIMES or (name and _is_split_part(name)):
        return "files"
    if ext in VIDEO_EXTENSIONS or ext in _DEEP_EXTRA_VIDEO_EXTENSIONS:
        return "video"
    if (mime.startswith("video/") or getattr(message, "video", None) is not None) and ext not in _KNOWN_EXTS:
        return "video"                                # a real non-video extension (".srt", ".txt" ...) is never a video by MIME
    return None


def _clear_other(info: dict) -> None:
    info.pop("other_reason", None)
    info.pop("other_detail", None)


@_new_rule(lambda *a, **k: [])
def _widen_names(message, names0: list) -> list:
    """The candidate names of the final check: the ones already tried, then EVERY usable caption line (up to 12) and the
    caption lines with a clear release signal, the file name first. The split-part suffix is removed by the parser itself."""
    out = []
    fn = get_filename(message)
    first = [fn] if fn and not _caption_primary(fn) else []
    for n in first + list(names0) + _candidate_names(message, 12, True):
        if n and n not in out:
            out.append(n)
    return out[:16]


def _widen_parse(raw: str, p: dict) -> None:
    """FINAL_PASS: one parsed name gets more title candidates - the CLASSIC (guessit-only) parse of the same name, and for a
    film with a year the joined / split / transliterated spellings. The deep clean-up can mangle a title the classic parse
    reads right (and the other way round), so both are tried."""
    p["final_pass"] = True
    try:
        classic = _parse_name(raw, deep=False)
    except Exception:
        classic = None
    if classic:
        have = {_norm_cmp(t) for t in p["titles"]}
        for t in classic.get("titles") or []:
            k = _norm_cmp(t)
            if k and k not in have and not _generic_title(t):
                p["titles"].append(t)
                have.add(k)
        p["titles"] = p["titles"][:6]
        keep = {c["title"]: c for c in p.get("candidates") or []}
        p["candidates"] = [keep.get(t) or {"title": t, "dropped": 0, "kind": "classic"} for t in p["titles"]]
    mv = p.get("movie")
    if mv and mv.get("title") and p.get("year"):
        mv["variants"] = list(dict.fromkeys(list(mv.get("variants") or []) + _title_variants(mv["title"], translit=True)))


async def _final_check(message, category, session, tmdb_key, use_anilist, anilist_key, openai_key, info, anime_mode) -> str:
    """FINAL_PASS: the last look before a file stays in Other. Skipped for deliberate Others (sample / trailer, course words
    with a known show, adult content). Otherwise:
      1. audio / pdf / image / archive / video once more, by the cleaned extension and the MIME type;
      2. ONE more run of the deep pipeline (_deep_series) over a wider candidate set: the file name, EVERY caption line, the
         lines with a release signal, the classic guessit titles, transliteration variants - TMDB, AniList, TVmaze, Wikidata,
         IMDb / OMDb, the anime lists, exactly the sources the first run uses (nothing is called twice: every search, show
         and film verdict is cached per title);
      3. education once more when no name could be parsed at all.
    -> the (possibly new) category; on success info[...] is replaced by the second run's details and other_reason is
    cleared; otherwise other_reason / other_detail are made as precise as possible and info["final_pass"] says what was tried.
    A TMDB failure during the extra lookups is no evidence: the first verdict stands (it never becomes tmdb_error)."""
    if category != "other" or not FINAL_PASS:
        return category
    reason = info.get("other_reason")
    if reason in _FINAL_SKIP_REASONS:
        return category
    fp = info.setdefault("final_pass", {})
    fp.update(checked=True, names=0, summary="")
    ctx = info.get("ctx") or {}
    names0, is_files = list(ctx.get("names") or []), bool(ctx.get("is_files"))
    if is_nsfw(message):
        fp["summary"] = "adult-content keywords: no lookups"
        return category

    sig = _signal_recheck(message)
    if sig in ("pdf", "image", "audio", "files"):
        _clear_other(info)
        fp.update(rescued=sig, summary=f"extension / MIME type say {sig}")
        logger.info("autosort final-pass: msg=%s name=%r other -> %s (extension / MIME type)",
                    getattr(message, "id", "-"), (get_filename(message) or "-")[:60], sig)
        return sig
    if sig == "video" and not names0:
        names0 = _candidate_names(message, CAPTION_LINES_WHEN_NAME_USELESS, True)
        is_files = getattr(message, "document", None) is not None
    if not names0:
        fp["summary"] = "no candidate name (not a video, or no file name and no usable caption)"
        return category
    if not tmdb_key or aiohttp is None or guessit is None:
        fp["summary"] = "no TMDB key / HTTP client / guessit: nothing to look up"
        return category

    names2 = _widen_names(message, names0)
    has_fn = bool(get_filename(message))
    parsed2 = []
    for i, raw in enumerate(names2):
        p = _parse_name_safe(raw, check_extra=(i == 0), deep=True, anime=anime_mode)
        if p:
            p["from_name"] = bool(has_fn and raw == get_filename(message))
            _widen_parse(raw, p)
            parsed2.append(p)
    fp["names"] = len(names2)
    if not parsed2:
        if _edu_level(names2) >= 1:
            _clear_other(info)
            fp.update(rescued="education", summary=f"no title in {len(names2)} names, course words")
            return f"education_files" if is_files else "education"
        info["other_reason"], info["other_detail"] = "unparseable", f"no usable title in any of {len(names2)} candidate names"
        fp["summary"] = info["other_detail"]
        return category
    if parsed2[0].get("extra"):
        fp["summary"] = "sample / trailer / promo in the name"
        return category
    _propagate_series_like(parsed2, True)
    titles = [t for pp in parsed2 for t in pp["titles"]]
    fp["titles"] = len(set(_norm_cmp(t) for t in titles))

    key = (_norm_cmp(parsed2[0]["titles"][0]), parsed2[0].get("year") or next((pp.get("year") for pp in parsed2 if pp.get("year")), None),
           is_files, anime_mode)
    hit = _IDENT_CACHE.get(key)
    if hit is not None:                                       # the same title was already re-checked in this run
        _IDENT_CACHE.move_to_end(key)
        if hit["category"] == "other":
            info["other_reason"], info["other_detail"] = hit["reason"], hit["detail"]
            fp["summary"] = hit["fp"] + " (cached verdict)"
            return category
        for k, v in hit["info"].items():
            info[k] = v
        _clear_other(info)
        fp.update(rescued=hit["category"], summary=hit["fp"] + " (cached verdict)")
        return hit["category"]

    owns = session is None
    if owns:
        session = aiohttp.ClientSession()
    info2: dict = {}
    try:
        cat2 = await _deep_series(message, names2, parsed2, is_files, session, tmdb_key, use_anilist, anilist_key,
                                  openai_key, info2, anime_mode)
    finally:
        if owns:
            await session.close()
    summary = f"{len(names2)} names, {fp['titles']} titles, sources: TMDB" + (" + AniList" if use_anilist else "") + " + fallbacks"
    base = cat2[:-6] if isinstance(cat2, str) and cat2.endswith("_files") else cat2
    if base in ("movie", "series", "anime", "education"):
        for k in ("tmdb_id", "tmdb_media", "tmdb_match", "series", "anime", "movie"):
            if k in info2:
                info[k] = info2[k]
        _clear_other(info)
        fp.update(rescued=cat2, summary=summary)
        if len(_IDENT_CACHE) >= _CACHE_MAX:
            _IDENT_CACHE.popitem(last=False)
        _IDENT_CACHE[key] = {"category": cat2, "fp": summary,
                             "info": {k: info2[k] for k in ("tmdb_id", "tmdb_media", "tmdb_match", "series", "anime", "movie") if k in info2}}
        logger.info("autosort final-pass: msg=%s name=%r other(%s) -> %s (%s)", getattr(message, "id", "-"),
                    (names2[0] or "-")[:60], reason, cat2, summary)
        return cat2
    if cat2 == TMDB_UNAVAILABLE:
        fp["summary"] = summary + "; TMDB failed on the extra lookups, first verdict kept"
        return category
    r2, d2 = info2.get("other_reason"), info2.get("other_detail")
    if r2 and reason in (None, "unidentified"):
        info["other_reason"], info["other_detail"] = r2, d2 or "-"        # searched and nothing matched beats "no reason"
    fp["summary"] = summary
    if r2 == "not_found":
        if len(_IDENT_CACHE) >= _CACHE_MAX:
            _IDENT_CACHE.popitem(last=False)
        _IDENT_CACHE[key] = {"category": "other", "reason": info.get("other_reason") or r2,
                             "detail": info.get("other_detail") or d2 or "-", "fp": summary}
    logger.info("autosort final-pass: msg=%s name=%r stays other (%s): %s", getattr(message, "id", "-"),
                (names2[0] or "-")[:60], info.get("other_reason"), summary)
    return category


@_new_rule(lambda *a, **k: None)
def _build_ident(info: dict, category: str):
    """IDENT_RECORD: info["ident"] = {kind, title, year, source, id} for a file classified movie / series / anime (+ "_files"),
    from the record the identification found - never from the raw file name:
      movie   TMDB title + release year (or the film the IMDb / variant search found)
      series  the TMDB / TVmaze / Wikidata show title + first-air year
      anime   AniList / Jikan title (English, falling back to romaji; trailing "2nd Season" words removed, the season goes
              into SxxExx) + the entry's year; TMDB's name when only TMDB knew it
    No record (the title was proven another way - episode marker alone, Wikidata only, OpenAI ...): no info["ident"], and the
    caption builder keeps today's file-name caption."""
    info.pop("ident", None)
    if not IDENT_RECORD:
        return None
    base = category[:-6] if category.endswith("_files") else category
    if base not in ("movie", "series", "anime"):
        return None
    tm = info.get("tmdb_match") or {}
    title = year = source = sid = season_hint = None
    if base == "anime":
        a = info.get("anime") or {}
        t = a.get("title_en") or a.get("title_romaji")
        if t:
            base_t, season_hint, _part = _strip_season_words(t)           # "X 2nd Season" -> "X", season 2
            title, year = base_t or t, a.get("media_year")
            source, sid = a.get("source") or "anilist", a.get("anilist_id") or a.get("mal_id")
        elif tm.get("title"):
            title, year, source, sid = tm["title"], tm.get("year"), "tmdb", tm.get("id")
    elif base == "series":
        canon = (info.get("series") or {}).get("canon") or {}
        if canon.get("title"):
            title, year, source, sid = canon["title"], canon.get("year"), canon.get("source") or "tmdb", canon.get("id")
        elif tm.get("media") == "tv" and tm.get("title"):
            title, year, source, sid = tm["title"], tm.get("year"), "tmdb", tm.get("id")
    else:
        if tm.get("media") == "movie" and tm.get("title"):
            title, year, source, sid = tm["title"], tm.get("year"), "tmdb", tm.get("id")
        else:
            mv = info.get("movie") or {}
            if mv.get("matched") and mv.get("source"):
                title, year, source, sid = mv["matched"], mv.get("matched_year"), mv["source"], mv.get("tmdb_id") or mv.get("imdb_id")
    if not title or not str(title).strip():
        return None
    info["ident"] = {"kind": base, "title": str(title).strip(), "year": year if isinstance(year, int) else None,
                     "source": source, "id": sid}
    if base == "anime" and isinstance(season_hint, int):
        info["ident"]["season"] = season_hint
    return info["ident"]


async def classify_async(message, session=None, tmdb_key: str = "", use_anilist: bool = False, anilist_key: str = "", openai_key: str = "", info=None, deep_series: bool = False, deep_anime: bool = False) -> str:
    """Category for forwarding - see _classify_async_main() for the full description.

    deep_series=False (/fwd, matches_async): exactly _classify_async_main(), nothing else runs.
    deep_series=True (/autosort), after the main classification:
      * FINAL_PASS: a file that would stay in "other" gets ONE more full check (see _final_check());
      * IDENT_RECORD: info["ident"] = the identified record (kind, canonical title, year, source, id), see _build_ident().
    Both are wrapped: an exception logs one WARNING and the main answer stands."""
    if not deep_series:
        return await _classify_async_main(message, session, tmdb_key, use_anilist, anilist_key, openai_key, info,
                                          deep_series, deep_anime)
    if info is None:
        info = {}
    category = await _classify_async_main(message, session, tmdb_key, use_anilist, anilist_key, openai_key, info,
                                          deep_series, deep_anime)
    try:
        category = await _final_check(message, category, session, tmdb_key or ENV_TMDB_API_KEY, use_anilist, anilist_key,
                                      openai_key, info, bool(deep_anime and deep_series))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        _rule_warn("_final_check", e)
    _build_ident(info, category)
    return category


async def _classify_async_main(message, session=None, tmdb_key: str = "", use_anilist: bool = False, anilist_key: str = "", openai_key: str = "", info=None, deep_series: bool = False, deep_anime: bool = False) -> str:
    """Category for forwarding. Videos are routed to movie / series /
    anime ONLY when TMDB confirms the title; otherwise "unidentified"
    (or "tmdb_error") is returned, which no channel maps to -> skipped.

    Without a TMDB key (or without aiohttp/guessit) this falls back to the
    filename-only guess from classify().

    info: optional dict. When TMDB matched the file it receives "tmdb_id" and
    "tmdb_media" ("movie" | "tv"). /autosort uses them for its runtime check
    and its log line; passing it never changes the returned category.

    deep_series: opt-in (default False = everything behaves as before). /autosort
    passes True: names are normalised and parsed deeper (URL-decoding, junk tokens,
    uploader tags, platform / language / franchise tables), a TMDB-unknown show is looked
    up on TVmaze and then Wikidata with a list of title candidates, and a "series" verdict
    needs a confidence score (see "/autosort deep SERIES identification" at the end of
    this file). It may fill info["series"].

    deep_anime: opt-in, only effective together with deep_series (default False = everything
    behaves as before). /autosort passes True: anime names are cleaned (groups, dub words, "_Remove"
    junk, season markers), anime candidates ask AniList (Jikan if AniList failed) BEFORE TMDB with a
    scored choice, TMDB keywords no longer make a show anime (Animation + Asian origin, the
    Fribb anime list, or AniList do), and NCOP / NCED / AMV / ... are extras. It may fill info["anime"].
    See "/autosort deep ANIME identification" at the end of this file.
    """
    early, names, is_files = _early_category(message, archives_as_files=deep_series)
    if info is not None and deep_series:
        info.setdefault("ctx", {}).update(names=list(names), is_files=bool(is_files), early=early)   # read by _final_check()
    if early is not None:
        if early in ("other", "files"):
            _other_note(info, *_early_other_reason(message, early))
        return early

    tmdb_key = tmdb_key or ENV_TMDB_API_KEY
    if not tmdb_key or aiohttp is None or guessit is None:
        guess = classify(message)
        if guess == "other":
            _other_note(info, "unidentified", "no TMDB key / HTTP client / guessit available: file-name guess only")
        return guess

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

    anime_mode = bool(deep_anime and deep_series)
    parsed = []
    has_fn = bool(get_filename(message))
    for i, raw in enumerate(names):
        # The first name (the file name, or - for native videos that have none -
        # the first caption line) is checked for sample / trailer / teaser ...
        # Before this was skipped when there was no file name, so a caption-only
        # "Movie (2025) Official Trailer" was matched to the movie on TMDB.
        p = _parse_name_safe(raw, check_extra=(i == 0), deep=deep_series, anime=anime_mode)
        if p:
            p["from_name"] = bool(has_fn and raw == get_filename(message))   # this parse is the FILE NAME's (not a caption line's)
            parsed.append(p)
    if not parsed:
        _other_note(info, "unidentified", "no usable title in the name")
        return await _other_or_openai(message, names, is_files, session, openai_key)
    if parsed[0]["extra"]:
        _other_note(info, "extra", "sample / trailer / promo / recap in the name")
        if deep_series and info is not None and any(pp["strong_episode"] for pp in parsed):
            info["series"] = {"candidate": True, "source": "-", "reason": "extra (sample/promo/trailer/recap) -> other",
                              "title": parsed[0].get("clean_title") or parsed[0]["titles"][0],
                              "language": parsed[0].get("language")}
        return "other"
    # One name says "Episode 5" / "S01E02" / "Season 2": look up ALL names of this file as TV only. Before, another name
    # (a plain caption line without a marker) was still searched as a movie and could turn an episode into a movie that
    # merely shares the show's title. Deep: a marker found ONLY in the caption does not do that to a file name that is
    # clearly a movie (year + quality tags, no marker) - see _propagate_series_like().
    _propagate_series_like(parsed, deep_series)
    if deep_series and _edu_precheck(message, names, parsed):
        # EDU_PRECHECK: a strong course name with no year / release tag is education - decided before any TMDB / AniList call
        return _finish("education")

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()
    try:
        if deep_series:
            return await _deep_series(message, names, parsed, is_files, session, tmdb_key, use_anilist,
                                      anilist_key, openai_key, {} if info is None else info, anime_mode)
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
#   SPLIT_PARTS_SKIP_DURATION  1 (default) | 0: a split video part (".part004.mkv", ".mkv.001") is only
#                         a slice of the film, so the duration guard and the TMDB runtime check
#                         (all three gates) skip it. Its thumbnail rules still apply.
# Documents (message.document, not message.video) skip the thumbnail / ratio / duration / runtime
# gates altogether: Telegram gives a document no preview size and no duration - handlers/autosort.py.
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


def movie_thumb_gate(message, ratio_min=None, ratio_max=None, unknown=None, require_thumb=False):
    """-> (ok, ratio, source): does this video look like a normal landscape
    video preview?

    Ratio (width / height) comes from, in this order:
      "thumb"   the LARGEST entry (width * height) of media.thumbs
      "video"   media.width / media.height, when there is no usable thumbnail
      "unknown" neither exists -> ratio None
    ok is True when MOVIE_THUMB_RATIO_MIN <= ratio <= MOVIE_THUMB_RATIO_MAX
    (default 1.30 .. 2.40: 4:3 up to scope-wide; square and portrait posters
    fail). With an unknown ratio ok follows MOVIE_RATIO_UNKNOWN ("other" ->
    False, "allow" -> True).

    ratio_min / ratio_max / unknown override those three settings (the series
    gate passes its own SERIES_* values); left out, movies behave exactly as before.

    require_thumb=True (the series gate, SERIES_REQUIRE_THUMB): a video / document whose
    media.thumbs is empty or None has NO thumbnail and is not acceptable - the width /
    height fallback must not rescue it -> (False, None, "no-thumb"). Default False, so
    the movie behaviour is byte-for-byte what it was."""
    lo = MOVIE_THUMB_RATIO_MIN if ratio_min is None else ratio_min
    hi = MOVIE_THUMB_RATIO_MAX if ratio_max is None else ratio_max
    unknown = MOVIE_RATIO_UNKNOWN if unknown is None else unknown
    media = _movie_media(message)
    if require_thumb and (media is None or not getattr(media, "thumbs", None)):
        return False, None, "no-thumb"
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
        return unknown == "allow", None, "unknown"
    return lo <= ratio <= hi, ratio, source


def has_video_thumb(message) -> bool:
    """True when the video / video-document carries at least one thumbnail (media.thumbs
    is neither None nor empty). Used by the series gate and its log line."""
    media = _movie_media(message)
    return media is not None and bool(getattr(media, "thumbs", None))


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


SPLIT_PARTS_SKIP_DURATION = os.environ.get("SPLIT_PARTS_SKIP_DURATION", "1").strip().lower() not in ("0", "false", "no")


def is_split_video_part(message) -> bool:
    """True for one slice of a split video: ".part004.mkv", "name.mkv.001", "name.mkv.r00". The file must be
    a split name AND the whole file must carry a video extension, so a ".part1.rar" archive is not one."""
    name = ""
    try:
        name = get_filename(message) or ""
        return (_is_split_part(name)
                and os.path.splitext(_strip_split_suffix(name))[1].lower() in VIDEO_EXTENSIONS)
    except Exception:
        return False


def runtime_mismatch(file_minutes, runtime_minutes) -> bool:
    """True when both lengths are known and the file differs from TMDB's runtime
    by more than MOVIE_RUNTIME_TOLERANCE (default 35 %) of that runtime."""
    if not file_minutes or not runtime_minutes or file_minutes <= 0 or runtime_minutes <= 0:
        return False
    return abs(file_minutes - runtime_minutes) / runtime_minutes > MOVIE_RUNTIME_TOLERANCE


# ============================================================
# /autosort deep SERIES identification + series gate settings
# ============================================================
# Opt-in: classify_async(..., deep_series=True). Only handlers/autosort.py passes
# it, so /fwd, matches_async() and the whitelists behave exactly as before.
#
# Sources, in order: TMDB -> AniList (existing, inside the TMDB step, only when TMDB is
# unsure) -> TVmaze -> Wikidata -> OpenAI (existing, only when a key is passed) -> scored
# fallback. A source that answered "not found" is not a source that FAILED: if TMDB
# failed the file is still TMDB_UNAVAILABLE (nothing is guessed); a failed TVmaze /
# Wikidata only means there is no evidence from that source for that file.
#
# Every source is searched with the SAME list of title candidates (see
# _build_candidates(): cleaned title, title + language, shortened titles), best first, and
# stops at the first confident hit. Each source is asked at most SERIES_MAX_SEARCHES times
# per distinct show (per source, because their rate limits are separate), and the verdict
# is cached per show (cleaned title + language + year), "not found" included, so the
# hundreds of episodes of one show cost a handful of calls in total.
#
# Env vars (all optional):
#   USE_TVMAZE=1                 look up shows TMDB does not know on TVmaze (no API key)
#   USE_WIKIDATA=1               ... and then on Wikidata (no API key; TV-type items only)
#   WIKIDATA_USER_AGENT          User-Agent sent to Wikidata (a default is built in)
#   SERIES_MAX_SEARCHES=4        title candidates searched per show and source (1..8)
#   SERIES_MIN_SCORE=4           confidence needed for "series" (weights below)
#   SERIES_THUMB_RATIO_MIN / _MAX  thumbnail band of the series gate, default 1.30 .. 2.40
#   SERIES_RATIO_UNKNOWN=other   "other" | "allow": no thumbnail size at all
#   SERIES_REQUIRE_THUMB=1       a video / document with NO thumbnail goes to Other
#   SERIES_MIN_MINUTES=8         shorter = clip / promo        (handlers/autosort.py _series_gate)
#   SERIES_MAX_MINUTES=360       longer  = compilation / film  (handlers/autosort.py _series_gate)
#   SERIES_UPLOADER_TAGS=YourShivay   comma separated uploader tags removed from names
USE_TVMAZE = os.environ.get("USE_TVMAZE", "1").strip().lower() not in ("0", "false", "no")
USE_WIKIDATA = os.environ.get("USE_WIKIDATA", "1").strip().lower() not in ("0", "false", "no")
SERIES_MIN_SCORE = _env_float("SERIES_MIN_SCORE", 4.0)
SERIES_MAX_SEARCHES = max(1, min(8, int(_env_float("SERIES_MAX_SEARCHES", 4))))

SERIES_MIN_MINUTES = max(0.0, _env_float("SERIES_MIN_MINUTES", 8.0))
SERIES_MAX_MINUTES = _env_float("SERIES_MAX_MINUTES", 360.0)
if SERIES_MAX_MINUTES <= SERIES_MIN_MINUTES:
    logger.warning("SERIES_MIN_MINUTES/MAX (%s / %s) are not a valid range - using 8 / 360",
                   SERIES_MIN_MINUTES, SERIES_MAX_MINUTES)
    SERIES_MIN_MINUTES, SERIES_MAX_MINUTES = 8.0, 360.0

SERIES_THUMB_RATIO_MIN = _env_float("SERIES_THUMB_RATIO_MIN", _DEFAULT_THUMB_RATIO_MIN)
SERIES_THUMB_RATIO_MAX = _env_float("SERIES_THUMB_RATIO_MAX", _DEFAULT_THUMB_RATIO_MAX)
if not 0 < SERIES_THUMB_RATIO_MIN < SERIES_THUMB_RATIO_MAX:
    logger.warning("SERIES_THUMB_RATIO_MIN/MAX (%s / %s) are not a valid range - using %s / %s",
                   SERIES_THUMB_RATIO_MIN, SERIES_THUMB_RATIO_MAX, _DEFAULT_THUMB_RATIO_MIN, _DEFAULT_THUMB_RATIO_MAX)
    SERIES_THUMB_RATIO_MIN, SERIES_THUMB_RATIO_MAX = _DEFAULT_THUMB_RATIO_MIN, _DEFAULT_THUMB_RATIO_MAX

SERIES_RATIO_UNKNOWN = os.environ.get("SERIES_RATIO_UNKNOWN", "other").strip().lower()
if SERIES_RATIO_UNKNOWN not in ("other", "allow"):
    SERIES_RATIO_UNKNOWN = "other"

# A video / document without any thumbnail is not a proper series episode: "other".
SERIES_REQUIRE_THUMB = os.environ.get("SERIES_REQUIRE_THUMB", "1").strip().lower() not in ("0", "false", "no")

# ---- ONE thumbnail gate for ALL native-video channels (handlers/autosort.py) --------------------------------------------
# A native video (message.video) sent to Movies / Series / Anime / Education needs a REAL thumbnail of a landscape shape;
# the video's own width / height never rescues a missing thumbnail. First failure wins, the result is "other" (reason
# no_thumb / ratio). Documents ("*_files" channels) are never gated.
#   MOVIE_REQUIRE_THUMB=1       Movies:    a video with NO thumbnail goes to Other (the ratio band is MOVIE_THUMB_RATIO_*)
#   EDUCATION_REQUIRE_THUMB=1   Education: the same for the education channel (video only, no duration limits)
#   EDUCATION_THUMB_GATE=1      master switch of the education gate (0 = education videos are not gated at all)
#   EDUCATION_THUMB_RATIO_MIN / _MAX  thumbnail band of the education gate, default 1.30 .. 2.40
#   EDUCATION_RATIO_UNKNOWN=other     "other" | "allow": no thumbnail size at all
#   SEASONPACK_SKIP_MAX_DURATION=1    a season pack skips SERIES_MAX_MINUTES / ANIME_MAX_MINUTES (minimum, thumbnail and
#                                     ratio rules still apply)
MOVIE_REQUIRE_THUMB = _env_on("MOVIE_REQUIRE_THUMB")
EDUCATION_REQUIRE_THUMB = _env_on("EDUCATION_REQUIRE_THUMB")
EDUCATION_THUMB_GATE = _env_on("EDUCATION_THUMB_GATE")
SEASONPACK_SKIP_MAX_DURATION = _env_on("SEASONPACK_SKIP_MAX_DURATION")

EDUCATION_THUMB_RATIO_MIN = _env_float("EDUCATION_THUMB_RATIO_MIN", _DEFAULT_THUMB_RATIO_MIN)
EDUCATION_THUMB_RATIO_MAX = _env_float("EDUCATION_THUMB_RATIO_MAX", _DEFAULT_THUMB_RATIO_MAX)
if not 0 < EDUCATION_THUMB_RATIO_MIN < EDUCATION_THUMB_RATIO_MAX:
    logger.warning("EDUCATION_THUMB_RATIO_MIN/MAX (%s / %s) are not a valid range - using %s / %s",
                   EDUCATION_THUMB_RATIO_MIN, EDUCATION_THUMB_RATIO_MAX, _DEFAULT_THUMB_RATIO_MIN, _DEFAULT_THUMB_RATIO_MAX)
    EDUCATION_THUMB_RATIO_MIN, EDUCATION_THUMB_RATIO_MAX = _DEFAULT_THUMB_RATIO_MIN, _DEFAULT_THUMB_RATIO_MAX

EDUCATION_RATIO_UNKNOWN = os.environ.get("EDUCATION_RATIO_UNKNOWN", "other").strip().lower()
if EDUCATION_RATIO_UNKNOWN not in ("other", "allow"):
    EDUCATION_RATIO_UNKNOWN = "other"

# Score weights (_score_series). A guessit-only "episode" verdict (no explicit marker)
# counts half a marker: guessit also reads bare numbers ("Show 105") that way.
# "Part N" is never an episode marker (film sequels use it).
_W_MARKER, _W_WEAK_EPISODE = 4, 2
_W_TV_SOURCE = 2                 # PDTV / HDTV / DSR / TVRip / DVBS in the name: a TV recording (a positive series signal)
_W_TMDB, _W_TMDB_EXACT = 3, 1
_W_TVMAZE, _W_AGREE = 3, 1
_W_WIKIDATA = 2                  # Wikidata knows it as a TV-type item (only asked when TMDB and TVmaze do not)
_W_OPENAI = 3                    # OpenAI said "series" (only asked when TMDB, TVmaze and Wikidata know nothing)
_W_YEAR, _W_NUMBERS, _W_PLATFORM, _W_DURATION = 1, 1, 1, 1
_W_LANG = 1                      # language read from the name matches the matched show's language
_W_EP_TITLE = 1                  # the episode title is an episode name of that season on TMDB
_W_EDU = _W_NSFW = _W_EXTRA = -4
_W_MOVIE_LIKE = -2
_W_JUNK_UNCONFIRMED = -3         # "None" / "null" ... had to be cut out of the name AND no source confirms the show
_W_SEASON_BEYOND = -1            # season number above what TMDB lists (TMDB lags for new / regional shows: soft)
_SHORTEN_PENALTY = 0.06          # per word dropped from a title candidate (see _build_candidates)


# ---- fuzzy title matching for the deep matcher ----------------------------------
def _sim(a: str, b: str) -> float:
    if _rf_fuzz is not None:
        return _rf_fuzz.ratio(a, b) / 100.0
    return SequenceMatcher(None, a, b).ratio()


def _collapse(s: str) -> str:
    return re.sub(r"(.)\1+", r"\1", s)


def _title_score_deep(query: str, cand: str) -> float:
    """_title_score() for the deep series matcher: rapidfuzz when installed (else
    difflib), and doubled letters are ignored so Indian spelling variants ("Kkoi" /
    "Koi", "Chhajje" / "Chajje") still match. The digit rule is unchanged."""
    if not query or not cand:
        return 0.0
    if query == cand:
        return 1.0
    if _DIGITS.findall(query) != _DIGITS.findall(cand):
        return 0.0
    if _keys_equal(query, cand):
        return 1.0                                            # "wall e" = "walle", "house mates" = "housemates"
    return max(_sim(query, cand), _sim(_collapse(query), _collapse(cand)))


def _lang_code_of(value):
    """ISO code of a language given as TMDB's "kn" or TVmaze's "Kannada"; None when unknown."""
    if not value or not isinstance(value, str):
        return None
    v = _alnum(value)
    return _LANG_ANY.get(v) or (v if v in _LANG_FULL.values() else None)


# ---- TVmaze (free, no key) --------------------------------------------------------
TVMAZE_BASE = "https://api.tvmaze.com"
TVMAZE_TIMEOUT_SECONDS = 8
TVMAZE_RETRIES = 3
TVMAZE_MIN_INTERVAL = 0.6        # TVmaze allows about 20 calls / 10 s: stay under it
TVMAZE_DOWN_SECONDS = 60         # after a failed call skip TVmaze this long (a dead TVmaze must not slow a 1-lakh run)
_tvmaze_lock = None
_tvmaze_last = 0.0
_tvmaze_down_until = 0.0


async def _tvmaze_get(session, path: str, params=None):
    """Parsed JSON, None (HTTP 404 = not found) or _TMDB_ERROR. Never raises. One
    request at a time, spaced TVMAZE_MIN_INTERVAL apart (shared by every file);
    429 -> wait and retry."""
    global _tvmaze_lock, _tvmaze_last, _tvmaze_down_until
    if aiohttp is None:
        return _TMDB_ERROR
    if _tvmaze_lock is None:
        _tvmaze_lock = asyncio.Lock()
    loop = asyncio.get_event_loop()
    if loop.time() < _tvmaze_down_until:
        return _TMDB_ERROR
    for attempt in range(TVMAZE_RETRIES):
        async with _tvmaze_lock:
            wait = TVMAZE_MIN_INTERVAL - (loop.time() - _tvmaze_last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with session.get(
                    TVMAZE_BASE + path, params=params,
                    timeout=aiohttp.ClientTimeout(total=TVMAZE_TIMEOUT_SECONDS),
                ) as resp:
                    _tvmaze_last = loop.time()
                    if resp.status == 429:
                        try:
                            retry = int(resp.headers.get("Retry-After", 3))
                        except (TypeError, ValueError):
                            retry = 3
                        await asyncio.sleep(min(max(retry, 1), 30))
                        continue
                    if resp.status >= 500:
                        await asyncio.sleep(1 + attempt)
                        continue
                    if resp.status == 404:
                        return None
                    if resp.status != 200:
                        return _TMDB_ERROR
                    return await resp.json()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("TVmaze %s failed: %s", path, e)
                await asyncio.sleep(1 + attempt)
                continue
    _tvmaze_down_until = loop.time() + TVMAZE_DOWN_SECONDS
    return _TMDB_ERROR


# ---- Wikidata (free, no key): the last identification layer -------------------------
# Mainly for regional / reality shows that TMDB and TVmaze both lack. A hit counts only when
# the item's label / alias matches the title (same fuzzy rule as TMDB) AND one of its
# "instance of" (P31) classes is a television-type class. The class is decided at run time
# from its English label and up to two "subclass of" (P279) steps - no Q-id is trusted
# blindly - and the verdict per class is cached for the whole run.
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_TIMEOUT_SECONDS = 8
WIKIDATA_RETRIES = 2
WIKIDATA_MIN_INTERVAL = 1.0      # one request per second, shared by every file
WIKIDATA_DOWN_SECONDS = 60
WIKIDATA_USER_AGENT = (os.environ.get("WIKIDATA_USER_AGENT", "").strip()
                       or "ForwardV5-AutoSort/1.0 (Telegram media sorter; read-only title lookups)")
_wd_lock = None
_wd_last = 0.0
_wd_down_until = 0.0
_WD_TV_WORDS = ("television series", "television program", "television show", "tv series", "tv program",
                "reality television", "reality show", "web series", "miniseries", "mini-series", "soap opera",
                "telenovela", "game show", "talk show", "sitcom", "television serial")
_WD_NOT_WORDS = ("season", "episode", "film", "character", "list of", "channel", "studio", "genre")
_WD_CLASS_CACHE: "OrderedDict" = OrderedDict()      # class Q-id -> bool (is it a television-type class)


async def _wikidata_get(session, params: dict):
    """Parsed JSON dict or _TMDB_ERROR. Never raises. One request at a time, spaced
    WIKIDATA_MIN_INTERVAL apart, a proper User-Agent, 8 s timeout; 429 -> wait and retry;
    after repeated failures Wikidata is skipped for WIKIDATA_DOWN_SECONDS."""
    global _wd_lock, _wd_last, _wd_down_until
    if aiohttp is None:
        return _TMDB_ERROR
    if _wd_lock is None:
        _wd_lock = asyncio.Lock()
    loop = asyncio.get_event_loop()
    if loop.time() < _wd_down_until:
        return _TMDB_ERROR
    query = dict(params)
    query["format"] = "json"
    headers = {"User-Agent": WIKIDATA_USER_AGENT, "Accept": "application/json"}
    for attempt in range(WIKIDATA_RETRIES):
        async with _wd_lock:
            wait = WIKIDATA_MIN_INTERVAL - (loop.time() - _wd_last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with session.get(
                    WIKIDATA_API, params=query, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=WIKIDATA_TIMEOUT_SECONDS),
                ) as resp:
                    _wd_last = loop.time()
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
                    data = await resp.json(content_type=None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Wikidata %s failed: %s", query.get("action"), e)
                await asyncio.sleep(1 + attempt)
                continue
        if isinstance(data, dict) and not data.get("error"):
            return data
        await asyncio.sleep(1 + attempt)                 # {"error": ...} (maxlag, readonly ...): try again
    _wd_down_until = loop.time() + WIKIDATA_DOWN_SECONDS
    return _TMDB_ERROR


def _wd_label_says_tv(label: str) -> bool:
    low = (label or "").lower()
    if not low or any(w in low for w in _WD_NOT_WORDS):
        return False
    return any(w in low for w in _WD_TV_WORDS)


async def _wd_class_is_tv(session, qid: str, depth: int = 0):
    """True / False, or None when Wikidata could not be asked (nothing is cached then)."""
    hit = _WD_CLASS_CACHE.get(qid)
    if hit is not None:
        _WD_CLASS_CACHE.move_to_end(qid)
        return hit
    data = await _wikidata_get(session, {"action": "wbgetentities", "ids": qid, "props": "labels|claims",
                                         "languages": "en"})
    if data is _TMDB_ERROR:
        return None
    ent = (data.get("entities") or {}).get(qid) or {}
    label = ((ent.get("labels") or {}).get("en") or {}).get("value") or ""
    result = _wd_label_says_tv(label)
    if not result and depth < 2:
        unknown = False
        for parent in _wd_claim_ids(ent, "P279")[:5]:
            sub = await _wd_class_is_tv(session, parent, depth + 1)
            if sub:
                result = True
                break
            if sub is None:
                unknown = True
        if not result and unknown:
            return None
    _WD_CLASS_CACHE[qid] = bool(result)
    if len(_WD_CLASS_CACHE) > _CACHE_MAX:
        _WD_CLASS_CACHE.popitem(last=False)
    return bool(result)


def _wd_claim_ids(entity: dict, prop: str) -> list:
    """Q-ids a statement property points at (deprecated statements ignored)."""
    out = []
    for st in ((entity.get("claims") or {}).get(prop) or []):
        try:
            if st.get("rank") == "deprecated":
                continue
            qid = st["mainsnak"]["datavalue"]["value"]["id"]
        except (KeyError, TypeError, AttributeError):
            continue
        if isinstance(qid, str) and qid not in out:
            out.append(qid)
    return out


# ---- show facts (one shape for TMDB, TVmaze and Wikidata) and the per-show cache --
def _empty_facts(source: str, sid=None) -> dict:
    return {"source": source, "id": sid, "names": [], "first_year": None, "last_year": None,
            "season_years": set(), "seasons": {}, "total_eps": None, "in_production": None,
            "networks": [], "lang": None, "runtime": None, "genres": [], "type": None}


def _tmdb_facts(d: dict) -> dict:
    f = _empty_facts("tmdb", d.get("id"))
    f["names"] = [n for n in (d.get("name"), d.get("original_name")) if n]
    f["first_year"], f["last_year"] = _year_of(d.get("first_air_date")), _year_of(d.get("last_air_date"))
    for s in d.get("seasons") or []:
        if isinstance(s, dict) and s.get("season_number"):          # season 0 = specials
            f["seasons"][s["season_number"]] = s.get("episode_count")
            y = _year_of(s.get("air_date"))
            if y:
                f["season_years"].add(y)
    f["total_eps"] = d.get("number_of_episodes") or sum(v for v in f["seasons"].values() if v) or None
    f["in_production"] = d.get("in_production")
    f["networks"] = [n.get("name") for n in d.get("networks") or [] if isinstance(n, dict) and n.get("name")]
    f["lang"] = d.get("original_language")
    runs = [r for r in d.get("episode_run_time") or [] if isinstance(r, int) and r > 0]
    f["runtime"] = runs[0] if runs else None
    f["genres"] = [g.get("name") for g in d.get("genres") or [] if isinstance(g, dict) and g.get("name")]
    f["type"] = d.get("type")
    return f


def _tvmaze_facts(show: dict) -> dict:
    f = _empty_facts("tvmaze", show.get("id"))
    f["names"] = [show.get("name")] if show.get("name") else []
    f["first_year"] = _year_of(show.get("premiered"))
    running = show.get("status") == "Running"
    f["last_year"] = _year_of(show.get("ended")) or (datetime.date.today().year if running else f["first_year"])
    for s in (show.get("_embedded") or {}).get("seasons") or []:
        if isinstance(s, dict) and s.get("number"):
            f["seasons"][s["number"]] = s.get("episodeOrder")
            y = _year_of(s.get("premiereDate"))
            if y:
                f["season_years"].add(y)
    f["total_eps"] = sum(v for v in f["seasons"].values() if v) or None
    f["in_production"] = running
    net = show.get("network") or show.get("webChannel") or {}
    f["networks"] = [net["name"]] if isinstance(net, dict) and net.get("name") else []
    f["lang"] = show.get("language")
    f["runtime"] = show.get("averageRuntime") or show.get("runtime")
    f["genres"] = [g for g in show.get("genres") or [] if isinstance(g, str)]
    f["type"] = show.get("type")
    return f


# (normalized cleaned title, language, year or None) -> {"tmdb": entry, "tvmaze": entry, "wikidata": entry};
# entry = {"source", "id", "verdict": "found" | "none", "facts", ...}. One key per SHOW, so the
# hundreds of episodes of one show reuse one verdict - "not found" included, so a missing show
# is searched once, not once per episode. (TMDB's own search results are cached separately in
# _SEARCH_CACHE, TVmaze / Wikidata search results in _QUERY_CACHE.) Errors are never cached.
# Same title + different language / year = different key. LRU, _CACHE_MAX entries.
_SHOW_CACHE: "OrderedDict" = OrderedDict()
_QUERY_CACHE: "OrderedDict" = OrderedDict()     # (source, normalized query) -> raw search results


def _show_year(p: dict):
    return None if p.get("date_episode") else p.get("year")


def _show_key(p: dict):
    title = p.get("clean_title") or (p["titles"][0] if p.get("titles") else "")
    key = (_norm_cmp(title), p.get("language"), _show_year(p))
    return key + ("final",) if p.get("final_pass") else key       # the final check (FINAL_PASS) caches its own verdicts


def _show_get(p: dict, source: str):
    key = _show_key(p)
    rec = _SHOW_CACHE.get(key)
    if rec is None:
        return None
    _SHOW_CACHE.move_to_end(key)
    return rec.get(source)


def _show_put(p: dict, source: str, entry: dict) -> None:
    key = _show_key(p)
    _SHOW_CACHE.setdefault(key, {})[source] = entry
    _SHOW_CACHE.move_to_end(key)
    if len(_SHOW_CACHE) > _CACHE_MAX:
        _SHOW_CACHE.popitem(last=False)


def _qc_get(source: str, title: str):
    key = (source, _norm_cmp(title))
    hit = _QUERY_CACHE.get(key)
    if hit is not None:
        _QUERY_CACHE.move_to_end(key)
    return hit


def _qc_put(source: str, title: str, value) -> None:
    _QUERY_CACHE[(source, _norm_cmp(title))] = value
    if len(_QUERY_CACHE) > _CACHE_MAX:
        _QUERY_CACHE.popitem(last=False)


def _cands_of(p: dict) -> list:
    """The search candidates of a parsed name (deep mode sets them; else its titles)."""
    return p.get("candidates") or [{"title": t, "dropped": 0, "kind": "title"} for t in p.get("titles") or []]


async def _tmdb_tv_details(session, tmdb_id, tmdb_key: str):
    """Normalized facts of one TMDB show (GET /tv/{id}) or _TMDB_ERROR."""
    for attempt in range(TMDB_RETRIES):
        try:
            async with session.get(
                f"{TMDB_BASE}/tv/{tmdb_id}",
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
            logger.debug("TMDB tv details failed for tv/%s: %s", tmdb_id, e)
            await asyncio.sleep(1 + attempt)
            continue
        return _tmdb_facts(payload) if isinstance(payload, dict) else _TMDB_ERROR
    return _TMDB_ERROR


_SEASON_CACHE: "OrderedDict" = OrderedDict()    # (tmdb tv id, season) -> [episode names] ([] = TMDB lists none)


async def _tmdb_season_names(session, tmdb_key: str, tv_id, season):
    """Episode names of one TMDB season (GET /tv/{id}/season/{n}), cached; [] when TMDB has no
    such season (404 is cached too); _TMDB_ERROR on a failure (not cached)."""
    ck = (tv_id, season)
    hit = _SEASON_CACHE.get(ck)
    if hit is not None:
        _SEASON_CACHE.move_to_end(ck)
        return hit
    for attempt in range(TMDB_RETRIES):
        try:
            async with session.get(
                f"{TMDB_BASE}/tv/{tv_id}/season/{season}",
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
                if resp.status == 404:
                    payload = {}
                elif resp.status != 200:
                    return _TMDB_ERROR
                else:
                    payload = await resp.json()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("TMDB season failed for tv/%s/season/%s: %s", tv_id, season, e)
            await asyncio.sleep(1 + attempt)
            continue
        rows = payload.get("episodes") if isinstance(payload, dict) else None
        names = [e.get("name") for e in rows or [] if isinstance(e, dict) and e.get("name")]
        _SEASON_CACHE[ck] = names
        if len(_SEASON_CACHE) > _CACHE_MAX:
            _SEASON_CACHE.popitem(last=False)
        return names
    return _TMDB_ERROR


def _episode_name_hit(names, episode_title: str) -> bool:
    """The file's episode title is (nearly) the name of an episode in `names`."""
    q = _norm_cmp(episode_title or "")
    if len(q) < 4:
        return False
    for n in names or []:
        c = _norm_cmp(n or "")
        if not c:
            continue
        if q == c or _title_score_deep(q, c) >= 0.85:
            return True
        if len(q.split()) >= 2 and len(c.split()) >= 2 and (f" {q} " in f" {c} " or f" {c} " in f" {q} "):
            return True
    return False


async def _episode_title_ok(session, tmdb_key: str, p: dict, facts) -> bool:
    """+1 signal: the episode title of the file is an episode name of that season on TMDB.
    No call without an episode title + season, or when TMDB does not list that season."""
    title, season = p.get("episode_title"), p.get("season")
    if not title or not season or not facts or not facts.get("id"):
        return False
    seasons = facts.get("seasons") or {}
    if seasons and season not in seasons:
        return False
    names = await _tmdb_season_names(session, tmdb_key or ENV_TMDB_API_KEY, facts["id"], season)
    return names is not _TMDB_ERROR and _episode_name_hit(names, title)


# ---- TMDB TV match for deep mode ---------------------------------------------------
def _tv_rank(results, cand: dict, p: dict) -> list:
    """TV results whose name matches one title candidate: [(result, effective score, raw
    score)]. A candidate is accepted only when raw >= MIN_TITLE_SCORE (exact for weak names
    with enough votes, as before) AND raw - dropped words * _SHORTEN_PENALTY still reaches
    MIN_TITLE_SCORE - so a title shortened by one word must match the show's name (almost)
    exactly and one shortened by two words exactly."""
    q = _norm_cmp(cand["title"])
    weak = bool(p.get("weak"))
    need_raw = 1.0 if weak else MIN_TITLE_SCORE
    min_votes = EPISODE_MIN_VOTES if p.get("strong_episode") else STRICT_MIN_VOTES
    penalty = cand.get("dropped", 0) * _SHORTEN_PENALTY
    prefix2 = bool(PREFIX2_NEEDS_YEAR and cand.get("kind") == "prefix2")
    rows = []
    for r in (results or [])[:10]:
        raw = max((_title_score_deep(q, _norm_cmp(n)) for n in (r.get("name"), r.get("original_name")) if n),
                  default=0.0)
        eff = raw - penalty
        if raw < need_raw or eff < MIN_TITLE_SCORE - 1e-9:
            continue
        if weak and (r.get("vote_count") or 0) < min_votes:
            continue
        if prefix2:
            # a 2-word PREFIX of a longer title ("Blue Eye" of "Blue Eye Samurai") only counts when the file's year agrees
            ry, fy = _year_of(r.get("first_air_date")), p.get("year")
            if not (fy and ry and abs(ry - fy) <= 1):
                continue
        rows.append((r, eff, raw))
    return rows


def _tv_choose(rows, p: dict, proven=None) -> dict:
    """Pick one of the matching TV results.

    Best title score wins; among results within 0.02 of it the order is: language of the show
    equals the language read from the name, anime when the name looks like anime, the air
    year, votes. A franchise with regional versions (Bigg Boss ...) whose language is NOT
    known and whose equally good matches are different-language versions is AMBIGUOUS: the
    most voted one is taken as the generic franchise match (still a series), flagged.
    proven (deep_anime only): the ids among the tie that an anime list confirms. Then naming alone ("Episode N"
    without a season) no longer prefers the anime sibling - only a confirmed one is preferred.
    -> {"chosen", "tie", "ambiguous", "lang_ok"}"""
    lang, hint = p.get("language"), bool(p.get("anime_hint"))
    best = max(e for _r, e, _raw in rows)
    tie = [r for r, e, _raw in rows if e >= best - 0.02]

    def key(r):
        ry = _year_of(r.get("first_air_date"))
        anime_pref = (r.get("id") in proven) if proven is not None else (hint and _is_anime_result(r, "tv"))
        return (1 if lang and r.get("original_language") == lang else 0,
                1 if anime_pref else 0,
                1 if (p.get("year") and ry == p["year"]) else 0,
                r.get("vote_count") or 0)

    tie.sort(key=key, reverse=True)
    chosen = tie[0]
    ids = {r.get("id") for r in tie}
    langs = {r.get("original_language") for r in tie}
    ambiguous = bool(not lang and p.get("regional") and len(ids) >= 2 and len(langs) >= 2)
    lang_ok = None if not lang else (chosen.get("original_language") == lang)
    return {"chosen": chosen, "tie": tie, "ambiguous": ambiguous, "lang_ok": lang_ok}


async def _tmdb_tv_show(session, tmdb_key: str, p: dict, anime_mode: bool = False):
    """Identify the SHOW of one parsed name on TMDB with the candidate list.
    -> (entry | None, errored). entry = {"verdict": "found" | "none", "kind": "series" | "anime",
    "id", "facts", "animated", "ambiguous", "lang_ok", "cand", "why", "cacheable"}; None entry
    = TMDB failed and found nothing (never cached)."""
    errored = False
    for cand in _cands_of(p)[:SERIES_MAX_SEARCHES]:
        results = await _tmdb_search(session, "tv", cand["title"], tmdb_key)
        if results is _TMDB_ERROR:
            errored = True
            continue
        rows = _tv_rank(results, cand, p)
        if cand.get("dropped"):
            # a shortened title is a weaker identification: it may confirm a live-action series,
            # but it never turns a file into anime (a "Monster Hunter ..." name must not match
            # the anime "Monster")
            rows = [row for row in rows if TMDB_ANIMATION_GENRE_ID not in (row[0].get("genre_ids") or [])]
        if not rows and results and not cand.get("dropped"):
            # romaji / alternative titles of animated Asian-origin shows (unchanged rule)
            alt = await _match_by_alt_titles(session, results, "tv", [_norm_cmp(cand["title"])],
                                             p.get("year"), bool(p.get("weak")), tmdb_key)
            if alt:
                rows = [(alt, 1.0, 1.0)]
        if not rows:
            continue
        proven = None
        if anime_mode and p.get("anime_hint") and not (p.get("anime_tag") or (p.get("anime") or {}).get("strong")):
            # deep_anime: the name only LOOKS anime-ish (episode without a season, dub word): it must not decide
            # between a live-action and an animated show with the same title - an anime-list hit does
            top = max(e for _r, e, _raw in rows)
            proven = set()
            for r, e, _raw in rows:
                if e >= top - 0.02 and r.get("id") and _is_anime_result(r, "tv") and await _anime_list_lookup(session, "tv", r["id"]):
                    proven.add(r["id"])
        pick = _tv_choose(rows, p, proven)
        chosen, ambiguous, lang_ok = pick["chosen"], pick["ambiguous"], pick["lang_ok"]
        distinct = {r.get("id"): r for r in pick["tie"]}
        if len(distinct) >= 2 and p.get("episode_title") and p.get("season"):
            # tie-breaker: the version whose season lists an episode with this title
            hits = []
            for r in sorted(distinct.values(), key=lambda x: x.get("vote_count") or 0, reverse=True)[:3]:
                names = await _tmdb_season_names(session, tmdb_key, r.get("id"), p["season"])
                if names is not _TMDB_ERROR and _episode_name_hit(names, p["episode_title"]):
                    hits.append(r)
            if len(hits) == 1:
                chosen, ambiguous = hits[0], False
                lang_ok = None if not p.get("language") else (chosen.get("original_language") == p["language"])
        mid = chosen.get("id")
        animated = TMDB_ANIMATION_GENRE_ID in (chosen.get("genre_ids") or [])
        kind, why, a_src, a_list = "series", "", None, None
        if _is_anime_result(chosen, "tv"):
            kind, why = "anime", f"tmdb tv {mid}: animation + original_language={chosen.get('original_language')}"
            a_src = "tmdb"
        elif anime_mode:
            # deep_anime: no keyword rule (it made Blue Eye Samurai anime). Animation + Asian
            # origin_country, or the anime list.
            ok, a_src, why2, a_list = await _tmdb_anime_confirm(session, chosen, "tv", mid, animated)
            if ok:
                kind, why = "anime", f"tmdb tv {mid}: {why2}"
        elif animated and mid:
            kws = await _tmdb_keywords(session, "tv", mid, tmdb_key)
            if kws is _TMDB_ERROR:
                return None, True                 # don't guess during an outage (as before)
            if _keywords_say_anime(kws, strict=False):
                kind, why = "anime", f"tmdb tv {mid}: animated + anime keywords"
        facts, cacheable = None, True
        if kind == "series" and mid:
            facts = await _tmdb_tv_details(session, mid, tmdb_key)
            if facts is _TMDB_ERROR:
                facts, cacheable = _empty_facts("tmdb", mid), False
        if ambiguous:
            logger.info("autosort series-ident: AMBIGUOUS franchise %r, language unknown, %d versions match equally "
                        "(%s) -> generic match tmdb tv %s kept as series", cand["title"], len(distinct),
                        ",".join(sorted(str(r.get("original_language")) for r in distinct.values())), mid)
        elif p.get("language") and lang_ok is False:
            logger.info("autosort series-ident: language %s read from the name, TMDB tv %s is %s -> series, no language bonus",
                        p["language"], mid, chosen.get("original_language"))
        return ({"source": "tmdb", "id": mid, "verdict": "found", "kind": kind, "animated": animated,
                 "facts": facts, "ambiguous": ambiguous, "lang_ok": lang_ok, "cand": cand, "why": why,
                 "cacheable": cacheable, "anime_source": a_src if kind == "anime" else None, "animelist": a_list,
                 "tmdb_title": chosen.get("name"), "tmdb_year": _year_of(chosen.get("first_air_date")),
                 "tmdb_lang": chosen.get("original_language"),
                 "tmdb_origin": ",".join(chosen.get("origin_country") or [])}, errored)
    if errored:
        return None, True
    return {"source": "tmdb", "id": None, "verdict": "none", "kind": None, "animated": False, "facts": None,
            "ambiguous": False, "lang_ok": None, "cand": None, "why": "", "cacheable": True}, False


async def _deep_tmdb_tv(session, tmdb_key: str, p: dict, use_anilist: bool, anilist_key: str, info: dict,
                        anime_mode: bool = False) -> dict:
    """TMDB (+ AniList when TMDB is unsure) for an episode-like name, deep mode. Same meaning as
    _tmdb_identify(): -> {"status": "found" | "none" | "error", "kind", "entry", "why"}. The show
    verdict is cached per show, so only the first episode of a show searches."""
    cache_src = "tmdb_a" if anime_mode else "tmdb"     # the two modes reach different verdicts: separate cache entries
    ent, errored = _show_get(p, cache_src), False
    if ent is None:
        ent, errored = await _tmdb_tv_show(session, tmdb_key, p, anime_mode)
        if ent is not None and ent.get("cacheable"):
            _show_put(p, cache_src, ent)
    found = bool(ent and ent["verdict"] == "found")
    status = "found" if found else ("error" if errored and ent is None else "none")
    kind = ent["kind"] if found else None
    why = ent.get("why", "") if found else ""
    if found and info is not None:
        info["tmdb_id"], info["tmdb_media"] = ent["id"], "tv"
        if IDENT_RECORD and ent.get("tmdb_title"):
            info["tmdb_match"] = {"media": "tv", "id": ent["id"], "title": ent.get("tmdb_title"), "original": None,
                                  "year": ent.get("tmdb_year"), "animated": bool(ent.get("animated"))}
        if anime_mode:
            _anime_tmdb_fields(info, ent.get("animated"), ent.get("tmdb_lang"), ent.get("tmdb_origin"),
                               ent.get("anime_source"), ent.get("why") if kind == "anime" else None, ent.get("animelist"))
    if use_anilist and (status != "found" or (kind == "series" and ent.get("animated"))):
        if anime_mode:
            res = await _anime_lookup(session, p, anilist_key, True)
            _anime_note(info, anilist=res["status"])
            if res["status"] == "found":
                status, kind, why = "found", "anime", f"{res['source']} match"
                _anime_note(info, source=res["source"], verdict="anime", **_anime_media_fields(res["media"], res["source"]))
        else:
            titles = [c["title"] for c in _cands_of(p) if not c.get("dropped")][:3]
            a_status, _media = await anilist_identify(
                session, {"titles": titles, "year": p.get("year"), "weak": p.get("weak", False)}, anilist_key)
            if a_status == "found":
                status, kind, why = "found", "anime", "anilist match"
    elif (not use_anilist and anime_mode and USE_ANIME_OFFLINE_DB
          and (status != "found" or (kind == "series" and ent.get("animated")))):
        # AniList is OFF and TMDB is unsure: the anime-offline-database may know it (exact title + year, never raises)
        res = await _anime_lookup(session, p, anilist_key, False)
        if res["status"] == "found":
            status, kind, why = "found", "anime", f"{res['source']} match"
            _anime_note(info, anilist="off", source=res["source"], verdict="anime",
                        **_anime_media_fields(res["media"], res["source"]))
    return {"status": status, "kind": kind, "entry": ent, "why": why}


async def _tmdb_facts_for(session, tmdb_key: str, p: dict, tmdb_id):
    """Facts of the TMDB show a file matched (cached per show). If the details call
    fails the match still counts, just without the soft signals."""
    e = _show_get(p, "tmdb")
    if e and e.get("id") == tmdb_id and e.get("facts"):
        return e["facts"]
    facts = await _tmdb_tv_details(session, tmdb_id, tmdb_key or ENV_TMDB_API_KEY)
    if facts is _TMDB_ERROR:
        return _empty_facts("tmdb", tmdb_id)
    _show_put(p, "tmdb", {"source": "tmdb", "id": tmdb_id, "verdict": "found", "kind": "series", "animated": False,
                          "facts": facts, "ambiguous": False, "lang_ok": None, "cand": None, "why": "",
                          "cacheable": True})
    return facts


# ---- TVmaze match -------------------------------------------------------------------
def _tvmaze_pick(results, cand: dict, p: dict):
    """The TVmaze show whose title genuinely matches this candidate (same fuzzy rule as TMDB:
    close title - exact for weak names -, shortened candidates need a near-exact title; the
    year must fall inside the years the show aired). With a language read from the name, a
    show in that language wins a tie."""
    best, best_key = None, None
    year = _show_year(p)
    q = _norm_cmp(cand["title"])
    need = 1.0 if p.get("weak") else MIN_TITLE_SCORE
    penalty = cand.get("dropped", 0) * _SHORTEN_PENALTY
    lang = p.get("language")
    for r in (results or [])[:5]:
        show = r.get("show") if isinstance(r, dict) else None
        if not isinstance(show, dict) or not show.get("name"):
            continue
        raw = _title_score_deep(q, _norm_cmp(show["name"]))
        eff = raw - penalty
        if raw < need or eff < MIN_TITLE_SCORE - 1e-9:
            continue
        if year:
            sy = _year_of(show.get("premiered"))
            ey = _year_of(show.get("ended")) or (datetime.date.today().year + 1)
            if sy and (year < sy - 1 or year > ey + 1):
                continue
        if PREFIX2_NEEDS_YEAR and cand.get("kind") == "prefix2":
            sy = _year_of(show.get("premiered"))
            if not (year and sy and abs(year - sy) <= 1):     # a 2-word prefix needs the year to agree
                continue
        key = (round(eff, 2), 1 if lang and _lang_code_of(show.get("language")) == lang else 0, show.get("weight") or 0)
        if best is None or key > best_key:
            best, best_key = show, key
    return best


async def _tvmaze_lookup(session, cand: dict, p: dict):
    """Cache entry for one title candidate (found / none), or None when TVmaze failed."""
    data = _qc_get("tvmaze", cand["title"])
    if data is None:
        data = await _tvmaze_get(session, "/search/shows", {"q": cand["title"]})
        if data is _TMDB_ERROR:
            return None
        data = data if isinstance(data, list) else []
        _qc_put("tvmaze", cand["title"], data)
    show = _tvmaze_pick(data, cand, p)
    if show is None:
        return {"source": "tvmaze", "id": None, "verdict": "none", "facts": None}
    detail = await _tvmaze_get(session, f"/shows/{show.get('id')}", {"embed": "seasons"})
    if isinstance(detail, dict):               # seasons for the number check; the search hit is enough if this fails
        show = detail
    return {"source": "tvmaze", "id": show.get("id"), "verdict": "found", "facts": _tvmaze_facts(show)}


async def _tvmaze_facts_for(session, p: dict):
    """-> (facts | None, "found" | "none" | "error") for the show of one parsed name."""
    entry = _show_get(p, "tvmaze")
    if entry is None:
        errored = False
        for cand in _cands_of(p)[:SERIES_MAX_SEARCHES]:
            e = await _tvmaze_lookup(session, cand, p)
            if e is None:
                errored = True
                continue
            if e["verdict"] == "found":
                entry = e
                break
        if entry is None and not errored:
            entry = {"source": "tvmaze", "id": None, "verdict": "none", "facts": None}
        if entry is not None:
            _show_put(p, "tvmaze", entry)
    if entry is None:
        return None, "error"
    return (entry["facts"], "found") if entry["verdict"] == "found" else (None, "none")


# ---- Wikidata match -------------------------------------------------------------------
async def _wikidata_lookup(session, cand: dict, p: dict):
    """Cache entry for one title candidate (found / none), or None when Wikidata failed."""
    hits = _qc_get("wikidata", cand["title"])
    if hits is None:
        data = await _wikidata_get(session, {"action": "wbsearchentities", "search": cand["title"], "language": "en",
                                             "uselang": "en", "type": "item", "limit": "7"})
        if data is _TMDB_ERROR:
            return None
        hits = [h for h in (data.get("search") or []) if isinstance(h, dict) and h.get("id")]
        _qc_put("wikidata", cand["title"], hits)
    q = _norm_cmp(cand["title"])
    need = 1.0 if p.get("weak") else MIN_TITLE_SCORE
    penalty = cand.get("dropped", 0) * _SHORTEN_PENALTY
    scored = []
    for h in hits:
        match = h.get("match") or {}
        labels = [h.get("label")] + list(h.get("aliases") or []) + ([match.get("text")] if match.get("type") == "alias" else [])
        raw = max((_title_score_deep(q, _norm_cmp(n)) for n in labels if isinstance(n, str) and n), default=0.0)
        if raw >= need and raw - penalty >= MIN_TITLE_SCORE - 1e-9:
            scored.append((raw - penalty, h))
    scored.sort(key=lambda t: t[0], reverse=True)
    scored = scored[:3]
    none = {"source": "wikidata", "id": None, "verdict": "none", "facts": None}
    if not scored:
        return none
    ents = await _wikidata_get(session, {"action": "wbgetentities", "ids": "|".join(h["id"] for _s, h in scored),
                                         "props": "claims"})
    if ents is _TMDB_ERROR:
        return None
    unknown = False
    for _score, h in scored:
        entity = (ents.get("entities") or {}).get(h["id"]) or {}
        for cls in _wd_claim_ids(entity, "P31")[:8]:
            verdict = await _wd_class_is_tv(session, cls)
            if verdict:
                facts = _empty_facts("wikidata", h["id"])
                facts["names"] = [n for n in [h.get("label")] + list(h.get("aliases") or []) if isinstance(n, str) and n]
                return {"source": "wikidata", "id": h["id"], "verdict": "found", "facts": facts}
            if verdict is None:
                unknown = True
    return None if unknown else none


async def _wikidata_facts_for(session, p: dict):
    """-> (facts | None, "found" | "none" | "error") for the show of one parsed name."""
    entry = _show_get(p, "wikidata")
    if entry is None:
        errored = False
        for cand in _cands_of(p)[:SERIES_MAX_SEARCHES]:
            if cand.get("kind") in ("prefix2", "spacevar"):
                continue                                      # Wikidata has no year rule for these weaker candidates
            e = await _wikidata_lookup(session, cand, p)
            if e is None:
                errored = True
                continue
            if e["verdict"] == "found":
                entry = e
                break
        if entry is None and not errored:
            entry = {"source": "wikidata", "id": None, "verdict": "none", "facts": None}
        if entry is not None:
            _show_put(p, "wikidata", entry)
    if entry is None:
        return None, "error"
    return (entry["facts"], "found") if entry["verdict"] == "found" else (None, "none")


# ---- deep MOVIE identification: what happens when TMDB answered "not found" -----------------------------------------
# Only for MOVIE-LIKE names (p["movie"], see _movie_info()) in /autosort's deep mode, and only after the normal
# TMDB search of the cleaned titles found nothing (TMDB reachable, no match). In this order:
#   1. QUERY VARIANTS ("Radhe krishna" -> "Radhekrishna", "Madharasi" -> "Madharaasi", "Chaadum" -> "Chadum"): TMDB is
#      searched with each variant (with the year, then one /search/multi), and a result counts ONLY when the file has a
#      year AND the result's release year agrees (+-1) AND the title key matches (_ext_rank()). So a variant can never
#      match a wrong film, and a film with few votes is accepted when year and language agree.
#   2. YEAR + LANGUAGE POOL: TMDB's /discover pages of that year in hi / ta / te / ml / kn / bn / mr / pa, compared locally.
#   3. IMDb suggestions (OMDb too when OMDB_API_KEY is set): same title, same year (+-1), same type; the tt id is resolved
#      with TMDB /find.
#   4. WIKIDATA, strict: the item's label / alias equals the title, its publication year (P577) agrees (+-1) and one
#      of its "instance of" classes is a FILM class. No Q-id is trusted blindly: a class is a film class when its
#      English label is "film" (Q11424) / "<genre> film" / "feature film", or one of its "subclass of" parents is.
# The verdict ("found" / "none") of the whole chain is cached per film (cleaned title + year); a network error is never cached.
# Env vars (all optional):  MOVIE_MAX_SEARCHES=6      TMDB query-variant searches per distinct title (0 = off;
#                                                     MOVIE_VARIANT_SEARCHES is the old name of the same setting)
#                           MOVIE_POOL_PAGES=5        /discover pages per year + media + language (0 = off)
#                           MOVIE_FUZZY_MIN=0.9       closest-title score that also needs an Indian language on both sides
#                           USE_IMDB_SUGGEST=1        IMDb suggestions        OMDB_API_KEY=  OMDb (asked only when set)
#                           USE_WIKIDATA_FILMS=1      Wikidata for films (also needs USE_WIKIDATA=1)
MOVIE_MAX_SEARCHES = max(0, min(10, int(_env_float("MOVIE_MAX_SEARCHES", _env_float("MOVIE_VARIANT_SEARCHES", 6)))))
MOVIE_POOL_PAGES = max(0, min(10, int(_env_float("MOVIE_POOL_PAGES", 5))))     # /discover pages per year + media + language (0 = off)
MOVIE_FUZZY_MIN = _env_float("MOVIE_FUZZY_MIN", 0.9)       # fuzzy title key score that needs an Indian-language match too
USE_IMDB_SUGGEST = os.environ.get("USE_IMDB_SUGGEST", "1").strip().lower() not in ("0", "false", "no")
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "").strip()   # OMDb is only asked when this is set
USE_WIKIDATA_FILMS = os.environ.get("USE_WIKIDATA_FILMS", "1").strip().lower() not in ("0", "false", "no")
_INDIAN_LANGS = {"hi", "ml", "ta", "te", "kn", "mr", "bn", "pa", "gu", "bho", "or", "as", "ur"}
_WD_FILM_NOT_WORDS = ("series", "festival", "studio", "character", "score", "award", "genre", "franchise",
                      "adaptation", "critic", "industry", "company", "list of", "category", "short film",
                      "soundtrack", "poster")
_WD_FILM_CACHE: "OrderedDict" = OrderedDict()      # class Q-id -> bool (is it a film class)


def _movie_note(info, **kw) -> dict:
    """info["movie"] (what handlers/autosort.py may log); a throw-away dict when info is None."""
    m = info.setdefault("movie", {}) if info is not None else {}
    m.update(kw)
    return m


# ---- the extra layers below: shared rules and helpers (all /autosort deep mode, none of them ever raises) ----------
# A hit must agree with the file's YEAR (+-1) and TYPE. "Not found" is a real answer; a network error means "unknown" (no
# guess, no exception). Every extra source is throttled, cached and has an 8 s timeout. Titles are compared LOCALLY with
# _cmp_key() on both sides (lower case, accents folded, punctuation / apostrophes / spaces removed, doubled vowels
# collapsed; for Indian-language names also every transliteration spelling in both directions).
_POOL_LANGS = ("hi", "ta", "te", "ml", "kn", "bn", "mr", "pa")
_POOL_CACHE: "OrderedDict" = OrderedDict()       # (media, year, lang, page) -> (rows, total_pages): /discover pages
_EXT_STATE: dict = {}                            # service -> {"lock", "last", "down"}: throttle state shared by every file
_EXT_INTERVAL = {"imdb": 0.4, "omdb": 0.3}       # seconds between two requests to one service
_EXT_TIMEOUT_SECONDS = 8
_EXT_DOWN_SECONDS = 60                           # after a failed request the service is skipped this long


def _cmp_key(text: str) -> str:
    """Comparison key of a title ("Madharaasi" == "Madharasi", "Radhe krishna" == "Radhekrishna", "Aren't" == "Arent")."""
    return re.sub(r"([aeiou])\1+", r"\1", _norm_cmp(text or "").replace(" ", ""))


def _ext_keys(text: str) -> set:
    """_cmp_key() of a title and of all its transliteration spellings, shortened and lengthened."""
    return {k for k in (_cmp_key(v) for v in [text, *_translit_variants(text), *_translit_expand(text)]) if k}


def _key_score(a: str, b: str, translit: bool = False) -> float:
    """1.0 when the two titles share a key; 0 when their numbers differ ("Spider-Man 2" is not "Spider-Man") or the key
    is too short for a fuzzy match; else the fuzzy similarity of the keys (rapidfuzz, difflib without it)."""
    ka, kb = _cmp_key(a), _cmp_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb or (translit and _ext_keys(a) & _ext_keys(b)):
        return 1.0
    if _DIGITS.findall(ka) != _DIGITS.findall(kb) or min(len(ka), len(kb)) < 5:
        return 0.0
    return _rf_fuzz.ratio(ka, kb) / 100.0 if _rf_fuzz is not None else SequenceMatcher(None, ka, kb).ratio()


def _row_names(row: dict, media: str) -> list:
    keys = ("title", "original_title") if media == "movie" else ("name", "original_name")
    return [row[k] for k in keys if isinstance(row.get(k), str) and row[k]]


def _row_year(row: dict, media: str):
    return _year_of(row.get("release_date") if media == "movie" else row.get("first_air_date"))


def _row_indian(row: dict) -> bool:
    return row.get("original_language") in _INDIAN_LANGS or "IN" in (row.get("origin_country") or ())


def _ext_rank(row: dict, media: str, titles, year, indian_name: bool, translit: bool, pool: bool = False, extra_names=()):
    """Rank tuple (title score, same year, votes) when a TMDB result passes the deep rules, else None. The year agrees
    (+-1) AND the title key matches: exactly (spacing / spelling variants) with the name OR TMDB saying Indian, or - in
    the year pool - with the same year alone; closely (>= MOVIE_FUZZY_MIN) only when BOTH say Indian. Few votes never
    matter here: a new regional film has none."""
    ry = _row_year(row, media)
    if not year or not ry or abs(ry - year) > 1:
        return None
    names = _row_names(row, media) + [n for n in extra_names if isinstance(n, str) and n]
    score = max((_key_score(t, n, translit) for t in titles for n in names), default=0.0)
    if score >= 1.0:
        ok = indian_name or _row_indian(row) or (pool and ry == year)
    else:
        ok = score >= MOVIE_FUZZY_MIN and indian_name and _row_indian(row)
    return (score, 1 if ry == year else 0, row.get("vote_count") or 0) if ok else None


async def _tmdb_json(session, tmdb_key: str, path: str, params: dict):
    """Parsed JSON of one TMDB GET (discover / find) or _TMDB_ERROR: the retry rules of _tmdb_search()."""
    if not tmdb_key:
        return _TMDB_ERROR
    query = dict(params, api_key=tmdb_key)
    for attempt in range(TMDB_RETRIES):
        try:
            async with session.get(f"{TMDB_BASE}{path}", params=query,
                                   timeout=aiohttp.ClientTimeout(total=TMDB_TIMEOUT_SECONDS)) as resp:
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
                data = await resp.json()
                return data if isinstance(data, dict) else _TMDB_ERROR
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("TMDB %s failed: %s", path, e)
            await asyncio.sleep(1 + attempt)
    return _TMDB_ERROR


def _pool_langs(p: dict) -> list:
    """Pool languages, the file's own first (the language words of the name), then the rest."""
    own = []
    for w in [*((p.get("movie") or {}).get("languages") or []), p.get("language")]:
        c = _lang_code_of(w)
        if c in _POOL_LANGS and c not in own:
            own.append(c)
    return own + [c for c in _POOL_LANGS if c not in own]


async def _pool_page(session, tmdb_key: str, media: str, year: int, lang: str, page: int):
    """(rows, total_pages) of one /discover page of the year + language pool, or _TMDB_ERROR. Cached."""
    ck = (media, year, lang, page)
    hit = _POOL_CACHE.get(ck)
    if hit is not None:
        _POOL_CACHE.move_to_end(ck)
        return hit
    params = {"sort_by": "popularity.desc", "include_adult": "false", "with_original_language": lang, "page": page}
    if media == "movie":
        params["primary_release_year"] = year
    else:
        params["first_air_date_year"] = year
        params["with_origin_country"] = "IN"
    data = await _tmdb_json(session, tmdb_key, f"/discover/{media}", params)
    if data is _TMDB_ERROR:
        return _TMDB_ERROR
    try:
        val = ([r for r in (data.get("results") or []) if isinstance(r, dict)], max(1, int(data.get("total_pages") or 1)))
    except (TypeError, ValueError):
        return _TMDB_ERROR
    _POOL_CACHE[ck] = val
    if len(_POOL_CACHE) > _CACHE_MAX:
        _POOL_CACHE.popitem(last=False)
    return val


async def _tmdb_pool_pick(session, tmdb_key: str, p: dict, media: str, titles, year, indian_name: bool, translit: bool):
    """-> (best TMDB row | None, errored): the file's title looked up in the year + language pool (see the block comment
    above), page by page, the file's own language first, stopping at the first confident hit. A near miss (title
    similarity >= 0.6) is checked against its cached alternative titles (at most 3 per search)."""
    if MOVIE_POOL_PAGES <= 0 or not year:
        return None, False
    errored, alt_budget = False, 3
    for lang in _pool_langs(p):
        best, best_rank = None, None
        for page in range(1, MOVIE_POOL_PAGES + 1):
            got = await _pool_page(session, tmdb_key, media, year, lang, page)
            if got is _TMDB_ERROR:
                errored = True
                break
            rows, total = got
            for r in rows:
                rank = _ext_rank(r, media, titles, year, indian_name, translit, pool=True)
                if rank is None and alt_budget > 0 and r.get("id") and _row_year(r, media) == year:
                    near = max((_key_score(t, n, translit) for t in titles for n in _row_names(r, media)), default=0.0)
                    if near >= 0.6:
                        alt_budget -= 1
                        alts = await _tmdb_alt_titles(session, media, r["id"], tmdb_key)
                        if alts is not _TMDB_ERROR:
                            rank = _ext_rank(r, media, titles, year, indian_name, translit, pool=True, extra_names=alts)
                if rank is not None and (best is None or rank > best_rank):
                    best, best_rank = r, rank
            if best is not None or page >= total:
                break
        if best is not None:
            return best, errored
    return None, errored


async def _ext_get(session, service: str, url: str, params=None, headers=None):
    """Parsed JSON, None (HTTP 404) or _TMDB_ERROR for IMDb / OMDb. Never raises. One request at a time, spaced apart,
    8 s timeout, one retry on 429 / 5xx; after a failure the service is skipped for _EXT_DOWN_SECONDS."""
    if aiohttp is None:
        return _TMDB_ERROR
    st = _EXT_STATE.setdefault(service, {"lock": None, "last": 0.0, "down": 0.0})
    if st["lock"] is None:
        st["lock"] = asyncio.Lock()
    loop = asyncio.get_event_loop()
    if loop.time() < st["down"]:
        return _TMDB_ERROR
    for attempt in range(2):
        async with st["lock"]:
            wait = _EXT_INTERVAL.get(service, 0.5) - (loop.time() - st["last"])
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with session.get(url, params=params, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=_EXT_TIMEOUT_SECONDS)) as resp:
                    st["last"] = loop.time()
                    if resp.status == 404:
                        return None
                    if resp.status == 429 or resp.status >= 500:
                        await asyncio.sleep(1 + attempt)
                        continue
                    if resp.status != 200:
                        break
                    return await resp.json(content_type=None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("%s request failed: %s", service, e)
                await asyncio.sleep(1 + attempt)
    st["down"] = loop.time() + _EXT_DOWN_SECONDS
    return _TMDB_ERROR


async def _imdb_suggest(session, query: str):
    """IMDb suggestion rows ([] = nothing) or _TMDB_ERROR. Cached per query. (IMDb's public autocomplete endpoint;
    off with USE_IMDB_SUGGEST=0.) The parser is defensive: any other shape counts as "no rows"."""
    q = re.sub(r"\s+", " ", (query or "").strip().lower())
    first = q[:1] if q[:1].isascii() and q[:1].isalnum() else ""
    if not USE_IMDB_SUGGEST or not first:
        return []
    rows = _qc_get("imdb", q)
    if rows is not None:
        return rows
    data = await _ext_get(session, "imdb", f"https://v3.sg.media-imdb.com/suggestion/{first}/{quote(q)}.json")
    if data is _TMDB_ERROR:
        return _TMDB_ERROR
    d = data.get("d") if isinstance(data, dict) else None
    rows = [r for r in (d if isinstance(d, list) else []) if isinstance(r, dict) and str(r.get("id") or "").startswith("tt")]
    _qc_put("imdb", q, rows)
    return rows


def _imdb_kind(row: dict):
    """"movie" | "tv" | None for one suggestion row: features and TV films are movies; TV series / mini-series are shows."""
    qid, label = str(row.get("qid") or ""), str(row.get("q") or "").lower()
    if qid in ("movie", "tvMovie") or label in ("feature", "tv movie"):
        return "movie"
    if qid in ("tvSeries", "tvMiniSeries") or label in ("tv series", "tv mini-series", "tv mini series"):
        return "tv"
    return None


async def _omdb_rows(session, title: str, kind: str):
    """OMDb search rows in the IMDb row shape ({"id", "l", "y", "qid"}); [] without OMDB_API_KEY; _TMDB_ERROR on failure."""
    if not OMDB_API_KEY:
        return []
    key = f"{title} {kind}"
    rows = _qc_get("omdb", key)
    if rows is not None:
        return rows
    data = await _ext_get(session, "omdb", "https://www.omdbapi.com/",
                          params={"apikey": OMDB_API_KEY, "s": title, "type": "movie" if kind == "movie" else "series"})
    if data is _TMDB_ERROR:
        return _TMDB_ERROR
    rows = []
    for r in ((data or {}).get("Search") or []) if isinstance(data, dict) else []:
        try:
            rows.append({"id": str(r["imdbID"]), "l": str(r["Title"]), "y": int(str(r["Year"])[:4]),
                         "qid": "movie" if kind == "movie" else "tvSeries"})
        except (KeyError, TypeError, ValueError):
            continue
    _qc_put("omdb", key, rows)
    return rows


async def _tmdb_by_imdb(session, tmdb_key: str, imdb_id: str, kind: str):
    """The TMDB row of an IMDb id (GET /find): a dict, None (TMDB does not list it) or _TMDB_ERROR. Cached."""
    hit = _qc_get("find", imdb_id)
    if hit is None:
        data = await _tmdb_json(session, tmdb_key, f"/find/{imdb_id}", {"external_source": "imdb_id"})
        if data is _TMDB_ERROR:
            return _TMDB_ERROR
        rows = data.get("movie_results" if kind == "movie" else "tv_results") or []
        hit = rows[0] if rows and isinstance(rows[0], dict) else {}
        _qc_put("find", imdb_id, hit)
    return hit or None


async def _imdb_lookup(session, tmdb_key: str, titles, year, kind: str, translit: bool):
    """-> (entry | None, errored). entry = {"source": "imdb" | "omdb", "imdb_id", "title", "year", "tmdb": TMDB row | None}.
    IMDb's suggestions (then OMDb, only with OMDB_API_KEY) must list the SAME title (exact key) for the file's year (+-1)
    and type; the tt id is resolved with TMDB /find, so the film / show gets its TMDB id when TMDB has it."""
    if not year:
        return None, False
    errored = False
    for service, fetch in (("imdb", _imdb_suggest), ("omdb", None)):
        for t in titles[:2]:
            rows = await (fetch(session, t) if fetch else _omdb_rows(session, t, kind))
            if rows is _TMDB_ERROR:
                errored = True
                continue
            best = None
            for r in rows:
                y = r.get("y")
                if _imdb_kind(r) != kind or not isinstance(y, int) or abs(y - year) > 1:
                    continue
                if max((_key_score(q, str(r.get("l") or ""), translit) for q in titles), default=0.0) < 1.0:
                    continue                                   # the other source must name the SAME title
                if best is None or (y == year and best.get("y") != year):
                    best = r
            if best is not None:
                row = await _tmdb_by_imdb(session, tmdb_key, best["id"], kind)
                return {"source": service, "imdb_id": best["id"], "title": best.get("l"), "year": best.get("y"),
                        "tmdb": None if row is _TMDB_ERROR else row}, errored
    return None, errored


async def _tmdb_movie_variants(session, tmdb_key: str, p: dict):
    """-> (result | None, errored): the TMDB movie found by the query variants of the file's title (_title_variants():
    joined / split / transliterated / without a leading number / first words), each searched WITH the year, then one
    /search/multi - at most MOVIE_MAX_SEARCHES requests per distinct title, stopping at the first confident hit. A result
    counts by _ext_rank(): the year (+-1) AND the title key agree, with the Indian-language conditions described there."""
    mv, year = p["movie"], p.get("year")
    if not year or MOVIE_MAX_SEARCHES <= 0:
        return None, False
    translit = bool(mv["indian"] or _TRANSLIT_HINT_RE.search(mv["title"]))
    mine = list(dict.fromkeys([mv["title"], *mv["titles"]]))
    plan = [("movie", v) for v in mv["variants"][:max(0, MOVIE_MAX_SEARCHES - 1)]] + [("multi", mv["title"])]
    best, best_rank, errored = None, None, False
    for media, query in plan[:MOVIE_MAX_SEARCHES]:
        results = await _tmdb_search(session, media, query, tmdb_key, year if media == "movie" else None)
        if results is _TMDB_ERROR:
            errored = True
            continue
        for r in (results or [])[:10]:
            if media == "multi" and r.get("media_type") != "movie":
                continue                                       # people and shows are not this file
            rank = _ext_rank(r, "movie", mine, year, mv["indian"], translit)
            if rank is not None and (best is None or rank > best_rank):
                best, best_rank = r, rank
        if best is not None:
            break
    return best, errored


def _wd_label_says_film(label: str) -> bool:
    low = (label or "").strip().lower()
    if not low or any(w in low for w in _WD_FILM_NOT_WORDS):
        return False
    return low == "film" or low.endswith(" film")          # "film", "feature film", "comedy film", "animated film" ...


async def _wd_class_is_film(session, qid: str, depth: int = 0):
    """True / False, or None when Wikidata could not be asked (nothing is cached then). Q11424 ("film") is found
    by its English label like every other class; "subclass of" parents are followed up to two steps."""
    hit = _WD_FILM_CACHE.get(qid)
    if hit is not None:
        _WD_FILM_CACHE.move_to_end(qid)
        return hit
    data = await _wikidata_get(session, {"action": "wbgetentities", "ids": qid, "props": "labels|claims",
                                         "languages": "en"})
    if data is _TMDB_ERROR:
        return None
    ent = (data.get("entities") or {}).get(qid) or {}
    label = ((ent.get("labels") or {}).get("en") or {}).get("value") or ""
    result = _wd_label_says_film(label)
    if not result and depth < 2:
        unknown = False
        for parent in _wd_claim_ids(ent, "P279")[:5]:
            sub = await _wd_class_is_film(session, parent, depth + 1)
            if sub:
                result = True
                break
            if sub is None:
                unknown = True
        if not result and unknown:
            return None
    _WD_FILM_CACHE[qid] = bool(result)
    if len(_WD_FILM_CACHE) > _CACHE_MAX:
        _WD_FILM_CACHE.popitem(last=False)
    return bool(result)


def _wd_years(entity: dict, prop: str = "P577") -> list:
    """Years of a Wikidata date property (publication date), deprecated statements ignored."""
    out = []
    for st in ((entity.get("claims") or {}).get(prop) or []):
        try:
            if st.get("rank") == "deprecated":
                continue
            y = int(str(st["mainsnak"]["datavalue"]["value"]["time"]).lstrip("+-")[:4])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if y not in out:
            out.append(y)
    return out


async def _wikidata_film_lookup(session, title: str, year: int):
    """Cache entry for one title (found / none), or None when Wikidata failed."""
    hits = _qc_get("wikidata", title)
    if hits is None:
        data = await _wikidata_get(session, {"action": "wbsearchentities", "search": title, "language": "en",
                                             "uselang": "en", "type": "item", "limit": "7"})
        if data is _TMDB_ERROR:
            return None
        hits = [h for h in (data.get("search") or []) if isinstance(h, dict) and h.get("id")]
        _qc_put("wikidata", title, hits)
    q = _norm_cmp(title)
    scored = []
    for h in hits:
        match = h.get("match") or {}
        labels = [h.get("label")] + list(h.get("aliases") or []) + ([match.get("text")] if match.get("type") == "alias" else [])
        if any(isinstance(n, str) and n and _title_score_movie(q, _norm_cmp(n)) >= 1.0 for n in labels):
            scored.append(h)                                  # strict: the label / an alias IS the title
    scored = scored[:3]
    none = {"source": "wikidata", "id": None, "verdict": "none", "facts": None}
    if not scored:
        return none
    ents = await _wikidata_get(session, {"action": "wbgetentities", "ids": "|".join(h["id"] for h in scored),
                                         "props": "claims"})
    if ents is _TMDB_ERROR:
        return None
    unknown = False
    for h in scored:
        entity = (ents.get("entities") or {}).get(h["id"]) or {}
        if not any(abs(y - year) <= 1 for y in _wd_years(entity)):
            continue                                          # the year must agree (an item with no year never matches)
        for cls in _wd_claim_ids(entity, "P31")[:8]:
            verdict = await _wd_class_is_film(session, cls)
            if verdict:
                return {"source": "wikidata", "id": h["id"], "verdict": "found", "facts": None}
            if verdict is None:
                unknown = True
    return None if unknown else none


async def _wikidata_film_for(session, p: dict):
    """The Wikidata film item (its Q-id) for a movie-like name, or None. The verdict is cached per film
    (cleaned title + year), "not found" included, so the parts of one film cost one lookup."""
    year = p.get("year")
    if not year:
        return None
    entry = _show_get(p, "wikidata_film")
    if entry is None:
        errored = False
        for title in (p.get("titles") or p["movie"]["titles"])[:3]:
            e = await _wikidata_film_lookup(session, title, year)
            if e is None:
                errored = True
                continue
            if e["verdict"] == "found":
                entry = e
                break
        if entry is None and not errored:
            entry = {"source": "wikidata", "id": None, "verdict": "none", "facts": None}
        if entry is not None:
            _show_put(p, "wikidata_film", entry)
    return entry["id"] if entry and entry["verdict"] == "found" else None


async def _movie_fallbacks(session, tmdb_key: str, p: dict, info: dict, anime_mode: bool):
    """-> ("found", kind) | ("none", None) | ("error", None) for a movie-like name TMDB's own title search did not know.
    Order: TMDB query variants -> TMDB year + language pool -> IMDb (OMDb) -> Wikidata (see the block comment above).
    TMDB failing while nothing else vouches is "error" (no guessing during an outage); IMDb / OMDb / Wikidata failing is
    only "no evidence". The verdict is cached per film. Never raises."""
    try:
        mv, year = p["movie"], p.get("year")
        cached = _show_get(p, "movie_fb")
        if cached is not None:                                # another part of the same film: replay the verdict
            if cached["status"] != "found":
                return "none", None
            if cached.get("tmdb_id"):
                info["tmdb_id"], info["tmdb_media"] = cached["tmdb_id"], "movie"
            _movie_note(info, **cached["note"])
            return "found", cached["kind"]
        translit = bool(mv["indian"] or _TRANSLIT_HINT_RE.search(mv["title"]))
        mine = list(dict.fromkeys([mv["title"], *mv["titles"]]))
        tmdb_err = ext_err = False

        def _store(kind: str, tmdb_id, note: dict):
            if tmdb_id:
                info["tmdb_id"], info["tmdb_media"] = tmdb_id, "movie"
            _movie_note(info, **note)
            _show_put(p, "movie_fb", {"status": "found", "kind": kind, "tmdb_id": tmdb_id, "note": note})
            return "found", kind

        async def _tmdb_found(match: dict, source: str):
            mid = match.get("id")
            animated = TMDB_ANIMATION_GENRE_ID in (match.get("genre_ids") or [])
            kind = "movie"
            if _is_anime_result(match, "movie"):
                kind = "anime"
            elif anime_mode:
                ok, _src, _why, _lst = await _tmdb_anime_confirm(session, match, "movie", mid, animated)
                kind = "anime" if ok else "movie"
            return _store(kind, mid, {"source": source, "matched": match.get("title"), "tmdb_id": mid,
                                      "matched_year": _year_of(match.get("release_date"))})

        if year:
            match, err = await _tmdb_movie_variants(session, tmdb_key, p)
            tmdb_err = tmdb_err or err
            if match:
                return await _tmdb_found(match, "tmdb-variant")
            if translit:                                      # the year + language pool is for Indian-language names
                match, err = await _tmdb_pool_pick(session, tmdb_key, p, "movie", mine, year, mv["indian"], translit)
                tmdb_err = tmdb_err or err
                if match:
                    return await _tmdb_found(match, "tmdb-pool")
            ent, err = await _imdb_lookup(session, tmdb_key, mine + mv["variants"][:1], year, "movie", translit)
            ext_err = ext_err or err
            if ent:
                if ent["tmdb"]:
                    return await _tmdb_found(ent["tmdb"], ent["source"])
                return _store("movie", None, {"source": ent["source"], "imdb_id": ent["imdb_id"], "matched": ent["title"]})
            if USE_WIKIDATA and USE_WIKIDATA_FILMS:
                qid = await _wikidata_film_for(session, p)
                if qid:
                    return _store("movie", None, {"source": "wikidata", "wikidata_id": qid})
                ext_err = ext_err or _show_get(p, "wikidata_film") is None      # no cached verdict = Wikidata failed
        if tmdb_err:
            return "error", None                              # don't guess during an outage
        if not ext_err:
            _show_put(p, "movie_fb", {"status": "none"})      # a clean "nobody knows it"; a failure is never cached
        return "none", None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("deep movie fallbacks failed for %r: %s", p.get("clean_title"), e)
        return "none", None


async def _tv_fallbacks(session, tmdb_key: str, p: dict, info: dict) -> dict:
    """The deep show search found nothing for a show-like name WITH a year: TMDB query variants searched with the year
    (+ one /search/multi), then the year + language pool of Indian shows (/discover/tv, first_air_date_year, origin IN).
    -> {"status": "found" | "none" | "error", "kind": "series" | None, "entry": None, "why": str}; a found show sets
    info["tmdb_id"] / info["tmdb_media"]. Anime is never decided here (the anime authorities do that). Never raises."""
    none = {"status": "none", "kind": None, "entry": None}
    try:
        year = p.get("year")
        if not year or MOVIE_MAX_SEARCHES <= 0:
            return none
        cached = _show_get(p, "tv_fb")
        if cached is not None:
            if not cached.get("tmdb_id"):
                return none
            info["tmdb_id"], info["tmdb_media"] = cached["tmdb_id"], "tv"
            return {"status": "found", "kind": "series", "entry": None, "why": cached["why"]}
        titles = [c["title"] for c in _cands_of(p) if not c.get("dropped")][:SERIES_MAX_SEARCHES] or list(p["titles"])[:1]
        indian = p.get("language") in _INDIAN_LANGS
        translit = bool(indian or _TRANSLIT_HINT_RE.search(titles[0]))
        plan = [("tv", v) for v in _title_variants(titles[0], translit)[:max(0, MOVIE_MAX_SEARCHES - 1)]]
        plan.append(("multi", titles[0]))
        best, best_rank, source, errored = None, None, "", False
        for media, query in plan[:MOVIE_MAX_SEARCHES]:
            results = await _tmdb_search(session, media, query, tmdb_key, year if media == "tv" else None)
            if results is _TMDB_ERROR:
                errored = True
                continue
            for r in (results or [])[:10]:
                if (media == "multi" and r.get("media_type") != "tv") or _is_anime_result(r, "tv"):
                    continue
                rank = _ext_rank(r, "tv", titles, year, indian, translit)
                if rank is not None and (best is None or rank > best_rank):
                    best, best_rank, source = r, rank, "tmdb-variant"
            if best is not None:
                break
        if best is None and translit:
            best, err = await _tmdb_pool_pick(session, tmdb_key, p, "tv", titles, year, indian, translit)
            errored, source = errored or err, "tmdb-pool"
        if best is None:
            if errored:
                return {"status": "error", "kind": None, "entry": None}
            _show_put(p, "tv_fb", {"tmdb_id": None})
            return none
        why = f"{source} match {best.get('name')!r}"
        info["tmdb_id"], info["tmdb_media"] = best.get("id"), "tv"
        _show_put(p, "tv_fb", {"tmdb_id": best.get("id"), "why": why})
        return {"status": "found", "kind": "series", "entry": None, "why": why}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("deep show fallbacks failed for %r: %s", p.get("clean_title"), e)
        return none


async def _imdb_show_facts(session, tmdb_key: str, p: dict):
    """Show facts (the _empty_facts shape, source "imdb" / "omdb") when IMDb / OMDb list a TV series with the SAME title
    and a start year within +-1 of the file's year; None otherwise (no year, nothing found, a failure). Cached per show."""
    try:
        year = p.get("year")
        if not year:
            return None
        entry = _show_get(p, "imdb")
        if entry is None:
            titles = [c["title"] for c in _cands_of(p) if not c.get("dropped")][:2] or list(p["titles"])[:1]
            translit = bool(p.get("language") in _INDIAN_LANGS or _TRANSLIT_HINT_RE.search(titles[0]))
            ent, err = await _imdb_lookup(session, tmdb_key, titles, year, "tv", translit)
            if ent:
                facts = _empty_facts(ent["source"], ent["imdb_id"])
                facts["names"], facts["first_year"] = [ent["title"]], ent["year"]
                entry = {"verdict": "found", "facts": facts}
            elif not err:
                entry = {"verdict": "none", "facts": None}
            if entry is not None:
                _show_put(p, "imdb", entry)
        return entry["facts"] if entry and entry["verdict"] == "found" else None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("IMDb show lookup failed for %r: %s", p.get("clean_title"), e)
        return None


# ---- the confidence score ---------------------------------------------------------
def _facts_exact(p: dict, facts) -> bool:
    """A name of the file equals a name of the show (after normalising): the cleaned title
    or any candidate that has no word dropped."""
    if not facts:
        return False
    queries = {_norm_cmp(c["title"]) for c in _cands_of(p) if not c.get("dropped")}
    if p.get("clean_title"):
        queries.add(_norm_cmp(p["clean_title"]))
    return any(_norm_cmp(n) in queries for n in facts["names"])


def _facts_agree(a: dict, b: dict) -> bool:
    """TMDB and TVmaze describe the same show: same first year (+-1). Two different
    shows with one name (a UK and a US remake) must not vouch for each other."""
    ya, yb = a.get("first_year"), b.get("first_year")
    return not (ya and yb and abs(ya - yb) > 1)


def _year_ok(p: dict, f: dict) -> bool:
    """The year in the name (a date-named episode: its air year) is within +-1 of the
    show's first year or of any season's year; an air date may also fall anywhere in
    the years the show has aired."""
    y = p.get("air_year") if p.get("date_episode") else p.get("year")
    if not y:
        return False
    fy, ly = f.get("first_year"), f.get("last_year")
    if fy and abs(y - fy) <= 1:
        return True
    if any(abs(y - sy) <= 1 for sy in f.get("season_years") or ()):
        return True
    return bool(p.get("date_episode") and fy and ly and fy - 1 <= y <= ly + 1)


def _numbers_ok(p: dict, f: dict) -> bool:
    """The season / episode number exists in the show's data (season listed, and the
    episode within that season's count; or, with no season, within the total)."""
    season, episode = p.get("season"), p.get("episode")
    seasons = f.get("seasons") or {}
    if season and seasons:
        if season not in seasons:
            return False
        count = seasons[season]
        return episode <= count if (episode and count) else True
    if episode and not season and f.get("total_eps"):
        return episode <= f["total_eps"]
    return False


# STRONG course signals: a course / lecture / tutorial / bootcamp word, a course platform, or "class 5" / "lesson 12" /
# "module 3" / "chapter 2" with a number. _EDU_RE alone (a single word such as "Lessons" or "Classroom") is only SOFT.
_EDU_COURSE_RE = re.compile(
    r"(?<![A-Za-z])(?:lectures?|lec|tutorials?|courses?|bootcamp|masterclass|seminar|webinar|syllabus|udemy|coursera|"
    r"edx|skillshare|pluralsight|simplilearn|nptel|udacity|linkedin[\s._-]?learning|"
    r"(?:class|classes|lesson|lessons|module|modules|chapter|chapters)[\s._-]*(?:no\.?[\s._-]*)?\d{1,3})(?![A-Za-z])",
    re.IGNORECASE,
)


# Indian exam-prep / course videos (EDU_EXAM_RULES): SOFT signals of five independent classes. A file needs TWO different
# classes to be a soft education candidate - a single word ("Police", "Sir", "Science") never is one:
#   course   revision / marathon / PYQ / mock test / demo / one shot / crash course / full course / complete syllabus
#            (and every STRONG course word of _EDU_COURSE_RE: lecture, tutorial, class 5, chapter 2 ...)
#   exam     board / exam words (NCERT, CBSE, ICSE, SSC, CET, UPSC, NEET, JEE, GATE, railway, group d, police, banking, TET ...)
#   subject  physics, chemistry, biology, maths, science, polity, history, geography, reasoning, english grammar ...
#   teacher  "by X Sir", "Mam", "Sir"
#   list     a YouTube list "A | B | C" with a hashtag, or "Concept | Theory | Numericals"
# A name with a bracketed year, a release tag (WEB-DL, BluRay, HDRip, x264 ...) or an episode marker is a release, not a
# course. The result is only SOFT: any source that knows a movie / series by the cleaned title (TMDB, TVmaze, IMDb,
# Wikidata, AniList) wins over it (see _deep_series()).
_EDU_RELEASE_GUARD_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:web[\s._-]?(?:dl|rip)|blu[\s._-]?ray|br[\s._-]?rip|bd[\s._-]?rip|hd[\s._-]?rip|hdtv|dvd[\s._-]?rip|"
    r"x26[45]|h[\s.]?26[45]|hevc|10bit|dd[p+]?[\s._]?\d)(?![A-Za-z0-9])", re.IGNORECASE)
_EDU_SOFT_COURSE_RE = re.compile(
    r"(?<![a-z])(?:revision|revise|marathon|pyqs?|previous year|mock test|test series|demo class|demo|one shot|oneshot|"
    r"crash course|full course|full syllabus|complete syllabus|doubt session|doubt class)(?![a-z])")
_EDU_SUBJECT_RE = re.compile(
    r"(?<![a-z])(?:physics|chemistry|biology|maths?|mathematics|science|polity|history|geography|reasoning|economics|"
    r"english grammar|current affairs)(?![a-z])")
_EDU_TEACHER_RE = re.compile(r"(?<![a-z])(?:sir|mam|maam|madam|ma am)(?![a-z])")
_EDU_GROUP_D_RE = re.compile(r"(?<![a-z])group[\s]*d(?![a-z])")
_EDU_LIST_WORDS = {"concept", "concepts", "theory", "numericals", "numerical", "questions", "pyq", "pyqs", "tricks",
                   "shortcuts", "formulas", "formula", "notes", "revision", "practice", "mcq", "mcqs"}


@_new_rule(lambda *a, **k: set())
def _edu_signals(names, show_names=()) -> set:
    """The independent classes of SOFT education signals found in the names (see the block comment above). Empty when
    EDU_EXAM_RULES is off. Words of the matched show's own title never count. Never raises."""
    found = set()
    if not EDU_EXAM_RULES:
        return found
    try:
        shows = [_norm_cmp(s) for s in show_names if s and s.strip()]
        for raw in names:
            text = raw or ""
            if _MOVIE_BRACKET_YEAR_RE.search(text) or _EDU_RELEASE_GUARD_RE.search(text) or _DEEP_EP_RE.search(text):
                continue
            rest = _norm_cmp(re.sub(r"[._]", " ", _VIDEO_EXT_TAIL_RE.sub("", text)))
            for s in shows:
                if s:
                    rest = rest.replace(s, " ")
            if _EDU_SOFT_COURSE_RE.search(rest) or _EDU_COURSE_RE.search(rest):
                found.add("course")
            if set(rest.split()) & _EDU_EXAM_WORDS or _EDU_GROUP_D_RE.search(rest):
                found.add("exam")
            if _EDU_SUBJECT_RE.search(rest):
                found.add("subject")
            if _EDU_TEACHER_RE.search(rest):
                found.add("teacher")
            segs = [s.strip() for s in re.split(r"\s*[|｜]\s*", text) if s.strip()]
            if (len(segs) >= 3 and "#" in text) or sum(1 for s in segs if _only_words(s, _EDU_LIST_WORDS)) >= 2:
                found.add("list")
    except Exception as e:
        logger.debug("education signals failed on %r: %s", names, e)
    return found


def _edu_level(names, show_names=()) -> int:
    """0 = no course naming, 1 = SOFT (only generic words: "Lessons", "Classroom", "Chapter" - or two independent
    exam-prep signals, see _edu_signals()), 2 = STRONG (a numbered lecture "5.1.3 Intro to Python", a course / lecture /
    tutorial / bootcamp / platform word, "class 5").
    Words that belong to the matched SHOW's own title ("Some Lessons Aren't Taught In Classrooms") never count; the
    comparison ignores case, accents and punctuation. A year in brackets - "(2025)" - marks a release name, not a
    course, so it switches every word signal off (a numbered lecture name stays strong)."""
    shows = [_norm_cmp(s) for s in show_names if s and s.strip()]
    level = 0
    for i, raw in enumerate(names):
        text = raw or ""
        if i == 0 and _EDU_NUMBERED_RE.match(text):
            return 2
        if _MOVIE_BRACKET_YEAR_RE.search(text):
            continue
        rest = _norm_cmp(text.replace(".", " ").replace("_", " "))
        for s in shows:
            if s:
                rest = rest.replace(s, " ")
        if _EDU_COURSE_RE.search(rest):
            return 2
        if _EDU_RE.search(rest):
            level = 1
    if level < 2 and EDU_EXAM_RULES:
        sig = _edu_signals(names, show_names)
        if len(sig) + (1 if level == 1 else 0) >= 2:
            level = 1                                             # soft: a source that knows the title still wins
    return level


def _edu_hit(names, show_names=()) -> bool:
    """Any course naming at all (soft or strong): see _edu_level()."""
    return _edu_level(names, show_names) > 0


def _extra_hit(parsed, names) -> bool:
    """sample / trailer / promo / recap in ANY name of the file (the first name is
    already checked by _parse_name; caption lines were not)."""
    if any(pp.get("extra") for pp in parsed):
        return True
    for raw in names:
        stem = _VIDEO_EXT_TAIL_RE.sub("", _clean_name(raw or ""))
        if _deep_extra(stem):
            return True
    return False


async def _tmdb_movie_title_exists(session, tmdb_key: str, p: dict) -> bool:
    """TMDB has a MOVIE with exactly this title (any year). Searches are cached, and
    the movie search of this very name has normally just been made."""
    queries = {_norm_cmp(t) for t in p["titles"]}
    for title in p["titles"]:
        results = await _tmdb_search(session, "movie", title, tmdb_key)
        if results is _TMDB_ERROR:
            continue
        for r in (results or [])[:10]:
            if any(n and _norm_cmp(n) in queries for n in (r.get("title"), r.get("original_title"))):
                return True
    return False


def _season_beyond(p: dict, facts) -> bool:
    """The file's season number is above every season TMDB lists for the show."""
    season, seasons = p.get("season"), (facts or {}).get("seasons") or {}
    return bool(season and seasons and season > max(seasons))


def _score_series(p: dict, tmdb=None, tvmaze=None, openai: bool = False, exact: bool = False,
                  minutes=None, edu: bool = False, nsfw: bool = False, extra: bool = False,
                  movie_like: bool = False, wikidata=None, lang_ok: bool = False, ep_title: bool = False,
                  ambiguous: bool = False):
    """How sure are we that this file is a real series episode?
    -> (ok, score, signals, reason). tmdb / tvmaze / wikidata: show facts (None = no match).

    Weights are the _W_* constants. Hard rules on top of the score:
      5. a generic title (Episode / Video / Part / Movie / one letter or number) never counts
      4. an edu / NSFW / sample-promo-recap hit is never a series
      3. no explicit episode marker (or air date) needs TMDB AND TVmaze to agree: a
         plain title match, or a single source, is not enough (a TV-source tag - PDTV / HDTV /
         DSR / TVRip / DVBS - is a positive series signal, so one source is enough then)
      1/2. marker + one source, or a marker alone (SERIES_EPISODE_FALLBACK), pass
    An AMBIGUOUS franchise match (language unknown, several versions) still confirms the
    franchise but is not used for the year / season / language / episode-title signals."""
    sig, score = [], 0
    if tmdb and tvmaze and not _facts_agree(tmdb, tvmaze):
        sig.append("tvmaze-mismatch")
        tvmaze = None
    marker = bool(p.get("strong_episode"))
    if marker:
        score += _W_MARKER
        sig.append("date" if p.get("date_episode") else "marker")
    elif p.get("is_episode") or p.get("series_like"):
        score += _W_WEAK_EPISODE
        sig.append("guess-ep")
    tv_source = bool(p.get("tv_source"))
    if tv_source:
        score += _W_TV_SOURCE
        sig.append("tv-source")
    if tmdb:
        score += _W_TMDB
        sig.append("tmdb")
        if exact:
            score += _W_TMDB_EXACT
            sig.append("exact")
    if tvmaze:
        score += _W_TVMAZE
        sig.append("tvmaze")
        if tmdb:
            score += _W_AGREE
            sig.append("agree")
    if wikidata:
        score += _W_WIKIDATA
        sig.append(wikidata.get("source") or "wikidata")      # "imdb" facts travel in this slot too
    if openai:
        score += _W_OPENAI
        sig.append("openai")
    facts = [f for f in (tmdb, tvmaze) if f]
    if not ambiguous:
        if any(_year_ok(p, f) for f in facts):
            score += _W_YEAR
            sig.append("year")
        if any(_numbers_ok(p, f) for f in facts):
            score += _W_NUMBERS
            sig.append("numbers")
        elif tmdb and _season_beyond(p, tmdb):
            score += _W_SEASON_BEYOND
            sig.append("season-beyond")
        if lang_ok and p.get("language"):
            score += _W_LANG
            sig.append("lang:" + p["language"])
        if ep_title:
            score += _W_EP_TITLE
            sig.append("ep-title")
    else:
        sig.append("ambiguous")
    if p.get("platforms"):
        score += _W_PLATFORM
        sig.append("plat:" + p["platforms"][0])
    if minutes is not None and SERIES_MIN_MINUTES <= minutes <= SERIES_MAX_MINUTES:
        score += _W_DURATION
        sig.append("dur")
    for hit, weight, label in ((edu, _W_EDU, "edu"), (nsfw, _W_NSFW, "nsfw"),
                               (extra, _W_EXTRA, "extra"), (movie_like, _W_MOVIE_LIKE, "movie-like")):
        if hit:
            score += weight
            sig.append("-" + label)
    if p.get("junk_removed") and not (tmdb or tvmaze or wikidata or openai):
        score += _W_JUNK_UNCONFIRMED
        sig.append("-junk")

    title = p.get("clean_title") or ((p.get("titles") or [""])[0])
    if edu or nsfw or extra:
        return False, score, sig, ("course / lecture name" if edu else
                                   "adult content" if nsfw else "sample / promo / recap")
    if _generic_title(title):
        sig.append("-generic")
        return False, score, sig, "generic title"
    if not marker and not (tmdb and tvmaze) and not (tv_source and (tmdb or tvmaze or wikidata)):
        return False, score, sig, "no episode marker and no second source agreeing"
    if marker and not (tmdb or tvmaze or wikidata or openai) and not SERIES_EPISODE_FALLBACK:
        return False, score, sig, "episode-marker fallback is switched off"
    if score < SERIES_MIN_SCORE:
        return False, score, sig, f"score {score} below SERIES_MIN_SCORE {SERIES_MIN_SCORE:g}"
    return True, score, sig, "ok"


def _series_other_reason(p: dict, reason: str, vouched: bool):
    """(reason, detail) for info["other_reason"] when the deep series scoring ends in Other.
    vouched = TMDB / TVmaze / Wikidata / OpenAI knew the title."""
    low = (reason or "").lower()
    if low.startswith("sample"):
        return "extra", reason
    title = p.get("clean_title") or ((p.get("titles") or [""])[0])
    if not vouched and p.get("movie"):
        shown = ", ".join(repr(t) for t in (p["movie"].get("titles") or [title])[:3])
        year = f" ({p['year']})" if p.get("year") else ""
        return "not_found", f"no TMDB / Wikidata film match for {shown}{year}"
    if low.startswith("course") and vouched:
        return "education_vetoed", f"course words in the name, but a source knows {title!r}: {reason}"
    if p.get("season_pack") and not vouched:
        return "season_pack", f"season pack of {title!r}: {reason}"
    if low.startswith(("course", "adult", "generic")):
        return "unidentified", reason
    if not vouched:
        return "not_found", f"no source knows {title!r}: {reason}"
    return "unidentified", reason


# ---- /autosort entry point: classify_async(..., deep_series=True) ------------------
async def _deep_series(message, names, parsed, is_files: bool, session, tmdb_key: str, use_anilist: bool,
                       anilist_key: str, openai_key: str, info: dict, anime_mode: bool = False) -> str:
    """The deep series pipeline behind classify_async(deep_series=True). Returns the
    usual category strings; "series" is only returned when _score_series() agrees.
    Everything else that looked like a series becomes "other" (never dropped).

    The verdict details go to info["series"] for handlers/autosort.py's log lines:
    candidate, source (tmdb | tvmaze | wikidata | openai | fallback | -), id, score, signals,
    reason, runtime (TMDB / TVmaze minutes per episode, information only), title (the cleaned
    show title), language, ambiguous, looked_series (the name looked like a series episode) and
    to_anime (why a series-looking file was sent to anime, else None)."""
    def _finish(kind: str) -> str:
        return f"{kind}_files" if is_files else kind

    first = parsed[0]
    sinfo = {"candidate": False, "source": "-", "id": None, "score": 0, "signals": [],
             "reason": "", "runtime": None, "title": first.get("clean_title") or first["titles"][0],
             "language": first.get("language"), "ambiguous": False, "to_anime": None,
             "season_pack": any(pp.get("season_pack") for pp in parsed),       # "S01 Combined": the length gate skips it
             "pack": is_season_pack(parsed),                                   # any season pack (S01 / Season 1 / Batch ...)
             "tv_source": any(pp.get("tv_source") for pp in parsed),
             "looked_series": any(pp.get("strong_episode") or pp.get("is_episode") or pp.get("series_like")
                                  or pp.get("tv_source") for pp in parsed)}
    info["series"] = sinfo
    minutes = movie_duration_minutes(message)
    nsfw = is_nsfw(message)
    extra = _extra_hit(parsed, names)

    async def _decide(p, source, ident=None, tmdb=None, tvmaze=None, openai=False, wikidata=None, entry=None) -> str:
        shows = [n for f in (tmdb, tvmaze, wikidata) if f for n in f["names"]]
        ambiguous = bool(entry and entry.get("ambiguous"))
        movie_like = False
        if tmdb and not p.get("strong_episode") and not p.get("tv_source") and p.get("year") and p.get("release_group"):
            movie_like = await _tmdb_movie_title_exists(session, tmdb_key, p)
        lang = p.get("language")
        lang_ok = bool(lang and not ambiguous and (
            any(_lang_code_of(f.get("lang")) == lang for f in (tmdb, tvmaze) if f)
            or bool(entry and entry.get("lang_ok"))))
        ep_title = bool(tmdb and not ambiguous and await _episode_title_ok(session, tmdb_key, p, tmdb))
        # Education is only a CANDIDATE: a source that knows the show wins over soft course words ("Lessons", "Classroom"),
        # over words of the show's own title and over a bracketed year. Strong course words still veto a known show.
        confirmed = bool(tmdb or tvmaze or wikidata or openai)
        edu_level = _edu_level(names, shows)
        edu = edu_level == 2 or (edu_level == 1 and not confirmed)
        ok, score, signals, reason = _score_series(
            p, tmdb, tvmaze, openai, _facts_exact(p, tmdb), minutes,
            edu, nsfw, extra, movie_like, wikidata, lang_ok, ep_title, ambiguous)
        if ok and tvmaze and any(str(g).lower() == "anime" for g in tvmaze["genres"]):
            ok, reason = False, "TVmaze lists it as anime"
        if ok and IDENT_RECORD:
            for f in (tmdb, tvmaze, wikidata):          # IDENT_RECORD: the canonical show title / first year of the vouching source
                if f and f.get("names"):
                    sinfo["canon"] = {"title": f["names"][0], "year": f.get("first_year"),
                                      "source": f.get("source") or source, "id": f.get("id")}
                    break
        sinfo.update(
            candidate=bool(p.get("strong_episode") or p.get("is_episode") or p.get("series_like")
                           or tmdb or tvmaze or wikidata or openai),
            source=source, id=ident, score=score, signals=signals, reason=reason, ambiguous=ambiguous,
            title=p.get("clean_title") or p["titles"][0], language=p.get("language"),
            runtime=next((f["runtime"] for f in (tmdb, tvmaze) if f and f.get("runtime")), None))
        if not ok and edu and not (nsfw or extra) and not confirmed:
            return _finish("education")                # course signals and no source knows it: education, not Other
        if not ok:
            _other_note(info, *_series_other_reason(p, reason, confirmed))
        return _finish("series") if ok else "other"

    # what the movie cleaner made of the name (handlers/autosort.py logs it when the file ends in Other)
    for pp in parsed:
        if pp.get("movie"):
            mvi = pp["movie"]
            _movie_note(info, title=mvi["title"], titles=list(pp.get("titles") or [])[:4], year=pp.get("year"),
                        languages=list(mvi["languages"]))
            break

    # 0) deep_anime only: an anime CANDIDATE asks AniList (Jikan when AniList failed) BEFORE TMDB
    if anime_mode:
        ap0 = first.get("anime") or {}
        _anime_note(info, candidate=bool(ap0.get("candidate")), signals=list(ap0.get("signals") or []),
                    title=ap0.get("title") or first.get("clean_title") or first["titles"][0],
                    language=first.get("language") or ap0.get("language"),
                    season=first.get("season") or ap0.get("season"),
                    episode=first.get("episode") or ap0.get("episode"),
                    source="-", verdict=None, anilist="-", pack=sinfo["pack"])
        if not use_anilist:
            _offline_db_kick()                       # AniList is off: start loading the offline anime database (background)
        if await _anime_first_pass(session, parsed, use_anilist, anilist_key, minutes, info, tmdb_key):
            src = info["anime"].get("source") or "anilist"
            sinfo["reason"] = f"{src}: anime (asked before TMDB)"
            if sinfo["looked_series"]:
                sinfo["to_anime"] = f"{src} match (anime candidate, asked before TMDB)"
            return _finish("anime")

    # 1) TMDB (+ AniList inside, only when TMDB is unsure). Episode-like names use the deep
    #    show matcher (candidates, language, per-show cache); everything else is unchanged.
    found, errored = None, False
    for p in parsed:
        hit = None
        if p.get("is_episode") or p.get("series_like"):
            hit = await _deep_tmdb_tv(session, tmdb_key, p, use_anilist, anilist_key, info, anime_mode)
            status, kind = hit["status"], hit["kind"]
            if status == "none" and p.get("year"):
                # TMDB answered "no such show": query variants with the year, then the year + language pool
                alt = await _tv_fallbacks(session, tmdb_key, p, info)
                if alt["status"] != "none":
                    hit, status, kind = alt, alt["status"], alt["kind"]
        else:
            status, kind = await _tmdb_identify(session, tmdb_key, p, use_anilist, anilist_key, info, anime_mode)
            if status == "none" and p.get("movie"):
                # TMDB answered "no such film": spelling variants (Chaadum / Chadum), then Wikidata
                status, kind = await _movie_fallbacks(session, tmdb_key, p, info, anime_mode)
        if status == "found":
            found = (p, kind, hit)
            break
        if status == "error":
            errored = True
    if found:
        p, kind, hit = found
        if anime_mode and kind != "anime":
            await _anime_downgrade_note(session, tmdb_key, info)
        if kind != "series":                       # movie / anime: decided exactly as before
            sinfo["reason"] = f"{(info.get('movie') or {}).get('source', 'tmdb')}: {kind}"
            if kind == "anime" and sinfo["looked_series"]:
                sinfo["to_anime"] = ((hit or {}).get("why")
                                     or (f"tmdb {info.get('tmdb_media')} {info.get('tmdb_id')} says anime"
                                         if info.get("tmdb_id") else "anilist says anime (TMDB had no match)"))
            return _finish(kind)
        entry = (hit or {}).get("entry")
        tmdb_id = info.get("tmdb_id")
        if entry and entry.get("facts"):
            tmdb = entry["facts"]
        else:
            tmdb = await _tmdb_facts_for(session, tmdb_key, p, tmdb_id) if tmdb_id else _empty_facts("tmdb")
        tvmaze = None
        if USE_TVMAZE and not p.get("strong_episode"):
            # with an explicit marker TMDB alone already passes: TVmaze is only asked
            # when its agreement is what could make a marker-less name a series
            tvmaze, _state = await _tvmaze_facts_for(session, p)
        return await _decide(p, "tmdb", tmdb_id, tmdb, tvmaze, entry=entry)

    # 2) TVmaze, then Wikidata, then IMDb (OMDb): only for names with an explicit marker / air date / TV-source tag
    marked = [pp for pp in parsed if pp.get("strong_episode") or pp.get("tv_source")]
    tvmaze, wikidata, ext_p = None, None, None
    if USE_TVMAZE:
        for pp in marked:
            facts, _state = await _tvmaze_facts_for(session, pp)
            if facts:
                tvmaze, ext_p = facts, pp
                break
    if USE_WIKIDATA and tvmaze is None:
        for pp in marked:
            facts, _state = await _wikidata_facts_for(session, pp)
            if facts:
                wikidata, ext_p = facts, pp
                break
    if tvmaze is None and wikidata is None and (USE_IMDB_SUGGEST or OMDB_API_KEY):
        for pp in marked:                                  # IMDb needs the year: it must agree (+-1) with the show's start
            facts = await _imdb_show_facts(session, tmdb_key, pp)
            if facts:
                wikidata, ext_p = facts, pp                # travels in the Wikidata slot, source "imdb" / "omdb"
                break
    if errored and tvmaze is None and wikidata is None:
        # TMDB failed and nothing else vouches for the file: don't guess during an outage
        sinfo["reason"] = "TMDB unavailable"
        return TMDB_UNAVAILABLE
    if tvmaze is not None:
        return await _decide(ext_p, "tvmaze", tvmaze.get("id"), None, tvmaze)
    if wikidata is not None:
        return await _decide(ext_p, wikidata.get("source") or "wikidata", wikidata.get("id"), None, None, wikidata=wikidata)

    # 3) OpenAI (only when a key was passed), 4) the scored marker fallback
    verdict = await _other_or_openai(message, names, is_files, session, openai_key)
    p = marked[0] if marked else parsed[0]
    if verdict in ("series", "series_files"):
        return await _decide(p, "openai", None, None, None, openai=True)
    if verdict != "other":
        sinfo["reason"] = f"openai: {verdict}"
        return verdict
    return await _decide(p, "fallback")


# ============================================================
# /autosort deep ANIME identification + anime gate settings
# ============================================================
# Opt-in: classify_async(..., deep_series=True, deep_anime=True). Only handlers/autosort.py
# passes both, so /fwd, /autosortai, matches_async() and the /settings AniList buttons
# (anilist_lookup / anilist_identify) behave exactly as before.
#
# What deep_anime changes (and only then):
#   1. Names are cleaned for anime (_anime_info): fansub / release groups, "_Remove" junk,
#      audio / codec / language brackets, quality words and dub words leave the title (dub words
#      become a language signal), season / part / cour markers are split off. Anime lookups
#      use two title candidates only: the cleaned title and the same title without season
#      markers - never progressive word-dropping ("My Hero").
#   2. A file is an anime CANDIDATE when it has a fansub / known anime release group, a dub /
#      dual-audio / Indian-language signal together with an episode marker, or anime-style
#      naming (anime_tag / anime_hint). Candidates ask AniList FIRST (when the user enabled
#      AniList), then TMDB. Everything else keeps the old order (TMDB first, AniList second
#      only when TMDB is unsure).
#   3. AniList is asked with a stronger query and a scored choice (_anilist_best_match_deep).
#      If AniList FAILED (not "not found") Jikan (MyAnimeList, no key) answers instead.
#   4. TMDB keywords NEVER make a show anime any more (the old rule turned Blue Eye Samurai
#      into anime: its keywords are japan / samurai / edo period). A TMDB match is anime when
#      it is Animation + an Asian origin, OR its id is in the Fribb anime-lists data set, OR
#      AniList / Jikan confirm it. A live-action match is never anime.
#   5. NCOP / NCED / creditless / AMV / PV / CM / preview / opening / ending / menu / bonus
#      files are extras ("other"), even for anime.
#
# Env vars (all optional):
#   USE_ANIME_LISTS=1     second opinion from Fribb/anime-lists (a TMDB id in the list = anime)
#   USE_JIKAN=1           Jikan answers when AniList FAILED (never as an extra vote)
#   ANIME_GROUPS=         extra anime release groups, comma separated (added to the built-in list)
#   ANIME_MIN_MINUTES=3 / ANIME_MAX_MINUTES=300      anime gate length band (handlers/autosort.py)
#   ANIME_THUMB_RATIO_MIN=1.30 / ANIME_THUMB_RATIO_MAX=2.40   anime gate thumbnail band
#   ANIME_RATIO_UNKNOWN=other   "other" | "allow": no thumbnail size at all
#   ANIME_REQUIRE_THUMB=1       a video / document with NO thumbnail goes to Other
USE_ANIME_LISTS = os.environ.get("USE_ANIME_LISTS", "1").strip().lower() not in ("0", "false", "no")
USE_JIKAN = os.environ.get("USE_JIKAN", "1").strip().lower() not in ("0", "false", "no")

ANIME_MIN_MINUTES = max(0.0, _env_float("ANIME_MIN_MINUTES", 3.0))
ANIME_MAX_MINUTES = _env_float("ANIME_MAX_MINUTES", 300.0)
if ANIME_MAX_MINUTES <= ANIME_MIN_MINUTES:
    logger.warning("ANIME_MIN_MINUTES/MAX (%s / %s) are not a valid range - using 3 / 300",
                   ANIME_MIN_MINUTES, ANIME_MAX_MINUTES)
    ANIME_MIN_MINUTES, ANIME_MAX_MINUTES = 3.0, 300.0

ANIME_THUMB_RATIO_MIN = _env_float("ANIME_THUMB_RATIO_MIN", _DEFAULT_THUMB_RATIO_MIN)
ANIME_THUMB_RATIO_MAX = _env_float("ANIME_THUMB_RATIO_MAX", _DEFAULT_THUMB_RATIO_MAX)
if not 0 < ANIME_THUMB_RATIO_MIN < ANIME_THUMB_RATIO_MAX:
    logger.warning("ANIME_THUMB_RATIO_MIN/MAX (%s / %s) are not a valid range - using %s / %s",
                   ANIME_THUMB_RATIO_MIN, ANIME_THUMB_RATIO_MAX, _DEFAULT_THUMB_RATIO_MIN, _DEFAULT_THUMB_RATIO_MAX)
    ANIME_THUMB_RATIO_MIN, ANIME_THUMB_RATIO_MAX = _DEFAULT_THUMB_RATIO_MIN, _DEFAULT_THUMB_RATIO_MAX

ANIME_RATIO_UNKNOWN = os.environ.get("ANIME_RATIO_UNKNOWN", "other").strip().lower()
if ANIME_RATIO_UNKNOWN not in ("other", "allow"):
    ANIME_RATIO_UNKNOWN = "other"

# A video / document without any thumbnail (blank preview) is not a proper anime episode.
ANIME_REQUIRE_THUMB = os.environ.get("ANIME_REQUIRE_THUMB", "1").strip().lower() not in ("0", "false", "no")


# ---- anime name cleaning --------------------------------------------------------------------
# Release groups that only ever publish anime (alphanumerics only, lower case). A hyphenated
# group ("JUDAS-LUMiX") counts when any of its parts is in the set. Add more with ANIME_GROUPS.
_ANIME_GROUPS = {
    "subsplease", "erairaws", "horriblesubs", "judas", "commie", "ember", "asw", "tsundereraws", "ohysraws",
    "animetime", "kekkai", "coalgirls", "gjm", "doki", "utw", "hiryuu", "yameii", "dkb", "animeland", "chihiro",
    "naisu", "sokudo", "toonshub", "lostyears", "lolihouse", "moozzi2", "nekomoe", "bonkai77", "dragsterps",
    "sallysubs", "reaktor", "yuisubs", "cleo", "mtbb",
}
for _g in os.environ.get("ANIME_GROUPS", "").split(","):
    _g = re.sub(r"[^a-z0-9]", "", _g.lower())
    if _g:
        _ANIME_GROUPS.add(_g)

_ANIME_JUNK_SUFFIX_RE = re.compile(r"(?:_+|\s+-\s+)(?:remove[d]?|removed?[\s._-]?ads?)\s*$", re.IGNORECASE)
# a fansub / release group glued to a technical tag: "...SubS-Me7alh", "...x265-GROUP"
_ANIME_TAIL_GROUP_RE = re.compile(
    r"(?:x26[45]|h[\s.]?26[45]|hevc|avc|av1|10bit|\d{3,4}p|blu[\s._-]?ray|web[\s._-]?dl|web[\s._-]?rip|hdtv|"
    r"aac[\s._]?\d?|ddp?[\s._]?\d?|opus|flac|subs?)[\s._]*-\s*([A-Za-z0-9]{2,20})\s*(?=$|[\s(\[-])", re.IGNORECASE)
_ANIME_BARE_SEASON_RE = re.compile(r"(?<![A-Za-z0-9])s0*\d{1,2}(?![A-Za-z0-9])", re.IGNORECASE)
_ANIME_DASH_EP_RE = re.compile(r"^0*(\d{1,4})(?:v\d)?(?![A-Za-z0-9])")
_ANIME_YEAR_RE = re.compile(r"(?<![0-9])((?:19|20)\d{2})(?![0-9])")
_ANIME_DUB_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:dubbed|dub|dual[\s._-]?audio|multi[\s._-]?audio)(?![A-Za-z0-9])", re.IGNORECASE)
_ANIME_DUB_WORDS = {"dubbed", "dub", "dual", "multi", "audio"}      # may sit at a title edge ...
_ANIME_DUB_REAL = {"dubbed", "dub"}                                 # ... but only these (or "dual / multi AUDIO") prove a dub
_ANIME_SUB_WORDS = {"subbed", "sub", "subs", "esub", "esubs", "hsub", "hsubs", "msub", "msubs", "org", "original"}
_ANIME_EN_WORDS = {"english", "eng", "japanese", "jap", "jpn"}

# Season / part / cour markers at the END of a title: "Season 2", "2nd Season", "S02", "Part 2",
# "Cour 2", "Final Season", roman numerals II / III / IV. Needs a separator in front, so "Hawaii" stays.
_SEASON_WORDS_RE = re.compile(
    r"(?:^|[\s:\-–—,]+)(?:"
    r"(?:the\s+)?final\s+season"
    r"|season\s*0*(?P<s1>\d{1,2})"
    r"|0*(?P<s2>\d{1,2})(?:st|nd|rd|th)\s+season"
    r"|s0*(?P<s3>\d{1,2})"
    r"|part\s*0*(?P<p1>\d{1,2})"
    r"|cour\s*0*(?P<p2>\d{1,2})"
    r"|(?P<r>iii|ii|iv)"
    r")\s*$", re.IGNORECASE)
_ROMAN_SEASON = {"ii": 2, "iii": 3, "iv": 4}


def _strip_season_words(text: str):
    """(title without trailing season / part / cour markers, season number | None, part | None).
    Never strips a title down to nothing."""
    cur, season, part = (text or "").strip(), None, None
    for _ in range(3):
        m = _SEASON_WORDS_RE.search(cur)
        if not m:
            break
        rest = cur[:m.start()].rstrip(" :-–—,")
        if len(re.findall(r"[A-Za-z0-9]", rest)) < 2:
            break
        s = m.group("s1") or m.group("s2") or m.group("s3")
        if m.group("r"):
            s = _ROMAN_SEASON[m.group("r").lower()]
        if s and season is None:
            season = int(s)
        pp = m.group("p1") or m.group("p2")
        if pp and part is None:
            part = int(pp)
        cur = rest
    return cur, season, part


def _anime_strip_edges(title: str):
    """(title without LEADING / TRAILING dub and language words, language codes, dub flag).
    Dub words (Dubbed, Dub, Dual Audio, Multi) and Indian language words (Hindi, Tamil ...) are
    removed from the title edges and kept as a signal. A lone "English" / "Japanese" is a real
    title word and stays; "English Dub" goes."""
    langs, state = [], {"dub": False}

    def is_edge(tok):
        a = _alnum(tok)
        return a in _ANIME_DUB_WORDS or a in _ANIME_SUB_WORDS or a in _LANG_FULL or a in _ANIME_EN_WORDS

    def trim(seq, from_end):
        n, i = len(seq), 0
        while i < n and is_edge(seq[n - 1 - i] if from_end else seq[i]):
            i += 1
        if i == 0 or i >= n:                      # nothing to strip / the whole title would vanish
            return seq
        run = seq[n - i:] if from_end else seq[:i]
        words = {_alnum(t) for t in run}
        proof = bool(words & _ANIME_DUB_REAL or words & set(_LANG_FULL) or ({"dual", "multi"} & words and "audio" in words))
        if not proof:
            return seq                            # "Dual Survival", a lone English / Japanese: part of the title
        for t in run:
            a = _alnum(t)
            if a in _LANG_FULL:
                langs.append(_LANG_FULL[a])
            elif a in ("english", "eng") and "en" not in langs:
                langs.append("en")
        state["dub"] = True
        return seq[:n - i] if from_end else seq[i:]

    toks = title.split()
    toks = trim(toks, True)
    toks = trim(toks, False)
    return " ".join(toks), langs, state["dub"]


def _known_anime_group(group: str) -> bool:
    if not group:
        return False
    if re.sub(r"[^a-z0-9]", "", group.lower()) in _ANIME_GROUPS:
        return True
    return any(re.sub(r"[^a-z0-9]", "", part.lower()) in _ANIME_GROUPS for part in re.split(r"[-_\s.]+", group))


def _anime_info(name: str) -> dict:
    """Pure anime clean-up of one release name (no network). `name` is the name BEFORE the deep
    normaliser (which deletes audio-only brackets such as "[Dual Audio]" - exactly the signal wanted).

      title        cleaned title: no groups, brackets, quality / source / codec words, no dub words
      titles       [cleaned title, same without season / part / cour markers]  (deduplicated)
      language     "hi" / "ta" / "te" ... / "en" (English dub) from dub words and audio brackets, else None
      dub          a dub / dual-audio / Indian-language signal was seen
      fansub       leading [Group] (SubsPlease, Erai-raws ...)       groups  trailing release groups
      known_group  one of them is a known anime-only group           season / part / episode / year
    Episode forms: S03E14, E1090, EP12, Episode 12, 1x05 and the fansub style "Title - 12"."""
    out = {"title": "", "titles": [], "language": None, "dub": False, "fansub": None, "groups": [],
           "known_group": False, "season": None, "part": None, "episode": None, "year": None,
           "has_episode": False, "junk": False}
    stem = _VIDEO_EXT_END_RE.sub("", name or "").strip()
    cleaned = _ANIME_JUNK_SUFFIX_RE.sub("", stem)
    if cleaned != stem:
        out["junk"], stem = True, cleaned
    if not stem:
        return out
    out["fansub"] = _fansub_group(stem)

    langs, dub = [], bool(_ANIME_DUB_RE.search(stem))
    groups = []
    for m in _BRACKET_GROUP_RE.finditer(stem):
        inner = m.group(1).strip()
        if not inner:
            continue
        tokens = [t for t in re.split(r"[\s._\-+,;/|&:]+", inner) if t]
        at_end = not re.sub(r"[\s._\-–—]+", "", stem[m.end():])
        if re.fullmatch(r"(?:19|20)\d{2}", inner):
            out["year"] = out["year"] or int(inner)
        elif re.fullmatch(r"[0-9A-Fa-f]{8}", inner):
            continue                                          # CRC32
        else:
            codes = _lang_codes(tokens, short=False)
            for c in codes:
                if c not in langs:
                    langs.append(c)
            if codes or any(_alnum(t) in ("dual", "multi") for t in tokens):
                dub = True
            looks_like_group = (re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*", inner)
                                and len(inner) >= 3 and not codes
                                and not any(_TECH_TOKEN_RE.match(t) or _AUDIO_FRAG_RE.match(t) for t in tokens))
            if at_end and looks_like_group and inner != out["fansub"]:
                groups.append(inner)
    mt = _ANIME_TAIL_GROUP_RE.search(stem)
    if mt and mt.group(1) not in groups and _alnum(mt.group(1)) not in _LANG_ANY:
        groups.append(mt.group(1))
    out["groups"] = groups
    out["known_group"] = any(_known_anime_group(g) for g in groups) or _known_anime_group(out["fansub"] or "")

    text = stem
    for _ in range(3):                                        # inner groups first, then the shell left behind
        new = _BRACKET_GROUP_RE.sub(" ", text)
        if new == text:
            break
        text = new
    spaced = re.sub(r"\s{2,}", " ", _spaced(text)).strip()
    segs = [s.strip() for s in re.split(r"\s+[-–—]\s+", spaced) if s.strip()]
    if not segs:
        return out

    # episode / season numbers: S03E14, 1x05, Episode 12, E1090, a bare S02 ... else "Title - 12"
    season, episode = None, None
    m = (re.search(r"(?<![A-Za-z0-9])s0*(\d{1,2})[ ._-]?e0*(\d{1,4})(?![0-9])", spaced, re.IGNORECASE)
         or re.search(r"(?<![A-Za-z0-9])(\d{1,2})x0*(\d{1,3})(?![A-Za-z0-9])", spaced, re.IGNORECASE))
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(?<![A-Za-z0-9])(?:(?:episodes?|ep|epi)\.?[\s._#:-]*0*(\d{1,4})|e0*(\d{2,4}))(?![A-Za-z0-9])",
                      spaced, re.IGNORECASE)
        if m:
            episode = int(m.group(1) or m.group(2))
        m = _ANIME_BARE_SEASON_RE.search(segs[0])
        if m:
            season = int(re.sub(r"\D", "", m.group(0)))
    if episode is None:
        for seg in segs[1:]:
            m = _ANIME_DASH_EP_RE.match(seg)
            if m:
                n = int(m.group(1))
                if not (len(m.group(1)) == 4 and 1900 <= n <= 2099):      # a year is not an episode
                    episode = n
                    break
    out["episode"] = episode
    out["has_episode"] = episode is not None or bool(_DEEP_EP_RE.search(spaced))

    # the title is the first segment, cut at the first episode / season designator / technical tag
    head = segs[0]
    cut = len(head)
    for rx in (_DEEP_EP_RE, _ANIME_BARE_SEASON_RE, _TECH_START_RE):
        for m in rx.finditer(head):
            if m.start() > 0:
                cut = min(cut, m.start())
                break
    title = re.sub(r"^[\s\-–—_:|~.,]+|[\s\-–—_:|~.,]+$", "", head[:cut])
    # the year, when the name has one outside brackets ("Your.Name.2016.1080p")
    if out["year"] is None:
        m = _ANIME_YEAR_RE.search(spaced)
        if m and m.start() > 0:
            out["year"] = int(m.group(1))

    title, edge_langs, edge_dub = _anime_strip_edges(title)
    dub = dub or edge_dub
    # language words AFTER the title ("Doraemon S01E10 Hindi Dubbed 720p"): the tail of the first segment
    # and the other segments
    tail = " ".join([head[cut:]] + segs[1:])
    for c in _lang_codes(_tokens(tail), short=False):
        if c not in langs:
            langs.append(c)
    if re.search(r"(?<![A-Za-z0-9])(?:english|eng)[\s._-]*dub(?:bed)?(?![A-Za-z0-9])", stem, re.IGNORECASE) and "en" not in langs:
        langs.append("en")
    for c in edge_langs:
        if c not in langs:
            langs.insert(0, c)
    out["dub"] = dub
    indian = [c for c in langs if c != "en"]
    out["language"] = indian[0] if indian else (langs[0] if langs else None)
    if len(indian) > 1 and not edge_langs:
        out["language"] = None                                # several audio tracks: not one language

    if len(re.findall(r"[A-Za-z0-9]", title)) < 2 or _generic_title(title):
        return out
    alts = []
    if TITLE_ALT_SPLIT:
        try:                                                  # "Ne Zha A.K.A. Nezha Mo tong jiang shi": every part is a title
            parts = _split_alt_titles(title)
            if len(parts) > 1:
                title, alts = parts[0], [x for x in parts[1:] if not _generic_title(x) and not _extra_title_junk(x, pipe_list=False)]
        except Exception as e:
            logger.warning("anime alternate-title split failed on %r: %s - old behaviour", name, e)
    stripped, s_season, s_part = _strip_season_words(title)
    out["season"] = season if season is not None else s_season
    out["part"] = s_part
    out["title"] = title
    titles = [title]
    if stripped and _norm_cmp(stripped) != _norm_cmp(title) and not _generic_title(stripped):
        titles.append(stripped)
    for x in alts:
        if _norm_cmp(x) not in {_norm_cmp(t) for t in titles}:
            titles.append(x)
    out["titles"] = titles
    return out


# unambiguous anime extras: always "other" in deep_anime mode
_ANIME_EXTRA_SURE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:nc[\s._-]?(?:op|ed)\d*|(?:non|no)[\s._-]?credit(?:s|ed)?|creditless|textless|"
    r"clean[\s._-]?(?:op|ed|opening|ending)\d*|amv|anime[\s._-]?music[\s._-]?video)(?![A-Za-z0-9])", re.IGNORECASE)
# contextual words: only for a file that already looks like anime (a movie called "The Ending" is no extra)
_ANIME_EXTRA_CTX_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:pv[\s._-]?\d{0,2}|cm[\s._-]?\d{0,2}|preview|(?:op|ed)[\s._-]?\d{1,2}(?:v\d)?)(?![A-Za-z0-9])",
    re.IGNORECASE)
_ANIME_EXTRA_LOOSE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:opening|ending|menu|bonus|extras?|special[\s._-]?features?)(?![A-Za-z0-9])", re.IGNORECASE)


def _anime_extra(stem: str, anime_like: bool, has_marker: bool) -> bool:
    """NCOP / NCED / creditless / AMV always; PV / CM / preview / OP1 / ED2 when the file looks like
    anime; opening / ending / menu / bonus when it looks like anime AND has no episode marker
    ("Show S01E05 - The Opening" is an episode). A hit as the very first word of the name does not
    count unless a dash follows (same rule as _deep_extra())."""
    def hit(rx):
        for m in rx.finditer(stem):
            if m.start() > 0 or re.match(r"\s*[-–—:|]", stem[m.end():]):
                return True
        return False
    if hit(_ANIME_EXTRA_SURE_RE):
        return True
    if anime_like and hit(_ANIME_EXTRA_CTX_RE):
        return True
    return bool(anime_like and not has_marker and hit(_ANIME_EXTRA_LOOSE_RE))


def _anime_attach(p: dict, raw_name: str, check_extra: bool) -> None:
    """Deep-anime part of _parse_name(): adds p["anime"], flags anime extras and - for a STRONG anime
    candidate - swaps the TMDB title candidates for the cleaned anime ones (cleaned title, and the
    same without season markers; no "<title> Hindi", no progressive shortening)."""
    a = _anime_info(raw_name)
    signals = []
    if p.get("anime_tag"):
        signals.append("tag")
    serial_style = bool(p.get("platforms") or p.get("date_episode") or p.get("serial_style"))
    own_hint = bool(a["has_episode"] and not serial_style and (
        (a["season"] is None and not p.get("season")) or len(a["titles"]) > 1))
    if a["fansub"]:
        signals.append("fansub:" + a["fansub"])
    elif p.get("anime_hint") or own_hint:
        signals.append("hint")
    if a["known_group"]:
        signals.append("group:" + (next((g for g in a["groups"] if _known_anime_group(g)), None) or a["fansub"] or "?"))
    has_ep = bool(a["has_episode"] or p.get("strong_episode"))
    if a["dub"] and has_ep:
        signals.append("dub+ep")
    # Anime must be PROVEN, never guessed from naming: only a fansub tag / a known anime-only group is "strong" (it
    # swaps the title candidates). "dub + episode" and "episode without season" make a SOFT candidate: AniList is
    # asked first, but the answer only counts with an exact title + popularity (see _anime_lookup), the title
    # candidates of the name are kept, and TMDB / TVmaze / IMDb decide when AniList does not know it.
    strong = bool(p.get("anime_tag") or a["fansub"] or a["known_group"])
    a["signals"], a["candidate"], a["strong"] = signals, bool(signals), strong
    a["soft"] = bool(signals) and not strong
    p["anime"] = a

    if check_extra and not p["extra"]:
        anime_like = bool(signals) or bool(re.search(r"(?<![A-Za-z0-9])anime(?![A-Za-z0-9])", raw_name, re.IGNORECASE))
        stem = _VIDEO_EXT_END_RE.sub("", raw_name)
        if _anime_extra(stem, anime_like, has_ep):
            p["extra"] = True
    if a["language"] and not p.get("language"):
        p["language"] = a["language"]
    if strong and a["titles"] and not p.get("regional"):
        titles = [t for t in a["titles"] if not _generic_title(t)]
        if titles:
            p["titles"] = titles
            p["candidates"] = [{"title": t, "dropped": 0, "kind": "anime"} for t in titles]
            p["clean_title"] = titles[0]


# ---- AniList: scored choice (deep_anime) -----------------------------------------------------------
_ANILIST_EP_FORMATS = {"TV", "TV_SHORT", "ONA", "OVA", "SPECIAL"}
# ANILIST_ANY_ORIGIN: formats an entry of ANY country of origin may have to count as anime (type ANIME is in the query)
_ANILIST_ANY_FORMATS = {"TV", "TV_SHORT", "ONA", "OVA", "MOVIE", "SPECIAL"}


def _anime_score(q: str, n: str) -> float:
    """Title similarity of two normalised titles, rapidfuzz when installed (else difflib); the digit
    rule of _title_score() is kept ("Mob Psycho 100" never matches "Mob Psycho 200")."""
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if _DIGITS.findall(q) != _DIGITS.findall(n):
        return 0.0
    if _keys_equal(q, n):
        return 1.0                                            # punctuation / spacing only ("Dr. Stone" = "Dr Stone")
    return _sim(q, n)


def _anilist_best_match_deep(results, queries, year, strict: bool, ctx: dict):
    """The deep_anime choice among AniList results -> (media | None, details dict).

    Title score is computed on the BASE titles (season / part / cour words removed from both
    sides), so "My Hero Academia" can reach "Boku no Hero Academia 3rd Season"; which season it is
    is decided by the preference below, not by the fuzzy score. Hard filters: MUSIC format, origin
    outside ANILIST_ORIGINS, a score below MIN_TITLE_SCORE (1.0 and ANILIST_STRICT_MIN_POPULARITY
    for weak names), a file year outside the show's years. Ranking, in this order: exact normalised
    title, title score, format (episode marker -> TV / TV_SHORT / ONA / OVA / SPECIAL; a year and no
    episode marker -> MOVIE), season (the file's season number vs a "2nd Season" / "Season 3" / "III"
    marker in the entry's titles or synonyms), popularity. The file length vs the entry's episode
    length is only reported, never a veto."""
    qs = []
    for q in queries:
        if q:
            qs.append((q, _strip_season_words(q)[0] or q))
    has_ep, fseason, minutes = bool(ctx.get("episode")), ctx.get("season"), ctx.get("minutes")
    best, best_key, best_d = None, None, None
    for r in (results or [])[:10]:
        fmt = r.get("format")
        if fmt == "MUSIC":
            continue
        origin = r.get("countryOfOrigin") or "JP"
        foreign = origin not in ANILIST_ORIGINS
        if foreign and not (ANILIST_ANY_ORIGIN and ctx.get("any_origin") and fmt in _ANILIST_ANY_FORMATS):
            continue                                          # old rule: only JP / CN / KR / TW (or any-origin rules below)
        t = r.get("title") or {}
        names = [t.get("romaji"), t.get("english"), t.get("native")] + list(r.get("synonyms") or [])
        score, exact, seasons = 0.0, False, set()
        for n in names:
            if not n or not isinstance(n, str):
                continue
            nn = _norm_cmp(n)
            nb, ns, _np = _strip_season_words(nn)
            nb = nb or nn
            if ns is not None:
                seasons.add(ns)
            for q, qb in qs:
                s = _anime_score(qb, nb)
                if s > score:
                    score = s
                if qb and qb == nb:
                    exact = True
        if score < (1.0 if strict else MIN_TITLE_SCORE):
            continue
        if foreign and not exact:
            continue                                          # a non-Asian entry needs the EXACT normalised title, never a fuzzy one
        pop = r.get("popularity") or 0
        if strict and pop < ANILIST_STRICT_MIN_POPULARITY:
            continue
        if year:
            if fmt == "MOVIE":
                ry = r.get("seasonYear") or ((r.get("startDate") or {}).get("year"))
                if ry and abs(ry - year) > 1:
                    continue
            else:
                sy = (r.get("startDate") or {}).get("year") or r.get("seasonYear")
                ey = (r.get("endDate") or {}).get("year") or (datetime.date.today().year + 1)
                if sy and (year < sy - 1 or year > ey + 1):
                    continue
        if foreign and year:
            # ... and the file's year must agree (+-1) with the entry's: its first year (a series) or its year (a film)
            sy = (r.get("startDate") or {}).get("year") or r.get("seasonYear")
            if not sy or (year < sy - 1 or (fmt == "MOVIE" and year > sy + 1)):
                continue
        if has_ep:
            fmt_pref = 1 if fmt in _ANILIST_EP_FORMATS else 0
        elif year:
            fmt_pref = 1 if fmt == "MOVIE" else 0
        else:
            fmt_pref = 0
        if fseason and fseason >= 2:
            season_pref = 2 if fseason in seasons else (-1 if seasons else 0)
        else:
            season_pref = 1 if not seasons else 0
        key = (1 if exact else 0, round(score, 4), fmt_pref, season_pref, pop)
        if best is None or key > best_key:
            dur, note = r.get("duration"), None
            if minutes and isinstance(dur, (int, float)) and dur > 0:
                note = f"file {minutes:.0f}m vs entry {dur:.0f}m per episode"
            best, best_key = r, key
            best_d = {"score": round(score, 3), "exact": exact, "format_pref": fmt_pref, "season_pref": season_pref,
                      "dur": note, "foreign": origin if foreign else None}
    return best, (best_d or {})


# ---- Jikan (MyAnimeList, free, no key): used ONLY when AniList FAILED ---------------------------------
JIKAN_URL = "https://api.jikan.moe/v4/anime"
JIKAN_TIMEOUT_SECONDS = 8
JIKAN_RETRIES = 3
JIKAN_MIN_INTERVAL = 1.0            # Jikan allows ~3 requests / second and 60 / minute: one per second
JIKAN_DOWN_SECONDS = 30             # after a failed call skip Jikan this long (a dead Jikan must not slow a run)
_JIKAN_CACHE: "OrderedDict" = OrderedDict()      # normalized query -> normalized results
_jikan_lock = None
_jikan_last_call = 0.0
_jikan_down_until = 0.0
_JIKAN_FORMATS = {"TV": "TV", "Movie": "MOVIE", "OVA": "OVA", "ONA": "ONA", "Special": "SPECIAL",
                  "TV Special": "SPECIAL", "Music": "MUSIC", "CM": "MUSIC", "PV": "MUSIC"}


def _jikan_minutes(text):
    """"24 min per ep" -> 24, "1 hr 50 min" -> 110, anything else -> None."""
    if not isinstance(text, str):
        return None
    h = re.search(r"(\d+)\s*hr", text)
    m = re.search(r"(\d+)\s*min", text)
    total = (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
    return total or None


def _jikan_media(d):
    """One Jikan entry in the AniList media shape (so one scorer serves both). MAL has no country
    of origin: it is a Japanese anime database, so every entry counts as "JP"."""
    if not isinstance(d, dict) or not d.get("mal_id"):
        return None
    aired = d.get("aired") or {}
    sy = _year_of(aired.get("from")) or _as_int(d.get("year"))
    ey = _year_of(aired.get("to"))
    synonyms = list(d.get("title_synonyms") or [])
    synonyms += [t.get("title") for t in (d.get("titles") or []) if isinstance(t, dict) and t.get("title")]
    return {
        "id": d["mal_id"], "idMal": d["mal_id"], "format": _JIKAN_FORMATS.get(d.get("type") or "TV", "TV"),
        "seasonYear": sy, "startDate": {"year": sy}, "endDate": {"year": ey},
        "popularity": d.get("members") or 0, "countryOfOrigin": "JP",
        "title": {"romaji": d.get("title"), "english": d.get("title_english"), "native": d.get("title_japanese")},
        "synonyms": synonyms, "episodes": d.get("episodes"), "duration": _jikan_minutes(d.get("duration")),
        "status": d.get("status"), "isAdult": str(d.get("rating") or "").startswith("Rx"),
        "genres": [g.get("name") for g in (d.get("genres") or []) if isinstance(g, dict)],
    }


async def _jikan_search(session, query: str):
    """List of AniList-shaped media ([] = nothing found) or _TMDB_ERROR. Never raises. One request at
    a time, JIKAN_MIN_INTERVAL apart; 429 -> Retry-After; 8 s timeout."""
    global _jikan_lock, _jikan_last_call, _jikan_down_until
    if aiohttp is None or session is None:
        return _TMDB_ERROR
    ck = _norm_cmp(query)
    hit = _JIKAN_CACHE.get(ck)
    if hit is not None:
        _JIKAN_CACHE.move_to_end(ck)
        return hit
    loop = asyncio.get_event_loop()
    if loop.time() < _jikan_down_until:
        return _TMDB_ERROR
    if _jikan_lock is None:
        _jikan_lock = asyncio.Lock()
    for attempt in range(JIKAN_RETRIES):
        async with _jikan_lock:
            wait = JIKAN_MIN_INTERVAL - (loop.time() - _jikan_last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with session.get(
                    JIKAN_URL, params={"q": query, "limit": 5},
                    timeout=aiohttp.ClientTimeout(total=JIKAN_TIMEOUT_SECONDS),
                ) as resp:
                    _jikan_last_call = loop.time()
                    if resp.status == 429:
                        try:
                            retry = int(resp.headers.get("Retry-After", 3))
                        except (TypeError, ValueError):
                            retry = 3
                        await asyncio.sleep(min(max(retry, 1), 30))
                        continue
                    if resp.status >= 500:
                        await asyncio.sleep(1 + attempt)
                        continue
                    if resp.status != 200:
                        break
                    data = await resp.json()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Jikan lookup failed for %r: %s", query, e)
                await asyncio.sleep(1 + attempt)
                continue
        rows = data.get("data") if isinstance(data, dict) else None
        results = [m for m in (_jikan_media(d) for d in (rows or [])) if m]
        _JIKAN_CACHE[ck] = results
        if len(_JIKAN_CACHE) > _CACHE_MAX:
            _JIKAN_CACHE.popitem(last=False)
        return results
    _jikan_down_until = loop.time() + JIKAN_DOWN_SECONDS
    return _TMDB_ERROR


# ---- Fribb/anime-lists: a TMDB id in this list is anime (deterministic second opinion) -------------
# https://github.com/Fribb/anime-lists  anime-list-full.json. Verified against the repository README:
# each entry has "type" (TV / MOVIE / OVA ...), "anilist_id", "mal_id", "anidb_id", "tvdb_id", "season"
# and "themoviedb_id" as an OBJECT: {"tv": 44298} or {"movie": [128]} (tv = one id, movie = a list).
# TMDB ids are NOT unique across movie and TV (tv/25 is Star Wars Droids, movie/25 is Jarhead), so the
# table is keyed (media, id), never by the bare id. An entry typed MOVIE can sit under "tv" (hack//G.U.
# Trilogy), so the object's own key decides the media, not "type".
ANIME_LIST_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
ANIME_LIST_TIMEOUT_SECONDS = 60
ANIME_LIST_RETRY_SECONDS = 1800      # after a failed download try again after this long (not on every file)
_ANIME_LIST = None                   # {(media, tmdb id): (anilist_id, mal_id, type)} once loaded
_anime_list_lock = None
_anime_list_retry_at = 0.0


def _anime_list_tmdb_ids(entry: dict) -> list:
    """[("tv" | "movie", id), ...] of one list entry; tolerates a bare id / list (older shapes)."""
    tm = entry.get("themoviedb_id")
    out = []
    if isinstance(tm, dict):
        for media in ("tv", "movie"):
            v = tm.get(media)
            for x in (v if isinstance(v, (list, tuple)) else [v]):
                i = _as_int(x)
                if i:
                    out.append((media, i))
    elif tm not in (None, "", []):
        media = "movie" if str(entry.get("type") or "").upper() == "MOVIE" else "tv"
        for x in (tm if isinstance(tm, (list, tuple)) else [tm]):
            i = _as_int(x)
            if i:
                out.append((media, i))
    return out


def _anime_list_build(raw) -> dict:
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("anime-list-full.json is not a list")
    table, entries = {}, 0
    for e in data:
        if not isinstance(e, dict):
            continue
        entries += 1
        ids = _anime_list_tmdb_ids(e)
        if not ids:
            continue
        val = (_as_int(e.get("anilist_id")), _as_int(e.get("mal_id")), str(e.get("type") or "").upper())
        for k in ids:
            table.setdefault(k, val)
    logger.info("anime-list: %d entries, %d TMDB ids (tv %d / movie %d)", entries, len(table),
                sum(1 for k in table if k[0] == "tv"), sum(1 for k in table if k[0] == "movie"))
    if entries and not table:
        logger.warning("anime-list: no usable themoviedb_id in %d entries (first entry keys: %s) - "
                       "the file format may have changed", entries,
                       sorted(data[0].keys()) if isinstance(data[0], dict) else "?")
    return table


async def _anime_list_load(session):
    """The table, or None when the source is switched off / unavailable. Loaded lazily, once per
    process; a failed download is retried after ANIME_LIST_RETRY_SECONDS. Never raises."""
    global _ANIME_LIST, _anime_list_lock, _anime_list_retry_at
    if _ANIME_LIST is not None:
        return _ANIME_LIST
    if not USE_ANIME_LISTS or aiohttp is None or session is None:
        return None
    loop = asyncio.get_event_loop()
    if loop.time() < _anime_list_retry_at:
        return None
    if _anime_list_lock is None:
        _anime_list_lock = asyncio.Lock()
    async with _anime_list_lock:
        if _ANIME_LIST is not None:
            return _ANIME_LIST
        if loop.time() < _anime_list_retry_at:
            return None
        try:
            async with session.get(
                ANIME_LIST_URL, timeout=aiohttp.ClientTimeout(total=ANIME_LIST_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                raw = await resp.read()
            _ANIME_LIST = await loop.run_in_executor(None, _anime_list_build, raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("anime-list unavailable (%s) - not used; retry in %d min", e, ANIME_LIST_RETRY_SECONDS // 60)
            _anime_list_retry_at = loop.time() + ANIME_LIST_RETRY_SECONDS
            return None
    return _ANIME_LIST


async def _anime_list_lookup(session, media_type: str, tmdb_id):
    """(anilist_id, mal_id, type) when this TMDB id (media_type "tv" | "movie") is in the list, else None
    (also None while the list is unavailable)."""
    table = await _anime_list_load(session)
    i = _as_int(tmdb_id)
    if not table or not i:
        return None
    return table.get((media_type, i))


# ---- manami-project/anime-offline-database: an offline anime authority (USE_ANIME_OFFLINE_DB) -----------------------------
# https://github.com/manami-project/anime-offline-database  - dataset files (README, checked): anime-offline-database.jsonl
# (JSON lines, the FIRST line is metadata, every other line is one anime), anime-offline-database-minified.json, and .zst
# versions; "There is a latest release". Entry fields: sources (URLs: anilist.co/anime/<id>, myanimelist.net/anime/<id> ...),
# title, type (TV | MOVIE | OVA | ONA | SPECIAL | UNKNOWN), episodes, status, animeSeason {season, year}, synonyms ...
# NOTE: the repository was archived by its owner on 4 Jul 2026 (read-only): the last release still downloads but no longer
# gets updates. Loaded lazily, ONCE, in a background task with its own HTTP session - a file never waits for it: until the
# index is ready (or when the download failed) the source simply says nothing. Only a compact index is kept
# {title key -> [(type, year, anilist id, mal id, title, episodes)]}; the big download is dropped. It is asked only when
# AniList is OFF or FAILED (an AniList "not found" is respected), and it never raises. A hit needs the EXACT title key
# (punctuation / spacing ignored) AND the file's year (+-1); a name with no year is only asked for an anime CANDIDATE
# (fansub tag / known group ...). With ANILIST_ANY_ORIGIN=0 only a strong anime candidate may use it (the dataset has no
# country of origin).
ANIME_OFFLINE_DB_URLS = tuple(u.strip() for u in os.environ.get(
    "ANIME_OFFLINE_DB_URL",
    "https://github.com/manami-project/anime-offline-database/releases/latest/download/anime-offline-database.jsonl,"
    "https://github.com/manami-project/anime-offline-database/releases/latest/download/anime-offline-database-minified.json",
).split(",") if u.strip())
ANIME_OFFLINE_DB_TIMEOUT_SECONDS = 180
ANIME_OFFLINE_DB_RETRY_SECONDS = 3600
_OFFLINE_DB = None                    # {title key: [(type, year, anilist_id, mal_id, title, episodes), ...]} once loaded
_offline_db_task = None
_offline_db_retry_at = 0.0
_OFFLINE_TYPES = {"TV", "MOVIE", "OVA", "ONA", "SPECIAL"}
_OFFLINE_AID_RE = re.compile(r"anilist\.co/anime/(\d+)")
_OFFLINE_MID_RE = re.compile(r"myanimelist\.net/anime/(\d+)")


def _offline_db_build(raw) -> dict:
    """The compact index of the dataset (JSONL, or the minified JSON). Raises on an unusable download (the caller logs it)."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("data"), list):
            entries.extend(x for x in obj["data"] if isinstance(x, dict))     # the minified .json: one line, {"data": [...]}
        elif isinstance(obj, dict) and obj.get("title"):
            entries.append(obj)                                               # (the metadata line has no title)
    idx, count = {}, 0
    for e in entries:
        etype = str(e.get("type") or "").upper()
        title = e.get("title")
        if etype not in _OFFLINE_TYPES or not isinstance(title, str) or not title:
            continue
        season = e.get("animeSeason") if isinstance(e.get("animeSeason"), dict) else {}
        year = _as_int(season.get("year")) or None
        aid = mid = None
        for src in e.get("sources") or []:
            if isinstance(src, str):
                ma, mm = _OFFLINE_AID_RE.search(src), _OFFLINE_MID_RE.search(src)
                aid = aid or (int(ma.group(1)) if ma else None)
                mid = mid or (int(mm.group(1)) if mm else None)
        val = (etype, year, aid, mid, title, _as_int(e.get("episodes")) or None)
        count += 1
        seen = set()
        for name in [title, *[s for s in (e.get("synonyms") or []) if isinstance(s, str)][:30]]:
            key = _title_key(name)
            if len(key) < 3 or key in seen:
                continue
            seen.add(key)
            rows = idx.setdefault(key, [])
            if len(rows) < 12:
                rows.append(val)
    if not idx:
        raise ValueError("no usable entries in the anime-offline-database download")
    logger.info("anime-offline-database: %d entries, %d title keys", count, len(idx))
    return idx


async def _offline_db_run() -> None:
    """Background task: download + index the dataset once. Never raises; a failure is retried after an hour."""
    global _OFFLINE_DB, _offline_db_retry_at
    loop = asyncio.get_event_loop()
    try:
        raw = None
        async with aiohttp.ClientSession() as own:
            for url in ANIME_OFFLINE_DB_URLS:
                try:
                    async with own.get(url, timeout=aiohttp.ClientTimeout(total=ANIME_OFFLINE_DB_TIMEOUT_SECONDS)) as resp:
                        if resp.status != 200:
                            continue
                        raw = await resp.read()
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("anime-offline-database download %s failed: %s", url, e)
        if raw is None:
            raise RuntimeError("no download URL answered")
        _OFFLINE_DB = await loop.run_in_executor(None, _offline_db_build, raw)
        raw = None                                                # the big download is dropped, only the index stays
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("anime-offline-database unavailable (%s) - not used; retry in %d min", e,
                       ANIME_OFFLINE_DB_RETRY_SECONDS // 60)
        try:
            _offline_db_retry_at = loop.time() + ANIME_OFFLINE_DB_RETRY_SECONDS
        except Exception:
            pass


@_new_rule(lambda *a, **k: None)
def _offline_db_kick() -> None:
    """Start the background load once (when it is on, not loaded, not already running, not waiting for a retry).
    Never blocks and never raises."""
    global _offline_db_task
    try:
        if not USE_ANIME_OFFLINE_DB or aiohttp is None or _OFFLINE_DB is not None:
            return
        loop = asyncio.get_event_loop()
        if (_offline_db_task is not None and not _offline_db_task.done()) or loop.time() < _offline_db_retry_at:
            return
        _offline_db_task = loop.create_task(_offline_db_run())
    except Exception as e:
        logger.debug("anime-offline-database kick failed: %s", e)


@_new_rule(lambda *a, **k: None)
def _offline_anime_lookup(p: dict, titles, ap: dict):
    """An AniList-shaped media dict for the entry of the anime-offline-database that IS this file, or None (not loaded, not
    listed, year disagrees, ...). Synchronous - it only reads the index. Never raises."""
    try:
        idx = _OFFLINE_DB
        if not USE_ANIME_OFFLINE_DB or not idx:
            return None
        if not ANILIST_ANY_ORIGIN and not (ap or {}).get("strong"):
            return None
        year = p.get("year") or (ap or {}).get("year")
        candidate = bool((ap or {}).get("candidate"))
        if not year and not candidate:
            return None                                           # a bare title could be any same-named show
        has_ep = bool((ap or {}).get("has_episode") or p.get("strong_episode") or p.get("is_episode"))
        best, best_key = None, None
        for t in titles:
            keys = {_title_key(t), _title_key(_strip_season_words(t)[0] or t)}
            for key in keys:
                for row in idx.get(key) or ():
                    etype, ey = row[0], row[1]
                    if year and not (ey and abs(ey - year) <= 1):
                        continue
                    pref = 1 if ((etype != "MOVIE") == has_ep) else 0
                    k = (pref, -(abs(ey - year) if (ey and year) else 0), -(ey or 9999))
                    if best is None or k > best_key:
                        best, best_key = row, k
        if best is None:
            return None
        etype, ey, aid, mid, title, eps = best
        return {"id": aid, "idMal": mid, "format": etype, "seasonYear": ey, "startDate": {"year": ey}, "endDate": {},
                "popularity": 0, "countryOfOrigin": None, "title": {"romaji": title, "english": None, "native": None},
                "synonyms": [], "episodes": eps, "duration": None}
    except Exception as e:
        logger.warning("anime-offline-database lookup failed for %r: %s - ignored", titles, e)
        return None


# ---- lookup (AniList first, Jikan when AniList failed), cache, first pass --------------------------------
_ANIME_CACHE: "OrderedDict" = OrderedDict()      # (title without season markers, year | None, strict) -> verdict dict


def _anime_note(info, **kw) -> dict:
    """info["anime"] (what handlers/autosort.py logs); a throw-away dict when info is None."""
    a = info.setdefault("anime", {}) if info is not None else {}
    a.update(kw)
    return a


def _anime_media_fields(media: dict, source: str = "anilist") -> dict:
    """What handlers/autosort.py logs about the matched entry. A Jikan entry's id IS its MyAnimeList id
    (there is no AniList id then), and MyAnimeList has no country of origin."""
    t = (media or {}).get("title") or {}
    jikan = source == "jikan"
    return {"anilist_id": None if jikan else media.get("id"), "mal_id": media.get("id") if jikan else media.get("idMal"),
            "format": media.get("format"), "origin": None if jikan else media.get("countryOfOrigin"),
            "matched": t.get("romaji") or t.get("english") or t.get("native"),
            # IDENT_RECORD: the canonical names / year of the entry (the caption is rebuilt from them)
            "title_en": t.get("english"), "title_romaji": t.get("romaji"),
            "media_year": media.get("seasonYear") or ((media.get("startDate") or {}).get("year"))}


async def _anime_lookup_core(session, p: dict, anilist_key: str, use_anilist: bool, minutes=None) -> dict:
    """AniList (then Jikan, only when AniList FAILED) for one parsed name, with the anime verdict cache.
    -> {"status": "found" | "none" | "error" | "off", "source": "anilist" | "jikan" | "-",
        "media": dict | None, "d": details, "cached": bool}
    "none" = the source answered and did not know it; "error" = it could not answer. Errors are never
    cached; "none" and "found" are, per title (season markers stripped) + year, so the hundreds of
    episodes of one show cost one to three requests."""
    off = {"status": "off", "source": "-", "media": None, "d": {}, "cached": False}
    ap = p.get("anime") or {}
    titles = [t for t in (ap.get("titles") or p.get("titles") or []) if t and not _generic_title(t)][:(4 if TITLE_ALT_SPLIT else 2)]

    def _offline(why: str):
        """The anime-offline-database's answer when AniList is OFF / FAILED, else None (see _offline_anime_lookup)."""
        media = _offline_anime_lookup(p, titles, ap)
        if media is None:
            return None
        return {"status": "found", "source": "offline", "media": media, "d": {"offline": why}, "cached": False}

    if not titles:
        return off
    if not use_anilist:
        _offline_db_kick()
        return _offline("AniList is off") or off
    # (AniList is on: the offline database is only the last resort when AniList and Jikan both FAILED, below)
    year = p.get("year") or ap.get("year")
    base = _strip_season_words(_norm_cmp(titles[0]))[0] or _norm_cmp(titles[0])
    # strict (exact title + popularity) for a name with no release signal AND for a SOFT anime candidate: naming alone
    # ("Episode 25" + "Dubbed", an episode number without a season) is never proof, an obscure same-name entry is not one
    weak = bool(p.get("weak", False)) or bool(ap.get("soft"))
    ck = (base, year or None, weak)       # a strict "none" must not answer a fansub-tagged name (and the other way round)
    hit = _ANIME_CACHE.get(ck)
    if hit is not None:
        _ANIME_CACHE.move_to_end(ck)
        return {"status": hit["status"], "source": hit["source"], "media": hit.get("media"), "d": hit.get("d") or {},
                "cached": True}

    ctx = {"episode": bool(ap.get("has_episode") or p.get("strong_episode") or p.get("is_episode")),
           "season": ap.get("season") or p.get("season"), "minutes": minutes,
           "any_origin": ANILIST_ANY_ORIGIN}
    queries = [_norm_cmp(t) for t in titles]
    search_titles = list(titles)
    if TITLE_SPACE_VARIANTS:                                  # "House Mates" -> "Housemates" for the AniList search
        for v in _space_variants(titles[0])[:1]:
            if _norm_cmp(v) not in queries and len(search_titles) < 4:
                search_titles.append(v)
                queries.append(_norm_cmp(v))

    errored = False
    for title in search_titles:
        results = await _anilist_search(session, title, anilist_key)
        if results is _TMDB_ERROR:
            errored = True
            continue
        match, d = _anilist_best_match(results, queries, year, weak, deep=ctx)
        if match:
            rec = {"status": "found", "source": "anilist", "media": match, "d": d}
            _anime_cache_put(ck, rec)
            return dict(rec, cached=False)
    if not errored:
        rec = {"status": "none", "source": "anilist", "media": None, "d": {}}
        _anime_cache_put(ck, rec)
        return dict(rec, cached=False)

    # AniList FAILED (not "not found"): Jikan answers instead - never as an extra vote
    _offline_db_kick()
    if not USE_JIKAN:
        return _offline("AniList failed") or {"status": "error", "source": "-", "media": None, "d": {}, "cached": False}
    j_errored = False
    for title in titles:
        results = await _jikan_search(session, title)
        if results is _TMDB_ERROR:
            j_errored = True
            continue
        match, d = _anilist_best_match(results, queries, year, weak, deep=ctx)
        if match:
            rec = {"status": "found", "source": "jikan", "media": match, "d": d}
            _anime_cache_put(ck, rec)
            return dict(rec, cached=False)
    # Jikan answered "not found" while AniList is unknown: the offline database may know it; else report, do not cache
    return _offline("AniList failed") or {"status": "error" if j_errored else "none", "source": "jikan", "media": None,
                                           "d": {}, "cached": False}


def _anime_cache_put(key, rec: dict) -> None:
    _ANIME_CACHE[key] = rec
    _ANIME_CACHE.move_to_end(key)
    if len(_ANIME_CACHE) > _CACHE_MAX:
        _ANIME_CACHE.popitem(last=False)


def _anime_episode_conflict(p: dict, media: dict):
    """An anime authority's entry must FIT the file. A file that is episode 25 cannot be an entry with two episodes:
    "FORBIDDEN LOVE | Episode 25" (a Hindi serial dubbed in English) is not the 2003 OVA "Forbidden Love" that only shares
    its title. Only a gross mismatch counts (the episode number is more than twice the entry's episode count + 10), so
    absolute numbering across seasons and split entries still fits; an unknown count (a show still airing) never
    conflicts, and a fansub-tagged / known-anime-group name (a STRONG candidate) is not asked. Returns a reason or None."""
    ap = p.get("anime") or {}
    if ap.get("strong"):
        return None
    n, eps = p.get("episode") or ap.get("episode"), media.get("episodes")
    if isinstance(n, bool) or not isinstance(n, int) or isinstance(eps, bool) or not isinstance(eps, int) or eps <= 0:
        return None
    if n <= eps * 2 + 10:
        return None
    return f"the file is episode {n} but that entry has only {eps} episode{'s' if eps != 1 else ''}"


async def _anime_lookup(session, p: dict, anilist_key: str, use_anilist: bool, minutes=None) -> dict:
    """_anime_lookup_core() plus the per-file fit check: a "found" entry that cannot be this file
    (_anime_episode_conflict) becomes status "episode-skip" - nothing found, the reason in d["skip"]. The check runs
    here, after the per-show verdict cache, because it depends on the file's episode number and not on the show."""
    res = await _anime_lookup_core(session, p, anilist_key, use_anilist, minutes)
    if res["status"] == "found":
        why = _anime_episode_conflict(p, res["media"])
        if why:
            return {"status": "episode-skip", "source": res["source"], "media": None, "d": {"skip": why},
                    "cached": res["cached"]}
    return res


def _anime_year_conflict(p: dict, media: dict):
    """AniList-FIRST guard against a live-action sibling of a long-running anime. A TV entry that is
    still running contains every later year, so "One Piece (2023)" (the Netflix series) would match the
    1999 anime. When the file carries a year that is NOT the show's first year (+-1), AniList does not
    decide first; TMDB (which knows the live-action show by year) gets the file. Returns a reason or None."""
    year = p.get("year") or (p.get("anime") or {}).get("year")
    if not year or media.get("format") == "MOVIE":
        return None
    sy = (media.get("startDate") or {}).get("year") or media.get("seasonYear")
    if sy and year > sy + 1:
        return f"file year {year} is not the first year ({sy}) of that entry"
    return None


async def _live_action_twin(session, tmdb_key: str, p: dict, media: dict):
    """deep_anime rule "a live-action match is never anime", for AniList-FIRST (which answers before TMDB ever
    sees the file): the text "tmdb tv 123 'Alice in Borderland' is live-action" when TMDB has a title that is
    EXACTLY the file's title (year +-1 when the file has one) and every such title is live-action (genres known,
    none is Animation). A TMDB twin that IS animated keeps AniList's verdict, and so does anything unknown
    (no key, TMDB failed, genres not filled in yet): nothing is guessed. Never raises."""
    try:
        if not tmdb_key:
            return None
        ap = p.get("anime") or {}
        title = ap.get("title") or p.get("clean_title") or (p.get("titles") or [""])[0]
        key = _norm_cmp(title)
        if not key:
            return None
        year = p.get("year") or ap.get("year")
        mt = "movie" if media.get("format") == "MOVIE" else "tv"
        name_keys = ("title", "original_title") if mt == "movie" else ("name", "original_name")
        results = await _tmdb_search(session, mt, title, tmdb_key)
        if results is _TMDB_ERROR:
            return None
        live = None
        for r in results or []:
            names = [r.get(k) for k in name_keys if r.get(k)]
            if not any(_title_score(key, _norm_cmp(n)) >= 1.0 for n in names):
                continue
            ry = _year_of(r.get("release_date" if mt == "movie" else "first_air_date"))
            if year and ry and abs(ry - year) > 1:
                continue
            genres = r.get("genre_ids") or []
            if not genres or TMDB_ANIMATION_GENRE_ID in genres:
                return None
            live = live or f"tmdb {mt} {r.get('id')} {names[0]!r} is live-action"
        return live
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug("live-action check failed for %r: %s", p.get("clean_title"), e)
        return None


async def _anime_first_pass(session, parsed, use_anilist: bool, anilist_key: str, minutes, info: dict,
                            tmdb_key: str = "") -> bool:
    """AniList FIRST for anime candidates. True = an Asian-origin AniList / Jikan match was found and
    info["anime"] is filled; False = ask TMDB as usual (info["anime"]["anilist"] says what AniList said).
    tmdb_key: with a key, an AniList match is dropped when TMDB knows the same title only as live-action."""
    for p in parsed:
        ap = p.get("anime")
        if not ap or not ap.get("candidate") or p.get("extra"):
            continue
        res = await _anime_lookup(session, p, anilist_key, use_anilist, minutes)
        a = _anime_note(info, anilist=res["status"])
        if res["status"] == "episode-skip":
            a.setdefault("notes", []).append("anilist-first skipped: " + res["d"]["skip"])
        if res["status"] != "found":
            continue
        why = _anime_year_conflict(p, res["media"])
        if why:
            a["anilist"] = "year-skip"
            a.setdefault("notes", []).append("anilist-first skipped: " + why)
            continue
        live = await _live_action_twin(session, tmdb_key, p, res["media"])
        if live:
            a["anilist"] = "live-action-skip"
            a.setdefault("notes", []).append("anilist-first skipped: " + live)
            continue
        _anime_note(info, source=res["source"], verdict="anime", **_anime_media_fields(res["media"], res["source"]))
        if res["d"].get("dur"):
            a.setdefault("notes", []).append(res["d"]["dur"])
        if res["d"].get("foreign"):
            a.setdefault("notes", []).append(
                f"any-origin: AniList lists it (country {res['d']['foreign']}): exact title, year agrees, "
                f"TMDB does not say live-action")
        if res["d"].get("offline"):
            a.setdefault("notes", []).append(f"anime-offline-database: exact title + year ({res['d']['offline']})")
        if res["cached"]:
            a.setdefault("notes", []).append("cached verdict")
        return True
    return False


# ---- TMDB side of the anime verdict -----------------------------------------------------------------------
def _tmdb_asian_origin(result: dict):
    """"original_language=ja" / "origin_country=JP" when a TMDB result has an Asian origin, else None."""
    lang = result.get("original_language")
    if lang in _ANIME_ORIGIN_LANGUAGES:
        return f"original_language={lang}"
    for c in result.get("origin_country") or []:
        if c in ANILIST_ORIGINS:
            return f"origin_country={c}"
    return None


async def _tmdb_anime_confirm(session, match: dict, media_type: str, mid, animated: bool):
    """deep_anime rule for a TMDB match that is not (Animation + ja / zh / ko): anime when it is Animation
    with an Asian origin_country, or its id is in the anime list. TMDB keywords never decide.
    A live-action match (genres known, no Animation) is never anime. The list also counts when TMDB has
    no genres for the title yet.
    -> (is_anime, source, why, list_state) with list_state "hit" | "miss" | "unavailable" | None (not asked)"""
    if animated:
        origin = _tmdb_asian_origin(match)
        if origin:
            return True, "tmdb", f"animation + {origin}", None
    if mid and (animated or not (match.get("genre_ids") or [])):
        hit = await _anime_list_lookup(session, media_type, mid)
        if hit:
            return True, "animelist", (f"tmdb {media_type} {mid} is in the anime list "
                                       f"(anilist {hit[0] or '-'}, mal {hit[1] or '-'})"), "hit"
        return False, None, "", ("unavailable" if _ANIME_LIST is None else "miss")
    return False, None, "", None


def _anime_tmdb_fields(info, animated: bool, lang, origin, source=None, why=None, animelist=None) -> None:
    """What TMDB said, for the log line (and for _anime_downgrade_note)."""
    a = _anime_note(info, tmdb_animated=bool(animated), tmdb_lang=lang, tmdb_origin=origin or None)
    if source:
        a["source"], a["verdict"] = source, "anime"
    if why:
        a.setdefault("notes", []).append(why)
    if animelist:
        a["animelist"] = animelist


async def _anime_downgrade_note(session, tmdb_key: str, info: dict) -> None:
    """The OLD rule (TMDB keywords such as japan / samurai / edo period) would have called this animated
    TMDB title anime; the new rules did not. Say so in info["anime"]["downgraded"] (the log line explains
    why a title like Blue Eye Samurai is now a series). Log-only: the verdict is already made."""
    a = (info or {}).get("anime") or {}
    media, mid = info.get("tmdb_media"), info.get("tmdb_id")
    if not a.get("tmdb_animated") or not media or not mid or a.get("verdict") == "anime":
        return
    kws = await _tmdb_keywords(session, media, mid, tmdb_key)
    if kws is _TMDB_ERROR:
        return
    words = _ANIME_MOVIE_KEYWORDS if media == "movie" else _ANIME_KEYWORDS
    said = sorted({(k.get("name") or "").strip().lower() for k in kws or []
                   if (k.get("name") or "").strip().lower() in words or k.get("id") in _ANIME_KEYWORD_IDS})
    if said:
        a["downgraded"] = (f"tmdb {media} {mid} is animated (lang={a.get('tmdb_lang')}, origin={a.get('tmdb_origin') or '-'}) "
                           f"and its keywords say anime ({', '.join(said)}), but it has no Asian origin, is not in the "
                           f"anime list ({a.get('animelist') or 'not checked'}) and AniList says {a.get('anilist') or '-'}")
