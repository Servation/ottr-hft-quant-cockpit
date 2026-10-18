"""
Tests for the multi-model council (bot/council).

What it covers: JSON extraction, schema validation, the repair retry, quorum/fail-closed
behavior, anonymization, and the computed tally. Uses fake adapters: no CLI or network.
"""

import json

import pytest

from bot.council.council import Council, compute_tally
from bot.council.models import ModelAdapter, ModelError, ask_json, extract_json
from bot.council.schemas import SchemaError, make_analyst_validator, make_chair_validator

UNI = ["BTC", "ETH"]


def analyst_json(btc="BUY", eth="HOLD", conv=0.7):
    return json.dumps({
        "overall_view": "v",
        "assets": [
            {"asset": "BTC", "stance": btc, "conviction": conv, "thesis": "t", "key_risk": "r"},
            {"asset": "ETH", "stance": eth, "conviction": conv, "thesis": "t", "key_risk": "r"},
        ],
    })


class Fake(ModelAdapter):
    def __init__(self, name, replies):
        self.name, self.replies, self.prompts = name, list(replies), []

    async def complete(self, prompt, timeout=240.0):
        self.prompts.append(prompt)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_extract_json_handles_fences_and_chatter():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_analyst_validator_requires_every_asset_and_known_stance():
    v = make_analyst_validator(UNI)
    assert v(json.loads(analyst_json()))["assets"][0]["asset"] == "BTC"
    bad = json.loads(analyst_json())
    bad["assets"].pop()
    with pytest.raises(SchemaError):
        v(bad)
    bad = json.loads(analyst_json(btc="MOON"))
    with pytest.raises(SchemaError):
        v(bad)


def test_chair_validator_rules():
    v = make_chair_validator(UNI, max_trades=1)
    ok = {"summary": "s", "trades": [
        {"action": "buy", "asset": "btc", "usd_amount": 100, "conviction": 0.5, "reasoning": "r"}]}
    assert v(ok)["trades"][0]["asset"] == "BTC"
    for trades in (
        [{"action": "SELL", "asset": "BTC", "fraction_of_position": 1.5, "conviction": 0.5, "reasoning": "r"}],
        [{"action": "BUY", "asset": "DOGE", "usd_amount": 5, "conviction": 0.5, "reasoning": "r"}],
        [{"action": "BUY", "asset": "BTC", "usd_amount": -5, "conviction": 0.5, "reasoning": "r"}],
    ):
        with pytest.raises(SchemaError):
            v({"summary": "s", "trades": trades})
    with pytest.raises(SchemaError):  # over max_trades
        v({"summary": "s", "trades": ok["trades"] * 2})


@pytest.mark.asyncio
async def test_ask_json_repairs_once():
    m = Fake("x", ["garbage", analyst_json()])
    out = await ask_json(m, "p", make_analyst_validator(UNI))
    assert out["overall_view"] == "v"
    assert "rejected" in m.prompts[1]


@pytest.mark.asyncio
async def test_ask_json_gives_up_after_retry():
    m = Fake("x", ["garbage", "still garbage"])
    with pytest.raises(ModelError):
        await ask_json(m, "p", make_analyst_validator(UNI))


def test_tally_counts_and_split():
    reps = {n: make_analyst_validator(UNI)(json.loads(analyst_json(btc=b)))
            for n, b in (("a", "BUY"), ("b", "BUY"), ("c", "SELL"))}
    t = compute_tally(reps, UNI)
    assert t["assets"]["BTC"]["counts"] == {"BUY": 2, "SELL": 1}
    assert t["assets"]["BTC"]["split"] and t["assets"]["BTC"]["leader"] == "BUY"
    assert t["assets"]["ETH"]["unanimous"]


CHAIR_OK = json.dumps({"summary": "s", "dissent_notes": "", "trades": [
    {"action": "BUY", "asset": "BTC", "usd_amount": 100, "conviction": 0.6, "reasoning": "r"}]})


@pytest.mark.asyncio
async def test_council_happy_path_anonymizes_for_chair():
    chair = Fake("chair", [CHAIR_OK])
    analysts = {n: Fake(n, [analyst_json()]) for n in ("claude", "gpt", "gemini")}
    rec = await Council(analysts, chair, UNI).run("packet")
    assert rec["status"] == "ok" and len(rec["trades"]) == 1
    p = chair.prompts[0]
    assert "Analyst A" in p and "claude" not in p.lower() and "gemini" not in p.lower()


@pytest.mark.asyncio
async def test_council_abstains_below_quorum():
    chair = Fake("chair", [CHAIR_OK])
    analysts = {
        "claude": Fake("claude", [analyst_json()]),
        "gpt": Fake("gpt", [ModelError("down")]),
        "gemini": Fake("gemini", [ModelError("down")]),
    }
    rec = await Council(analysts, chair, UNI).run("packet")
    assert rec["status"].startswith("abstain") and rec["trades"] == []
    assert chair.prompts == []  # chair never consulted without quorum


@pytest.mark.asyncio
async def test_council_abstains_when_chair_fails():
    chair = Fake("chair", [ModelError("down")])
    analysts = {n: Fake(n, [analyst_json()]) for n in ("claude", "gpt")}
    rec = await Council(analysts, chair, UNI).run("packet")
    assert rec["status"].startswith("abstain: chair") and rec["trades"] == []
