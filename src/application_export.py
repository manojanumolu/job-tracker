"""Excel (.xlsx) and PDF exports of the Application Tracker (Nov 2026).

Both are written with the standard library alone (zipfile + SpreadsheetML,
and a small PDF writer with the built-in Helvetica fonts), so the feature
adds no dependency. They contain exactly the applications the caller
passes in — the signed-in admin's own, read through their ApplicationBook
— and never read anything themselves. Deleted applications are dropped
here too, whatever the caller passes.

The PDF uses the PDF standard fonts, which cover Western European text
(Windows-1252). Anything else is written readably ("₹" as "Rs.", accents
kept where the font has them) or as "?"; the Excel file keeps every
character as typed.
"""

from __future__ import annotations

import re
import time
import unicodedata
import zipfile
import zlib
from datetime import date, datetime, timezone
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr

import applications as apps

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"


# ---------------------------------------------------------------------------
# The values, shared by both formats
# ---------------------------------------------------------------------------

def _day(value: object) -> date | None:
    return apps.parse_day(value)


def nice_day(value: object) -> str:
    d = _day(value)
    return d.strftime("%d %b %Y") if d else ""


def source_label(doc: dict) -> str:
    src = doc.get("source") or ""
    other = (doc.get("source_other") or "").strip()
    return f"Other — {other}" if src == "Other" and other else src


def follow_up_label(doc: dict) -> str:
    fu = doc.get("follow_up") if isinstance(doc.get("follow_up"), dict) else {}
    if not fu.get("date"):
        return ""
    return nice_day(fu["date"]) + (" (done)" if fu.get("done") is True else "")


def interview_lines(doc: dict) -> list[str]:
    out = []
    for r in doc.get("interviews") or []:
        if not isinstance(r, dict):
            continue
        when = " ".join(x for x in (nice_day(r.get("date")), r.get("time") or "") if x)
        bits = [r.get("round") or "Interview", when, r.get("type") or "", r.get("outcome") or ""]
        line = " · ".join(b for b in bits if b)
        if r.get("location"):
            line += f" · {r['location']}"
        if r.get("interviewer"):
            line += f" · with {r['interviewer']}"
        out.append(line)
    return out


def _updated(doc: dict) -> str:
    ts = apps._iso_seconds(doc.get("updated_at"))
    return datetime.fromtimestamp(ts, apps.IST).strftime("%d %b %Y, %H:%M IST") if ts else ""


# (header, getter, Excel width, kind)
COLUMNS = [
    ("Company", lambda d: d.get("company") or "", 22, "text"),
    ("Job title", lambda d: d.get("title") or "", 32, "text"),
    ("Location", lambda d: d.get("location") or "", 20, "text"),
    ("Job URL", lambda d: d.get("url") or "", 36, "url"),
    ("Job ID", lambda d: d.get("job_id") or "", 16, "text"),
    ("Date applied", lambda d: d.get("applied_on") or "", 14, "date"),
    ("Posting date", lambda d: d.get("posted_on") or "", 14, "date"),
    ("Application progress", lambda d: d.get("progress") or "", 20, "text"),
    ("Email response", lambda d: d.get("email_status") or "", 26, "text"),
    ("Follow-up date", lambda d: (d.get("follow_up") or {}).get("date") or "", 14, "date"),
    ("Follow-up done", lambda d: ("Yes" if (d.get("follow_up") or {}).get("done") is True else "No")
     if (d.get("follow_up") or {}).get("date") else "", 13, "text"),
    ("Application source", source_label, 22, "text"),
    ("Salary/package", lambda d: d.get("salary") or "", 16, "text"),
    ("Resume name/version", lambda d: d.get("resume") or "", 24, "text"),
    ("Interviews", lambda d: "\n".join(interview_lines(d)), 44, "wrap"),
    ("Notes", lambda d: d.get("notes") or "", 44, "wrap"),
    ("Last updated", _updated, 22, "text"),
]
INTERVIEW_COLUMNS = [("Company", 22), ("Job title", 30), ("Round", 20), ("Date", 14), ("Time", 9), ("Type", 12),
                     ("Location or meeting link", 30), ("Interviewer", 20), ("Outcome", 16),
                     ("Preparation notes", 40), ("Feedback", 40)]


def exportable(docs) -> list[dict]:
    """Live applications only, newest application first."""
    return apps.sort_apps(apps.live(docs), "applied_new")


