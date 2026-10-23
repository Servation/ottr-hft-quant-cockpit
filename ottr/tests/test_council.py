"""
Council tests: JSON extraction, schema rules, repair retry, anonymization, code-side rules,
and the fallback chair that stands in when the chair fails.
"""

import pytest

from ottr.council.council import Council, compute_tally, enforce_rules
from ottr.council.models import ModelError, ask_json, extract_json
from ottr.council.schemas import SchemaError, analyst_validator, chair_validator
from tests.fakes import ScriptedModel, analyst_reply

U = ["SPY", "TLT"]


def test_extract_json_tolerates_fences_rejects_nan():
    assert extract_json('ok\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json('{"conviction": NaN}')
    with pytest.raises(ValueError):
        extract_json("no json")


def test_analyst_schema_requires_every_asset_once():
    v = analyst_validator(U)
    ok = analyst_reply(U)
    assert [a["asset"] for a in v(ok)["assets"]] == U
    ok["assets"].pop()
    with pytest.raises(SchemaError):
        v(ok)
    with pytest.raises(SchemaError):
        v(analyst_reply(U, {"SPY": ("MOON", 0.5)}))
    with pytest.raises(SchemaError):
        v(analyst_reply(U, {"SPY": ("BUY", 1.5)}))


def test_chair_schema_has_no_sizing_and_bounds():
    v = chair_validator(U, max_trades=1)
    assert v({"summary": "s", "trades": [{"action": "buy", "asset": "tlt", "conviction": 0.5,
                                          "reasoning": "r"}]})["trades"][0]["asset"] == "TLT"
    for t in ([{"action": "BUY", "asset": "TLT", "conviction": 0.5, "usd_amount": 5, "reasoning": "r"}],
              [{"action": "SELL", "asset": "TLT", "fraction_of_position": 0, "reasoning": "r"}],
              [{"action": "BUY", "asset": "DOGE", "conviction": 0.5, "reasoning": "r"}],
              [{"action": "BUY", "asset": "TLT", "conviction": True, "reasoning": "r"}]):
        with pytest.raises(SchemaError):
            v({"summary": "s", "trades": t})


async def test_ask_json_repairs_once_then_gives_up():
    m = ScriptedModel("x", ["garbage", analyst_reply(U)])
    assert (await ask_json(m, "p", analyst_validator(U)))["overall_view"] == "test view"
    assert "rejected" in m.prompts[1]
    with pytest.raises(ModelError):
        await ask_json(ScriptedModel("x", ["bad", "bad"]), "p", analyst_validator(U))


def test_tally_and_rules():
    reps = {n: analyst_validator(U)(analyst_reply(U, {"SPY": (s, 0.6)}))
            for n, s in (("a", "BUY"), ("b", "BUY"), ("c", "SELL"))}
    t = compute_tally(reps, U)
    assert t["assets"]["SPY"]["counts"] == {"BUY": 2, "SELL": 1} and t["assets"]["SPY"]["split"]
    decision = {"trades": [{"action": "BUY", "asset": "SPY"}, {"action": "BUY", "asset": "TLT"},
                           {"action": "SELL", "asset": "TLT"}]}
    kept, dropped = enforce_rules(decision, reps, held=set())
    assert kept == [{"action": "BUY", "asset": "SPY"}]
    assert [w for _, w in dropped] == ["no analyst rated it BUY", "not held (desk is long-only)"]


async def test_chair_sees_anonymized_reports():
    chair = ScriptedModel("chair", [{"summary": "s", "trades": []}])
    analysts = {n: ScriptedModel(n, [analyst_reply(U)]) for n in ("claude", "gpt", "gemini")}
    rec = await Council(analysts, chair, U).run("ctx", held=set())
    p = chair.prompts[0].lower()
    assert "analyst a" in p and not any(n in p for n in ("claude", "gpt", "gemini"))
    assert rec["labels"] == {"claude": "A", "gemini": "B", "gpt": "C"}  # kept internally


# -- the fallback chair ----------------------------------------------------------------------------
SIGNED_OUT = "claude exit 1: Not logged in · Please run /login"


def _analysts():
    return {n: ScriptedModel(n, [analyst_reply(U, {"SPY": ("BUY", 0.7)})]) for n in ("claude", "gpt", "gemini")}


async def test_a_failed_chair_hands_the_same_reports_to_the_fallback():
    """2026-10-23: the claude CLI was signed out, so the chair failed and the 10:15 meeting
    abstained although GPT and Gemini had reported. Now the fallback chairs instead."""
    chair = ScriptedModel("claude", [ModelError(SIGNED_OUT)])
    fallback = ScriptedModel("gpt", [{"summary": "buy spy", "trades": [
        {"action": "BUY", "asset": "SPY", "conviction": 0.6, "reasoning": "trend"}]}])
    events = []
    rec = await Council(_analysts(), chair, U, fallback=fallback).run(
        "ctx", held=set(), observe=lambda k, d: events.append((k, d)))
    assert rec["status"] == "ok" and [t["asset"] for t in rec["trades"]] == ["SPY"]
    assert rec["chair"]["name"] == "gpt" and rec["chair"]["replaced"] == "claude"
    assert rec["chair"]["error"] == SIGNED_OUT and rec["chair"]["failed_after_sec"] >= 0
    assert fallback.prompts == chair.prompts  # the same anonymized reports and rules
    assert [k for k, _ in events if k.startswith("chair")] == ["chair_started", "chair_replaced", "chair_done"]
    replaced = dict(events)["chair_replaced"]
    assert (replaced["name"], replaced["replaced"], replaced["error"]) == ("gpt", "claude", SIGNED_OUT)


async def test_a_chair_that_works_is_recorded_and_the_fallback_never_asked():
    chair = ScriptedModel("claude", [{"summary": "s", "trades": []}])
    fallback = ScriptedModel("gpt", [])
    rec = await Council(_analysts(), chair, U, fallback=fallback).run("ctx", held=set())
    assert rec["status"] == "ok" and rec["chair"] == {"name": "claude"} and fallback.prompts == []


async def test_both_chairs_failing_abstains_with_both_reasons():
    bad = {"summary": "s", "trades": [{"action": "BUY", "asset": "SPY", "usd_amount": 900, "reasoning": "r"}]}
    chair = ScriptedModel("claude", [ModelError(SIGNED_OUT)])
    fallback = ScriptedModel("gpt", [bad, bad])  # sizes in dollars twice: invalid after the retry
    events = []
    rec = await Council(_analysts(), chair, U, fallback=fallback).run(
        "ctx", held=set(), observe=lambda k, d: events.append((k, d)))
    assert rec["status"].startswith(f"abstain: chair failed ({SIGNED_OUT}); fallback gpt failed too (gpt: invalid output")
    assert rec["trades"] == [] and rec["chair"]["name"] == "gpt"
    assert events[-1][0] == "chair_failed" and "invalid output" in events[-1][1]["error"]


async def test_without_a_fallback_a_failed_chair_abstains():
    rec = await Council(_analysts(), ScriptedModel("claude", [ModelError(SIGNED_OUT)]), U).run("ctx", held=set())
    assert rec["status"] == f"abstain: chair failed ({SIGNED_OUT})"
