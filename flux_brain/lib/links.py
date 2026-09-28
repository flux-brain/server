"""Google Drive links in a captured message (2026-09-28): which files a message points at, and whether the owner asked
for their text.

A link is only an address to the routine, which has no Google access by design, so a bare Docs link used to reach
the vault as a URL and nothing else. With `[drive] links = "details"` the relay looks each linked file up with the
Drive token and adds its name, type, folder and last edit to the capture (`## Links`); the file itself stays in Drive.
Its text is copied into the vault only when the message also carries the word `+text`, because a copy in the
repository syncs to every device and no longer follows later edits to the original.
"""
import re

__all__ = ["drive_links", "wants_text", "MAX_LINKS", "kind_label", "EXPORTS"]

MAX_LINKS = 5                      # links looked up per message; the rest stay plain URLs in the message text

_ID = r"([A-Za-z0-9_-]{20,})"
# (pattern, whether the id is a folder). Docs editors, Drive file pages and the old open?id= form all carry the id.
_PATTERNS = [
    (re.compile(r"https://docs\.google\.com/(?:document|spreadsheets|presentation)/d/" + _ID), False),
    (re.compile(r"https://drive\.google\.com/file/d/" + _ID), False),
    (re.compile(r"https://drive\.google\.com/open\?(?:[^\s#]*&)?id=" + _ID), False),
    (re.compile(r"https://drive\.google\.com/drive/(?:u/\d+/)?folders/" + _ID), True),
]
_PLUS_TEXT = re.compile(r"(?<!\S)\+text(?!\S)", re.I)

# Google-native types: how their text is exported. Sheets go out as xlsx, not csv: a csv export holds the FIRST sheet
# only, while the xlsx converter in lib.extract reads every sheet.
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", "Google Docs text export"),
    "application/vnd.google-apps.presentation": ("text/plain", "Google Slides text export"),
    "application/vnd.google-apps.spreadsheet": (XLSX, "Google Sheets export (xlsx, every sheet)"),
}
_LABELS = {
    "application/vnd.google-apps.document": "Google Docs document",
    "application/vnd.google-apps.spreadsheet": "Google Sheets spreadsheet",
    "application/vnd.google-apps.presentation": "Google Slides presentation",
    "application/vnd.google-apps.folder": "Drive folder",
    "application/vnd.google-apps.form": "Google Form",
}


def drive_links(text):
    """[(url, file_id, is_folder)] in order of appearance, each id once, at most MAX_LINKS."""
    found = []
    for pat, folder in _PATTERNS:
        for m in pat.finditer(text or ""):
            found.append((m.start(), m.group(0), m.group(1), folder))
    out, seen = [], set()
    for _, url, fid, folder in sorted(found):
        if fid not in seen:
            seen.add(fid)
            out.append((url, fid, folder))
    return out[:MAX_LINKS]


def wants_text(text):
    """True when the message carries the word `+text` (the owner asks for the linked files' text in the vault)."""
    return bool(_PLUS_TEXT.search(text or ""))


def kind_label(mime):
    """A readable type for the `## Links` line."""
    return _LABELS.get(mime) or mime or "file"
