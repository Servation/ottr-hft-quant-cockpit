"""
Knowledge Graph extension for tracking agent predictions and computing reputation.
Backed by networkx and a local JSON file.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict
from uuid import uuid4

import networkx as nx

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
GRAPH_PATH = DATA_DIR / "agent_reputation_graph.json"

THRESHOLD_PCT = 1.5
HORIZON_HOURS = 1.0

class AgentReputationGraph:
    def __init__(self):
        self.graph = nx.MultiDiGraph()
        self.load()

    def load(self):
        if GRAPH_PATH.exists():
            try:
                data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
                self.graph = nx.node_link_graph(data)
                logger.info("Loaded reputation graph.")
            except Exception as e:
                logger.warning(f"Failed to load graph, starting fresh: {e}")
                self.graph = nx.MultiDiGraph()
        else:
            self.graph = nx.MultiDiGraph()

    def save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            data = nx.node_link_data(self.graph)
            GRAPH_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to save reputation graph: {e}")

    def record_vote(self, agent_id: str, vote_direction: str, asset: str, current_price: float):
        vote_id = f"vote_{uuid4().hex[:8]}"
        asset = asset.upper()
        
        if not self.graph.has_node(agent_id):
            self.graph.add_node(agent_id, type="agent")
        if not self.graph.has_node(asset):
            self.graph.add_node(asset, type="asset")
            
        ts = datetime.now(timezone.utc).isoformat()
        self.graph.add_node(
            vote_id, 
            type="vote",
            direction=vote_direction.upper(),
            price_at_vote=current_price,
            timestamp=ts,
            status="PENDING"
        )
        
        self.graph.add_edge(agent_id, vote_id, relation="cast_vote")
        self.graph.add_edge(vote_id, asset, relation="voted_on")
        self.save()
        logger.info(f"Recorded vote: {agent_id} -> {vote_direction} {asset} @ ${current_price}")

    def evaluate_pending_votes(self, current_prices: Dict[str, float]):
        """
        current_prices maps asset symbol (e.g. 'SOL') to its current price (e.g. 140.5)
        """
        now = datetime.now(timezone.utc)
        changed = False
        
        for node, data in self.graph.nodes(data=True):
            if data.get("type") != "vote" or data.get("status") != "PENDING":
                continue
                
            edges_out = list(self.graph.out_edges(node, data=True))
            if not edges_out: continue
            asset = edges_out[0][1]
            
            current_price = current_prices.get(asset.upper())
            if current_price is None:
                continue
                
            vote_price = data.get("price_at_vote")
            direction = data.get("direction")
            ts_str = data.get("timestamp")
            try:
                vote_time = datetime.fromisoformat(ts_str)
            except ValueError:
                self.graph.nodes[node]["status"] = "ERROR"
                changed = True
                continue
            
            if vote_price == 0: continue
            pct_change = ((current_price - vote_price) / vote_price) * 100.0
            hours_elapsed = (now - vote_time).total_seconds() / 3600.0
            
            status = "PENDING"
            if direction == "BUY":
                if pct_change >= THRESHOLD_PCT:
                    status = "HIT"
                elif pct_change <= -THRESHOLD_PCT:
                    status = "MISS"
            elif direction == "SELL":
                if pct_change <= -THRESHOLD_PCT:
                    status = "HIT"
                elif pct_change >= THRESHOLD_PCT:
                    status = "MISS"
            elif direction == "HOLD":
                if abs(pct_change) >= THRESHOLD_PCT:
                    status = "MISS"
            elif direction == "ABSTAIN":
                status = "IGNORED"
                
            if status == "PENDING" and hours_elapsed >= HORIZON_HOURS:
                if direction == "HOLD":
                    status = "HIT"
                else:
                    status = "MISS"
                    
            if status != "PENDING":
                self.graph.nodes[node]["status"] = status
                self.graph.nodes[node]["resolved_at"] = now.isoformat()
                changed = True
                logger.info(f"Resolved vote {node} as {status}. Pct change: {pct_change:.2f}%")
                
        if changed:
            self.save()

    def get_reputation_summary(self) -> str:
        agent_stats = {}
        for node, data in self.graph.nodes(data=True):
            if data.get("type") == "agent":
                agent_stats[node] = {}
        
        for u, v, edgedata in self.graph.edges(data=True):
            if edgedata.get("relation") == "cast_vote":
                agent_id = u
                vote_id = v
                vote_data = self.graph.nodes[vote_id]
                status = vote_data.get("status")
                
                if status in ("HIT", "MISS"):
                    asset_edges = list(self.graph.out_edges(vote_id))
                    if not asset_edges: continue
                    asset = asset_edges[0][1]
                    
                    if asset not in agent_stats[agent_id]:
                        agent_stats[agent_id][asset] = {"hits": 0, "misses": 0}
                        
                    if status == "HIT":
                        agent_stats[agent_id][asset]["hits"] += 1
                    else:
                        agent_stats[agent_id][asset]["misses"] += 1
                        
        lines = []
        for agent, assets in agent_stats.items():
            if not assets:
                continue
                
            asset_strs = []
            for asset, counts in assets.items():
                total = counts["hits"] + counts["misses"]
                if total > 0:
                    win_rate = (counts["hits"] / total) * 100
                    asset_strs.append(f"{asset}: {win_rate:.0f}% ({counts['hits']}/{total})")
            
            if asset_strs:
                lines.append(f"- **{agent}**: " + ", ".join(asset_strs))
                
        if not lines:
            return "No historical predictions recorded yet."
            
        return "\n".join(lines)

    def get_agent_weights(self) -> dict:
        """
        Returns a dict mapping agent_id -> asset -> weight (from -1.0 to 1.0)
        Using Bayesian Smoothing: pseudo_wins = 2.5, pseudo_trades = 5.0
        """
        agent_stats = {}
        for node, data in self.graph.nodes(data=True):
            if data.get("type") == "agent":
                agent_stats[node] = {}
                
        for u, v, edgedata in self.graph.edges(data=True):
            if edgedata.get("relation") == "cast_vote":
                agent_id = u
                vote_id = v
                vote_data = self.graph.nodes[vote_id]
                status = vote_data.get("status")
                
                if status in ("HIT", "MISS"):
                    asset_edges = list(self.graph.out_edges(vote_id))
                    if not asset_edges: continue
                    asset = asset_edges[0][1]
                    
                    if asset not in agent_stats[agent_id]:
                        agent_stats[agent_id][asset] = {"hits": 0, "total": 0}
                        
                    agent_stats[agent_id][asset]["total"] += 1
                    if status == "HIT":
                        agent_stats[agent_id][asset]["hits"] += 1
                        
        weights = {}
        for agent, assets in agent_stats.items():
            weights[agent] = {}
            for asset, counts in assets.items():
                hits = counts["hits"]
                total = counts["total"]
                
                # Bayesian Smoothing
                pseudo_wins = 2.5
                pseudo_trades = 5.0
                
                smoothed_win_rate = (hits + pseudo_wins) / (total + pseudo_trades)
                
                # Z-Score Mapping [-1.0 to 1.0]
                weight = (smoothed_win_rate - 0.5) * 2.0
                weights[agent][asset] = weight
                
        return weights

reputation_graph = AgentReputationGraph()
