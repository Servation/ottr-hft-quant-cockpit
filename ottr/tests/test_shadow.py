"""
Model sizing end to end: the chair sizes the live book's BUYs and a code-sized shadow book
takes the same decisions, so the two can be compared.

Covers the engine (the fork at the first BUY the chair sizes, the same decision sized by
the formula in the shadow, the shadow staying quiet, both books' equity points, the
shadow's own jobs and risk checks, its holdings the council can't see being sold off),
the shadow's life (kept across restarts, retired by code sizing or a book it wasn't
forked from), the comparison (`compare`, `report`, `status_line`), and where it shows:
Discord, the API, the CLI and the archive.
"""

import dataclasses
import json
from types import SimpleNamespace

import aiohttp
import pytest

from ottr import archive, cli
from ottr.api import ApiServer
from ottr.config import Alerts as Limits
from ottr.engine import shadow as shadowbook
from ottr.engine.engine import Engine
from ottr.ledger.store import Store
from ottr.notify import Alerts, format_event
from ottr.universe import SUNSET, Member
from tests.fakes import (OPEN, LiveRecorder, analyst_reply, buy_council, council_for, market_rig,
                         no_corporate_actions)

FAST = Limits(live_edit_sec=0.05)
KEY = "test-api-key-0123456789"


def sized(cfg, sizer: str):
    return dataclasses.replace(cfg, sizing=dataclasses.replace(cfg.sizing, sizer=sizer))


async def make_engine(cfg, store, council=None, now: float = OPEN):
    hook = LiveRecorder()
    cfg = dataclasses.replace(cfg, alerts=FAST)
    no_corporate_actions(store, cfg.core)
    rig = await market_rig(cfg, store, now=now).ready()
    eng = Engine(cfg, store, rig.market, council=council or buy_council(cfg, "TLT", weight_pct=20),
                 alerts=Alerts(hook, FAST, live=hook), clock=rig.clock)
    await eng.refresh_members()                                           # Alpaca's asset records: fractional
    return eng, hook, rig


async def meet(eng) -> dict:
    async with eng.alerts.running():
        rec = await eng.meeting(execute=True)
        await eng.drain()
    return rec


def council_fill(store, asset):
    r = store.conn.execute("SELECT notional FROM fills WHERE origin='council' AND asset=? AND side='BUY'",
                           (asset,)).fetchone()
    return float(r[0]) if r else None


def verdict(store, asset, side="BUY"):
    r = store.conn.execute("SELECT status, verdict FROM orders WHERE origin='council' AND asset=? AND side=? "
                           "ORDER BY ts DESC LIMIT 1", (asset, side)).fetchone()
    return r[0], json.loads(r[1] or "{}")


def sell_council(cfg, asset="TLT", fraction=0.5):
    u = list(cfg.core)
    reps = [analyst_reply(u, {asset: ("SELL", 0.7)}), analyst_reply(u, {asset: ("SELL", 0.6)}), analyst_reply(u)]
    chair = {"summary": "trim", "dissent_notes": "", "trades": [
        {"action": "SELL", "asset": asset, "fraction_of_position": fraction, "reasoning": "trim"}]}
    return council_for(cfg, reps, chair, model_sizing=True)


