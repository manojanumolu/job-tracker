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


def exportable(docs) -> list[dict]:
    """Live applications only, newest application first."""
    return apps.sort_apps(apps.live(docs), "applied_new")


def file_name(ext: str, now: float | None = None) -> str:
    stamp = datetime.fromtimestamp(time.time() if now is None else now, apps.IST).strftime("%Y-%m-%d")
    return f"applications-{stamp}.{ext}"


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
# Three sheets, in this order:
#   Summary       counts of exactly the exported applications (nothing else)
#   Applications  one row each — the fields to scan first, then the rest
#   Interviews    one row per interview round
# Each list is a real Excel table (filter buttons, banded rows) under a
# title, with its header row frozen and repeated on printed pages. Dates
# are Excel dates, links are clickable, statuses are coloured. Every value
# is kept in full: long text wraps, its row grows up to MAX_LINES lines,
# and anything longer stays in the cell (the formula bar shows it all).

_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_EXCEL_EPOCH = date(1899, 12, 30)
HEAD_ROW = 4                 # title, subtitle, a gap, then a table's header row
MAX_LINES = 8

# tone -> (fill, text colour)
_TONES = {"blue": ("FFE8EEFD", "FF2347B5"), "purple": ("FFF1EAFE", "FF6B32C8"), "amber": ("FFFEF3D8", "FF8A5A00"),
          "green": ("FFE3F6EA", "FF17703A"), "teal": ("FFDDF4F1", "FF0B6B63"), "red": ("FFFDE7E7", "FFB42318"),
          "gray": ("FFF0F1F4", "FF4B5262"), "orange": ("FFFFEDDF", "FFB2470B")}
PROGRESS_TONE = {"Applied": "blue", "Assessment": "purple", "Interview": "amber", "Offer Received": "green",
                 "Accepted": "teal", "Rejected": "red", "Withdrawn": "gray", "On Hold": "orange"}
EMAIL_TONE = {"No Email Received": "gray", "Acknowledgement Received": "blue", "Assessment Email Received": "purple",
              "Interview Email Received": "amber", "Rejection Email Received": "red", "Offer Email Received": "green",
              "Other Email Received": "gray"}
FOLLOW_UP_WORDS = {"overdue": "Overdue", "today": "Due today", "upcoming": "Upcoming", "done": "Done"}
FOLLOW_UP_TONE = {"Overdue": "red", "Due today": "amber", "Upcoming": "blue", "Done": "green"}
OUTCOME_TONE = {"Awaiting result": "amber", "Cleared": "green", "Not cleared": "red", "Rescheduled": "blue",
                "Cancelled": "gray"}


def _follow_up_state(doc: dict, today: date) -> str:
    return FOLLOW_UP_WORDS.get(apps.follow_up_state(doc, today), "")


def _rounds(doc: dict) -> list[dict]:
    return [r for r in doc.get("interviews") or [] if isinstance(r, dict)]


def _updated_at(doc: dict) -> datetime | None:
    ts = apps._iso_seconds(doc.get("updated_at"))
    return datetime.fromtimestamp(ts, apps.IST).replace(tzinfo=None) if ts else None


