"""BSE bhavcopy adapter: three formats, soft-404 holidays, validation, resumable fetch, sink (test rows removed afterwards)."""
import io
import zipfile
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from qsde.stream.adapters import bse
from qsde.stream.base import RawItem

UDIFF_HEAD = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,"
              "HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,"
              "SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def udiff(rows: int = 1600, day: str = "2026-10-16") -> bytes:
    lines = [UDIFF_HEAD]
    for i in range(rows):
        code = 500000 + i
        lines.append(f"{day},{day},CM,BSE,STK,{code},INE{i:05d}01{i % 10}2,SYM{i},A,,,,,NAME {i} LIMITED,100,105,99,102,102,101,,101.5,,,1000,102000,10,F1,1,,,,,")
    lines.append(f"{day},{day},CM,BSE,STK,599999,INE999990112,UNTRD,X,,,,,UNTRADED LTD,0,0,0,55.5,55.5,55.5,,55.5,,,0,0,0,F1,1,,,,,")
    return ("\n".join(lines) + "\n").encode()


def old_zip(rows: int = 1600, isin: bool = True) -> bytes:
    head = "SC_CODE,SC_NAME,SC_GROUP,SC_TYPE,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,NO_TRADES,NO_OF_SHRS,NET_TURNOV,TDCLOINDI" + (",ISIN_CODE,TRADING_DATE,FILLER2,FILLER3" if isin else "")
    lines = [head]
    for i in range(rows):
        tail = f",INE{i:05d}0112,14-Jun-19,," if isin else ""
        lines.append(f"{500000 + i},NAME {i} LTD.    ,A ,Q,100,105,99,102,102,101,10,1000,102000,{tail}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("EQ_ISINCODE_140619.CSV" if isin else "EQ140616.CSV", "\n".join(lines) + "\n")
    return buf.getvalue()


HTML = b"<!DOCTYPE html><html lang='en'><head><title>BSE</title></head><body>holiday page</body></html>"


def test_detects_the_holiday_html_page_as_not_a_data_file():
    assert not bse.is_data_file(HTML) and not bse.is_data_file(b"<html>x</html>")
    assert bse.is_data_file(udiff(3)) and bse.is_data_file(old_zip(3))


def test_udiff_is_parsed_with_isin_ticker_and_untraded_scrips_keep_their_close():
    df = bse.parse_bhavcopy(udiff(5), date(2026, 10, 16), "f")
    assert len(df) == 6 and df["source_format"].eq("udiff").all() and df["date"].dt.date.eq(date(2026, 10, 16)).all()
    r = df[df["scrip_code"] == 500002].iloc[0]
    assert r["ticker"] == "SYM2" and r["isin"].startswith("INE") and r["close"] == 102 and r["volume"] == 1000 and r["turnover_value"] == 102000
    u = df[df["scrip_code"] == 599999].iloc[0]
    assert pd.isna(u["open"]) and pd.isna(u["high"]) and u["close"] == 55.5                    # zero O/H/L = no trade
    assert df["event_time"].dt.tz is not None


def test_legacy_formats_with_and_without_isin():
    a = bse.parse_bhavcopy(old_zip(4, isin=True), date(2019, 6, 14), "f")
    assert a["source_format"].eq("eq_isincode").all() and a["isin"].notna().all() and a["date"].dt.date.eq(date(2019, 6, 14)).all()
    assert a["grp"].eq("A").all() and a["ticker"].isna().all()
    b = bse.parse_bhavcopy(old_zip(4, isin=False), date(2016, 6, 14), "f")
    assert b["source_format"].eq("eq_legacy").all() and b["isin"].isna().all() and b["date"].dt.date.eq(date(2016, 6, 14)).all()
    with pytest.raises(ValueError):
        bse.parse_bhavcopy(b"A,B\n1,2\n", None, "f")


def test_urls_follow_the_date():
    assert [f for f, _ in bse.urls_for(date(2026, 10, 16))] == ["udiff", "eq_isincode", "eq_legacy"]
    assert [f for f, _ in bse.urls_for(date(2019, 6, 14))] == ["eq_isincode", "eq_legacy"]
    assert bse.urls_for(date(2019, 6, 14))[0][1].endswith("EQ_ISINCODE_140619.zip")


class FakeFetcher:
    def __init__(self, files): self.files, self.calls = files, []

    def get(self, url, headers=None):
        self.calls.append(url)
        for key, payload in self.files.items():
            if key in url:
                return payload
        return HTML


NOW = lambda: datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)              # Saturday 19:30 IST: Friday's file is out


