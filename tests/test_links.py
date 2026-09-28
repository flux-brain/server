"""Offline tests for Drive links in a message ([drive] links, 2026-09-28): which URLs count, the `## Links` line with
the file's details, `+text` writing the text file, a file the token cannot read, a failed text export (strict, then
lenient), and the default "off". No network: Discord, Drive and GitHub are fakes."""
import pytest
import requests

import flux_brain.relay as m
from flux_brain.lib import links as L
from fakes import Resp

DOC = "1TestDocIdAbCdEfGhIjKlMnOpQrStUv"
SHEET = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
DOC_URL = f"https://docs.google.com/document/d/{DOC}/edit?usp=drivesdk"


def msg(content, mid="200"):
    return {"id": mid, "author": {"bot": False}, "type": 0, "content": content,
            "timestamp": "2026-09-28T10:27:00.000000+00:00", "attachments": []}


# ---------- parsing ----------

def test_drive_links_finds_each_form_once_in_order():
    text = (f"see {DOC_URL} and https://drive.google.com/file/d/{SHEET}/view, again {DOC_URL}, "
            f"https://drive.google.com/open?usp=x&id={'F' * 25} and https://drive.google.com/drive/u/0/folders/{'D' * 30}")
    got = L.drive_links(text)
    assert [g[1] for g in got] == [DOC, SHEET, "F" * 25, "D" * 30]
    assert [g[2] for g in got] == [False, False, False, True]


def test_drive_links_ignores_other_urls_and_caps_the_count():
    assert L.drive_links("https://example.com/document/d/" + DOC) == []
    many = " ".join(f"https://docs.google.com/document/d/{c * 25}/edit" for c in "ABCDEFGH")
    assert len(L.drive_links(many)) == L.MAX_LINKS


def test_wants_text_is_a_whole_word():
    assert L.wants_text(f"{DOC_URL} +text") and L.wants_text("+TEXT please")
    assert not L.wants_text("a+text") and not L.wants_text("+texts") and not L.wants_text(DOC_URL)


# ---------- the relay ----------

META = {"id": DOC, "name": "Meeting notes [draft]", "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-09-28T06:52:21.934Z", "webViewLink": DOC_URL, "folder": "Projects",
        "lastModifyingUser": {"displayName": "Anna"}}


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setattr(m.state, "save_state", lambda st: None)
    monkeypatch.setattr(m.CFG, "drive_links", "details")

    def make(meta=META, lookup_error=None, export_error=None):
        r = m.Relay.__new__(m.Relay)
        r.state, r.dh, r.drive = {}, {}, object()   # any non-None drive: the wrappers below are stubbed
        r.discord = lambda method, path, **kw: Resp(200)
        r.puts, r.posts, r.exports = [], [], []
        r.put_file = lambda path, data, message: r.puts.append((path, data.decode()))
        r.post = lambda channel, content, **kw: r.posts.append(content)

        def metadata(fid):
            if lookup_error:
                raise lookup_error
            return dict(meta, id=fid)

        def file_bytes(fid, export_mime=None):
            r.exports.append((fid, export_mime))
            if export_error:
                raise export_error
            return "Anna: the fee is fixed".encode()
        r.drive_metadata, r.drive_file_bytes = metadata, file_bytes
        return r
    return make


def note_of(r):
    return [d for p, d in r.puts if p.startswith("inbox/")][0]


def test_details_line_without_text(relay):
    r = relay()
    assert r.file_message("c", msg(DOC_URL)) == 1
    note = note_of(r)
    assert "## Links" in note
    assert f"- [Meeting notes (draft)]({DOC_URL}) (Google Docs document, folder \"Projects\", last edited 2026-09-28 06:52 UTC by Anna" in note
    assert "stays in Google Drive)" in note and "text:" not in note
    assert r.exports == [] and len(r.puts) == 1   # no copy without +text