# (header, width, value, kind) — kind is a cell style, or a tone map name
COLUMNS = [
    ("Company", 20, lambda d, t: d.get("company") or "", "strong"),
    ("Job title", 30, lambda d, t: d.get("title") or "", "wrap"),
    ("Status", 16, lambda d, t: d.get("progress") or "", "progress"),
    ("Date applied", 13, lambda d, t: d.get("applied_on") or "", "date"),
    ("Email response", 24, lambda d, t: d.get("email_status") or "", "email"),
    ("Follow-up date", 14, lambda d, t: (d.get("follow_up") or {}).get("date") or "", "date"),
    ("Follow-up status", 15, lambda d, t: _follow_up_state(d, t), "follow_up"),
    ("Interviews", 11, lambda d, t: len(_rounds(d)), "int"),
    ("Location", 18, lambda d, t: d.get("location") or "", "wrap"),
    ("Posting date", 13, lambda d, t: d.get("posted_on") or "", "date"),
    ("Source", 20, lambda d, t: source_label(d), "wrap"),
    ("Salary / package", 15, lambda d, t: d.get("salary") or "", "wrap"),
    ("Resume version", 22, lambda d, t: d.get("resume") or "", "wrap"),
    ("Job ID", 13, lambda d, t: d.get("job_id") or "", "text"),
    ("Job URL", 30, lambda d, t: d.get("url") or "", "url"),
    ("Notes", 50, lambda d, t: d.get("notes") or "", "wrap"),
    ("Last updated", 18, lambda d, t: _updated_at(d), "datetime"),
]
INTERVIEW_COLUMNS = [
    ("Company", 20, lambda d, r: d.get("company") or "", "strong"),
    ("Job title", 28, lambda d, r: d.get("title") or "", "wrap"),
    ("Interview round", 20, lambda d, r: r.get("round") or "", "wrap"),
    ("Date", 13, lambda d, r: r.get("date") or "", "date"),
    ("Time", 9, lambda d, r: r.get("time") or "", "text"),
    ("Interview type", 13, lambda d, r: r.get("type") or "", "text"),
    ("Location or meeting link", 30, lambda d, r: r.get("location") or "", "maybe_url"),
    ("Interviewer", 20, lambda d, r: r.get("interviewer") or "", "wrap"),
    ("Outcome", 16, lambda d, r: r.get("outcome") or "", "outcome"),
    ("Preparation notes", 42, lambda d, r: r.get("prep") or "", "wrap"),
    ("Feedback", 42, lambda d, r: r.get("feedback") or "", "wrap"),
]
_TONE_MAPS = {"progress": PROGRESS_TONE, "email": EMAIL_TONE, "follow_up": FOLLOW_UP_TONE, "outcome": OUTCOME_TONE}


def _x(text: object) -> str:
    return escape(_XML_BAD.sub("", str(text))[:32767])