# ---- the engine --------------------------------------------------------------------------------
async def test_the_chair_sizes_the_live_book_and_the_formula_sizes_the_shadow(cfg, store, ledger):
    eng, hook, _ = await make_engine(cfg, store)
    assert eng.model_sizing and eng.shadow is None          # nothing to compare until the chair sizes a BUY
    try:
        await meet(eng)
        shadow = eng.shadow
        assert shadow is not None and shadowbook.path_for(store).exists()
        live_usd, shadow_usd = council_fill(store, "TLT"), council_fill(shadow.store, "TLT")
        st, lv = verdict(store, "TLT")
        assert st == "filled" and (lv["sized_by"], lv["target_pct"]) == ("model", 20.0)
        assert live_usd == pytest.approx(2000.0, rel=0.02)                       # 20% of $10,000
        sst, sv = verdict(shadow.store, "TLT")
        assert sst == "filled" and sv["sized_by"] == "code" and sv["target_pct"] == lv["code_target_pct"]
        assert shadow_usd == pytest.approx(sv["target_pct"] / 100 * 10000, rel=0.02) and shadow_usd < live_usd
        # The same decision on both books: one meeting, one idempotency key each.
        (lk,) = [r[0] for r in store.conn.execute("SELECT idem_key FROM orders WHERE origin='council'")]
        (sk,) = [r[0] for r in shadow.store.conn.execute("SELECT idem_key FROM orders WHERE origin='council'")]
        assert lk == sk
        # Marked as this book's shadow, from the moment before the trade.
        assert shadow.store.meta(shadowbook.SHADOW_OF) == store.meta(shadowbook.BOOK_ID) is not None
        assert float(shadow.store.meta(shadowbook.FORKED_EQUITY)) == pytest.approx(10000.0)
        assert store.audits("shadow_forked") and shadow.store.audits("shadow_forked")
        # The chair got the facts to size by, never the formula's targets to anchor on.
        prompt = eng.council.chair.prompts[0]
        assert "SIZING: the chair sets each BUY's weight_pct" in prompt and "Risk-model target weight" not in prompt
        # Quiet: Discord hears that model sizing started and about the live order, never the shadow's.
        assert sum("Model sizing starts" in p for p in hook.posts) == 1
        assert "BUY TLT (to 20.0% of equity, conviction 0.70)" in hook.shown
        assert f"(the formula: {lv['code_target_pct']:.1f}%)" in hook.shown
        assert not any(f"{shadow_usd:,.2f}" in t for _, _, t in hook.calls)
    finally:
        eng.close()


async def test_a_council_sell_is_the_same_fraction_of_each_books_holding(cfg, store, ledger):
    eng, hook, _ = await make_engine(cfg, store)
    try:
        await meet(eng)
        before = (eng.ledger.positions()["TLT"].qty, eng.shadow.ledger.positions()["TLT"].qty)
        eng.council = sell_council(cfg, "TLT", 0.5)
        await meet(eng)
        after = (eng.ledger.positions()["TLT"].qty, eng.shadow.ledger.positions()["TLT"].qty)
        assert [float(a / b) for a, b in zip(after, before)] == pytest.approx([0.5, 0.5], rel=0.01)
    finally:
        eng.close()


async def test_code_sizing_never_forks_a_shadow(cfg, store, ledger):
    cfg = sized(cfg, "code")
    eng, hook, _ = await make_engine(cfg, store, council=buy_council(cfg, "TLT"))
    await meet(eng)
    assert eng.shadow is None and not shadowbook.path_for(store).exists()
    st, v = verdict(store, "TLT")
    assert st == "filled" and v["sized_by"] == "code" and v["target_pct"] == v["code_target_pct"]
    assert not any("Model sizing" in p for p in hook.posts)
    assert "Risk-model target weight at conviction 1.0" in eng.council.chair.prompts[0]


async def test_both_books_get_an_equity_point_at_the_same_moment_and_compare(cfg, store, ledger):
    eng, hook, rig = await make_engine(cfg, store)
    try:
        await meet(eng)
        rig.at(OPEN + 600)
        out = await eng.equity_tick()
        assert "formula-sized shadow $" in out
        lts = [r[0] for r in store.conn.execute("SELECT ts FROM equity")]
        sts = [r[0] for r in eng.shadow.store.conn.execute("SELECT ts FROM equity")]
        assert lts[-1] == sts[-1] == OPEN + 600
        c = shadowbook.compare(store, eng.shadow.store, cfg.universe.cash_symbol)
        assert c["points"] == 1 and c["sessions"] == 0 and c["base_equity"] == pytest.approx(10000.0)
        assert c["live"]["council_buys"]["count"] == c["shadow"]["council_buys"]["count"] == 1
        assert c["live"]["council_buys"]["usd"] > c["shadow"]["council_buys"]["usd"]
        assert c["live"]["idle_pct"] < c["shadow"]["idle_pct"]               # the chair put more to work
        assert c["chair_sizing"]["buys"] == c["chair_sizing"]["above"] == 1
        assert c["difference_usd"] == pytest.approx(c["live"]["equity"] - c["shadow"]["equity"])
        text = "\n".join(shadowbook.report(c))
        assert "too early to judge before 20" in text and "chair (live)" in text and "formula (shadow)" in text
        assert "the chair sized 1 BUY(s): 1 above the formula's target" in text
        line = shadowbook.status_line(shadowbook.compare_files(store, cfg.universe.cash_symbol))
        assert line.startswith("sizing: chair $") and "vs formula $" in line
    finally:
        eng.close()