def test_plus_text_writes_the_text_file_and_links_it(relay):
    r = relay()
    r.file_message("c", msg(f"{DOC_URL} +text"))
    assert r.exports == [(DOC, "text/plain")]
    texts = [(p, d) for p, d in r.puts if p.startswith("raw/attachments/")]
    assert len(texts) == 1
    path, body = texts[0]
    assert path.startswith("raw/attachments/2026-09-28T1027Z-200-link-Meeting_notes__draft_") and path.endswith(".md")
    assert "Anna: the fee is fixed" in body and "Google Docs text export" in body
    assert "linked in the message" in body and "shared drive" not in body
    assert f"text: [[{path}|" in note_of(r)


def test_sheet_is_exported_as_xlsx_through_the_converters(relay, monkeypatch):
    seen = {}

    def fake_extract(data, mime, name):
        seen["mime"] = mime
        return "A1 B1", "Excel cells"
    monkeypatch.setattr(m.inbound, "extract_text", fake_extract)
    r = relay(meta=dict(META, mimeType="application/vnd.google-apps.spreadsheet"))
    r.file_message("c", msg(f"https://docs.google.com/spreadsheets/d/{SHEET}/edit +text"))
    assert r.exports == [(SHEET, L.XLSX)] and seen["mime"] == L.XLSX


def test_unreadable_file_keeps_the_link_and_never_blocks(relay):
    resp = requests.Response()
    resp.status_code = 404
    r = relay(lookup_error=requests.HTTPError(response=resp))
    assert r.file_message("c", msg(f"{DOC_URL} +text")) == 1
    assert "not accessible with the Drive token (HTTP 404); only the link is kept, no text" in note_of(r)


def test_failed_text_export_is_strict_then_lenient(relay):
    r = relay(export_error=requests.ConnectionError())
    with pytest.raises(requests.ConnectionError):
        r.file_message("c", msg(f"{DOC_URL} +text"))
    assert r.puts == []   # strict attempt: nothing filed, retried next tick
    r.file_message("c", msg(f"{DOC_URL} +text"), lenient=True)
    assert "text NOT fetched after 3 attempts (ConnectionError)" in note_of(r)


def test_folder_link_gives_details_only(relay):
    r = relay(meta=dict(META, name="Projects", mimeType="application/vnd.google-apps.folder"))
    r.file_message("c", msg(f"https://drive.google.com/drive/folders/{'D' * 30} +text"))
    assert "(Drive folder," in note_of(r) and r.exports == []


def test_default_off_and_module_off_leave_links_alone(relay, monkeypatch):
    r = relay()
    monkeypatch.setattr(m.CFG, "drive_links", "off")
    r.file_message("c", msg(DOC_URL))
    assert "## Links" not in note_of(r)
    r2 = relay()
    r2.drive = None   # Drive module off: nothing to look up with
    r2.file_message("c", msg(DOC_URL, mid="201"))
    assert "## Links" not in note_of(r2)


def test_minus_text_is_a_whole_word_and_wins():
    assert L.refuses_text("-text") and not L.refuses_text("pre-text") and not L.refuses_text("-texts")
    assert L.text_wanted("text", "a link") and not L.text_wanted("text", "a link -text")
    assert L.text_wanted("details", "+text") and not L.text_wanted("details", "a link")
    assert not L.text_wanted("details", "+text -text")


def test_text_mode_copies_by_default_and_minus_text_opts_out(relay, monkeypatch):
    monkeypatch.setattr(m.CFG, "drive_links", "text")
    r = relay()
    r.file_message("c", msg(DOC_URL))
    assert r.exports == [(DOC, "text/plain")] and "text: [[raw/attachments/" in note_of(r)
    r2 = relay()
    r2.file_message("c", msg(f"{DOC_URL} -text", mid="201"))
    assert r2.exports == [] and "## Links" in note_of(r2) and "text:" not in note_of(r2)
