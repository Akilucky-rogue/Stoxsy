"""Exchange calendar: the NSE holiday document is parsed and validated, session counting skips holidays and special days, the horizon is honest, and the stored calendar agrees with real price bars."""
import json
from datetime import date

import pandas as pd
import pytest

from qsde import exchange_calendar as X
from qsde.alerts import rules as AR
from qsde.stream.adapters.nse_holidays import NseHolidaysAdapter, parse_holidays

CAL = X.from_rows([("CM", date(2026, 11, 8), True), ("CM", date(2026, 11, 10), False), ("CM", date(2026, 12, 25), False), ("FO", date(2026, 11, 10), False)])


def doc(extra=None):
    rows = [{"tradingDate": f"{d:%d-%b-%Y}", "weekDay": f"{d:%A}", "description": n} for d, n in [
        (date(2026, 1, 26), "Republic Day"), (date(2026, 3, 3), "Holi"), (date(2026, 4, 3), "Good Friday"), (date(2026, 5, 1), "Maharashtra Day"), (date(2026, 9, 14), "Ganesh Chaturthi"),
        (date(2026, 10, 2), "Mahatma Gandhi Jayanti"), (date(2026, 10, 20), "Dussehra"), (date(2026, 11, 8), "Diwali Laxmi Pujan*"), (date(2026, 11, 10), "Diwali-Balipratipada"), (date(2026, 12, 25), "Christmas")]]
    rows += extra or []
    return json.dumps({"CM": rows, "FO": rows, "COM": []}).encode()


def test_sessions_skip_weekends_holidays_and_the_special_muhurat_day():
    assert CAL.is_session(date(2026, 11, 6)) and not CAL.is_session(date(2026, 11, 7)) and not CAL.is_session(date(2026, 11, 8))     # Saturday and the starred Sunday
    assert not CAL.is_session(date(2026, 11, 10))                                                                                     # Tuesday holiday
    d, est = CAL.add_sessions(date(2026, 11, 6), 5)                                                                                   # 9, (10 skipped) 11, 12, 13, 16
    assert d == date(2026, 11, 16) and est is False
    assert CAL.sessions_between(date(2026, 11, 6), date(2026, 11, 16)) == 5
    assert date(2026, 11, 8) in CAL.special and date(2026, 11, 10) not in CAL.special


def test_a_special_session_on_a_weekday_is_not_counted_as_a_regular_session():
    cal = X.from_rows([("CM", date(2026, 11, 11), True)])                                                                             # pretend Muhurat falls on a Wednesday
    assert cal.add_sessions(date(2026, 11, 10), 1)[0] == date(2026, 11, 12)


def test_past_the_loaded_year_the_answer_is_weekdays_and_says_estimated():
    d, est = CAL.add_sessions(date(2026, 12, 28), 5)
    assert est is True and d == date(2027, 1, 4) and CAL.loaded_through == date(2026, 12, 31)
    empty = X.Calendar()
    assert empty.add_sessions(date(2026, 10, 23), 1) == (date(2026, 10, 26), True) and empty.status(date(2026, 10, 25))["state"] == "missing"


def test_status_warns_inside_45_days_and_fails_after_the_horizon():
    assert CAL.status(date(2026, 6, 1))["state"] == "ok"
    s = CAL.status(date(2026, 11, 30))
    assert s["state"] == "ending" and s["days_left"] == 31 and "next year" in s["message"]
    e = CAL.status(date(2027, 1, 2))
    assert e["state"] == "expired" and "weekday estimates" in e["message"]


def test_parse_reads_both_segments_and_marks_the_starred_day_special():
    d = parse_holidays(doc())
    assert set(d["segment"]) == {"CM", "FO"} and len(d) == 20 and d["event_time"].dt.tz is not None
    row = d[(d["segment"] == "CM") & (d["holiday_date"] == "2026-11-08")].iloc[0]
    assert row["special_session"] and row["description"] == "Diwali Laxmi Pujan"
    assert int(d["special_session"].sum()) == 2                                                                                       # only the starred one, in each segment


def test_parse_refuses_what_is_not_the_document():
    with pytest.raises(ValueError, match="not JSON"):
        parse_holidays(b"<html>Access Denied</html>")
    with pytest.raises(ValueError, match="segments"):
        parse_holidays(json.dumps({"CM": []}).encode())
    with pytest.raises(ValueError, match="unparseable"):
        parse_holidays(doc([{"tradingDate": "32-Foo-2026", "weekDay": "Monday", "description": "x"}]))


