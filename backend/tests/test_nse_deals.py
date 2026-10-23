"""NSE bulk and block deals: parsing both layouts, repeated identical trades, bad rows, error pages, adapter dedup of an unchanged file, and the idempotent database write."""
from datetime import datetime, timezone

import pandas as pd
import pytest

from qsde.stream.adapters import nse_deals as D

NL = chr(10)
BULK = NL.join([
    "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks",
    "22-OCT-2026,AISL,ANI Integrated Serv Ltd.,DIPAKKUMAR  BABALBHAI PATEL,BUY,166800,29.00,-",
    "22-OCT-2026,AISL,ANI Integrated Serv Ltd.,DIPAKKUMAR BABALBHAI PATEL,BUY,166800,29.00,-",
    "22-OCT-2026,XYZ,Xyz Ltd,SOME FUND,SELL,\"1,200,000\",101.5,Pledge",
    "22-OCT-2026,BAD,Bad Ltd,NOBODY,HOLD,10,5,-",
    "bad-date,BAD,Bad Ltd,NOBODY,BUY,10,5,-",
])
BLOCK = NL.join([
    "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price",
    "22-OCT-2026,CAPINVIT,Capital Infra Trust,LARSEN & TOUBRO LIMITED,BUY,6644519,75.59",
])
HEADER_ONLY = "Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price" + NL


def test_bulk_layout_normalises_names_numbers_and_keeps_repeated_identical_trades_distinct():
    d = D.parse_deals(BULK.encode(), "bulk")
    assert len(d) == 3 and d.attrs["n_rows_file"] == 5 and d.attrs["n_unparsed"] == 2                         # the HOLD row and the bad date are counted, not hidden
    a = d[d.symbol == "AISL"]
    assert list(a["seq"]) == [0, 1] and set(a["client_name"]) == {"DIPAKKUMAR BABALBHAI PATEL"}                # a double space is normalised; the repeat gets seq 1
    x = d[d.symbol == "XYZ"].iloc[0]
    assert x.quantity == 1_200_000 and x.price == 101.5 and x.remarks == "Pledge" and x.side == "SELL" and a.iloc[0].remarks is None
    assert str(d["event_time"].iloc[0]) == "2026-10-22 10:00:00+00:00"                                       # 15:30 IST
    assert D.NseDealsAdapter().validate(d) == ["2 rows could not be parsed"]                                  # more than 5% unparsed is reported, not hidden
    clean = D.parse_deals(NL.join(BULK.split(NL)[:4]).encode(), "bulk")
    assert D.NseDealsAdapter().validate(clean) == []


def test_block_layout_has_no_remarks_column():
    d = D.parse_deals(BLOCK.encode(), "block")
    assert len(d) == 1 and d.iloc[0].kind == "block" and d.iloc[0].quantity == 6644519 and d.iloc[0].remarks is None


def test_an_empty_session_is_valid_and_an_error_page_is_refused():
    d = D.parse_deals(HEADER_ONLY.encode(), "block")
    assert d.empty and D.NseDealsAdapter().validate(d) == []
    for bad in (b"<!DOCTYPE html><html>not found</html>", b"", b"Symbol,Qty" + NL.encode() + b"A,1"):
        with pytest.raises(ValueError):
            D.parse_deals(bad, "bulk")
    with pytest.raises(ValueError, match="columns missing"):
        D.parse_deals(b"Date,Symbol,Client Name" + NL.encode() + b"22-OCT-2026,A,B", "bulk")
    with pytest.raises(ValueError):
        D.parse_deals(BULK.encode(), "other")


def test_validation_flags_mixed_sessions_and_absurd_prices():
    d = D.parse_deals(BULK.encode(), "bulk")
    d.loc[0, "trade_date"] = pd.Timestamp("2026-10-21")
    assert any("mixes sessions" in e for e in D.NseDealsAdapter().validate(d))
    d2 = D.parse_deals(BULK.encode(), "bulk")
    d2.loc[0, "price"] = 5e6
    assert any("10 lakh" in e for e in D.NseDealsAdapter().validate(d2))


class FakeFetcher:
    def __init__(self, files):
        self.files, self.calls = files, 0

    def get(self, url, headers=None):
        self.calls += 1
        v = self.files[url]
        if isinstance(v, Exception):
            raise v
        return v


def test_adapter_fetches_both_files_skips_an_unchanged_one_and_survives_a_failure():
    f = FakeFetcher({D.URLS["bulk"]: BULK.encode(), D.URLS["block"]: BLOCK.encode()})
    a = D.NseDealsAdapter(f, now=lambda: datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc))
    items = a.fetch(None)
    assert [i.meta["kind"] for i in items] == ["bulk", "block"] and all(i.ext == "csv" for i in items)
    assert a.fetch(None) == []                                                                                # nothing new within the same process
    f2 = FakeFetcher({D.URLS["bulk"]: RuntimeError("down"), D.URLS["block"]: b"<html>blocked</html>"})
    assert D.NseDealsAdapter(f2).fetch(None) == []


def test_database_write_is_idempotent():
    from qsde.db.connection import execute_sql, read_sql
    from qsde.stream.sinks import PgDealsSink

    try:
        d = D.parse_deals(BULK.encode(), "bulk")
        d["trade_date"] = pd.Timestamp("2001-01-01")                                                           # a date no real session has
        d["symbol"] = d["symbol"].map(lambda s: "ZZ" + s)
        d["known_at"] = pd.Timestamp("2026-10-22 12:00", tz="UTC")
        sink = PgDealsSink()
        assert sink.upsert("nse_deals", d, D.NseDealsAdapter.key_cols) == 3
        assert sink.upsert("nse_deals", d, D.NseDealsAdapter.key_cols) == 3
        r = read_sql("SELECT count(*) AS n, count(DISTINCT seq) AS s FROM deals_raw WHERE trade_date = DATE '2001-01-01'")
        assert int(r["n"].iloc[0]) == 3 and int(r["s"].iloc[0]) == 2
    finally:
        execute_sql("DELETE FROM deals_raw WHERE trade_date = DATE '2001-01-01'")
