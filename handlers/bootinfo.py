"""Text shown under the 'Forwarded Status' box after a bot restart:
which message number the task continues from, which message was forwarded
last, and which /settings -> Filters (audio / video / photo ...) are active.
Pure python (no pyrogram / database imports) so it is easy to test."""

_NAMES = {
    "text": "Text", "photo": "Photo", "video": "Video", "audio": "Audio", "voice": "Voice",
    "document": "Document", "animation": "GIF", "sticker": "Sticker", "poll": "Poll",
}


def filter_text(filter_types):
    """filter_types: list of allowed message types, or None / [] = nothing filtered."""
    if not filter_types:
        return "all types"
    return "only " + ", ".join(_NAMES.get(t, t.title()) for t in filter_types)


def build_restart_info(job, snap=None):
    """job: the saved job document (core/database.py). Returns an HTML string."""
    snap = snap or job.get("snap") or {}
    last_id = int(job.get("last_id") or 0)
    last_fwd = int(job.get("last_fwd_id") or 0)
    filtered = int(snap.get("filtered") or 0)
    return (
        "\n\n<b>♻️ ʙᴏᴛ ʀᴇsᴛᴀʀᴛᴇᴅ - ᴛᴀsᴋ ᴡɪʟʟ ᴄᴏɴᴛɪɴᴜᴇ</b>"
        f"\n<b>▶️ ᴄᴏɴᴛɪɴᴜᴇ ꜰʀᴏᴍ ᴍsɢ ɴᴏ :</b> <code>{last_id + 1}</code>"
        f"\n<b>✅ ʟᴀsᴛ ꜰᴡᴅ ᴍsɢ ɴᴏ :</b> <code>{last_fwd if last_fwd else '-'}</code>"
        f"\n<b>🎯 ғɪʟᴛᴇʀ :</b> <code>{filter_text(job.get('filter_types'))}</code>"
        f"\n<b>🚫 ғɪʟᴛᴇʀᴇᴅ ᴏᴜᴛ :</b> <code>{filtered}</code>"
    )
