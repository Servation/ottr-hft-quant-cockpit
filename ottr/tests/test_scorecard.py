"""
Scorecard tests: a small fixed set of calls and closes whose numbers are worked out by
hand below, the overlap count, pending and missing prices (never zero), the split by the
model the meeting record names, the chair's trades, and `ottr scorecard` read-only.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from ottr import cli
from ottr.council import scorecard as sc
from ottr.council.scorecard import NOT_RECORDED, Call, grade, independent, summarize
from ottr.ledger.store import Store, dumps
from tests.fakes import et

# Seven sessions with closes (Mon 2026-11-02 .. Tue 11-10), then three ahead in the calendar.
S = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]
CLOSES = {
    "SPY": [100, 101, 102, 100, 103, 105, 104],
    "AAA": [50, 51, 49, 52, 53, 55, 54],
    "BBB": [20, 19, 19.5, 18, 18, 19, 20],
}
C = {sym: dict(zip(S, xs)) for sym, xs in CLOSES.items()}
AS_OF = date(2026, 11, 10)


def call(who, action, asset, day, model="m/high", mid="x"):
    return Call(who=who, model=model, action=action, asset=asset, day=day, meeting_id=mid)


CALLS = [
    call("claude", "BUY", "AAA", S[0], mid="am"),       # the morning and afternoon meetings
    call("claude", "BUY", "AAA", S[0], mid="pm"),       # of one day share one base close
    call("claude", "BUY", "AAA", S[1]),
    call("gpt", "SELL", "BBB", S[0]),
    call("claude", "HOLD", "BBB", S[5]),                # +1 graded; +5 runs past the calendar
    call("claude", "HOLD", "BBB", S[6]),                # +1 due 11-11: after the last close
    call("claude", "HOLD", "CCC", S[0]),                # no closes at all
]


def row(rows, who, action, h):
    (r,) = [r for r in rows if (r["who"], r["action"], r["horizon"]) == (who, action, h)]
    return r


def test_hand_worked_numbers():
    rows = summarize(CALLS, S, C, AS_OF)
    # claude BUY AAA, +1. From 11-02 (twice): AAA 51/50 = +2%, SPY 101/100 = +1%, excess +1%.
    # From 11-03: AAA 49/51 = -3.92157%, SPY 102/101 = +0.99010%, excess -4.91168%.
    # Mean return (2 + 2 - 3.92157) / 3 = +0.02614%; mean excess (1 + 1 - 4.91168) / 3 =
    # -0.97056%. Two of three rose. Independent: two distinct asset-days.
    # SE: excess deviations +1.97056, +1.97056, -3.94112 (%) -> sample sd 3.41311% -> / sqrt(2).
    r = row(rows, "claude", "BUY", 1)
    assert (r["calls"], r["graded"], r["pending"], r["no_price"]) == (3, 3, 0, 0)
    assert (r["independent"], r["sessions"]) == (2, 2)
    assert r["mean_return"] == pytest.approx(0.0002614379, abs=1e-9)
    assert r["mean_excess"] == pytest.approx(-0.0097055588, abs=1e-9)
    assert r["excess_se"] == pytest.approx(0.0241344, abs=1e-6)
    assert r["hit_rate"] == pytest.approx(2 / 3)
    # +5. From 11-02 to 11-09: AAA 55/50 = +10%, SPY 105/100 = +5%, excess +5% (twice).
    # From 11-03 to 11-10: AAA 54/51 = +5.88235%, SPY 104/101 = +2.97030%, excess +2.91206%.
    # Mean return (10 + 10 + 5.88235) / 3 = 8.62745%; excess (5 + 5 + 2.91206) / 3 = 4.30402%.
    # The three windows all overlap on AAA: one independent call, so no standard error.
    r = row(rows, "claude", "BUY", 5)
    assert (r["graded"], r["independent"], r["sessions"]) == (3, 1, 2)
    assert r["mean_return"] == pytest.approx(0.0862745098, abs=1e-9)
    assert r["mean_excess"] == pytest.approx(0.0430401864, abs=1e-9)
    assert r["excess_se"] is None and r["hit_rate"] == 1.0
    # gpt SELL BBB from 11-02: +1 19/20 = -5% (SPY +1%: excess -6%); +5 19/20 = -5% (SPY +5%:
    # excess -10%). A SELL hits when the asset falls.
    r1, r5 = row(rows, "gpt", "SELL", 1), row(rows, "gpt", "SELL", 5)
    assert (r1["mean_return"], r1["mean_excess"], r1["hit_rate"]) == pytest.approx((-0.05, -0.06, 1.0))
    assert (r5["mean_return"], r5["mean_excess"], r5["hit_rate"]) == pytest.approx((-0.05, -0.10, 1.0))
    # claude HOLD: BBB from 11-09, +1: 20/19 = +5.26316%, SPY 104/105 = -0.95238%, excess
    # +6.21554%. BBB from 11-10 is pending at +1; both are pending at +5; CCC has no price.
    # HOLD has no hit rate.
    r = row(rows, "claude", "HOLD", 1)
    assert (r["calls"], r["graded"], r["pending"], r["no_price"]) == (3, 1, 1, 1)
    assert (r["mean_return"], r["mean_excess"]) == pytest.approx((0.0526315789, 0.0621553885))
    assert r["hit_rate"] is None
    r = row(rows, "claude", "HOLD", 5)
    assert (r["graded"], r["pending"], r["no_price"]) == (0, 2, 1)
    assert r["mean_return"] is None and r["mean_excess"] is None      # not yet, never zero
    # Rows come by who, model, then BUY / SELL / HOLD, each at +1 then +5.
    assert [(r["who"], r["action"], r["horizon"]) for r in rows] == [
        ("claude", "BUY", 1), ("claude", "BUY", 5), ("claude", "HOLD", 1), ("claude", "HOLD", 5),
        ("gpt", "SELL", 1), ("gpt", "SELL", 5)]


def test_grade_edges():
    assert grade("AAA", date(2026, 11, 8), 1, S, C, AS_OF).ret == pytest.approx(55 / 53 - 1)  # Sunday: Friday's close
    assert grade("AAA", date(2026, 10, 30), 1, S, C, AS_OF).status == sc.NO_PRICE         # before the calendar
    assert grade("AAA", S[0], 1, S, C, None).status == sc.PENDING                         # no benchmark closes yet
    assert grade("SPY", S[0], 5, S, C, AS_OF).excess == pytest.approx(0.0)


def test_independent_counts_windows_that_do_not_overlap():
    w = [("A", 0), ("A", 0), ("A", 1), ("A", 5), ("A", 6), ("B", 0)]
    assert independent(w, 1) == 5       # distinct asset-days: A 0, 1, 5, 6 and B 0
    assert independent(w, 5) == 3       # A 0 and 5 (1 and 6 overlap them), B 0
    assert independent([], 5) == 0


def test_model_label():
    assert sc.model_label({"model": "claude-opus-5-5", "effort": "high"}) == "claude-opus-5-5/high"
    assert sc.model_label({"model": "gemini-3.8-flash-high", "effort": None}) == "gemini-3.8-flash-high"
    assert sc.model_label(None) == sc.model_label({"model": None, "effort": "high"}) == NOT_RECORDED


# -- from a book -----------------------------------------------------------------------------
def write_book(path: Path) -> None:
    """Two meetings on the fixture's closes: one recorded before the record kept models,
    one after a model change, whose chair bought AAA."""
    s = Store(path, writer=True)
    with s.tx() as c:
        for d in S:
            c.execute("INSERT INTO calendar(day,open,close,settles) VALUES(?,?,?,NULL)",
                      (d.isoformat(), "09:30", "16:00"))
        for sym, xs in C.items():
            c.executemany("INSERT INTO adj_closes(symbol,day,close) VALUES(?,?,?)",
                          [(sym, d.isoformat(), x) for d, x in xs.items()])
        old = {"id": "m-old", "analysts": {}, "trades": []}
        new = {"id": "m-new", "analysts": {}, "status": "ok",
               "models": {"claude": {"model": "claude-fable-5-2", "effort": "xhigh"},
                          "gpt": {"model": "gpt-6.1-sol", "effort": "high"}},
               "chair": {"name": "claude", "model": "claude-opus-5-5", "effort": "high"},
               "trades": [{"action": "BUY", "asset": "AAA", "conviction": 0.6, "reasoning": "r"}]}
        for rec, ts in ((old, et(2026, 11, 2, 10, 15)), (new, et(2026, 11, 3, 15, 15))):
            c.execute("INSERT INTO meetings(id,ts,status,packet,record) VALUES(?,?,?,?,?)",
                      (rec["id"], ts, "ok", "packet", dumps(rec)))
            for who, stance in (("claude", "BUY"), ("gpt", "HOLD")):
                c.execute("INSERT INTO stances VALUES(?,?,?,?,?,?,?)", (rec["id"], who, "AAA", stance, 0.6, 1.0, ts))
    s.close()


def test_scorecard_splits_by_the_model_in_the_record(tmp_path):
    write_book(tmp_path / "ottr.db")
    store = Store(tmp_path / "ottr.db", writer=False)
    try:
        out = sc.scorecard(store)
    finally:
        store.close()
    assert (out["as_of"], out["meetings"], out["stances"], out["trades"]) == ("2026-11-10", 2, 4, 1)
    one = {(r["who"], r["model"], r["action"]): r for r in out["analysts"] if r["horizon"] == 1}
    assert set(one) == {("claude", NOT_RECORDED, "BUY"), ("claude", "claude-fable-5-2/xhigh", "BUY"),
                        ("gpt", NOT_RECORDED, "HOLD"), ("gpt", "gpt-6.1-sol/high", "HOLD")}
    assert one[("claude", NOT_RECORDED, "BUY")]["mean_return"] == pytest.approx(0.02)          # before
    assert one[("claude", "claude-fable-5-2/xhigh", "BUY")]["mean_return"] == pytest.approx(49 / 51 - 1)  # after
    (chair,) = [r for r in out["chair"] if r["horizon"] == 5]
    assert (chair["who"], chair["model"], chair["action"]) == ("claude", "claude-opus-5-5/high", "BUY")
    assert chair["mean_excess"] == pytest.approx(54 / 51 - 104 / 101)


def test_cli_scorecard_reads_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OTTR_DATA_DIR", str(tmp_path))
    write_book(tmp_path / "ottr.db")
    before = (tmp_path / "ottr.db").read_bytes()
    assert cli.main(["scorecard"]) == 0
    text = capsys.readouterr().out
    assert "2 meetings, 4 analyst stances, 1 chair trades" in text and "claude-fable-5-2/xhigh" in text
    assert "Chair's trades, +5 sessions:" in text
    assert cli.main(["scorecard", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stances"] == 4
    assert (tmp_path / "ottr.db").read_bytes() == before


def test_cli_scorecard_without_a_book(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OTTR_DATA_DIR", str(tmp_path))
    assert cli.main(["scorecard"]) == 3 and "no OTTR database" in capsys.readouterr().out
