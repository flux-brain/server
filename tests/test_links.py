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


# ---------- web pages ([capture] web_links, 2026-10-07) ----------
# No network: the relay's fetch_web wrapper is stubbed, and lib.web.fetch gets a fake `get` and a fake resolver.

from flux_brain.lib import web as W   # noqa: E402

PAGE_URL = "https://example.com/guides/tax-residence"
PAGE = (b"<html><head><title>Tax residence [2026] | Example</title><style>p{}</style></head><body>"
        b"<nav>Home Menu</nav><script>track()</script><h1>Residence</h1><p>You are resident from the day you arrive.</p>"
        b"<footer>Cookies</footer></body></html>")


def public(host, port, **kw):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def test_web_links_skips_drive_and_discord_hosts_and_trims_punctuation():
    text = (f"read {PAGE_URL}, then (https://example.org/a_(b)) and <https://example.net/x?y=1>. "
            f"{DOC_URL} https://cdn.discordapp.com/attachments/1/2/f.pdf {PAGE_URL}")
    assert L.web_links(text) == [PAGE_URL, "https://example.org/a_(b)", "https://example.net/x?y=1"]
    many = " ".join(f"https://example.com/{i}" for i in range(9))
    assert len(L.web_links(many)) == L.MAX_LINKS and L.web_links("no link, ftp://example.com/x") == []


@pytest.mark.parametrize("url,answer", [
    ("http://127.0.0.1/admin", "127.0.0.1"), ("https://intranet.example/", "10.0.0.7"),
    ("http://metadata.example/latest", "169.254.169.254"), ("https://six.example/", "::1"),
    ("https://mixed.example/", "192.168.1.5"),
])
def test_check_url_refuses_addresses_that_are_not_public(url, answer):
    resolve = lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", (answer, port))]  # noqa: E731
    with pytest.raises(W.Refused, match="not public"):
        W.check_url(url, resolve)


@pytest.mark.parametrize("url,reason", [
    ("ftp://example.com/x", "http"), ("https://example.com:9090/", "port"), ("https://user:pw@example.com/", "credentials"),
    ("https://example.com:notaport/", "port"),
])
def test_check_url_refuses_other_schemes_ports_and_credentials(url, reason):
    with pytest.raises(W.Refused, match=reason):
        W.check_url(url, public)


def test_check_url_refuses_a_host_that_does_not_resolve():
    def nowhere(host, port, **kw):
        raise OSError("no such host")
    with pytest.raises(W.Refused, match="resolve"):
        W.check_url(PAGE_URL, nowhere)
    W.check_url(PAGE_URL, public)   # the public one passes


class Stream(Resp):
    def iter_content(self, size):
        yield self.content

    def close(self):
        pass


def test_fetch_checks_every_redirect_hop():
    asked = []

    def get(url, **kw):
        asked.append(url)
        assert kw["allow_redirects"] is False and kw["stream"] is True
        if url == "https://short.example/abc":
            return Stream(302, headers={"Location": "http://localhost.example/secret"})
        return Stream(200, headers={"Content-Type": "text/html"}, content=PAGE)

    def resolve(host, port, **kw):
        return [(2, 1, 6, "", ("127.0.0.1" if host == "localhost.example" else "93.184.216.34", port))]
    with pytest.raises(W.Refused, match="not public"):
        W.fetch("https://short.example/abc", get=get, resolve=resolve)
    assert asked == ["https://short.example/abc"]   # the private hop was never requested


def test_fetch_follows_a_public_redirect_and_stops_at_the_size_limit():
    def get(url, **kw):
        if url.endswith("/old"):
            return Stream(301, headers={"Location": "/new"})
        return Stream(200, headers={"Content-Type": "text/html; charset=utf-8"}, content=PAGE)
    final, ctype, data = W.fetch("https://example.com/old", get=get, resolve=public)
    assert (final, ctype, data) == ("https://example.com/new", "text/html", PAGE)
    with pytest.raises(W.Refused, match="over"):
        W.fetch("https://example.com/new", max_bytes=10, get=get, resolve=public)
    loop = lambda url, **kw: Stream(302, headers={"Location": url + "x"})  # noqa: E731
    with pytest.raises(W.Refused, match="redirects"):
        W.fetch("https://example.com/a", get=loop, resolve=public)


def test_fetch_raises_on_an_http_error():
    with pytest.raises(requests.HTTPError):
        W.fetch(PAGE_URL, get=lambda url, **kw: Stream(403), resolve=public)