def file_name(ext: str, now: float | None = None) -> str:
    stamp = datetime.fromtimestamp(time.time() if now is None else now, apps.IST).strftime("%Y-%m-%d")
    return f"applications-{stamp}.{ext}"


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_EXCEL_EPOCH = date(1899, 12, 30)


def _x(text: object) -> str:
    return escape(_XML_BAD.sub("", str(text))[:32767])


def _col(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref: str, value: object, kind: str) -> str:
    if kind == "date" and _day(value):
        return f'<c r="{ref}" s="2"><v>{(_day(value) - _EXCEL_EPOCH).days}</v></c>'
    if value in ("", None):
        return f'<c r="{ref}" s="{4 if kind != "wrap" else 3}"/>'
    style = {"wrap": 3, "url": 5}.get(kind, 4)
    # an inline string is never a formula, whatever it starts with
    return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{_x(value)}</t></is></c>'


def _sheet(headers: list[tuple[str, int]], rows: list[list[tuple[object, str]]], links: list[tuple[str, str]]) -> str:
    last = _col(len(headers) - 1)
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, (_, w) in enumerate(headers))
    head = "".join(f'<c r="{_col(i)}1" t="inlineStr" s="1"><is><t>{_x(h)}</t></is></c>' for i, (h, _) in enumerate(headers))
    body = "".join(f'<row r="{r + 2}">' + "".join(_cell(f"{_col(c)}{r + 2}", v, k) for c, (v, k) in enumerate(row))
                   + "</row>" for r, row in enumerate(rows))
    end = max(2, len(rows) + 1)
    link_xml = ("<hyperlinks>" + "".join(f'<hyperlink ref="{ref}" r:id="rId{i + 1}"/>' for i, (ref, _) in enumerate(links))
                + "</hyperlinks>") if links else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<dimension ref="A1:{last}{end}"/>'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
            'state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>'
            f'<cols>{cols}</cols><sheetData><row r="1" ht="22" customHeight="1">{head}</row>{body}</sheetData>'
            f'<autoFilter ref="A1:{last}{end}"/>{link_xml}'
            '<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
            '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>')


def _sheet_rels(links: list[tuple[str, str]]) -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                      f'relationships/hyperlink" Target={quoteattr(_XML_BAD.sub("", url))} TargetMode="External"/>'
                      for i, (_, url) in enumerate(links))
            + "</Relationships>")


_STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<numFmts count="1"><numFmt numFmtId="164" formatCode="dd mmm yyyy"/></numFmts>'
           '<fonts count="3"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
           '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>'
           '<font><u/><sz val="11"/><color rgb="FF4636D4"/><name val="Calibri"/><family val="2"/></font></fonts>'
           '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
           '<fill><patternFill patternType="solid"><fgColor rgb="FF5B4CF5"/><bgColor indexed="64"/></patternFill></fill></fills>'
           '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
           '<border><left/><right/><top/><bottom style="thin"><color rgb="FFE5E7F0"/></bottom><diagonal/></border></borders>'
           '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
           '<cellXfs count="6">'
           '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
           '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">'
           '<alignment vertical="center" wrapText="1"/></xf>'
           '<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" '
           'applyAlignment="1"><alignment horizontal="left" vertical="top"/></xf>'
           '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">'
           '<alignment vertical="top" wrapText="1"/></xf>'
           '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">'
           '<alignment vertical="top"/></xf>'
           '<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
           '<alignment vertical="top"/></xf>'
           '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
           '</styleSheet>')