def _col(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# ── styles ───────────────────────────────────────────────────────────────────
_DARK, _MUTED, _ACCENT, _LINE = "FF1F2937", "FF6B7280", "FF4636D4", "FFE3E6EE"
_FONTS = [f'<font><sz val="11"/><color rgb="{_DARK}"/><name val="Calibri"/><family val="2"/></font>',
          '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>',
          f'<font><u/><sz val="11"/><color rgb="{_ACCENT}"/><name val="Calibri"/><family val="2"/></font>',
          f'<font><b/><sz val="18"/><color rgb="{_DARK}"/><name val="Calibri"/><family val="2"/></font>',
          f'<font><sz val="10"/><color rgb="{_MUTED}"/><name val="Calibri"/><family val="2"/></font>',
          f'<font><b/><sz val="12"/><color rgb="{_ACCENT}"/><name val="Calibri"/><family val="2"/></font>',
          f'<font><b/><sz val="14"/><color rgb="{_DARK}"/><name val="Calibri"/><family val="2"/></font>',
          f'<font><b/><sz val="11"/><color rgb="{_DARK}"/><name val="Calibri"/><family val="2"/></font>']
_FILLS = ['<fill><patternFill patternType="none"/></fill>', '<fill><patternFill patternType="gray125"/></fill>',
          '<fill><patternFill patternType="solid"><fgColor rgb="FF4F46E5"/><bgColor indexed="64"/></patternFill></fill>',
          '<fill><patternFill patternType="solid"><fgColor rgb="FFF5F6FB"/><bgColor indexed="64"/></patternFill></fill>']
_BORDER = (f'<border><left style="thin"><color rgb="{_LINE}"/></left><right style="thin"><color rgb="{_LINE}"/></right>'
           f'<top style="thin"><color rgb="{_LINE}"/></top><bottom style="thin"><color rgb="{_LINE}"/></bottom><diagonal/></border>')
_TOP, _TOP_WRAP = '<alignment vertical="top"/>', '<alignment vertical="top" wrapText="1"/>'
_CENTER = '<alignment horizontal="center" vertical="top"/>'
# name -> (numFmtId, fontId, fillId, borderId, alignment)
_XF = {"default": (0, 0, 0, 0, ""), "head": (0, 1, 2, 1, '<alignment vertical="center" wrapText="1"/>'),
       "text": (0, 0, 0, 1, _TOP), "strong": (0, 7, 0, 1, _TOP_WRAP), "wrap": (0, 0, 0, 1, _TOP_WRAP),
       "date": (164, 0, 0, 1, '<alignment horizontal="left" vertical="top"/>'),
       "datetime": (165, 0, 0, 1, '<alignment horizontal="left" vertical="top"/>'),
       "url": (0, 2, 0, 1, _TOP), "int": (1, 0, 0, 1, _CENTER), "pct": (9, 0, 0, 1, _CENTER),
       "title": (0, 3, 0, 0, '<alignment vertical="center"/>'), "sub": (0, 4, 0, 0, '<alignment vertical="center"/>'),
       "section": (0, 5, 0, 0, '<alignment vertical="bottom"/>'),
       "kpi_label": (0, 0, 3, 1, '<alignment vertical="center" indent="1"/>'),
       "kpi_value": (1, 6, 3, 1, '<alignment horizontal="left" vertical="center" indent="1"/>'),
       "kpi_date": (164, 6, 3, 1, '<alignment horizontal="left" vertical="center" indent="1"/>')}
for _i, (_tone, (_fill, _ink)) in enumerate(_TONES.items()):
    _FONTS.append(f'<font><b/><sz val="10"/><color rgb="{_ink}"/><name val="Calibri"/><family val="2"/></font>')
    _FILLS.append(f'<fill><patternFill patternType="solid"><fgColor rgb="{_fill}"/><bgColor indexed="64"/></patternFill></fill>')
    _XF[f"tone_{_tone}"] = (0, len(_FONTS) - 1, len(_FILLS) - 1, 1, '<alignment horizontal="center" vertical="top" wrapText="1"/>')
S = {name: i for i, name in enumerate(_XF)}


def _styles() -> str:
    xfs = "".join(f'<xf numFmtId="{n}" fontId="{f}" fillId="{fi}" borderId="{b}" xfId="0"'
                  + (' applyNumberFormat="1"' if n else "") + (' applyFont="1"' if f else "")
                  + (' applyFill="1"' if fi else "") + (' applyBorder="1"' if b else "")
                  + (f' applyAlignment="1">{a}</xf>' if a else "/>") for n, f, fi, b, a in _XF.values())
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<numFmts count="2"><numFmt numFmtId="164" formatCode="dd mmm yyyy"/>'
            '<numFmt numFmtId="165" formatCode="dd mmm yyyy, hh:mm"/></numFmts>'
            f'<fonts count="{len(_FONTS)}">{"".join(_FONTS)}</fonts><fills count="{len(_FILLS)}">{"".join(_FILLS)}</fills>'
            f'<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>{_BORDER}</borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            f'<cellXfs count="{len(_XF)}">{xfs}</cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '<dxfs count="0"/><tableStyles count="0" defaultTableStyle="TableStyleLight1" defaultPivotStyle="PivotStyleLight16"/>'
            '</styleSheet>')


# ── cells, rows and sheets ───────────────────────────────────────────────────

def _cell(ref: str, value: object, style: str, sst: dict[str, int]) -> str:
    s = S[style]
    if isinstance(value, datetime):
        serial = (value.date() - _EXCEL_EPOCH).days + (value.hour * 3600 + value.minute * 60 + value.second) / 86400
        return f'<c r="{ref}" s="{s}"><v>{serial:.6f}</v></c>'
    if style in ("date", "kpi_date"):
        day = value if isinstance(value, date) else _day(value)
        if day:
            return f'<c r="{ref}" s="{s}"><v>{(day - _EXCEL_EPOCH).days}</v></c>'
    if isinstance(value, bool) or value is None or value == "":
        return f'<c r="{ref}" s="{s}"/>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{s}"><v>{value}</v></c>'
    # a shared string is never a formula, whatever it starts with
    text = _XML_BAD.sub("", str(value))[:32767]
    return f'<c r="{ref}" t="s" s="{s}"><v>{sst.setdefault(text, len(sst))}</v></c>'


