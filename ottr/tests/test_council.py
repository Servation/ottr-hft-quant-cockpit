"""
Council tests: JSON extraction, schema rules, repair retry, anonymization, code-side rules,
the fallback chair that stands in when the chair fails, and round 2 where the analysts split.
"""

import pytest

from ottr.council.council import Council, compute_tally, enforce_rules
from ottr.council.models import ModelError, ask_json, extract_json
from ottr.council.schemas import SchemaError, analyst_validator, chair_validator, debate_validator
from tests.fakes import ScriptedModel, analyst_reply, debate_reply

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
    assert rec["status"] == "ok" and rec["chair"]["name"] == "claude" and fallback.prompts == []
    assert "replaced" not in rec["chair"]


def _as(model: ScriptedModel, name: str, effort=None) -> ScriptedModel:
    model.model, model.effort = name, effort
    return model


async def test_the_record_names_each_reports_model_and_the_chairs():
    """The scorecard splits every analyst's results by the model behind them, so a model
    change shows as a before and an after: the record keeps who wrote each report."""
    analysts = _analysts()
    _as(analysts["claude"], "claude-fable-5-2", "xhigh")
    _as(analysts["gemini"], "gemini-3.8-flash-high")
    analysts["gpt"] = ScriptedModel("gpt", [ModelError("codex exit 1: down")])
    chair = _as(ScriptedModel("claude", [{"summary": "s", "trades": []}]), "claude-opus-5-5", "high")
    rec = await Council(analysts, chair, U).run("ctx", held=set())
    assert rec["models"] == {"claude": {"model": "claude-fable-5-2", "effort": "xhigh"},
                             "gemini": {"model": "gemini-3.8-flash-high", "effort": None}}   # gpt dropped out
    assert rec["chair"] == {"name": "claude", "model": "claude-opus-5-5", "effort": "high"}


async def test_a_fallback_chair_is_recorded_with_its_own_model():
    chair = _as(ScriptedModel("claude", [ModelError(SIGNED_OUT)]), "claude-opus-5-5", "high")
    fallback = _as(ScriptedModel("gpt", [{"summary": "s", "trades": []}]), "gpt-6.1-sol", "high")
    rec = await Council(_analysts(), chair, U, fallback=fallback).run("ctx", held=set())
    assert (rec["chair"]["name"], rec["chair"]["replaced"]) == ("gpt", "claude")
    assert (rec["chair"]["model"], rec["chair"]["effort"]) == ("gpt-6.1-sol", "high")


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


# -- round 2 ---------------------------------------------------------------------------------------
def _split(round2, spy=("BUY", "HOLD", "HOLD")):
    """claude, gemini and gpt rate SPY as `spy` in round 1 (TLT: all HOLD), then answer
    round 2 with `round2` (a reply or an exception each, same order)."""
    return {n: ScriptedModel(n, [analyst_reply(U, {"SPY": (s, 0.7 if s == "BUY" else 0.5)}), r])
            for n, s, r in zip(("claude", "gemini", "gpt"), spy, round2)}


