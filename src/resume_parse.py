"""Résumé file intake for M6: check an uploaded file, then read its text.

Supported: PDF (text-based), DOCX, TXT. Everything happens in memory; the
file itself is never written to disk, the repository, a log or a database —
only the structured profile built from its text is kept (resume_store.py).

Checks, in order, before any parser sees the bytes:
  * size: 1 byte .. MAX_FILE_BYTES
  * extension: .pdf / .docx / .txt (case-insensitive) — anything else refused
  * signature: the bytes must be what the extension says (%PDF-, a ZIP
    with word/document.xml, or text without binary content)
PDF and DOCX are then read by resume_worker.py in a separate process with
CPU-time and memory limits and a wall-clock timeout, so a hostile document
(zip bomb, parser trap) can't stall or exhaust the app. TXT is decoded here.

Honest results: a scanned (image-only) PDF has no text layer and there is no
OCR in this app, so it is reported as "no_text" — never as read.

No Streamlit import; unit-tested in tests/test_m6_files.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
WORKER_TIMEOUT_S = 25
EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".txt": "txt"}
NAME_MAX = 120

# error code -> the message shown to the person (never echoes the file)
ERRORS = {
    "empty": "The file is empty.",
    "too_large": f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.",
    "unsupported": "Upload a PDF, DOCX or TXT file.",
    "signature": "The file's contents don't match its extension (is it really a {kind}?).",
    "encrypted": "The PDF is password-protected. Remove the password and upload it again.",
    "encrypted_or_doc": "This is a password-protected document or an old .doc file. Save it as an "
                        "unprotected .docx (or PDF) and upload it again.",
    "corrupt": "The file is damaged or not a valid {kind} — it couldn't be read.",
    "malformed": "The document's structure isn't valid, so it was not read.",
    "zip_bomb": "The document expands to an unsafe size, so it was not read.",
    "too_complex": "The document is too complex to read safely.",
    "timeout": "Reading the document took too long, so it was stopped.",
    "pdf_unavailable": "PDF reading isn't available on this server right now. Upload a DOCX or TXT instead.",
    "binary": "The file isn't plain text.",
    "worker": "The document couldn't be read right now. Try again in a moment.",
}


class ResumeFileError(ValueError):
    """A file this module refuses; ``code`` is a key of ERRORS and
    ``str(e)`` is safe to show."""

    def __init__(self, code: str, kind: str = ""):
        super().__init__(ERRORS.get(code, ERRORS["worker"]).format(kind=kind.upper() or "document"))
        self.code = code


@dataclass
class ParsedDocument:
    file_type: str                 # pdf | docx | txt
    file_name: str                 # cleaned, for display only
    size: int
    sha256: str                    # of the file's bytes (duplicate detection; reveals nothing)
    text: str
    status: str                    # "ok" | "partial" (read with warnings) | "no_text" (nothing readable)
    pages: int = 0
    pages_read: int = 0
    warnings: list[str] = field(default_factory=list)
    tables: int = 0                # DOCX tables (ATS hint)
    header_text: str = ""          # DOCX header/footer text (ATS hint)


def clean_file_name(name: object) -> str:
    """A file name safe to show and store: its last path component only, no
    control or bidi characters, at most NAME_MAX characters."""
    base = re.split(r"[\\/]", str(name or ""))[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C")
    base = " ".join(base.split()).strip(" .")
    return (base or "resume")[:NAME_MAX]


def file_kind(name: object) -> str:
    ext = os.path.splitext(str(name or "").lower().strip())[1]
    if ext not in EXTENSIONS:
        raise ResumeFileError("unsupported")
    return EXTENSIONS[ext]


def _decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    head = data[:4096]
    if b"\x00" in head or data.startswith((b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0")):
        raise ResumeFileError("binary")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    controls = sum(1 for ch in text[:4096] if unicodedata.category(ch) == "Cc" and ch not in "\n\r\t\f")
    if controls > 8:
        raise ResumeFileError("binary")
    return text


WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume_worker.py")


def _run_worker(kind: str, data: bytes) -> dict:
    try:
        proc = subprocess.run([sys.executable, "-I", WORKER, kind], input=data, capture_output=True,
                              timeout=WORKER_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        raise ResumeFileError("timeout", kind) from None
    except OSError:
        raise ResumeFileError("worker", kind) from None
    try:
        out = json.loads(proc.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # killed by its CPU or memory limit (no JSON): the document was too much to read safely
        raise ResumeFileError("too_complex" if proc.returncode else "worker", kind) from None
    if not isinstance(out, dict):
        raise ResumeFileError("worker", kind)
    if not out.get("ok"):
        raise ResumeFileError(str(out.get("error") or "corrupt"), kind)
    return out


def normalise_text(text: str) -> str:
    """Unicode NFKC, unified line ends and bullets, no control characters,
    no trailing spaces, at most two blank lines in a row."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    text = re.sub(r"[•●▪◦‣⁃➢∙]", "•", text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_TEXT_CHARS]


def read_document(name: object, data: object) -> ParsedDocument:
    """Check and read one uploaded résumé. Raises ResumeFileError (with a
    message safe to show) for anything it refuses."""
    if not isinstance(data, (bytes, bytearray)):
        raise ResumeFileError("unsupported")
    data = bytes(data)
    if not data:
        raise ResumeFileError("empty")
    if len(data) > MAX_FILE_BYTES:
        raise ResumeFileError("too_large")
    kind = file_kind(name)
    clean = clean_file_name(name)
    digest = hashlib.sha256(data).hexdigest()
    if kind == "txt":
        text = _decode_text(data)
        out = {"text": text, "pages": 0, "pages_read": 0, "warnings": [], "tables": 0, "header_text": ""}
    else:
        if kind == "pdf" and b"%PDF-" not in data[:1024]:
            raise ResumeFileError("signature", kind)
        if kind == "docx" and data[:4] != b"PK\x03\x04" and data[:4] != b"\xd0\xcf\x11\xe0":
            raise ResumeFileError("signature", kind)
        out = _run_worker(kind, data)
    raw = out.get("text") if isinstance(out.get("text"), str) else ""
    warnings = [str(w)[:200] for w in out.get("warnings") or [] if isinstance(w, str)][:10]
    if len(raw) > MAX_TEXT_CHARS:
        warnings.append("The document is very long; only its beginning was read.")
    text = normalise_text(raw)
    words = len(re.findall(r"[A-Za-z]{2,}", text))
    if words < 15:
        status = "no_text"
        if kind == "pdf":
            warnings.append("No readable text was found. This looks like a scanned (image-only) PDF; this app has "
                            "no OCR, so export the résumé as a text-based PDF or DOCX and upload that.")
        else:
            warnings.append("The file has almost no readable text.")
    else:
        status = "partial" if warnings else "ok"
    return ParsedDocument(file_type=kind, file_name=clean, size=len(data), sha256=digest, text=text, status=status,
                          pages=int(out.get("pages") or 0), pages_read=int(out.get("pages_read") or 0),
                          warnings=warnings, tables=int(out.get("tables") or 0),
                          header_text=normalise_text(str(out.get("header_text") or ""))[:2000])