def test_page_text_keeps_the_readable_part_of_html():
    title, text, method = W.page_text(PAGE, "text/html", PAGE_URL, None)
    assert title == "Tax residence [2026] | Example" and method == "web page text"
    assert "resident from the day you arrive" in text and "Residence" in text
    assert "track()" not in text and "Menu" not in text and "Cookies" not in text


def test_page_text_sends_a_pdf_to_the_converters_with_a_pdf_name():
    seen = {}

    def extract(data, mime, name):
        seen.update(mime=mime, name=name)
        return "page one", "PDF text layer"
    assert W.page_text(b"%PDF", "application/pdf", "https://example.com/files/report", extract) == ("report.pdf", "page one", "PDF text layer")
    assert seen == {"mime": "application/pdf", "name": "report.pdf"}


@pytest.fixture
def web_relay(relay, monkeypatch):
    monkeypatch.setattr(m.CFG, "drive_links", "off")
    monkeypatch.setattr(m.CFG, "web_links", "text")

    def make(result=None, error=None):
        r = relay()
        r.fetched = []

        def fetch_web(url):
            r.fetched.append(url)
            if error:
                raise error
            return result or (url, "text/html", PAGE)
        r.fetch_web = fetch_web
        return r
    return make


def test_web_link_gets_a_links_line_and_a_text_file(web_relay):
    r = web_relay()
    assert r.file_message("c", msg(f"worth reading {PAGE_URL}")) == 1
    texts = [(p, d) for p, d in r.puts if p.startswith("raw/attachments/")]
    assert len(texts) == 1 and r.fetched == [PAGE_URL]
    path, body = texts[0]
    assert path.startswith("raw/attachments/2026-09-28T1027Z-200-link-example.com-Tax_residence") and path.endswith(".md")
    assert "resident from the day you arrive" in body and "web page text" in body and "may have changed since" in body
    assert "Untrusted document content" in body and f'original: "{PAGE_URL}"' in body
    note = note_of(r)
    # the title's brackets and pipe would break the Markdown link and the wiki-link alias
    assert f"- [Tax residence 2026 Example]({PAGE_URL}) (web page on example.com, fetched 2026-09-28 10:27 UTC, text: [[{path}|" in note


def test_web_link_that_fails_keeps_the_link_and_never_blocks(web_relay):
    for error, said in [(W.Refused("address is not public"), "address is not public"),
                        (requests.Timeout(), "timeout"), (ConnectionError("down"), "ConnectionError")]:
        r = web_relay(error=error)
        assert r.file_message("c", msg(PAGE_URL)) == 1   # strict attempt: still filed
        assert f"- {PAGE_URL}: not fetched ({said}); only the link is kept" in note_of(r)
        assert len(r.puts) == 1
    resp = requests.Response()
    resp.status_code = 403
    r = web_relay(error=requests.HTTPError(response=resp))
    r.file_message("c", msg(PAGE_URL))
    assert "not fetched (HTTP 403)" in note_of(r)


def test_web_link_of_an_unconverted_type_says_so(web_relay, monkeypatch):
    monkeypatch.setattr(m.inbound, "extract_text", lambda data, mime, name: (None, None))
    r = web_relay(result=("https://example.com/a.zip", "application/zip", b"PK"))
    r.file_message("c", msg("https://example.com/a.zip"))
    assert "(web link, application/zip): no text (type not converted)" in note_of(r) and len(r.puts) == 1


def test_web_links_off_by_default_and_refused_by_minus_text(web_relay, monkeypatch):
    r = web_relay()
    r.file_message("c", msg(f"{PAGE_URL} -text"))
    assert r.fetched == [] and "## Links" not in note_of(r)
    monkeypatch.setattr(m.CFG, "web_links", "off")
    r = web_relay()
    monkeypatch.setattr(m.CFG, "web_links", "off")
    r.file_message("c", msg(PAGE_URL))
    assert r.fetched == [] and "## Links" not in note_of(r)


def test_drive_and_web_links_share_the_links_section(relay, monkeypatch):
    monkeypatch.setattr(m.CFG, "web_links", "text")
    r = relay()
    r.fetch_web = lambda url: (url, "text/html", PAGE)
    r.file_message("c", msg(f"{DOC_URL} and {PAGE_URL}"))
    note = note_of(r)
    assert note.count("## Links") == 1 and "Google Docs document" in note and "web page on example.com" in note