def _lines(text: object, width: float) -> int:
    per = max(1, int(width * 1.15))
    return sum(max(1, -(-len(seg) // per)) for seg in str(text).split("\n")) if text not in ("", None) else 1


class _Sheet:
    """One worksheet: rows of (value, style) cells from column A, column
    widths, links, at most one table and the print setup."""

    def __init__(self, widths: list[float]):
        self.widths, self.rows, self.links, self.merges, self.table = widths, [], [], [], None

    def add(self, cells=(), height: float | None = None) -> int:
        self.rows.append((list(cells), height))
        return len(self.rows)

    def gap(self, height: float = 10) -> None:
        self.add((), height)

    def heading(self, title: str, subtitle: str) -> None:
        self.add([(title, "title")], 30)
        self.add([(subtitle, "sub")], 18)
        self.gap(8)

    def records(self, name: str, columns: list[tuple], rows: list[list[tuple]]) -> None:
        """A table: the header row, then the rows (each row's height fits its
        longest wrapped text, up to MAX_LINES lines)."""
        top = self.add([(h, "head") for h, *_ in columns], 30)
        for cells in rows:
            lines = max([_lines(v, w) for (v, kind), (_, w, *_) in zip(cells, columns) if kind in ("wrap", "strong")]
                        or [1])
            r = self.add(cells, min(lines, MAX_LINES) * 15 + 6 if lines > 1 else 21)
            for c, (v, kind) in enumerate(cells):
                if kind == "url" and v:
                    self.links.append((f"{_col(c)}{r}", v))
        if not rows:
            self.add([("", "text")] * len(columns), 21)              # a table holds at least one row
        self.table = (name, top, len(self.rows), [h for h, *_ in columns])

    def xml(self, sst: dict[str, int], freeze_row: int = 0, freeze_col: int = 0) -> str:
        width = max([len(self.widths)] + [len(c) for c, _ in self.rows])
        cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(self.widths))
        body = []
        for r, (cells, height) in enumerate(self.rows, 1):
            ht = f' ht="{height}" customHeight="1"' if height else ""
            body.append(f'<row r="{r}"{ht}>' + "".join(_cell(f"{_col(c)}{r}", v, k, sst) for c, (v, k) in enumerate(cells))
                        + "</row>")
        pane = ""
        if freeze_row or freeze_col:
            where = "bottomRight" if freeze_row and freeze_col else "bottomLeft" if freeze_row else "topRight"
            cell = f"{_col(freeze_col)}{freeze_row + 1}"
            split = (f' xSplit="{freeze_col}"' if freeze_col else "") + (f' ySplit="{freeze_row}"' if freeze_row else "")
            pane = (f'<pane{split} topLeftCell="{cell}" activePane="{where}" state="frozen"/>'
                    f'<selection pane="{where}" activeCell="{cell}" sqref="{cell}"/>')
        merges = (f'<mergeCells count="{len(self.merges)}">' + "".join(f'<mergeCell ref="{m}"/>' for m in self.merges)
                  + "</mergeCells>") if self.merges else ""
        links = ("<hyperlinks>" + "".join(f'<hyperlink ref="{ref}" r:id="rId{i + 1}"/>' for i, (ref, _) in enumerate(self.links))
                 + "</hyperlinks>") if self.links else ""
        table = f'<tableParts count="1"><tablePart r:id="rId{len(self.links) + 1}"/></tableParts>' if self.table else ""
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
                f'<dimension ref="A1:{_col(width - 1)}{max(1, len(self.rows))}"/>'
                f'<sheetViews><sheetView workbookViewId="0" showGridLines="0">{pane}</sheetView></sheetViews>'
                f'<sheetFormatPr defaultRowHeight="15"/><cols>{cols}</cols><sheetData>{"".join(body)}</sheetData>'
                f'{merges}{links}'
                '<pageMargins left="0.4" right="0.4" top="0.5" bottom="0.5" header="0.3" footer="0.3"/>'
                '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
                f'{table}</worksheet>')

    def rels(self) -> str | None:
        if not (self.links or self.table):
            return None
        items = [f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                 f'relationships/hyperlink" Target={quoteattr(_XML_BAD.sub("", url))} TargetMode="External"/>'
                 for i, (_, url) in enumerate(self.links)]
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(items)
                + (f'<Relationship Id="rId{len(self.links) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/'
                   f'2006/relationships/table" Target="../tables/{{TABLE}}"/>' if self.table else "")
                + "</Relationships>")

    def table_xml(self, table_id: int) -> str:
        name, top, bottom, headers = self.table
        ref = f"A{top}:{_col(len(headers) - 1)}{bottom}"
        cols = "".join(f'<tableColumn id="{i + 1}" name={quoteattr(_XML_BAD.sub("", h))}/>' for i, h in enumerate(headers))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" id="{table_id}" '
                f'name="{name}" displayName="{name}" ref="{ref}" totalsRowShown="0">'
                f'<autoFilter ref="{ref}"/><tableColumns count="{len(headers)}">{cols}</tableColumns>'
                '<tableStyleInfo name="TableStyleLight1" showFirstColumn="0" showLastColumn="0" showRowStripes="1" '
                'showColumnStripes="0"/></table>')