async def test_the_shadow_is_kept_across_a_restart(cfg, store, ledger):
    eng, _, _ = await make_engine(cfg, store)
    await meet(eng)
    held = eng.shadow.ledger.positions()["TLT"].qty
    eng.close()
    again, _, _ = await make_engine(cfg, store)
    try:
        assert again.shadow is not None and again.shadow.ledger.positions()["TLT"].qty == held
        assert again.market.extra_symbols() >= {"TLT"}                     # the market keeps pricing it
    finally:
        again.close()


async def test_code_sizing_retires_the_shadow_and_never_deletes_it(cfg, store, ledger):
    eng, _, _ = await make_engine(cfg, store)
    await meet(eng)
    eng.close()
    code, _, _ = await make_engine(sized(cfg, "code"), store, council=buy_council(cfg, "TLT"))
    assert code.shadow is None and not shadowbook.path_for(store).exists()
    (kept,) = list(store.path.parent.glob("shadow.db.retired-*[0-9]"))
    assert Store(kept, writer=False).meta(shadowbook.SHADOW_OF) == store.meta(shadowbook.BOOK_ID)
    (audit,) = store.audits("shadow_retired")
    assert "sizer is code" in audit["why"]


async def test_a_shadow_forked_from_another_book_is_retired_and_forked_afresh(cfg, store, ledger):
    eng, _, _ = await make_engine(cfg, store)
    await meet(eng)
    eng.close()
    store.set_meta(shadowbook.BOOK_ID, "another-book")                  # e.g. a new book after an archive
    again, _, _ = await make_engine(cfg, store, council=buy_council(cfg, "IEF", weight_pct=10))
    try:
        assert again.shadow is None and store.audits("shadow_retired")
        await meet(again)
        assert again.shadow is not None
        assert again.shadow.store.meta(shadowbook.SHADOW_OF) == "another-book"
    finally:
        again.close()


async def test_a_half_finished_fork_is_retired(cfg, store, ledger):
    path = shadowbook.path_for(store)
    s = Store(path, writer=True)                                          # a copy that never got its marks
    s.close()
    eng, _, _ = await make_engine(cfg, store)
    assert eng.shadow is None and not path.exists() and store.audits("shadow_retired")


async def test_a_buy_only_the_formula_takes_is_sold_off_when_the_asset_leaves(cfg, store, ledger):
    """The chair's 0.5% target is under the minimum trade, so the live book buys no TLT;
    the formula's is not, so the shadow does. When TLT is being sold off the council (which
    sees only the live book) can't sell the shadow's TLT, so the shadow's jobs do."""
    eng, _, rig = await make_engine(cfg, store, council=buy_council(cfg, "TLT", weight_pct=0.5))
    try:
        await meet(eng)
        assert "TLT" not in eng.ledger.positions() and "TLT" in eng.shadow.ledger.positions()
        assert verdict(store, "TLT")[1]["reason"] == "sized_below_min_by_target"
        assert eng._stranded(eng.shadow.ledger.positions(), "2026-10-21", OPEN) == []   # still buyable
        eng.members["TLT"] = Member("TLT", "bond_etf", SUNSET)
        rig.at(OPEN + 300)
        assert await eng._shadow_jobs(OPEN + 300) is None
        await eng.drain()
        assert "TLT" not in eng.shadow.ledger.positions()
        (row,) = eng.shadow.store.conn.execute("SELECT origin, idem_key FROM orders WHERE side='SELL'").fetchall()
        assert tuple(row) == ("sunset", "sunset:TLT:2026-10-21")
    finally:
        eng.close()


