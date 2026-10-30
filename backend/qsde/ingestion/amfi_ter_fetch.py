"""Fetch AMFI's own monthly "TER of MF Schemes" workbook into the inbox: the one file the page's "Download Excel" button gives, never the table behind it page by page.

Deliberately gentle: at most the month list plus a few workbooks per run, a pause between them, an honest User-Agent, no cookies, no retry after a refusal (HTTP 4xx).
The files then go through the same reviewed loader as hand-saved ones; nothing here writes to the database. Run by the owner (about once a month), not by the daily routine."""
from __future__ import annotations

import hashlib
import io
import os
import re
import time
import zipfile
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

from qsde.ingestion.amfi_ter_db import INBOX

BASE = "https://www.amfiindia.com"
MONTHS_URL = f"{BASE}/api/populate-ter-month"
EXCEL_URL = f"{BASE}/api/populate-te-rdata-revised"
REFERER = f"{BASE}/ter-of-mf-schemes"
USER_AGENT = "Stoxsy-personal/1.0 (personal, non-commercial research; about one request a month)"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PAUSE_SECONDS = 5                                  # between two workbook requests
RECENT_MONTHS = 2                                  # by default look only at this month and last
MAX_PER_RUN = 3                                    # workbooks fetched per run, whatever else is missing
STALE_DAYS = 2                                     # a month is behind when its newest loaded day is older than this many days
MONTH_RE = re.compile(r"^(0[1-9]|1[0-2])-(\d{4})$")


class BadDownload(ValueError):
    """The response is not a usable workbook (a block page, a truncated body, the wrong type)."""


class Refused(RuntimeError):
    """AMFI answered 4xx: stop, do not retry."""


def financial_year(month: str) -> str:
    """'10-2026' -> '2026-2027' (the Indian financial year runs April to March)."""
    mm, yyyy = MONTH_RE.match(month).groups()
    start = int(yyyy) if int(mm) >= 4 else int(yyyy) - 1
    return f"{start}-{start + 1}"


def _key(month: str) -> tuple[int, int]:
    mm, yyyy = MONTH_RE.match(month).groups()
    return int(yyyy), int(mm)


def parse_months(payload) -> list[str]:
    """The month list's MonthNumber values ('MM-YYYY'), well-formed and unique, newest first. A payload that is not a list is an error, not an empty list."""
    if not isinstance(payload, list):
        raise ValueError("AMFI's month list is not a list")
    seen = {m for r in payload if isinstance(r, dict) for m in [r.get("MonthNumber")] if isinstance(m, str) and MONTH_RE.match(m)}
    return sorted(seen, key=_key, reverse=True)


def plan(available: list[str], loaded_max: dict[str, date], today: date, recent: int = RECENT_MONTHS, limit: int = MAX_PER_RUN) -> list[str]:
    """Months to fetch, newest first: among the `recent` newest started months, those never loaded or loaded only up to before their end (or before `today` - STALE_DAYS for the current month)."""
    started = [m for m in available if _key(m) <= (today.year, today.month)][:recent]
    out = []
    for m in started:
        yyyy, mm = _key(m)
        needed = min(date(yyyy, mm, monthrange(yyyy, mm)[1]), today - timedelta(days=STALE_DAYS))
        if m not in loaded_max or loaded_max[m] < needed:
            out.append(m)
    return out[:max(limit, 0)]


def check_payload(body: bytes, content_type: str) -> None:
    """Refuse anything that is not a whole .xlsx workbook, before it is kept."""
    if not (content_type or "").lower().startswith(XLSX_TYPE):
        raise BadDownload(f"AMFI sent {content_type or 'no content type'}, not a workbook")
    if not body.startswith(b"PK\x03\x04"):
        raise BadDownload("the response does not start like an .xlsx file")
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            if z.testzip() is not None or "xl/workbook.xml" not in z.namelist():
                raise BadDownload("the workbook archive is damaged or incomplete")
    except zipfile.BadZipFile as e:
        raise BadDownload(f"the workbook archive is damaged or truncated ({e})") from e