def _kind(kind: str, value: object) -> str:
    """A column's kind -> the cell style for this value."""
    if kind in _TONE_MAPS:
        return f"tone_{_TONE_MAPS[kind].get(value, 'gray')}" if value else "text"
    if kind == "maybe_url":
        return "url" if isinstance(value, str) and re.match(r"^https?://", value.strip(), re.I) else "wrap"
    return kind


def _summary_sheet(items: list[dict], scope: str, stamp: datetime, today: date) -> _Sheet:
    total = len(items)
    rounds = [r for d in items for r in _rounds(d)]
    states = [apps.follow_up_state(d, today) for d in items]
    applied = [x for x in (_day(d.get("applied_on")) for d in items) if x]
    sh = _Sheet([34, 34, 18, 16])
    sh.heading("Application Tracker — Summary",
               f"{_plural(total, 'application')} exported · {scope or 'All applications'} · "
               f"{stamp.strftime('%d %b %Y, %H:%M')} IST")
    sh.add([("Overview", "section")], 22)
    for label, value in (("Total applications", total),
                         ("Applications with interviews", sum(1 for d in items if _rounds(d))),
                         ("Interview rounds", len(rounds)),
                         ("Offers received", sum(1 for d in items if d.get("progress") in apps.OFFER_REACHED)),
                         ("Rejected applications", sum(1 for d in items if d.get("progress") == "Rejected")),
                         ("Upcoming follow-ups", sum(1 for s in states if s in ("today", "upcoming"))),
                         ("Overdue follow-ups", sum(1 for s in states if s == "overdue"))):
        sh.add([(label, "kpi_label"), (value, "kpi_value")], 24)
    for title, head, values, field, tones in (
            ("Applications by status", "Status", apps.PROGRESS, "progress", PROGRESS_TONE),
            ("Email responses", "Email response", apps.EMAIL_STATUSES, "email_status", EMAIL_TONE)):
        counts = {v: 0 for v in values}
        for d in items:
            counts[d.get(field) or ""] = counts.get(d.get(field) or "", 0) + 1
        sh.gap(14)
        sh.add([(title, "section")], 22)
        sh.add([(head, "head"), ("Applications", "head"), ("Share", "head")], 22)
        for v, n in counts.items():
            if v or n:
                sh.add([(v or "Not set", f"tone_{tones.get(v, 'gray')}"), (n, "int"), (n / total if total else 0, "pct")], 20)
    sh.gap(14)
    sh.add([("Application dates", "section")], 22)
    for label, value, style in (("First application", min(applied) if applied else "", "kpi_date"),
                                ("Latest application", max(applied) if applied else "", "kpi_date"),
                                ("Applied in the last 7 days", sum(1 for x in applied if 0 <= (today - x).days < 7), "kpi_value"),
                                ("Applied in the last 30 days", sum(1 for x in applied if 0 <= (today - x).days < 30), "kpi_value")):
        sh.add([(label, "kpi_label"), (value, style)], 24)
    sh.gap(14)
    sh.add([("Open follow-ups", "section")], 22)
    buckets = apps.follow_up_buckets(items, today)
    open_ = buckets["overdue"] + buckets["today"] + buckets["upcoming"]
    if not open_:
        sh.add([("No open follow-ups", "text")], 20)
    else:
        sh.add([("Company", "head"), ("Job title", "head"), ("Follow-up date", "head"), ("Status", "head")], 22)
        for d in open_:
            state = _follow_up_state(d, today)
            sh.add([(d.get("company") or "", "strong"), (d.get("title") or "", "wrap"),
                    (d["follow_up"]["date"], "date"), (state, f"tone_{FOLLOW_UP_TONE[state]}")],
                   max(21, min(_lines(d.get("title"), 34), MAX_LINES) * 15 + 6))
    return sh


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def to_xlsx(docs, scope: str = "", now: float | None = None) -> bytes:
    """A workbook: "Summary", "Applications" (one row each) and "Interviews"
    (one row per interview round) — exactly the applications passed in."""
    items = exportable(docs)
    ts = time.time() if now is None else now
    stamp = datetime.fromtimestamp(ts, apps.IST)
    today = apps.today_ist(ts)
    subtitle = (f"{scope or 'All applications'} · exported {stamp.strftime('%d %b %Y, %H:%M')} IST")

    summary = _summary_sheet(items, scope, stamp, today)
    app_sheet = _Sheet([w for _, w, *_ in COLUMNS])
    app_sheet.heading("Applications", f"{_plural(len(items), 'application')} · {subtitle}")
    app_sheet.records("ApplicationsTable", COLUMNS, [[(v := get(d, today), _kind(kind, v)) for _, _, get, kind in COLUMNS]
                                                for d in items])
    pairs = [(d, r) for d in items for r in _rounds(d)]
    iv_sheet = _Sheet([w for _, w, *_ in INTERVIEW_COLUMNS])
    iv_sheet.heading("Interviews", f"{_plural(len(pairs), 'interview round')} · {subtitle}")
    iv_sheet.records("InterviewsTable", INTERVIEW_COLUMNS,
                     [[(v := get(d, r), _kind(kind, v)) for _, _, get, kind in INTERVIEW_COLUMNS] for d, r in pairs])

    sheets = [("Summary", summary, (0, 0)), ("Applications", app_sheet, (HEAD_ROW, 2)), ("Interviews", iv_sheet, (HEAD_ROW, 2))]
    title = "Application Tracker export" + (f" — {scope}" if scope else "")
    ct = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    files = {
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
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{apps._now_iso(now)}</dcterms:created></cp:coreProperties>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<bookViews><workbookView activeTab="0"/></bookViews><sheets>'
        + "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, (n, _, _) in enumerate(sheets))
        + '</sheets><definedNames>'
        + "".join(f'<definedName name="_xlnm.Print_Titles" localSheetId="{i}">{n}!${HEAD_ROW}:${HEAD_ROW}</definedName>'
                  for i, (n, sh, _) in enumerate(sheets) if sh.table)
        + '</definedNames></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                  f'relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets)))
        + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
          'relationships/styles" Target="styles.xml"/>'
        + f'<Relationship Id="rId{len(sheets) + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
          'relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>',
        "xl/styles.xml": _styles(),
    }
    overrides = ['<Override PartName="/xl/workbook.xml" ContentType="' + ct + '.sheet.main+xml"/>',
                 '<Override PartName="/xl/styles.xml" ContentType="' + ct + '.styles+xml"/>',
                 '<Override PartName="/xl/sharedStrings.xml" ContentType="' + ct + '.sharedStrings+xml"/>',
                 '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>']
    n_tables, sst = 0, {}
    for i, (_, sh, (fr, fc)) in enumerate(sheets, 1):
        files[f"xl/worksheets/sheet{i}.xml"] = sh.xml(sst, fr, fc)
        overrides.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="{ct}.worksheet+xml"/>')
        rels = sh.rels()
        if sh.table:
            n_tables += 1
            files[f"xl/tables/table{n_tables}.xml"] = sh.table_xml(n_tables)
            overrides.append(f'<Override PartName="/xl/tables/table{n_tables}.xml" ContentType="{ct}.table+xml"/>')
            rels = rels.replace("{TABLE}", f"table{n_tables}.xml")
        if rels:
            files[f"xl/worksheets/_rels/sheet{i}.xml.rels"] = rels
    used = sum(files[f"xl/worksheets/sheet{i}.xml"].count(' t="s" ') for i in range(1, len(sheets) + 1))
    files["xl/sharedStrings.xml"] = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                     '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                                     f'count="{used}" uniqueCount="{len(sst)}">'
                                     + "".join(f'<si><t xml:space="preserve">{escape(t)}</t></si>' for t in sst)
                                     + "</sst>")
    files = {"[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>' + "".join(overrides) + '</Types>', **files}
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
