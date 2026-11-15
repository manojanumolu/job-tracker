"""M6 Part 12 A — résumé files: every format, and every way a file can be
wrong or hostile. Real PDFs/DOCX built in memory (tests/m6_docs.py)."""
import io
import time
import zipfile

import pytest

import resume_parse as rp
from m6_docs import docx, encrypted_pdf, pdf, zip_bomb_docx

BODY = ("Asha Rao\nEducation\nB.Tech in Computer Science, Example University, 2026\nSkills\nPython, SQL, Docker, Git\n"
        "Projects\nBuilt a REST API with Flask and PostgreSQL for a campus portal")


def _read(name, data):
    return rp.read_document(name, data)


def test_a_valid_pdf_is_read():
    d = _read("Resume.PDF", pdf([BODY]))
    assert d.file_type == "pdf" and d.status == "ok" and d.pages == 1 and d.pages_read == 1
    assert "B.Tech in Computer Science" in d.text and "Flask and PostgreSQL" in d.text
    assert len(d.sha256) == 64 and d.size > 0


def test_a_multi_page_pdf_is_read_in_order():
    d = _read("cv.pdf", pdf([BODY, "Experience\nSoftware intern at Example Labs, May 2025 - Jul 2025",
                             "Certifications\nAWS Cloud Practitioner"]))
    assert d.pages == 3 and d.pages_read == 3
    assert d.text.index("Asha Rao") < d.text.index("Software intern") < d.text.index("AWS Cloud Practitioner")


def test_a_pdf_with_more_pages_than_the_cap_is_read_partly_and_says_so(monkeypatch):
    many = [f"Page {i} with words about Python projects and SQL databases and more filler text" for i in range(25)]
    d = _read("long.pdf", pdf(many))
    assert d.pages == 25 and d.pages_read == 20 and d.status == "partial"
    assert any("Only the first 20 of 25 pages" in w for w in d.warnings)


def test_a_valid_docx_is_read_with_tables_and_headers():
    d = _read("cv.docx", docx(BODY.split("\n"), table=[["Languages", "Python, Java"], ["Databases", "MySQL"]],
                               header="asha@example.com | +91 98765 43210"))
    assert d.file_type == "docx" and d.status == "ok" and d.tables == 1
    assert "Languages | Python, Java" in d.text and "asha@example.com" in d.header_text


def test_txt_utf8_utf16_and_cp1252():
    assert "résumé" in _read("a.txt", ("Ananya — résumé\n" + BODY).encode("utf-8")).text
    assert "résumé" in _read("b.txt", ("\ufeffAnanya — résumé\n" + BODY).encode("utf-16")).text
    assert "café" in _read("c.txt", ("café\n" + BODY).encode("cp1252")).text


def test_unicode_text_survives_and_is_normalised():
    d = _read("u.txt", ("Ｐｙｔｈｏｎ developer — நன்றி — 日本語\n\u2022 Built\u00a0APIs\r\n" + BODY).encode("utf-8"))
    assert "Python developer" in d.text and "நன்றி" in d.text and "• Built APIs" in d.text and "\r" not in d.text