def to_xlsx(docs, scope: str = "", now: float | None = None) -> bytes:
    """A workbook: "Applications" (one row each) and "Interviews" (one row
    per interview round). Dates are real Excel dates; the header row is
    frozen and filterable; job links are clickable."""
    items = exportable(docs)
    rows, links = [], []
    for r, d in enumerate(items):
        row = []
        for c, (_, get, _, kind) in enumerate(COLUMNS):
            value = get(d)
            row.append((value, kind))
            if kind == "url" and value:
                links.append((f"{_col(c)}{r + 2}", value))
        rows.append(row)
    iv_rows = []
    for d in items:
        for iv in d.get("interviews") or []:
            if isinstance(iv, dict):
                iv_rows.append([(d.get("company") or "", "text"), (d.get("title") or "", "text"),
                                (iv.get("round") or "", "text"), (iv.get("date") or "", "date"),
                                (iv.get("time") or "", "text"), (iv.get("type") or "", "text"),
                                (iv.get("location") or "", "text"), (iv.get("interviewer") or "", "text"),
                                (iv.get("outcome") or "", "text"), (iv.get("prep") or "", "wrap"),
                                (iv.get("feedback") or "", "wrap")])
    stamp = apps._now_iso(now)
    title = "Application Tracker export" + (f" — {scope}" if scope else "")
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '</Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '</Relationships>',
        "docProps/core.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{_x(title)}</dc:title><dc:creator>Fresher Job Tracker</dc:creator>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created></cp:coreProperties>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Applications" sheetId="1" r:id="rId1"/><sheet name="Interviews" sheetId="2" r:id="rId2"/></sheets>'
        '<definedNames>'
        f'<definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">Applications!$A$1:${_col(len(COLUMNS) - 1)}${max(2, len(rows) + 1)}</definedName>'
        f'<definedName name="_xlnm._FilterDatabase" localSheetId="1" hidden="1">Interviews!$A$1:${_col(len(INTERVIEW_COLUMNS) - 1)}${max(2, len(iv_rows) + 1)}</definedName>'
        '</definedNames></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>',
        "xl/styles.xml": _STYLES,
        "xl/worksheets/sheet1.xml": _sheet([(h, w) for h, _, w, _ in COLUMNS], rows, links),
        "xl/worksheets/sheet2.xml": _sheet(INTERVIEW_COLUMNS, iv_rows, []),
    }
    if links:
        files["xl/worksheets/_rels/sheet1.xml.rels"] = _sheet_rels(links)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), text.encode("utf-8"), zipfile.ZIP_DEFLATED)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

# Helvetica / Helvetica-Bold advance widths (1/1000 em) for Windows-1252
# codes 32-255, from Adobe's AFM metrics
_W = {"F1": "278,278,355,556,556,889,667,191,333,333,389,584,278,333,278,278,556,556,556,556,556,556,556,556,556,556,278,278,584,584,584,556,1015,667,667,722,722,667,611,778,722,278,500,667,556,833,722,778,667,778,722,667,611,722,667,944,667,667,611,278,278,278,469,556,333,556,556,500,556,556,278,556,556,222,222,500,222,833,556,556,556,556,333,500,278,556,500,722,500,500,500,334,260,334,584,0,556,0,222,556,333,1000,556,556,333,1000,667,333,1000,0,611,0,0,222,222,333,333,350,556,1000,333,1000,500,333,944,0,500,667,278,333,556,556,556,556,260,556,333,737,370,556,584,333,737,333,400,584,333,333,333,556,537,278,333,333,365,556,834,834,834,611,667,667,667,667,667,667,1000,722,667,667,667,667,278,278,278,278,722,722,778,778,778,778,778,584,778,722,722,722,722,667,667,611,556,556,556,556,556,556,889,500,556,556,556,556,278,278,278,278,556,556,556,556,556,556,556,584,611,556,556,556,556,500,556,500",
      "F2": "278,333,474,556,556,889,722,238,333,333,389,584,278,333,278,278,556,556,556,556,556,556,556,556,556,556,333,333,584,584,584,611,975,722,722,722,722,667,611,778,722,278,556,722,611,833,722,778,667,778,722,667,611,722,667,944,667,667,611,333,278,333,584,556,333,556,611,556,611,556,333,611,611,278,278,556,278,889,611,611,611,611,389,556,333,611,556,778,556,556,500,389,280,389,584,0,556,0,278,556,500,1000,556,556,333,1000,667,333,1000,0,611,0,0,278,278,500,500,350,556,1000,333,1000,556,333,944,0,500,667,278,333,556,556,556,556,280,556,333,737,370,556,584,333,737,333,400,584,333,333,333,611,556,278,333,333,365,556,834,834,834,611,722,722,722,722,722,722,1000,722,667,667,667,667,278,278,278,278,722,722,778,778,778,778,778,584,778,722,722,722,722,667,667,611,556,556,556,556,556,556,889,556,556,556,556,556,278,278,278,278,611,611,611,611,611,611,611,584,611,611,611,611,611,556,611,556"}
_WIDTHS = {f: [int(x) for x in v.split(",")] for f, v in _W.items()}
_SWAP = {"₹": "Rs.", "→": "->", "←": "<-", "≥": ">=", "≤": "<=", "✓": "v", "×": "x", " ": " ", " ": " ",
         " ": " ", "−": "-", "‐": "-", "‑": "-"}


