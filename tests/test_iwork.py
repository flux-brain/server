"""Offline tests for the Apple iWork converter (2026-09-17, the owner: "find converter for apple pages"). Builds
.pages files in memory: no network, no real document."""
import io
import zipfile

import cramjam

from flux_brain.lib import extract


def iwa(payload: bytes) -> bytes:
    """One IWA stream: 0x00 + 3-byte little-endian length + the snappy-raw block."""
    block = bytes(cramjam.snappy.compress_raw(payload))
    return b"\x00" + len(block).to_bytes(3, "little") + block


def pages(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


BODY = ("Répartition du CA entre la gamme ultra-absorbants et la gamme films\n"
        "Gamme ultra absorbants : environ 40% du chiffre d'affaires\n"
        "Quelle évolution de la demande à horizon 5 ans pour les secteurs visés ?").encode()
STYLES = b"\x12text-12-paragraphstyle-Heading 2\x12 SF UI Text Regular\x12EEEE d MMMM y"
DOC = pages({"Index/Document.iwa": iwa(BODY), "Index/DocumentStylesheet.iwa": iwa(STYLES),
             "preview.jpg": b"\xff\xd8\xff\xe0 not really a jpeg"})
METHOD = "Apple iWork document body (styling lost)"


def tiny_pdf(words):
    """Smallest valid one-page PDF with a text object, built by hand (no PDF library needed)."""
    stream = f"BT /F1 24 Tf 72 700 Td ({words}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>",
            b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + body + b"endobj\n"
    start = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(objs) + 1}/Root 1 0 R>>\nstartxref\n{start}\n%%EOF\n".encode()
    return bytes(out)


def test_modern_pages_body_text():
    text, method = extract.extract_text(DOC, "application/octet-stream", "QUESTIONS.pages")
    assert method == METHOD   # dispatched by extension even with a generic MIME
    assert "Répartition du CA entre la gamme ultra-absorbants et la gamme films" in text   # accented text comes out whole
    assert text.count("\n\n") >= 2 and "horizon 5 ans" in text   # every paragraph kept
    assert "paragraphstyle" not in text and "SF UI" not in text and "EEEE" not in text   # style and font noise dropped


def test_quicklook_preview_preferred_when_it_is_a_real_pdf():
    t2, m2 = extract.extract_iwork(pages({"QuickLook/Preview.pdf": tiny_pdf("iWork preview text"), "Index/Document.iwa": iwa(BODY)}), "old.pages")
    assert m2.startswith("iWork QuickLook preview") and "preview text" in t2


def test_broken_preview_falls_back_to_the_body():
    t3, m3 = extract.extract_iwork(pages({"QuickLook/Preview.pdf": b"%PDF-1.4 broken", "Index/Document.iwa": iwa(BODY)}), "old.pages")
    assert m3 == METHOD and "Gamme ultra absorbants" in t3


def test_non_zip_and_empty_documents_yield_nothing():
    assert extract.extract_iwork(b"not a zip at all", "x.pages") == (None, None)
    assert extract.extract_iwork(pages({"Index/Document.iwa": iwa(b"")}), "e.pages") == (None, None)


def test_keynote_and_numbers_use_the_same_path():
    assert extract.extract_text(DOC, "", "deck.key")[1] == METHOD
    assert extract.extract_text(DOC, "", "sheet.numbers")[1] == METHOD
