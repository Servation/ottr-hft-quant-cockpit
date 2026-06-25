import pytest
from datetime import datetime, timedelta, timezone

from bot.knowledge_graph import (
    AgentReputationGraph,
    RESOLVE_HORIZON_HOURS,
    _per_horizon_sigma,
)


@pytest.fixture
def fresh_graph(tmp_path):
    # Override GRAPH_PATH for tests
    import bot.knowledge_graph
    original_path = bot.knowledge_graph.GRAPH_PATH
    bot.knowledge_graph.GRAPH_PATH = tmp_path / "test_graph.json"

    graph = AgentReputationGraph()
    yield graph

    bot.knowledge_graph.GRAPH_PATH = original_path


# --- helpers ---------------------------------------------------------------

def _age_all_pending(graph, hours=RESOLVE_HORIZON_HOURS + 1):
    """Backdate every PENDING vote so it's past the resolution horizon."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    for n, d in graph.graph.nodes(data=True):
        if d.get("type") == "vote" and d.get("status") == "PENDING":
            graph.graph.nodes[n]["timestamp"] = cutoff


def _vote_nodes(graph):
    return [graph.graph.nodes[n] for n, d in graph.graph.nodes(data=True) if d.get("type") == "vote"]


def _score_for_asset(graph, asset):
    """Score of the (single) resolved vote on the given asset, or None."""
    for n, d in graph.graph.nodes(data=True):
        if d.get("type") == "vote" and d.get("score") is not None:
            edges = list(graph.graph.out_edges(n))
            if edges and edges[0][1] == asset:
                return d["score"]
    return None


# --- recording -------------------------------------------------------------

def test_record_vote(fresh_graph):
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)

    assert fresh_graph.graph.has_node("trader")
    assert fresh_graph.graph.has_node("SOL")

    votes = _vote_nodes(fresh_graph)
    assert len(votes) == 1
    assert votes[0]["direction"] == "BUY"
    assert votes[0]["price_at_vote"] == 100.0
    assert votes[0]["status"] == "PENDING"


# --- resolution & scoring --------------------------------------------------

def test_not_resolved_before_horizon(fresh_graph):
    """A fresh vote stays PENDING until it reaches the resolution horizon
    (no early-stop at the first threshold-crossing cron tick)."""
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)
    fresh_graph.evaluate_pending_votes({"SOL": 110.0})  # +10% but only seconds old
    assert _vote_nodes(fresh_graph)[0]["status"] == "PENDING"


def test_buy_correct_move_scores_positive(fresh_graph):
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"SOL": 102.0})  # +2% ~= +1 sigma (default)

    v = _vote_nodes(fresh_graph)[0]
    assert v["status"] == "RESOLVED"
    assert v["outcome"] == "HIT"
    assert v["score"] == pytest.approx(1.0)


def test_buy_wrong_move_scores_negative(fresh_graph):
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"SOL": 98.0})  # -2%

    v = _vote_nodes(fresh_graph)[0]
    assert v["outcome"] == "MISS"
    assert v["score"] == pytest.approx(-1.0)


def test_sell_correct_move_scores_positive(fresh_graph):
    fresh_graph.record_vote("risk_auditor", "SELL", "BTC", 50000.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"BTC": 49000.0})  # -2%, good for a SELL

    v = _vote_nodes(fresh_graph)[0]
    assert v["outcome"] == "HIT"
    assert v["score"] > 0


def test_hold_wins_on_flat_tape(fresh_graph):
    """The key fix: HOLD is rewarded when price stays flat, instead of the old
    rule where any >1.5% move made HOLD a near-guaranteed loss."""
    fresh_graph.record_vote("trader", "HOLD", "ETH", 2000.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"ETH": 2000.0})  # dead flat

    v = _vote_nodes(fresh_graph)[0]
    assert v["outcome"] == "HIT"
    assert v["score"] == pytest.approx(1.0)


def test_hold_loses_on_big_move(fresh_graph):
    fresh_graph.record_vote("trader", "HOLD", "ETH", 2000.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"ETH": 2100.0})  # +5% ~= 2.5 sigma

    v = _vote_nodes(fresh_graph)[0]
    assert v["outcome"] == "MISS"
    assert v["score"] < 0


def test_magnitude_matters(fresh_graph):
    """A bigger correct move earns a higher score than a marginal one."""
    fresh_graph.record_vote("a_small", "BUY", "SOL", 100.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"SOL": 101.0})  # +1% -> ~0.5

    fresh_graph.record_vote("a_big", "BUY", "BTC", 100.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"BTC": 103.0})  # +3% -> clamps to 1.0

    by_agent = {}
    for n, d in fresh_graph.graph.nodes(data=True):
        if d.get("type") == "vote" and "score" in d:
            # map vote -> its agent via the incoming cast_vote edge
            agent = list(fresh_graph.graph.in_edges(n))[0][0]
            by_agent[agent] = d["score"]

    assert by_agent["a_small"] == pytest.approx(0.5)
    assert by_agent["a_big"] == pytest.approx(1.0)
    assert by_agent["a_big"] > by_agent["a_small"]


def test_volatility_scales_score(fresh_graph):
    """The same % move scores lower in a higher-volatility asset."""
    # Default-sigma baseline.
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"SOL": 102.0})  # +2%, default sigma -> 1.0
    default_score = _score_for_asset(fresh_graph, "SOL")

    # High annualized vol widens sigma, so +2% is a smaller fraction of it.
    ann_vol = 1.8718  # chosen so per-horizon sigma ~ 0.04
    fresh_graph.record_vote("trader", "BUY", "BTC", 100.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"BTC": 102.0}, {"BTC": ann_vol})

    vol_score = _score_for_asset(fresh_graph, "BTC")
    expected = min(1.0, 0.02 / _per_horizon_sigma(ann_vol))
    assert vol_score == pytest.approx(expected, abs=0.02)
    assert vol_score < default_score


def test_abstain_is_ignored(fresh_graph):
    fresh_graph.record_vote("trader", "ABSTAIN", "SOL", 100.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"SOL": 110.0})

    v = _vote_nodes(fresh_graph)[0]
    assert v["status"] == "IGNORED"
    assert "score" not in v
    # Ignored votes don't appear in weights.
    assert fresh_graph.get_agent_weights().get("trader", {}) == {}


# --- summary (sample-size guard) ------------------------------------------

def test_reputation_summary_flags_low_n(fresh_graph):
    """A single lucky call must not read as established skill (no '100% (1/1)')."""
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"SOL": 102.0})  # 1 HIT

    summary = fresh_graph.get_reputation_summary()
    assert "low confidence" in summary
    assert "n=1" in summary
    assert "(1/1)" not in summary


def test_reputation_summary_shows_ratio_with_enough_samples(fresh_graph):
    for _ in range(5):
        fresh_graph.record_vote("trader", "BUY", "BTC", 50000.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"BTC": 51000.0})  # 5 HITs

    summary = fresh_graph.get_reputation_summary()
    assert "BTC 100% (5/5)" in summary
    assert "low confidence" not in summary


# --- weights (shrinkage) ---------------------------------------------------

def test_weights_shrink_with_few_samples(fresh_graph):
    # agent1: 1 correct call.
    fresh_graph.record_vote("agent1", "BUY", "BTC", 50000.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"BTC": 51000.0})  # +2% -> score 1.0

    # agent2: 10 correct calls (same move).
    for _ in range(10):
        fresh_graph.record_vote("agent2", "BUY", "BTC", 50000.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"BTC": 51000.0})

    # agent3: 10 wrong calls.
    for _ in range(10):
        fresh_graph.record_vote("agent3", "BUY", "ETH", 2000.0)
    _age_all_pending(fresh_graph)
    fresh_graph.evaluate_pending_votes({"ETH": 1960.0})  # -2% -> score -1.0

    weights = fresh_graph.get_agent_weights()
    w1 = weights["agent1"]["BTC"]
    w2 = weights["agent2"]["BTC"]
    w3 = weights["agent3"]["ETH"]

    # Mean score shrunk toward 0: sum / (n + 4 pseudo-obs).
    assert w1 == pytest.approx(1.0 / 5.0, abs=0.01)    # 0.20
    assert w2 == pytest.approx(10.0 / 14.0, abs=0.01)  # 0.714
    assert w2 > w1                                      # more evidence -> more weight
    assert w3 == pytest.approx(-10.0 / 14.0, abs=0.01)
    assert w3 < 0


def test_legacy_status_still_counts(fresh_graph):
    """Votes graded under the old HIT/MISS scheme (no `score`) still contribute."""
    fresh_graph.record_vote("veteran", "BUY", "BTC", 50000.0)
    node = [n for n, d in fresh_graph.graph.nodes(data=True) if d.get("type") == "vote"][0]
    # Simulate a legacy-resolved node: old status, no continuous score.
    fresh_graph.graph.nodes[node]["status"] = "STRONG_HIT"

    weights = fresh_graph.get_agent_weights()
    assert weights["veteran"]["BTC"] > 0  # legacy STRONG_HIT maps to +1.0, shrunk


def test_vote_regex_tolerates_brackets_and_markdown():
    # Guards the production parser against formatting the model copies from the
    # prompt — "Final Vote: [HOLD] BTC" used to silently drop the vote.
    from bot.meetings import _VOTE_RE
    cases = {
        "Blah. Final Vote:  BUY  sol": ("BUY", "SOL"),
        "Final Vote: [HOLD] BTC": ("HOLD", "BTC"),
        "Final Vote: **SELL** SOL": ("SELL", "SOL"),
        "Final Vote: [BUY] [ETH]": ("BUY", "ETH"),
    }
    for text, (direction, asset) in cases.items():
        m = _VOTE_RE.search(text)
        assert m is not None, text
        assert (m.group(1).upper(), m.group(2).upper()) == (direction, asset), text