def pdf_text(text: object) -> bytes:
    """Text in the fonts' encoding: Windows-1252, with readable stand-ins
    (and accents dropped only where the font lacks the letter)."""
    out = bytearray()
    for ch in str(text):
        ch = _SWAP.get(ch, ch)
        try:
            out += ch.encode("cp1252")
            continue
        except UnicodeEncodeError:
            pass
        plain = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        try:
            out += plain.encode("cp1252") if plain else b"?"
        except UnicodeEncodeError:
            out += b"?"
    return bytes(out)


def _width(data: bytes, font: str, size: float) -> float:
    table = _WIDTHS[font]
    return sum(table[b - 32] if 32 <= b <= 255 else 0 for b in data) * size / 1000


def wrap(text: object, font: str, size: float, width: float) -> list[bytes]:
    """Lines no wider than ``width`` points (long words are broken)."""
    lines = []
    for para in str(text or "").split("\n"):
        words, cur = pdf_text(" ".join(para.split())).split(b" "), b""
        if words == [b""]:
            lines.append(b"")
            continue
        for word in words:
            trial = cur + b" " + word if cur else word
            if _width(trial, font, size) <= width:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            cur = b""
            while _width(word, font, size) > width:            # a word longer than the line
                cut = len(word)
                while cut > 1 and _width(word[:cut], font, size) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            cur = word
        lines.append(cur)
    return lines


def _esc(data: bytes) -> str:
    out = []
    for b in data:
        if b in (0x28, 0x29, 0x5C):
            out.append("\\" + chr(b))
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append(f"\\{b:03o}")
    return "".join(out)