def _session():
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Referer": REFERER})
    return s


def _get(s, url: str, params: dict, accept: str, timeout: int, tries: int = 3):
    """One GET with a short, polite retry for network errors and 5xx. A 4xx (blocked, throttled, gone) raises Refused at once."""
    import requests

    last = None
    for i in range(tries):
        try:
            r = s.get(url, params=params, headers={"Accept": accept}, timeout=timeout)
        except requests.RequestException as e:
            last = e
        else:
            if 400 <= r.status_code < 500:
                raise Refused(f"AMFI answered HTTP {r.status_code}; stopping")
            if r.status_code < 400:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        if i < tries - 1:
            time.sleep(10 * (i + 1))
    raise RuntimeError(f"AMFI did not answer after {tries} tries: {last}")


def list_months(fy: str, session=None) -> list[str]:
    """The months AMFI has published for a financial year ('2026-2027'), newest first. Metadata only."""
    r = _get(session or _session(), MONTHS_URL, {"year": fy}, "application/json", 30)
    return parse_months(r.json())


def fetch_month(month: str, session=None) -> bytes:
    """AMFI's workbook for one month, validated. One request; the same one the page's Download Excel button sends."""
    r = _get(session or _session(), EXCEL_URL, {"MF_ID": "All", "Month": month, "strCat": "-1", "strType": "-1", "excel": "true"}, XLSX_TYPE, 180)
    check_payload(r.content, r.headers.get("Content-Type", ""))
    return r.content


def save(body: bytes, month: str, today: date, inbox: Path = INBOX) -> Path | None:
    """Keep the workbook in the inbox as ter_revised_MM-YYYY__<fetch date>.xlsx (written under a .part name, then renamed). None when identical bytes are already in the inbox."""
    inbox = Path(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    for p in inbox.iterdir():
        if p.is_file() and p.suffix.lower() == ".xlsx" and not p.name.startswith("~$") and hashlib.sha256(p.read_bytes()).hexdigest() == digest:
            return None
    final = inbox / f"ter_revised_{month}__{today.isoformat()}.xlsx"
    part = final.with_name(final.name + ".part")
    part.write_bytes(body)
    os.replace(part, final)
    return final


def loaded_months(cur) -> dict[str, date]:
    """Newest loaded TER day per 'MM-YYYY' month, from the database."""
    cur.execute("SELECT to_char(date_trunc('month', ter_date), 'MM-YYYY') AS m, max(ter_date) AS d FROM mf_ter GROUP BY 1")
    return {r["m"]: r["d"] for r in cur.fetchall()}


def run(today: date, loaded_max: dict[str, date], check_only: bool = False, months: list[str] | None = None, recent: int = RECENT_MONTHS, limit: int = MAX_PER_RUN,
        inbox: Path = INBOX, session=None, pause: float = PAUSE_SECONDS) -> dict:
    """Decide what is missing, then fetch and keep it. Returns {'wanted': [...], 'saved': [paths], 'unchanged': [months]}; `check_only` asks AMFI only for the month list."""
    s = session or _session()
    if months:
        wanted = [m for m in months if MONTH_RE.match(m)][:max(limit, 0)]
        if len(wanted) != len(months):
            raise ValueError("months must look like MM-YYYY (e.g. 10-2026)")
    else:
        available = sorted({m for fy in {financial_year(today.strftime("%m-%Y")), financial_year((today.replace(day=1) - timedelta(days=1)).strftime("%m-%Y"))} for m in list_months(fy, s)}, key=_key, reverse=True)
        wanted = plan(available, loaded_max, today, recent, limit)
    out = {"wanted": wanted, "saved": [], "unchanged": []}
    if check_only:
        return out
    for i, m in enumerate(wanted):
        if i:
            time.sleep(pause)
        p = save(fetch_month(m, s), m, today, inbox)
        (out["saved"] if p else out["unchanged"]).append(p if p else m)
    return out