async def test_the_risk_monitor_protects_the_shadows_own_holdings(cfg, store, ledger):
    """A breached stop sells TLT in both books; only the live book's sale is announced."""
    eng, hook, rig = await make_engine(cfg, store)
    try:
        await meet(eng)
        stop = max(eng.ledger.positions()["TLT"].stop_price, eng.shadow.ledger.positions()["TLT"].stop_price)
        p = round(stop * 0.99, 2)
        rig.at(OPEN + 60, requote=False)
        rig.fake.add_quote("TLT", p, p + 0.02, age=2)
        rig.market._snaps_ts = 0
        posted = len(hook.posts)
        async with eng.alerts.running():
            res = await eng.risk_tick()
            await eng.drain()
        assert res.summary() == "stop TLT"                                   # the live book's result
        for book in (store, eng.shadow.store):
            (origin, price) = book.conn.execute("SELECT origin, price FROM fills WHERE side='SELL'").fetchone()
            assert (origin, float(price)) == ("stop", p)
        assert "TLT" not in eng.ledger.positions() and "TLT" not in eng.shadow.ledger.positions()
        # Discord hears the live book's gap and sale, and nothing of the shadow's.
        shadow_usd = float(eng.shadow.store.conn.execute("SELECT notional FROM fills WHERE side='SELL'").fetchone()[0])
        sales = [t for t in hook.posts[posted:] if t.startswith("**SELL TLT**")]
        assert len(sales) == 1 and f"${shadow_usd:,.2f}" not in "".join(hook.posts)
    finally:
        eng.close()


async def test_the_kill_switch_holds_the_shadow_too(cfg, store, ledger):
    eng, _, _ = await make_engine(cfg, store)
    eng.kill.set(True, "test", "hold everything")
    try:
        await meet(eng)
        assert eng.ledger.positions().get("TLT") is None and eng.shadow.ledger.positions().get("TLT") is None
        assert verdict(eng.shadow.store, "TLT")[0] == "dry_run"
    finally:
        eng.close()


# ---- the shadow module -------------------------------------------------------------------------
def test_sync_actions_copies_only_what_changed(cfg, store, tmp_path):
    (tmp_path / "s").mkdir()
    shadow = Store(tmp_path / "s" / "shadow.db", writer=True)
    try:
        def announce(aid, ts, detail="{}"):
            with store.tx() as c:
                c.execute("INSERT OR REPLACE INTO corporate_actions(id,kind,symbol,ex_day,detail,first_seen_ts,"
                          "updated_ts) VALUES(?,?,?,?,?,?,?)", (aid, "cash_dividend", "TLT", "2026-11-02", detail,
                                                               100.0, ts))
        announce("a1", 100.0)
        assert shadowbook.sync_actions(store, shadow) == 1
        assert shadowbook.sync_actions(store, shadow) == 0
        announce("a1", 200.0, '{"rate": 0.3}')                            # revised
        announce("a2", 200.0)
        assert shadowbook.sync_actions(store, shadow) == 2
        rows = dict(shadow.conn.execute("SELECT id, detail FROM corporate_actions").fetchall())
        assert rows == {"a1": '{"rate": 0.3}', "a2": "{}"}
    finally:
        shadow.close()


def test_the_report_before_a_shadow_exists():
    assert "no shadow book yet" in shadowbook.report(None)[0]
    assert shadowbook.status_line(None) is None


