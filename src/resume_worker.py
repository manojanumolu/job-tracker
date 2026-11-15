"""Reads ONE untrusted .pdf or .docx résumé and prints its text as JSON.

Run only by resume_parse.read_document, as a separate short-lived process:

    python -I resume_worker.py pdf|docx   < file bytes   > JSON

Why a separate process: a hostile document (a decompression bomb, a PDF
built to make the parser loop) must never be able to stall or exhaust the
app. Here it runs with its own CPU-time and memory limits, and the parent
kills it after a wall-clock timeout. ``-I`` (isolated mode) keeps the
environment and the current directory out of its import path.

It never writes a file and never logs the document. Output:
    {"ok": true, "text": ..., "pages": n, "pages_read": n, "warnings": [...],
     "tables": n, "header_text": ...}
    {"ok": false, "error": "<code>"}   (codes: see resume_parse.ERRORS)
Only the standard library and pypdf are imported.
"""

from __future__ import annotations

import html
import io
import json
import re
import sys
import zipfile

MAX_PAGES = 20                       # read at most this many PDF pages
MAX_TEXT = 100_000                   # characters kept, in total
MAX_ZIP_MEMBERS = 400
MAX_ZIP_TOTAL = 40 * 1024 * 1024     # bytes, uncompressed, all members together
MAX_ZIP_RATIO = 120                  # uncompressed / compressed, per member (a bomb is far above)
MAX_XML = 25 * 1024 * 1024           # the main document part, uncompressed
CPU_SECONDS = 20
MEMORY_BYTES = 768 * 1024 * 1024


def _limit() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 1))
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    except (ImportError, ValueError, OSError):    # not on this platform: the parent's timeout still applies
        pass


class Refused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ── PDF ─────────────────────────────────────────────────────────────────────

def read_pdf(data: bytes) -> dict:
    if not data[:1024].lstrip().startswith(b"%PDF-") and b"%PDF-" not in data[:1024]:
        raise Refused("signature")
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise Refused("pdf_unavailable") from None
    warnings: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")            # owner-password-only files open with an empty password
            except Exception:
                ok = 0
            if not ok:
                raise Refused("encrypted")
            warnings.append("The PDF is protected with an owner password; its text could still be read.")
        total = len(reader.pages)
        parts: list[str] = []
        size = 0
        read = 0
        for page in reader.pages[:MAX_PAGES]:
            try:
                text = page.extract_text() or ""
            except Exception:                      # one damaged page: keep the others
                text = ""
                warnings.append(f"Page {read + 1} could not be read.")
            read += 1
            parts.append(text)
            size += len(text)
            if size >= MAX_TEXT:
                warnings.append("The document is very long; only its beginning was read.")
                break
        if total > MAX_PAGES:
            warnings.append(f"Only the first {MAX_PAGES} of {total} pages were read.")
    except Refused:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, IndexError, OSError, RecursionError,
            AssertionError, zipfile.BadZipFile):
        raise Refused("corrupt") from None
    except MemoryError:
        raise Refused("too_complex") from None
    except Exception:
        raise Refused("corrupt") from None
    text = "\n\f\n".join(parts)[:MAX_TEXT]
    return {"text": text, "pages": total, "pages_read": read, "warnings": warnings, "tables": 0, "header_text": ""}


# ── DOCX ────────────────────────────────────────────────────────────────────

_TAG = re.compile(r"<[^>]+>")
_T_RUN = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>")


_TOKEN = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>|<w:tab\s*/>|<w:(?:br|cr)\b[^>]*/>|</w:tc>|</w:tr>|</w:p>")


def _xml_text(xml: str) -> str:
    """The text of a WordprocessingML part: runs in order, tabs and breaks
    kept, table cells joined with " | " and rows on their own lines."""
    if re.search(r"<!DOCTYPE|<!ENTITY", xml[:4096], re.I):
        raise Refused("malformed")
    out: list[str] = []
    for m in _TOKEN.finditer(xml):
        tok = m.group(0)
        if m.group(1) is not None:
            out.append(html.unescape(m.group(1)))
        elif tok.startswith("<w:tab"):
            out.append("\t")
        elif tok == "</w:tc>":
            out.append("\x1f")                       # a cell boundary, resolved below
        elif tok == "</w:tr>":
            out.append("\x1e")
        else:                                        # </w:p>, <w:br/>, <w:cr/>
            out.append("\n")
    text = "".join(out)
    text = re.sub(r"\n*\x1f\n*\x1e", "\n", text)       # a row's last cell ends the row
    text = re.sub(r"\n*\x1f\n*", " | ", text)            # a cell's own paragraph doesn't end the row
    return text.replace("\x1e", "\n")


def read_docx(data: bytes) -> dict:
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise Refused("encrypted_or_doc")         # a password-protected .docx or an old .doc (OLE file)
    if data[:4] != b"PK\x03\x04":
        raise Refused("signature")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                raise Refused("zip_bomb")
            total = 0
            for i in infos:
                name = i.filename
                if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/") or ":" in name:
                    raise Refused("malformed")    # never extracted, but a crafted archive is refused outright
                total += i.file_size
                if i.file_size > 1024 * 1024 and i.compress_size and i.file_size / i.compress_size > MAX_ZIP_RATIO:
                    raise Refused("zip_bomb")
            if total > MAX_ZIP_TOTAL:
                raise Refused("zip_bomb")
            names = {i.filename for i in infos}
            if "word/document.xml" not in names:
                raise Refused("malformed")
            doc = z.getinfo("word/document.xml")
            if doc.file_size > MAX_XML:
                raise Refused("zip_bomb")
            with z.open(doc) as fh:
                raw = fh.read(MAX_XML + 1)
            if len(raw) > MAX_XML:
                raise Refused("zip_bomb")
            xml = raw.decode("utf-8", errors="replace")
            header_text = ""
            for name in sorted(n for n in names if re.fullmatch(r"word/(?:header|footer)\d*\.xml", n))[:6]:
                info = z.getinfo(name)
                if info.file_size <= 2 * 1024 * 1024:
                    header_text += _xml_text(z.read(info).decode("utf-8", errors="replace")) + "\n"
    except Refused:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, OSError, EOFError, ValueError, NotImplementedError,
            RuntimeError):
        raise Refused("corrupt") from None
    except MemoryError:
        raise Refused("too_complex") from None
    tables = len(re.findall(r"<w:tbl[ >]", xml))
    text = _xml_text(xml)
    warnings = []
    if len(text) > MAX_TEXT:
        warnings.append("The document is very long; only its beginning was read.")
    return {"text": text[:MAX_TEXT], "pages": 0, "pages_read": 0, "warnings": warnings, "tables": tables,
            "header_text": header_text.strip()[:2000]}


def main(argv: list[str]) -> int:
    _limit()
    kind = argv[1] if len(argv) > 1 else ""
    data = sys.stdin.buffer.read(16 * 1024 * 1024)
    try:
        if kind == "pdf":
            out = read_pdf(data)
        elif kind == "docx":
            out = read_docx(data)
        else:
            raise Refused("unsupported")
        result = {"ok": True, **out}
    except Refused as e:
        result = {"ok": False, "error": e.code}
    except MemoryError:
        result = {"ok": False, "error": "too_complex"}
    except Exception:                              # never a traceback: it could quote the document
        result = {"ok": False, "error": "corrupt"}
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
