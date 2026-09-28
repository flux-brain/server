"""One Google Drive file as the vault sees it (2026-09-28): the details line and, on request, its text file.

Shared by the relay (a link in a Discord message, `[drive] links`) and the Drive watch module (a new file in a watched
folder), so both write the same `## Links` line and the same `raw/attachments/` text file. The callers pass in how to
fetch the bytes and how to convert them, which keeps the tests' stubs where they were.
"""
import re

from .extract import attachment_text_file
from .links import EXPORTS, kind_label

__all__ = ["details_line", "text_file", "NO_TEXT_TYPES"]

NO_TEXT_TYPES = ("application/vnd.google-apps.folder",)


def details_line(meta, url):
    """`- [name](link) (type, folder "X", last edited ... by ..., stays in Google Drive)` for a Drive metadata dict
    (lib.drive.Drive.metadata). Brackets in the name are swapped for parentheses, else they break the Markdown link."""
    name = (meta.get("name") or meta.get("id") or "file").replace("[", "(").replace("]", ")")
    details = [kind_label(meta.get("mimeType", ""))]
    if meta.get("folder"):
        details.append(f"folder \"{meta['folder']}\"")
    if meta.get("modifiedTime"):
        who = (meta.get("lastModifyingUser") or {}).get("displayName")
        details.append(f"last edited {meta['modifiedTime'][:16].replace('T', ' ')} UTC" + (f" by {who}" if who else ""))
    return f"- [{name}]({meta.get('webViewLink') or url}) ({', '.join(details)}, stays in Google Drive)"


def text_file(meta, file_bytes, extract, max_bytes, path_prefix, message_id, captured, origin):
    """The text of one Drive file, ready to store: (path, body, kb, suffix) where `suffix` is what the details line
    gets (`, text: [[path|name (text)]]`), or (None, None, 0, suffix) when there is no text to store (suffix says why).

    `file_bytes(file_id, export_mime_or_None)` fetches the bytes (an export for Google-native types, the stored file
    otherwise) and RAISES on failure: the caller decides whether that is fatal. `extract(data, mime, name)` is the
    attachment converter (lib.extract.extract_text or a test stub)."""
    mime = meta.get("mimeType", "")
    name = (meta.get("name") or meta.get("id") or "file").replace("[", "(").replace("]", ")")
    if mime in NO_TEXT_TYPES:
        return None, None, 0, ""
    export_mime, method = EXPORTS.get(mime, (None, None))
    if mime.startswith("application/vnd.google-apps.") and not export_mime:
        return None, None, 0, ", no text (type not converted)"
    size = int(meta.get("size") or 0)
    if size > max_bytes:
        return None, None, 0, f", no text (over {max_bytes // (1024 * 1024)} MB)"
    data = file_bytes(meta["id"], export_mime)
    if export_mime == "text/plain":
        text = data.decode("utf-8-sig", "replace")
    else:   # xlsx of a Sheet, or a stored file (PDF, Word...): the attachment converters
        text, method = extract(data, export_mime or mime, name)
    if not method:
        return None, None, 0, ", no text (type not converted)"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80]
    path = f"{path_prefix}-{safe}.md"
    kb = max(1, (size or len(data)) // 1024)
    body = attachment_text_file(name, meta.get("webViewLink") or "", mime, kb, method, text, message_id, captured,
                                origin=origin)
    return path, body, kb, f", text: [[{path}|{name} (text)]]"
