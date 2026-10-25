"""A same-day re-run of the prediction logger must be an identical re-log (only the stamp times differ), while a changed prediction is still refused. Real database, synthetic model."""
import pandas as pd
import pytest

from qsde.db.connection import get_sync_conn, read_sql
from qsde.live_loop import ledger as L

MODEL = "zz_relog_test_v1"


def rows(score=0.5):
    return pd.DataFrame([{"model_version": MODEL, "symbol": "ZZTEST", "as_of_date": pd.Timestamp("2001-01-02"), "prediction_time": pd.Timestamp("2001-01-02 16:00", tz="Asia/Kolkata"),
                          "horizon_id": "swing_5d", "side": 1, "score": score}])


@pytest.fixture()
def clean():
    yield
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SET LOCAL session_replication_role = replica")                           # the table is append-only by trigger; the test removes its own synthetic rows
        cur.execute("DELETE FROM prediction_log WHERE model_version = %s", (MODEL,))


def test_a_rerun_with_new_stamps_is_an_identical_relog_and_keeps_the_first_stamp(clean):
    t1, t2 = pd.Timestamp("2001-01-02 16:05", tz="Asia/Kolkata"), pd.Timestamp("2001-01-02 17:40", tz="Asia/Kolkata")
    a = L.append_predictions(rows(), now=t1, enforce_pre_entry=False)
    assert a == {"logged": 1, "identical_relogs": 0}
    first = read_sql("SELECT prediction_time, known_at, content_hash FROM prediction_log WHERE model_version = :m", params={"m": MODEL})
    b = L.append_predictions(rows(), now=t2, enforce_pre_entry=False)
    assert b == {"logged": 0, "identical_relogs": 1}
    after = read_sql("SELECT prediction_time, known_at, content_hash FROM prediction_log WHERE model_version = :m", params={"m": MODEL})
    assert len(after) == 1 and after.equals(first)                                                          # the first stamp and hash stand, untouched


def test_a_changed_prediction_is_still_refused_even_when_restamped(clean):
    L.append_predictions(rows(0.5), now=pd.Timestamp("2001-01-02 16:05", tz="Asia/Kolkata"), enforce_pre_entry=False)
    with pytest.raises(L.ImmutableViolation):
        L.append_predictions(rows(0.9), now=pd.Timestamp("2001-01-02 17:40", tz="Asia/Kolkata"), enforce_pre_entry=False)
    stored = read_sql("SELECT score FROM prediction_log WHERE model_version = :m", params={"m": MODEL})
    assert float(stored["score"].iloc[0]) == 0.5
