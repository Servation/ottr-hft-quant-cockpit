import pytest
from datetime import datetime, timedelta, timezone
from bot.knowledge_graph import AgentReputationGraph
import networkx as nx

@pytest.fixture
def fresh_graph(tmp_path):
    # Override GRAPH_PATH for tests
    import bot.knowledge_graph
    original_path = bot.knowledge_graph.GRAPH_PATH
    bot.knowledge_graph.GRAPH_PATH = tmp_path / "test_graph.json"
    
    graph = AgentReputationGraph()
    yield graph
    
    bot.knowledge_graph.GRAPH_PATH = original_path

def test_record_vote(fresh_graph):
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)
    
    assert fresh_graph.graph.has_node("trader")
    assert fresh_graph.graph.has_node("SOL")
    
    votes = [n for n, d in fresh_graph.graph.nodes(data=True) if d.get("type") == "vote"]
    assert len(votes) == 1
    
    vote_data = fresh_graph.graph.nodes[votes[0]]
    assert vote_data["direction"] == "BUY"
    assert vote_data["price_at_vote"] == 100.0
    assert vote_data["status"] == "PENDING"

def test_evaluate_hit_buy(fresh_graph):
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)
    # Price went up 2% (> 1.5%)
    fresh_graph.evaluate_pending_votes({"SOL": 102.0})
    
    votes = [n for n, d in fresh_graph.graph.nodes(data=True) if d.get("type") == "vote"]
    assert fresh_graph.graph.nodes[votes[0]]["status"] == "HIT"

def test_evaluate_miss_buy(fresh_graph):
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)
    # Price went down 2% (< -1.5%)
    fresh_graph.evaluate_pending_votes({"SOL": 98.0})
    
    votes = [n for n, d in fresh_graph.graph.nodes(data=True) if d.get("type") == "vote"]
    assert fresh_graph.graph.nodes[votes[0]]["status"] == "MISS"
    
def test_evaluate_hit_sell(fresh_graph):
    fresh_graph.record_vote("risk_auditor", "SELL", "BTC", 50000.0)
    # Price went down 2% (< -1.5%)
    fresh_graph.evaluate_pending_votes({"BTC": 49000.0})
    
    votes = [n for n, d in fresh_graph.graph.nodes(data=True) if d.get("type") == "vote"]
    assert fresh_graph.graph.nodes[votes[0]]["status"] == "HIT"
    
def test_evaluate_miss_sell(fresh_graph):
    fresh_graph.record_vote("risk_auditor", "SELL", "BTC", 50000.0)
    # Price went up 2% (> 1.5%)
    fresh_graph.evaluate_pending_votes({"BTC": 51000.0})
    
    votes = [n for n, d in fresh_graph.graph.nodes(data=True) if d.get("type") == "vote"]
    assert fresh_graph.graph.nodes[votes[0]]["status"] == "MISS"

def test_time_decay(fresh_graph):
    fresh_graph.record_vote("trader", "BUY", "ETH", 2000.0)
    
    # Manually hack the timestamp to be 1.5 hours ago
    votes = [n for n, d in fresh_graph.graph.nodes(data=True) if d.get("type") == "vote"]
    vote_node = votes[0]
    old_time = datetime.now(timezone.utc) - timedelta(hours=1.5)
    fresh_graph.graph.nodes[vote_node]["timestamp"] = old_time.isoformat()
    
    # Price hasn't moved enough (0% change)
    fresh_graph.evaluate_pending_votes({"ETH": 2000.0})
    
    assert fresh_graph.graph.nodes[vote_node]["status"] == "MISS" # Time decay triggers MISS
    
def test_time_decay_hold(fresh_graph):
    fresh_graph.record_vote("trader", "HOLD", "ETH", 2000.0)
    
    # Manually hack the timestamp to be 1.5 hours ago
    votes = [n for n, d in fresh_graph.graph.nodes(data=True) if d.get("type") == "vote"]
    vote_node = votes[0]
    old_time = datetime.now(timezone.utc) - timedelta(hours=1.5)
    fresh_graph.graph.nodes[vote_node]["timestamp"] = old_time.isoformat()
    
    # Price hasn't moved enough (0% change)
    fresh_graph.evaluate_pending_votes({"ETH": 2000.0})
    
    assert fresh_graph.graph.nodes[vote_node]["status"] == "HIT" # Time decay triggers HIT for HOLD

def test_reputation_summary(fresh_graph):
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)
    fresh_graph.evaluate_pending_votes({"SOL": 102.0}) # HIT
    
    fresh_graph.record_vote("trader", "BUY", "SOL", 100.0)
    fresh_graph.evaluate_pending_votes({"SOL": 98.0}) # MISS
    
    fresh_graph.record_vote("trader", "SELL", "ETH", 2000.0)
    fresh_graph.evaluate_pending_votes({"ETH": 1900.0}) # HIT
    
    summary = fresh_graph.get_reputation_summary()
    assert "- **trader**: SOL: 50% (1/2), ETH: 100% (1/1)" in summary or "- **trader**: ETH: 100% (1/1), SOL: 50% (1/2)" in summary

def test_regex_extraction():
    import re
    text = "Blah blah. Final Vote:  BUY  sol\nNext point."
    match = re.search(r"Final Vote:\s*(BUY|SELL|HOLD|ABSTAIN)\s*([A-Za-z0-9_]+)", text, re.IGNORECASE)
    assert match is not None
    assert match.group(1).upper() == "BUY"
    assert match.group(2).upper() == "SOL"

def test_bayesian_weights(fresh_graph):
    # Agent 1: 1 trade, 1 hit (100% win rate)
    fresh_graph.record_vote("agent1", "BUY", "BTC", 50000.0)
    fresh_graph.evaluate_pending_votes({"BTC": 51000.0}) # HIT

    # Agent 2: 10 trades, 10 hits (100% win rate)
    for i in range(10):
        fresh_graph.record_vote("agent2", "BUY", "BTC", 50000.0)
    fresh_graph.evaluate_pending_votes({"BTC": 51000.0}) # All HIT

    # Agent 3: 10 trades, 0 hits (0% win rate) -> should be negative weight
    for i in range(10):
        fresh_graph.record_vote("agent3", "BUY", "ETH", 2000.0)
    fresh_graph.evaluate_pending_votes({"ETH": 1900.0}) # All MISS
    
    weights = fresh_graph.get_agent_weights()
    
    w1 = weights["agent1"]["BTC"]
    w2 = weights["agent2"]["BTC"]
    w3 = weights["agent3"]["ETH"]
    
    # Bayesian smoothing: (1 + 2.5) / (1 + 5.0) = 3.5 / 6.0 = 0.5833
    # Weight = (0.5833 - 0.5) * 2 = 0.1666
    assert abs(w1 - 0.1666) < 0.01
    
    # Bayesian smoothing: (10 + 2.5) / (10 + 5.0) = 12.5 / 15.0 = 0.8333
    # Weight = (0.8333 - 0.5) * 2 = 0.6666
    assert abs(w2 - 0.6666) < 0.01
    
    # Check that 10 hits is weighted higher than 1 hit
    assert w2 > w1
    
    # Check inverse signals
    # Bayesian smoothing: (0 + 2.5) / (10 + 5.0) = 2.5 / 15.0 = 0.1666
    # Weight = (0.1666 - 0.5) * 2 = -0.6666
    assert w3 < 0
    assert abs(w3 - -0.6666) < 0.01