def test_validation_catches_a_weekday_that_disagrees_and_a_thin_year():
    ad = NseHolidaysAdapter(fetcher=object())
    assert ad.validate(parse_holidays(doc())) == []
    wrong = parse_holidays(doc([{"tradingDate": "05-Jan-2026", "weekDay": "Tuesday", "description": "x"}]))
    assert any("weekday disagrees" in e for e in ad.validate(wrong))
    full = json.loads(doc())
    thin = parse_holidays(json.dumps({"CM": full["CM"][:3], "FO": full["FO"]}).encode())
    assert any("only 3 holidays for CM" in e for e in ad.validate(thin))


def test_fetch_returns_nothing_on_a_network_error_and_skips_an_unchanged_file():
    class Boom:
        def get(self, url, headers=None):
            raise OSError("down")

    assert NseHolidaysAdapter(fetcher=Boom()).fetch(None) == []

    class Same:
        def get(self, url, headers=None):
            return doc()

    ad = NseHolidaysAdapter(fetcher=Same())
    assert len(ad.fetch(None)) == 1 and ad.fetch(None) == []


def test_alert_rule_is_quiet_when_ok_and_critical_when_missing_or_expired():
    cfg = AR.load_config()
    assert AR.rule_calendar({"calendar": {"state": "ok", "message": "fine"}}, cfg) == []
    assert AR.rule_calendar({}, cfg) == []
    w = AR.rule_calendar({"calendar": CAL.status(date(2026, 11, 30))}, cfg)
    assert w[0]["severity"] == "warning" and w[0]["dedup_key"] == "calendar:horizon"
    c = AR.rule_calendar({"calendar": X.Calendar().status(date(2026, 11, 30))}, cfg)
    assert c[0]["severity"] == "critical"
    assert AR.rule_calendar in AR.GLOBAL_RULES


def test_decision_cards_use_the_calendar_and_report_estimates_honestly():
    from qsde.decision.cards import PortfolioParams, build_cards

    days = pd.bdate_range("2025-01-01", periods=300)
    px = pd.DataFrame([{"symbol": "AAA", "date": d, "close": 100 + i * 0.05} for i, d in enumerate(days)])
    sc = pd.DataFrame([{"symbol": "AAA", "date": days[-1], "defensive_score": 0.9, "risk_bucket": 5, "liq": 1e9, "dd120": -0.01, "atr_pct": 0.01, "vol60": 0.1, "downside60": 0.1, "n_hist": 300,
                        "warnings": "", "regime": "calm_trend"}])
    as_of = days[-1].date()
    cover = X.from_rows([("CM", date(as_of.year + 1, 12, 25), False)])                    # a calendar that reaches well past the card dates
    book = build_cards(sc, px, PortfolioParams(calendar=cover))
    assert book["summary"]["dates_are_estimates"] is False and any("official NSE holiday list" in n for n in book["summary"]["notes"])
    assert book["summary"]["entry_date"] == cover.add_sessions(as_of, 1)[0].isoformat()
    far = build_cards(sc, px, PortfolioParams(calendar=X.Calendar()))
    assert far["summary"]["dates_are_estimates"] is True and any("weekday sessions" in n for n in far["summary"]["notes"])


def test_stored_calendar_agrees_with_real_price_bars():
    """Needs the loaded calendar and prices: no holiday has a bar, and no weekday without a bar is missing from the list."""
    from qsde.db.connection import read_sql

    cal = X.load()
    if cal.loaded_through is None:
        pytest.skip("no holiday calendar loaded in this database")
    bars = set(pd.to_datetime(read_sql(f"SELECT DISTINCT date FROM ohlcv WHERE date >= '{cal.loaded_through.year}-01-01'")["date"]).dt.date)
    if not bars:
        pytest.skip("no bars in the calendar's year")
    last = max(bars)
    assert not [d for d in cal.holidays if d.year == last.year and d <= last and d in bars]
    assert not [d.date() for d in pd.bdate_range(f"{last.year}-01-01", last) if d.date() not in bars and d.date() not in cal.holidays]