class _Doc:
    """A few pages of drawing operators: text, filled boxes, rules."""
    W, H, M = 842.0, 595.0, 36.0                    # A4 landscape, 0.5 in margins

    def __init__(self):
        self.pages: list[list[str]] = []
        self.y = 0.0
        self.new_page()

    def new_page(self):
        self.pages.append([])
        self.y = self.H - self.M

    def need(self, h: float) -> bool:
        """Start a new page unless ``h`` points still fit; True if it did."""
        if self.y - h < self.M + 18:
            self.new_page()
            return True
        return False

    def text(self, x: float, y: float, data: bytes, font: str = "F1", size: float = 9, rgb=(0.05, 0.06, 0.14)):
        self.pages[-1].append(f"{rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg BT /{font} {size} Tf "
                              f"1 0 0 1 {x:.2f} {y:.2f} Tm ({_esc(data)}) Tj ET")

    def box(self, x: float, y: float, w: float, h: float, rgb):
        self.pages[-1].append(f"{rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def rule(self, x1: float, y: float, x2: float, rgb=(0.9, 0.91, 0.94), w: float = 0.6):
        self.pages[-1].append(f"{rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} RG {w} w {x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S")


ACCENT, MUTED, TEXT, SOFT, ZEBRA = (0.357, 0.298, 0.961), (0.392, 0.416, 0.51), (0.05, 0.06, 0.14), \
    (0.937, 0.929, 1.0), (0.969, 0.973, 0.988)
_TABLE = [("Company", 118, lambda d: d.get("company") or ""), ("Job title", 170, lambda d: d.get("title") or ""),
          ("Location", 104, lambda d: d.get("location") or ""), ("Applied", 62, lambda d: nice_day(d.get("applied_on"))),
          ("Progress", 82, lambda d: d.get("progress") or ""), ("Email response", 120, lambda d: d.get("email_status") or ""),
          ("Follow-up", 112, follow_up_label)]


def _table_head(doc: _Doc) -> None:
    x = doc.M
    doc.box(doc.M, doc.y - 18, doc.W - 2 * doc.M, 18, SOFT)
    for head, w, _ in _TABLE:
        doc.text(x + 5, doc.y - 12.5, pdf_text(head), "F2", 8.5, ACCENT)
        x += w
    doc.y -= 18


def to_pdf(docs, scope: str = "", now: float | None = None) -> bytes:
    """A landscape A4 report: a summary table of every application, then
    each application's full details (link, ID, dates, source, resume,
    interview rounds, notes). Page numbers in the footer."""
    items = exportable(docs)
    stamp = datetime.fromtimestamp(time.time() if now is None else now, apps.IST)
    doc = _Doc()
    full = doc.W - 2 * doc.M
    doc.text(doc.M, doc.y - 20, pdf_text("Application Tracker"), "F2", 20, TEXT)
    sub = f"Exported {stamp.strftime('%d %b %Y, %H:%M')} IST · {len(items)} application{'s' if len(items) != 1 else ''}"
    if scope:
        sub += f" · {scope}"
    doc.text(doc.M, doc.y - 36, pdf_text(sub), "F1", 9.5, MUTED)
    doc.box(doc.M, doc.y - 46, 46, 2.5, ACCENT)
    doc.y -= 62
    if not items:
        doc.text(doc.M, doc.y - 12, pdf_text("No applications to export."), "F1", 11, MUTED)
    else:
        _table_head(doc)
        for i, d in enumerate(items):
            cells = [wrap(get(d), "F1", 8.5, w - 10) for _, w, get in _TABLE]
            h = max(len(c) for c in cells) * 11 + 8
            if doc.need(h):
                _table_head(doc)
            if i % 2:
                doc.box(doc.M, doc.y - h, full, h, ZEBRA)
            x = doc.M
            for (_, w, _), lines in zip(_TABLE, cells):
                for n, line in enumerate(lines):
                    doc.text(x + 5, doc.y - 12 - n * 11, line, "F1", 8.5, TEXT)
                x += w
            doc.y -= h
            doc.rule(doc.M, doc.y, doc.M + full)
        # every application in full
        doc.need(60)
        doc.y -= 26
        doc.text(doc.M, doc.y, pdf_text("Application details"), "F2", 13, TEXT)
        doc.y -= 14
        for d in items:
            rows = [("Location", d.get("location")), ("Job URL", d.get("url")), ("Job ID", d.get("job_id")),
                    ("Date applied", nice_day(d.get("applied_on"))), ("Posting date", nice_day(d.get("posted_on"))),
                    ("Application progress", d.get("progress")), ("Email response", d.get("email_status")),
                    ("Follow-up", follow_up_label(d)), ("Application source", source_label(d)),
                    ("Salary/package", d.get("salary")), ("Resume", d.get("resume")),
                    ("Interviews", "\n".join(interview_lines(d))), ("Notes", d.get("notes"))]
            rows = [(k, wrap(v, "F1", 9, full - 130)) for k, v in rows if v]
            title = wrap(f"{d.get('company') or ''} — {d.get('title') or ''}", "F2", 11, full)
            h = 22 + len(title) * 14 + sum(len(v) * 12 + 3 for _, v in rows)
            if h < doc.H - 2 * doc.M:
                doc.need(h)
            else:
                doc.need(80)
            doc.y -= 10
            doc.rule(doc.M, doc.y, doc.M + full, (0.83, 0.84, 0.89))
            doc.y -= 6
            for line in title:
                doc.y -= 14
                doc.text(doc.M, doc.y, line, "F2", 11, TEXT)
            doc.y -= 4
            for key, lines in rows:
                for n, line in enumerate(lines):
                    doc.need(12)
                    doc.y -= 12
                    if n == 0:
                        doc.text(doc.M, doc.y, pdf_text(key), "F2", 8.5, MUTED)
                    doc.text(doc.M + 130, doc.y, line, "F1", 9, TEXT)
                doc.y -= 3
    return _assemble(doc, "Application Tracker export" + (f" — {scope}" if scope else ""), stamp)


def _assemble(doc: _Doc, title: str, stamp: datetime) -> bytes:
    n = len(doc.pages)
    objs: list[bytes] = []                          # object i + 1

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    catalog = add(b"")
    pages_obj = add(b"")
    f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    kids = []
    for i, ops in enumerate(doc.pages):
        foot = pdf_text(f"Page {i + 1} of {n}")
        ops = ops + [f"0.392 0.416 0.510 rg BT /F1 8 Tf 1 0 0 1 {doc.W - doc.M - _width(foot, 'F1', 8):.2f} 20 Tm "
                     f"({_esc(foot)}) Tj ET",
                     f"0.392 0.416 0.510 rg BT /F1 8 Tf 1 0 0 1 {doc.M:.2f} 20 Tm "
                     f"({_esc(pdf_text('Fresher Job Tracker · Application Tracker · private to your account'))}) Tj ET"]
        raw = zlib.compress("\n".join(ops).encode("latin-1"))
        content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(raw) + raw + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {doc.W:.0f} {doc.H:.0f}] "
                        f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> /Contents {content} 0 R >>".encode()))
    objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    objs[pages_obj - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {n} >>".encode()
    when = stamp.astimezone(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
    info = add(f"<< /Title ({_esc(pdf_text(title))}) /Producer (Fresher Job Tracker) /CreationDate ({when}) >>".encode())
    out = BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs):
        offsets.append(out.tell())
        out.write(f"{i + 1} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