def test_fetch_lands_sessions_treats_holidays_as_no_session_and_is_resumable():
    f = FakeFetcher({"20261016": udiff(1600)})                                   # only Friday exists
    a = bse.BseBhavcopyAdapter(f, now=NOW, start_date=date(2026, 10, 14), landed_sessions=())
    items = a.fetch(None)
    assert [i.meta["session_date"] for i in items] == ["2026-10-16"] and items[0].meta["format"] == "udiff" and items[0].ext == "csv"
    assert date(2026, 10, 14) in a.no_file and date(2026, 10, 15) in a.no_file                        # HTML answers
    f2 = FakeFetcher({"20261016": udiff(1600)})
    a2 = bse.BseBhavcopyAdapter(f2, now=NOW, start_date=date(2026, 10, 14), landed_sessions={date(2026, 10, 16)})
    assert a2.fetch(None) == [] or all(i.meta["session_date"] != "2026-10-16" for i in a2.fetch(None))
    assert not any("20261016" in u for u in f2.calls)                                                  # no repeat download


def test_validation_flags_truncated_wrong_date_and_bad_prices():
    a = bse.BseBhavcopyAdapter(FakeFetcher({}), now=NOW)
    ok = a.parse(RawItem(udiff(1600), NOW(), None, {"session_date": "2026-10-16", "url": "u"}, "csv"))
    assert a.validate(ok) == []
    short = a.parse(RawItem(udiff(50), NOW(), None, {"session_date": "2026-10-16", "url": "u"}, "csv"))
    assert any("truncated" in e for e in a.validate(short))
    wrong = a.parse(RawItem(udiff(1600), NOW(), None, {"session_date": "2026-10-15", "url": "u"}, "csv"))
    assert any("holds" in e for e in a.validate(wrong))
    bad = ok.copy()
    bad.loc[bad.index[:60], "high"] = 1.0                                          # high below open/close on 60 rows
    assert any("price sanity" in e for e in a.validate(bad))


def test_sink_upserts_by_scrip_and_date_and_replaces_a_reload():
    from qsde.db.connection import execute_sql, read_sql
    from qsde.stream.sinks import PgBseEodSink
    a = bse.BseBhavcopyAdapter(FakeFetcher({}), now=NOW)
    df = a.parse(RawItem(udiff(3), NOW(), None, {"session_date": "2026-10-16", "url": "u"}, "csv"))
    df["scrip_code"] = df["scrip_code"] + 9_000_000                                 # far from any real scrip
    df["known_at"] = pd.Timestamp("2026-10-16 13:00", tz="UTC")
    try:
        assert PgBseEodSink().upsert("bse_bhavcopy", df, a.key_cols) == 4
        d2 = df.copy()
        d2["close"] = 777.0
        PgBseEodSink().upsert("bse_bhavcopy", d2, a.key_cols)
        got = read_sql("SELECT scrip_code, close, isin, ticker FROM bse_eod_raw WHERE scrip_code >= 9000000 AND date = '2026-10-16' ORDER BY scrip_code")
        assert len(got) == 4 and got["close"].eq(777.0).all() and got["isin"].notna().all()
    finally:
        execute_sql("DELETE FROM bse_eod_raw WHERE scrip_code >= 9000000")


def test_a_spliced_corrupt_record_is_dropped_counted_and_does_not_fail_the_file():
    good = old_zip(1600, isin=True)
    z = zipfile.ZipFile(io.BytesIO(good))
    text = z.read(z.namelist()[0]).decode().splitlines()
    spliced = text[:5] + [text[5] + "," + text[6], "6010.00,,INE802B01019,07-Feb-22,,"] + text[7:]       # two rows glued + a stray tail
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zz:
        zz.writestr("EQ_ISINCODE_070222.CSV", "\n".join(spliced) + "\n")
    df = bse.parse_bhavcopy(buf.getvalue(), date(2019, 6, 14), "f")
    assert df.attrs["malformed_lines"] >= 2 and len(df) <= 1600 - 1
    a = bse.BseBhavcopyAdapter(FakeFetcher({}), now=NOW)
    item = RawItem(buf.getvalue(), NOW(), None, {"session_date": "2019-06-14", "url": "u"}, "zip")
    assert a.validate(a.parse(item)) == []                                                                # a stray record is tolerated ...
    many = text[:5] + [text[i] + "," + text[i + 1] for i in range(5, 60, 2)] + text[61:]
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zz:
        zz.writestr("EQ_ISINCODE_070222.CSV", "\n".join(many) + "\n")
    assert any("malformed" in e for e in a.validate(a.parse(RawItem(buf2.getvalue(), NOW(), None, {"session_date": "2019-06-14", "url": "u"}, "zip"))))   # ... a format change is not