async def test_round_two_argues_only_the_split_and_the_chair_reads_both_rounds():
    analysts = _split([debate_reply({"SPY": ("BUY", 0.7)}), debate_reply({"SPY": ("HOLD", 0.6)}),
                       debate_reply({"SPY": ("BUY", 0.5)})])
    chair = ScriptedModel("chair", [{"summary": "s", "trades": []}])
    events = []
    rec = await Council(analysts, chair, U, debate=True).run("ctx", held=set(),
                                                             observe=lambda k, d: events.append(k))
    assert rec["debate"]["assets"] == ["SPY"]                          # TLT, all HOLD, isn't argued
    second = analysts["gpt"].prompts[1]
    assert second.startswith("You are Analyst C") and "split on SPY" in second
    body = second.split("=== ROUND 1 ===")[1]
    assert "Analyst A: BUY (0.70) t Risk: r" in body and "You: HOLD (0.50)" in body and "TLT:" not in body
    assert not any(n in body for n in ("claude", "gpt", "gemini"))   # anonymized, as for the chair
    assert rec["analysts"]["gpt"]["assets"][0]["stance"] == "HOLD"     # round 1 stays as it was
    assert rec["debate"]["changes"] == {"claude": [], "gemini": [],
                                        "gpt": [{"asset": "SPY", "from": "HOLD", "to": "BUY", "conviction": 0.5}]}
    assert rec["tally"]["assets"]["SPY"]["counts"] == {"BUY": 2, "HOLD": 1}   # final stances
    p = chair.prompts[0]
    assert "Where they split (SPY), each then read the others' cases" in p
    assert "Round 2: held BUY (0.70). on SPY: weighed it" in p and "Round 2: changed to BUY (0.50)" in p
    assert "Round 2: held HOLD (0.60)" in p
    assert events == ["analysts_started", *["analyst_done"] * 3, "debate_started", *["debate_done"] * 3,
                      "chair_started", "chair_done"]


async def test_no_round_two_when_the_analysts_agree_or_it_is_off():
    for debate, spy in ((True, ("BUY", "BUY", "BUY")), (False, ("BUY", "HOLD", "HOLD"))):
        analysts = _split([None] * 3, spy)
        chair = ScriptedModel("chair", [{"summary": "s", "trades": []}])
        rec = await Council(analysts, chair, U, debate=debate).run("ctx", held=set())
        assert rec["debate"] is None and all(len(a.prompts) == 1 for a in analysts.values())
        assert "Round 2" not in chair.prompts[0] and "Where they split" not in chair.prompts[0]


async def test_round_two_can_take_away_a_buys_only_backer():
    analysts = _split([debate_reply({"SPY": ("HOLD", 0.4)}), debate_reply({"SPY": ("HOLD", 0.5)}),
                       debate_reply({"SPY": ("HOLD", 0.5)})])
    chair = ScriptedModel("chair", [{"summary": "s", "trades": [
        {"action": "BUY", "asset": "SPY", "conviction": 0.6, "reasoning": "r"}]}])
    rec = await Council(analysts, chair, U, debate=True).run("ctx", held=set())
    assert rec["trades"] == [] and rec["dropped"][0]["why"] == "no analyst rated it BUY"


async def test_an_analyst_that_fails_round_two_keeps_its_round_one_stance():
    """claude's BUY is the only one; it times out in round 2, so its BUY still backs the
    chair's trade and the chair is told it didn't answer."""
    analysts = _split([ModelError("claude: timed out after 300s"), debate_reply({"SPY": ("HOLD", 0.5)}),
                       debate_reply({"SPY": ("HOLD", 0.5)})])
    chair = ScriptedModel("chair", [{"summary": "s", "trades": [
        {"action": "BUY", "asset": "SPY", "conviction": 0.6, "reasoning": "r"}]}])
    events = []
    rec = await Council(analysts, chair, U, debate=True).run("ctx", held=set(),
                                                             observe=lambda k, d: events.append((k, d)))
    assert rec["status"] == "ok" and [t["asset"] for t in rec["trades"]] == ["SPY"]
    assert rec["debate"]["errors"] == {"claude": "claude: timed out after 300s"}
    assert set(rec["debate"]["answers"]) == {"gemini", "gpt"}
    assert "Round 2: no answer, so its round-1 stance stands." in chair.prompts[0]
    dropped = [d for k, d in events if k == "debate_dropped"]
    assert [d["name"] for d in dropped] == ["claude"] and dropped[0]["error"].endswith("300s")


