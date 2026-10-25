"""Read a broker / registrar report (CSV or XLSX) into a plain table, finding the real header row under any banner.

Back-office exports put a banner above the table (client name, client code, PAN, DP id, report date) and totals below it. This reader:
  * scans the first rows for the one that looks like the header (at least `min_fields` distinct known fields, given by the caller as an alias -> canonical vocabulary);
  * SKIPS the banner and the footer totals and only COUNTS them: banner text is personal data and is never returned, stored, logged or printed;
  * returns a `layout` hash of the normalised header names, so a reviewer approves a layout once and a different layout is reviewed again.
Old binary .xls is refused with a clear message (re-save as .xlsx or .csv): reading it would need another parser for a format Excel itself has retired.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Mapping, Optional

MAX_BYTES = 25_000_000
MAX_ROWS = 200_000
SCAN_ROWS = 40
FOOTER = re.compile(r"^(grand\s+)?(sub\s*)?total|^net\s+(total|amount|value)|^report\s+generated|^disclaimer|^note\b|^\*+\s*$", re.I)


def norm(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h).lower())


@dataclass
class Table:
    header: list = field(default_factory=list)       # the raw header cells (column titles are not personal data)
    rows: list = field(default_factory=list)         # list of list[str], padded to the header width
    header_row: int = 0                              # 0-based position of the header in the sheet
    skipped_banner: int = 0
    skipped_footer: int = 0
    sheet: Optional[str] = None
    layout: str = ""
    sha256: str = ""
    fields: dict = field(default_factory=dict)       # canonical field -> header index (first match)
    unmatched: list = field(default_factory=list)    # header titles that matched no known field


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return repr(v) if v != int(v) else str(int(v))
    return str(v).strip()


def _decode(payload: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return payload.decode(enc)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _csv_rows(payload: bytes) -> list:
    text = _decode(payload)
    sample = "\n".join(text.splitlines()[:SCAN_ROWS])
    try:
        dialect = csv.Sniffer().sniff(sample[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]


def _xlsx_sheets(payload: bytes) -> list:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            rows = []
            for i, r in enumerate(ws.iter_rows(values_only=True)):
                if i >= MAX_ROWS:
                    raise ValueError(f"more than {MAX_ROWS} rows in sheet {ws.title!r}")
                rows.append([_cell(c) for c in r])
            out.append((ws.title, rows))
    finally:
        wb.close()
    return out


def _find_header(rows: list, vocab: Mapping, min_fields: int) -> Optional[int]:
    for i, r in enumerate(rows[:SCAN_ROWS]):
        got = {vocab[norm(c)] for c in r if c and norm(c) in vocab}
        if len(got) >= min_fields:
            return i
    return None


def read_table(payload: bytes, filename: str, vocab: Mapping, min_fields: int = 3) -> Table:
    """vocab: {normalised alias: canonical field}. Raises ValueError (with no personal data in the message) when the file cannot be read as a table of the expected kind."""
    if len(payload) > MAX_BYTES:
        raise ValueError(f"file is larger than {MAX_BYTES // 1_000_000} MB")
    name = (filename or "").lower()
    if name.endswith(".xls"):
        raise ValueError("old binary .xls files are not read: open it in Excel and save it as .xlsx or .csv, then import that")
    if payload[:2] == b"PK" or name.endswith((".xlsx", ".xlsm")):
        try:
            sheets = _xlsx_sheets(payload)
        except ValueError:
            raise
        except Exception as e:                                                              # noqa: BLE001
            raise ValueError(f"could not open the workbook ({type(e).__name__}); is it a damaged or password-protected file?") from e
    elif payload[:4] == b"%PDF":
        raise ValueError("PDF statements are not read here (they are password-protected and their layout varies): export the transactions as CSV or XLSX instead")
    else:
        sheets = [(None, _csv_rows(payload))]
    for sheet, rows in sheets:
        h = _find_header(rows, vocab, min_fields)
        if h is None:
            continue
        header = rows[h]
        width = len(header)
        body, footer = [], 0
        for r in rows[h + 1:]:
            if not any(c for c in r):
                continue
            first = next((c for c in r if c), "")
            if FOOTER.match(first):
                footer += 1
                continue
            body.append((r + [""] * width)[:width])
        names = [norm(c) for c in header]
        fields: dict = {}
        for i, n in enumerate(names):
            if n and n in vocab and vocab[n] not in fields:
                fields[vocab[n]] = i
        t = Table(header=header, rows=body, header_row=h, skipped_banner=h, skipped_footer=footer, sheet=sheet, fields=fields,
                  unmatched=[c for i, c in enumerate(header) if c and i not in set(fields.values())])
        t.layout = hashlib.sha256("|".join(n for n in names if n).encode()).hexdigest()
        t.sha256 = hashlib.sha256(payload).hexdigest()
        return t
    raise ValueError(f"no header row found in the first {SCAN_ROWS} rows of the file; expected at least {min_fields} of the columns: {', '.join(sorted(set(vocab.values())))}")


def aliases(spec: Mapping[str, Iterable[str]]) -> dict:
    """{canonical: [alias, ...]} -> {normalised alias: canonical}."""
    out: dict = {}
    for canon, names in spec.items():
        for n in (canon, *names):
            out.setdefault(norm(n), canon)
    return out


def to_csv_text(t: Table) -> str:
    """The table as clean CSV text (header + rows, banner and footer gone), for parsers that take CSV text."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(t.header)
    w.writerows(t.rows)
    return buf.getvalue()
