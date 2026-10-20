"""NSE F&O bhavcopy: both file formats, URL selection by date, idle-contract handling, validation."""
import io
import zipfile
from datetime import date

import pytest

from qsde.stream.adapters import nse_fo as F

NL = chr(10)
LEGACY = NL.join([
    "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,",
    "FUTIDX,BANKNIFTY,25-Jan-2024,0,XX,48002.25,48380,47933.2,48306.25,48306.25,131903,953386.71,2027040,13245,15-JAN-2024,",
    "OPTIDX,NIFTY,25-Jan-2024,21500,CE,150,160,140,155,155.5,1000,2500.5,5000,100,15-JAN-2024,",
    "OPTIDX,NIFTY,25-Jan-2024,30000,PE,0,0,0,0.05,0.05,0,0,0,0,15-JAN-2024,",
    "OPTSTK,TCS,25-Jan-2024,3800,PE,12,14,11,13,13.2,0,0,250,-25,15-JAN-2024,",
    "FUTSTK,TCS,25-Jan-2024,0,XX,3700,3750,3690,3745,3744,500,1500,9000,50,15-JAN-2024,",
])
UDIFF_HEAD = "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
UDIFF = NL.join([
    UDIFF_HEAD,
    "2026-10-16,2026-10-16,FO,NSE,STO,70267,,ABCAPITAL,,2026-11-23,2026-11-23,375.00,PE,ABCAPITAL26NOV375PE,0.00,0.00,0.00,20.20,0.00,20.20,379.39,12.82,0,0,0,0.00,0,F1,3100,,,,,",
    "2026-10-16,2026-10-16,FO,NSE,STO,67173,,ABCAPITAL,,2026-11-23,2026-11-23,330.00,PE,ABCAPITAL26NOV330PE,0.00,0.00,0.00,2.10,2.15,2.10,379.39,1.46,15500,0,0,0.00,0,F1,3100,,,,,",
    "2026-10-16,2026-10-16,FO,NSE,IDF,35003,,NIFTY,,2026-10-27,2026-10-27,,,NIFTY27OCT26,24990.0,25100.0,24950.0,25010.0,25012.0,24980.0,25005.0,25011.0,150000,5000,40000,9.9E10,12000,F1,75,,,,,",
    "2026-10-16,2026-10-16,FO,NSE,IDO,41001,,NIFTY,,2026-10-27,2026-10-27,25000.00,CE,NIFTY27OCT2625000CE,180,200,150,170,171,175,25005.0,171.5,90000,1000,25000,5.5E9,4000,F1,75,,,,,",
    "2026-10-16,2026-10-16,FO,NSE,IRF,99001,,GSEC,,2026-10-27,2026-10-27,,,GSEC,1,1,1,1,1,1,1,1,10,1,1,1,1,F1,1,,,,,",
])


def zipped(text: str, name: str) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(name, text)
    return b.getvalue()


def test_legacy_format_parses_futures_options_and_drops_only_idle_contracts():
    d = F.parse_fo(zipped(LEGACY, "fo15JAN2024bhav.csv"), "legacy", "x")
    assert len(d) == 4 and d.attrs["n_dropped_idle"] == 1 and d.attrs["n_rows_file"] == 5
    fut = d[(d.symbol == "BANKNIFTY")].iloc[0]
    assert (fut.instrument, fut.option_type, fut.strike) == ("FUTIDX", "FUT", 0.0) and fut.value_inr == pytest.approx(953386.71e5) and fut.open_interest == 2027040
    ce = d[d.option_type == "CE"].iloc[0]
    assert ce.strike == 21500 and ce.settle == 155.5 and ce.trade_date.date() == date(2024, 1, 15) and ce.expiry_date.date() == date(2024, 1, 25)
    stk = d[(d.symbol == "TCS") & (d.option_type == "PE")].iloc[0]                    # open interest but no volume: kept, O/H/L blanked, settlement kept
    assert stk.open_interest == 250 and stk.contracts == 0 and d[(d.symbol == "TCS") & (d.option_type == "PE")]["open"].isna().all() and stk.settle == 13.2


def test_udiff_format_maps_instrument_types_and_keeps_underlying_lot_and_ids():
    d = F.parse_fo(zipped(UDIFF, "BhavCopy_NSE_FO_0_0_0_20261016_F_0000.csv"), "udiff", "x")
    assert d.attrs["n_rows_file"] == 5 and d.attrs["n_dropped_idle"] == 1 and d.attrs["n_other_types"] == 1 and len(d) == 3
    assert set(d["instrument"]) == {"OPTSTK", "FUTIDX", "OPTIDX"}
    abc = d[d.symbol == "ABCAPITAL"].iloc[0]                                          # OI 15,500, no volume: stored with its settlement
    assert abc.open_interest == 15500 and abc.settle == 1.46 and abc.strike == 330.0 and abc.underlying_price == 379.39 and abc.lot_size == 3100
    fut = d[d.instrument == "FUTIDX"].iloc[0]
    assert fut.option_type == "FUT" and fut.strike == 0.0 and fut.fin_instrm_id == 35003 and fut.trade_date.date() == date(2026, 10, 16)
    assert d["_sha256"].nunique() == 1 and d["_file_rows"].iloc[0] == 5


def test_urls_follow_the_format_cutover_and_overlap():
    assert [f for f, _ in F.urls_for(date(2023, 5, 2))] == ["legacy"] and "fo02MAY2023bhav.csv.zip" in F.urls_for(date(2023, 5, 2))[0][1]
    assert [f for f, _ in F.urls_for(date(2024, 7, 3))] == ["legacy", "udiff"]
    assert [f for f, _ in F.urls_for(date(2024, 7, 8))] == ["udiff"] and "20240708" in F.urls_for(date(2024, 7, 8))[0][1]


def test_validation_flags_short_wrong_date_and_option_less_files_and_adapter_falls_back():
    a = F.NseFoBhavcopyAdapter(fetcher=None, min_rows=2)
    ok = F.parse_fo(zipped(NL.join(UDIFF.split(NL)[:-1]), "x.csv"), "udiff", "x")             # without the one unrecognised (interest-rate) row
    ok.attrs["session"] = date(2026, 10, 16)
    other = F.parse_fo(zipped(UDIFF, "x.csv"), "udiff", "x"); other.attrs["session"] = date(2026, 10, 16)
    assert any("unrecognised instrument type" in e for e in a.validate(other))
    assert a.validate(ok) == []
    wrong = ok.copy(); wrong.attrs["session"] = date(2026, 10, 15)
    assert any("holds" in e for e in a.validate(wrong))
    assert any("only" in e for e in F.NseFoBhavcopyAdapter(fetcher=None, min_rows=50).validate(ok))
    futs = ok[ok.instrument == "FUTIDX"].copy(); futs.attrs["session"] = date(2026, 10, 16)
    assert any("no option rows" in e for e in a.validate(futs))

    class Fx:
        def __init__(self): self.urls = []
        def get(self, url, headers=None):
            self.urls.append(url)
            if "historical" in url:
                raise OSError("404")
            return zipped(UDIFF, "x.csv")
    fx = Fx()
    items = F.NseFoBhavcopyAdapter(fetcher=fx, start_date=date(2024, 7, 3), end_date=date(2024, 7, 3), now=lambda: __import__("datetime").datetime(2024, 7, 4, 3, 0, tzinfo=__import__("datetime").timezone.utc)).fetch(None)
    assert len(items) == 1 and items[0].meta["format"] == "udiff" and len(fx.urls) == 2