@pytest.mark.parametrize("name, data, code", [
    ("cv.pdf", b"", "empty"),
    ("cv.txt", b"", "empty"),
    ("cv.exe", b"MZ\x90\x00", "unsupported"),
    ("cv.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100, "unsupported"),
    ("cv.pdf.exe", b"%PDF-1.4", "unsupported"),
    ("cv", b"Python developer", "unsupported"),
    ("cv.html", b"<html>Python</html>", "unsupported"),
    ("cv.pdf", b"PK\x03\x04 not a pdf at all" * 10, "signature"),
    ("cv.docx", b"%PDF-1.4 this is a pdf named docx", "signature"),
    ("cv.docx", b"plain text named docx" * 10, "signature"),
    ("cv.txt", b"%PDF-1.4\n%binary" + b"\x00" * 50, "binary"),
    ("cv.txt", b"PK\x03\x04" + b"\x00" * 50, "binary"),
    ("cv.txt", bytes(range(1, 32)) * 20, "binary"),
])
def test_wrong_extensions_and_signatures_are_refused(name, data, code):
    with pytest.raises(rp.ResumeFileError) as e:
        _read(name, data)
    assert e.value.code == code


def test_an_oversized_file_is_refused_before_it_is_read(monkeypatch):
    called = []
    monkeypatch.setattr(rp, "_run_worker", lambda *a: called.append(1))
    with pytest.raises(rp.ResumeFileError) as e:
        _read("big.pdf", b"%PDF-1.4" + b"0" * rp.MAX_FILE_BYTES)
    assert e.value.code == "too_large" and not called and "5 MB" in str(e.value)


def test_a_corrupt_pdf_and_a_corrupt_docx():
    with pytest.raises(rp.ResumeFileError) as e:
        _read("bad.pdf", b"%PDF-1.7\n1 0 obj << /Type /Catalog /Pages 9 0 R >> garbage" * 3)
    assert e.value.code == "corrupt"
    good = docx(["Python developer with SQL"])
    with pytest.raises(rp.ResumeFileError) as e:
        _read("bad.docx", good[: len(good) // 2])
    assert e.value.code in ("corrupt", "malformed")


def test_a_password_protected_pdf_is_refused_honestly():
    with pytest.raises(rp.ResumeFileError) as e:
        _read("locked.pdf", encrypted_pdf([BODY]))
    assert e.value.code == "encrypted" and "password-protected" in str(e.value)


def test_a_password_protected_docx_or_old_doc_is_refused():
    with pytest.raises(rp.ResumeFileError) as e:
        _read("locked.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
    assert e.value.code == "encrypted_or_doc"


def test_a_scanned_pdf_is_never_reported_as_read():
    d = _read("scan.pdf", pdf(["x", "y"], image_only=True))
    assert d.status == "no_text" and d.text == ""
    assert any("no OCR" in w and "scanned" in w for w in d.warnings)


def test_a_zip_bomb_is_refused_without_inflating_it():
    t = time.monotonic()
    with pytest.raises(rp.ResumeFileError) as e:
        _read("bomb.docx", zip_bomb_docx())
    assert e.value.code == "zip_bomb" and time.monotonic() - t < 10


@pytest.mark.parametrize("member", ["../evil.xml", "/etc/passwd", "word/../../x.xml", "C:\\x.xml"])
def test_archives_with_path_traversal_names_are_refused(member):
    with pytest.raises(rp.ResumeFileError) as e:
        _read("trav.docx", docx(["Python developer"], extra={member: b"x"}))
    assert e.value.code == "malformed"


def test_malformed_archives_are_refused():
    no_doc = io.BytesIO()
    with zipfile.ZipFile(no_doc, "w") as z:
        z.writestr("hello.txt", "no word document here")
    with pytest.raises(rp.ResumeFileError) as e:
        _read("x.docx", no_doc.getvalue())
    assert e.value.code == "malformed"
    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as z:
        z.writestr("word/document.xml", "<w:document/>")
        for i in range(500):
            z.writestr(f"f{i}.xml", "x")
    with pytest.raises(rp.ResumeFileError) as e:
        _read("y.docx", many.getvalue())
    assert e.value.code == "zip_bomb"


def test_an_xml_entity_expansion_is_refused():
    lol = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
           b'<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>&lol2;</w:t></w:r></w:p></w:body></w:document>')
    with pytest.raises(rp.ResumeFileError) as e:
        _read("lol.docx", docx([], document_xml=lol))
    assert e.value.code == "malformed"


def test_missing_sections_and_almost_empty_files():
    d = _read("thin.txt", b"Just a name\nand a line")
    assert d.status == "no_text" and any("almost no readable text" in w for w in d.warnings)


def test_a_very_long_document_is_cut_and_says_so():
    d = _read("long.txt", (BODY + "\n" + "Python SQL project line " * 6000).encode())
    assert len(d.text) <= rp.MAX_TEXT_CHARS and d.status == "partial"
    assert any("very long" in w for w in d.warnings)


def test_the_same_bytes_give_the_same_hash_for_duplicate_detection():
    a, b = _read("a.pdf", pdf([BODY])), _read("renamed.pdf", pdf([BODY]))
    assert a.sha256 == b.sha256 and a.file_name != b.file_name


@pytest.mark.parametrize("raw, clean", [
    ("../../etc/passwd.pdf", "passwd.pdf"),
    ("C:\\Users\\x\\My CV.pdf", "My CV.pdf"),
    ("cv\u202egnp.pdf", "cvgnp.pdf"),
    ("<script>alert(1)</script>.pdf", "script>.pdf"),     # the last path part; the app escapes it
    ("\x00\x07evil.txt", "evil.txt"),
    ("", "resume"),
    ("a" * 300 + ".pdf", "a" * 120),
])
def test_malicious_file_names_are_cleaned_for_display(raw, clean):
    out = rp.clean_file_name(raw)
    assert out.startswith(clean[:20]) and "/" not in out and "\\" not in out and len(out) <= rp.NAME_MAX
    assert all(ord(ch) >= 32 and ch not in "\u202e" for ch in out)


def test_the_worker_runs_isolated_with_limits_and_no_file_is_written(tmp_path, monkeypatch):
    calls = []
    real = rp.subprocess.run

    def spy(cmd, **kw):
        calls.append((cmd, kw))
        return real(cmd, **kw)
    monkeypatch.setattr(rp.subprocess, "run", spy)
    monkeypatch.chdir(tmp_path)
    _read("cv.pdf", pdf([BODY]))
    (cmd, kw), = calls
    assert cmd[1] == "-I" and cmd[2].endswith("resume_worker.py") and kw["timeout"] == rp.WORKER_TIMEOUT_S
    assert "input" in kw and not any(tmp_path.iterdir())                 # bytes over a pipe; nothing on disk
    src = open(rp.WORKER, encoding="utf-8").read()
    assert "RLIMIT_CPU" in src and "RLIMIT_AS" in src and "open(" not in src.replace("z.open(", "")


def test_a_worker_that_hangs_is_stopped(monkeypatch):
    def slow(cmd, **kw):
        raise rp.subprocess.TimeoutExpired(cmd, kw["timeout"])
    monkeypatch.setattr(rp.subprocess, "run", slow)
    with pytest.raises(rp.ResumeFileError) as e:
        _read("cv.pdf", pdf([BODY]))
    assert e.value.code == "timeout"


def test_a_worker_killed_by_its_memory_limit_is_reported_safely(monkeypatch):
    class Done:
        stdout, returncode = b"", -9
    monkeypatch.setattr(rp.subprocess, "run", lambda cmd, **kw: Done())
    with pytest.raises(rp.ResumeFileError) as e:
        _read("cv.docx", docx(["x"]))
    assert e.value.code == "too_complex"


def test_error_messages_never_echo_the_document():
    secret = b"%PDF-1.4 SECRET-CANDIDATE-TEXT " * 20
    with pytest.raises(rp.ResumeFileError) as e:
        _read("cv.pdf", secret)
    assert "SECRET" not in str(e.value)