def test_round_two_must_answer_exactly_the_split_assets():
    v = debate_validator(["SPY"])
    assert v(debate_reply({"SPY": ("buy", 0.6)}))["assets"][0]["stance"] == "BUY"
    for bad, why in ((debate_reply({}), "missing stance"), (debate_reply({"SPY": ("BUY", 0.6), "TLT": ("BUY", 0.6)}), "unknown asset"),
                     ({"assets": [{"asset": "SPY", "stance": "BUY", "conviction": 0.6}]}, "reply"),
                     ({"assets": [{"asset": "SPY", "stance": "BUY", "conviction": 1.5, "reply": "x"}]}, "outside 0..1")):
        with pytest.raises(SchemaError, match=why):
            v(bad)


# ---- model sizing: the chair gives each BUY a weight ----------------------------------------
def test_a_sized_chair_must_weigh_each_buy_and_never_name_dollars():
    sized, unsized = chair_validator(U, max_trades=2, sized=True), chair_validator(U, max_trades=2)
    buy = {"action": "BUY", "asset": "TLT", "conviction": 0.6, "weight_pct": 12.5, "reasoning": "r"}
    sell = {"action": "SELL", "asset": "SPY", "fraction_of_position": 0.5, "reasoning": "r"}
    out = sized({"summary": "s", "trades": [buy, sell]})["trades"]
    assert out[0]["weight_pct"] == 12.5 and "weight_pct" not in out[1]
    assert "weight_pct" not in unsized({"summary": "s", "trades": [buy]})["trades"][0]   # code sizes: ignored
    for bad in (None, 0, -5, 100.5, "lots", True, float("nan"), float("inf")):
        t = dict(buy, weight_pct=bad) if bad is not None else {k: v for k, v in buy.items() if k != "weight_pct"}
        with pytest.raises(SchemaError, match="weight_pct"):
            sized({"summary": "s", "trades": [t]})
    for money in ({"usd_amount": 500}, {"quantity": 3}):
        with pytest.raises(SchemaError, match="no dollar amounts or share counts; size a BUY with weight_pct"):
            sized({"summary": "s", "trades": [dict(buy, **money)]})


def test_the_prompts_say_who_sizes():
    from ottr.council.council import analyst_prompt, chair_prompt
    tally = compute_tally({}, U)
    sized = chair_prompt("ctx", {}, tally, U, 3, model_sizing=True)
    assert "You size the BUYs" in sized and '"weight_pct": 0-100' in sized and "clamps weight_pct" in sized
    code = chair_prompt("ctx", {}, tally, U, 3)
    assert "the risk model sizes it" in code and "weight_pct" not in code
    assert "The chair sizes positions" in analyst_prompt("ctx", "", U, model_sizing=True)
    assert "set by the desk's risk model" in analyst_prompt("ctx", "", U)


async def test_a_sized_chairs_weight_reaches_the_record():
    chair = ScriptedModel("claude", [{"summary": "s", "trades": [
        {"action": "BUY", "asset": "SPY", "conviction": 0.6, "weight_pct": 9, "reasoning": "r"}]}])
    rec = await Council(_analysts(), chair, U, model_sizing=True).run("ctx", held=set())
    assert rec["sizer"] == "model" and rec["trades"][0]["weight_pct"] == 9.0
    assert "You size the BUYs" in chair.prompts[0]
    plain = await Council(_analysts(), ScriptedModel("claude", [{"summary": "s", "trades": []}]), U).run(
        "ctx", held=set())
    assert plain["sizer"] == "code"


async def test_a_sized_chair_that_forgets_the_weight_gets_one_repair():
    """A BUY without weight_pct fails validation; the repair round asks again and a good
    answer is kept (the same rule as any other schema error)."""
    trade = {"action": "BUY", "asset": "SPY", "conviction": 0.6, "reasoning": "r"}   # the analysts back SPY
    chair = ScriptedModel("claude", [{"summary": "s", "trades": [trade]},
                                     {"summary": "s", "trades": [dict(trade, weight_pct=7)]}])
    rec = await Council(_analysts(), chair, U, model_sizing=True).run("ctx", held=set())
    assert rec["trades"][0]["weight_pct"] == 7.0 and len(chair.prompts) == 2
