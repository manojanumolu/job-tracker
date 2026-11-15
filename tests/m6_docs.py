"""Builders for the M6 test documents: real PDFs and DOCX files made in
memory (no binary fixtures in the repository, nobody's résumé)."""
from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape


def _pdf_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf(pages: list[str], *, image_only: bool = False) -> bytes:
    """A valid PDF, one text page per item (lines split on \\n), Helvetica.
    image_only=True draws a small grey image and no text (a "scanned" page)."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    image = add(b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
                b"/BitsPerComponent 8 /Length 4 >>\nstream\n\x80\x80\x80\x80\nendstream") if image_only else 0
    page_ids = []
    pages_obj_index = len(objs) + 1 + 2 * len(pages)          # placeholder number, fixed below
    kids = []
    for text in pages:
        if image_only:
            content = b"q 200 0 0 200 100 500 cm /Im1 Do Q"
        else:
            parts = [b"BT /F1 11 Tf 14 TL 50 790 Td"]
            for line in text.split("\n"):
                parts.append(f"({_pdf_escape(line)}) Tj T*".encode("cp1252", errors="replace"))
            parts.append(b"ET")
            content = b"\n".join(parts)
        c = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        res = (b"/XObject << /Im1 %d 0 R >>" % image) if image_only else (b"/Font << /F1 %d 0 R >>" % font)
        p = add(b"<< /Type /Page /Parent PARENT 0 R /MediaBox [0 0 595 842] /Contents %d 0 R /Resources << %s >> >>"
                % (c, res))
        page_ids.append(p)
    parent = add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % p for p in page_ids) + b"] /Count %d >>"
                 % len(page_ids))
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % parent)
    objs = [o.replace(b"PARENT", str(parent).encode()) for o in objs]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, xref))
    del pages_obj_index, kids
    return out.getvalue()


def encrypted_pdf(pages: list[str], password: str = "secret") -> bytes:
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for p in PdfReader(io.BytesIO(pdf(pages))).pages:
        w.add_page(p)
    w.encrypt(user_password=password, owner_password=password + "-owner", algorithm="RC4-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def docx(paragraphs: list[str], *, table: list[list[str]] | None = None, header: str = "",
         extra: dict[str, bytes] | None = None, document_xml: bytes | None = None) -> bytes:
    """A minimal valid .docx (paragraphs, optional table and header)."""
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{escape(c)}</w:t></w:r></w:p></w:tc>" for c in r)
                       + "</w:tr>" for r in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    xml = document_xml or (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>").encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr("word/document.xml", xml)
        if header:
            z.writestr("word/header1.xml", '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
                       f'2006/main"><w:p><w:r><w:t>{escape(header)}</w:t></w:r></w:p></w:hdr>')
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def zip_bomb_docx(size: int = 60 * 1024 * 1024) -> bytes:
    """A .docx whose document.xml inflates to ``size`` bytes of zeros."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        with z.open("word/document.xml", "w", force_zip64=True) as fh:
            chunk = b"0" * (1024 * 1024)
            for _ in range(size // len(chunk)):
                fh.write(chunk)
    return buf.getvalue()
