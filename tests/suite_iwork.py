"""Offline tests for the Apple iWork converter in vault-discord-relay.py (2026-09-17, the owner: "find converter for apple
pages"). Builds .pages files in memory: no network, no real document.

  python tests/test_vault_iwork.py [relay.py]
Canonical copy: tests/. Exit 1 on any FAIL.
"""
import io
import sys
import zipfile

import cramjam

from _load import relay  # noqa: E402  sets FLUX_HOME to a temp dir first

fails = 0


def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += 0 if cond else 1


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

doc = pages({"Index/Document.iwa": iwa(BODY), "Index/DocumentStylesheet.iwa": iwa(STYLES),
             "preview.jpg": b"\xff\xd8\xff\xe0 not really a jpeg"})
text, method = relay.extract_text(doc, "application/octet-stream", "QUESTIONS.pages")
check("dispatched by extension even with a generic MIME", method == "Apple iWork document body (styling lost)")
check("accented body text comes out whole", "Répartition du CA entre la gamme ultra-absorbants et la gamme films" in (text or ""))
check("every paragraph is kept", (text or "").count("\n\n") >= 2 and "horizon 5 ans" in (text or ""))
check("style and font noise dropped", "paragraphstyle" not in (text or "") and "SF UI" not in (text or "")
      and "EEEE" not in (text or ""))

# an old file whose QuickLook preview is a real PDF: the preview wins (it keeps the layout)
def tiny_pdf(words):
    """Smallest valid one-page PDF with a text object, built by hand (no PDF library on this host)."""
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


t2, m2 = relay.extract_iwork(pages({"QuickLook/Preview.pdf": tiny_pdf("iWork preview text"),
                                    "Index/Document.iwa": iwa(BODY)}), "old.pages")
check("QuickLook preview preferred when present", (m2 or "").startswith("iWork QuickLook preview") and "preview text" in (t2 or ""))

t3, m3 = relay.extract_iwork(pages({"QuickLook/Preview.pdf": b"%PDF-1.4 broken", "Index/Document.iwa": iwa(BODY)}), "old.pages")
check("a broken preview falls back to the body", m3 == "Apple iWork document body (styling lost)" and "Gamme ultra absorbants" in (t3 or ""))
check("a non-zip file is not text", relay.extract_iwork(b"not a zip at all", "x.pages") == (None, None))
check("an empty document yields nothing", relay.extract_iwork(pages({"Index/Document.iwa": iwa(b"")}), "e.pages") == (None, None))
check("keynote and numbers use the same path", relay.extract_text(doc, "", "deck.key")[1] == method
      and relay.extract_text(doc, "", "sheet.numbers")[1] == method)

print("FAILS:", fails)
sys.exit(1 if fails else 0)