# ---- where it shows -----------------------------------------------------------------------------
def test_discord_says_who_sized_a_buy():
    meeting = format_event("meeting", {"id": "m", "status": "ok", "summary": "s", "trades": [
        {"action": "BUY", "asset": "KO", "conviction": 0.6, "weight_pct": 6.0, "reasoning": "r"},
        {"action": "SELL", "asset": "SPY", "fraction_of_position": 0.5, "reasoning": "r"}]})
    assert "BUY KO to 6.0%, SELL SPY" in meeting
    started = format_event("shadow_forked", {"equity": 10012.5})
    assert started.startswith("**Model sizing starts**") and "$10,012.50" in started and "—" not in started


async def test_the_api_shows_both_books(cfg, store, ledger):
    cfg = dataclasses.replace(cfg, api_key=KEY)
    eng, _, rig = await make_engine(cfg, store)
    server = ApiServer(eng, "127.0.0.1", 0)
    try:
        await meet(eng)
        rig.at(OPEN + 600)
        await eng.equity_tick()
        await server.start()
        async with aiohttp.ClientSession(f"http://127.0.0.1:{server.port}") as session:
            async with session.get("/api/sizing", headers={"Authorization": f"Bearer {KEY}"}) as r:
                assert r.status == 200
                body = await r.json()
            async with session.get("/api/book", headers={"Authorization": f"Bearer {KEY}"}) as r:
                book = await r.json()
        assert body["sizer"] == "model" and body["comparison"]["live"]["council_buys"]["count"] == 1
        c = body["comparison"]
        assert book["sizing"] == {"since": c["since"], "sessions": 0, "chair_equity": c["live"]["equity"],
                                  "formula_equity": c["shadow"]["equity"], "difference_usd": c["difference_usd"]}
        view = meeting_order(store)
        assert (view["sized_by"], view["target_pct"]) == ("model", 20.0) and view["formula_pct"] > 0
    finally:
        await server.stop()
        eng.close()


def meeting_order(store):
    """The API's meeting view of the last meeting's TLT order."""
    from ottr.api import _meeting_view
    (mid, ts, status, record) = store.conn.execute("SELECT id, ts, status, record FROM meetings").fetchone()
    view = _meeting_view(SimpleNamespace(reader=store), mid, ts, status, record, full=False)
    return {o["asset"]: o for o in view["orders"]}["TLT"]


async def test_status_and_scorecard_show_the_comparison(cfg, store, ledger, capsys):
    eng, _, rig = await make_engine(cfg, store)
    await meet(eng)
    rig.at(OPEN + 600)
    await eng.equity_tick()
    eng.close()
    store.close()
    assert await cli.cmd_scorecard(cfg, SimpleNamespace(json=False)) == 0
    out = capsys.readouterr().out
    assert "Chair's sizing vs the formula" in out and "formula (shadow)" in out
    assert await cli.cmd_scorecard(cfg, SimpleNamespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)["sizing"]["points"] == 1


async def test_resume_clears_the_shadows_halt_too(cfg, store, ledger, capsys):
    eng, _, _ = await make_engine(cfg, store)
    await meet(eng)
    for s in (store, eng.shadow.store):
        s.set_state("risk", {"halted": True, "halted_since": OPEN, "peak_equity": 12000.0})
    shadow_path = eng.shadow.store.path
    eng.close()
    store.close()
    assert await cli.cmd_resume(cfg, SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert "halt cleared" in out and "shadow book: halt cleared" in out
    s = Store(shadow_path, writer=False)
    try:
        assert s.get_state("risk")["halted"] is False
    finally:
        s.close()


async def test_the_archive_keeps_and_retires_the_shadow_with_the_book(cfg, store, ledger, tmp_path):
    eng, _, _ = await make_engine(cfg, store)
    await meet(eng)
    eng.close()
    store.close()
    data = cfg.data_dir
    done = archive.archive(data, tmp_path / "archived", tmp_path / "missing.toml", retire=True)
    assert (done.dest / "shadow.db").exists() and not (data / "shadow.db").exists()
    assert list(data.glob("shadow.db.retired-*"))
    assert "Shadow book" in (done.dest / "NOTE.md").read_text(encoding="utf-8")
