"""
Knowledge Graph extension for tracking agent predictions and computing reputation.
Backed by networkx and a local JSON file.
"""

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from uuid import uuid4

import networkx as nx

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
GRAPH_PATH = DATA_DIR / "agent_reputation_graph.json"

# Votes resolve ONCE, at this mark, using the price then — no early-stop at the
# first cron tick that crosses a threshold (which biased the old grader toward
# short-term noise). 4h matches the meeting cadence so reputation updates each cycle.
RESOLVE_HORIZON_HOURS = 4.0
_HOURS_PER_YEAR = 365.0 * 24.0
# Fallback expected move over the horizon when an asset's volatility is unknown.
_DEFAULT_SIGMA = 0.02
# Below this many resolved votes, a win-rate is flagged low-confidence rather
# than shown as if it were established skill (no more "100% (1/1)").
_MIN_SAMPLES = 5
# Pseudo-observations that shrink an agent's weight toward neutral (0) when it
# has few resolved votes (Bayesian-style regularization on the mean score).
_PSEUDO_OBS = 4.0

# Legacy votes (graded under the old STRONG/WEAK HIT/MISS scheme, no `score`
# field) are mapped to a coarse score so historical data still contributes.
_LEGACY_SCORE = {
    "STRONG_HIT": 1.0, "WEAK_HIT": 0.5, "STRONG_MISS": -1.0, "WEAK_MISS": -0.5,
}


def _per_horizon_sigma(annualized_vol: Optional[float]) -> float:
    """Expected fractional move over the resolution horizon, from annualized vol."""
    if not annualized_vol or annualized_vol <= 0:
        return _DEFAULT_SIGMA
    return annualized_vol * math.sqrt(RESOLVE_HORIZON_HOURS / _HOURS_PER_YEAR)


def _score_vote(direction: str, price_at_vote: float, price_now: float, sigma: float) -> Optional[float]:
    """Continuous, volatility-scaled vote score in [-1, 1].

    The return is normalized by the asset's expected move (sigma), so magnitude
    matters and a 2% call in a calm asset counts more than 2% in a wild one:
    - BUY  scores +return/sigma, SELL scores -return/sigma (a ~1-sigma move the
      right way ≈ +1, the wrong way ≈ -1).
    - HOLD scores 1 - |return|/sigma, so a genuinely flat tape is rewarded (≈ +1)
      instead of being a near-guaranteed loss as under the old ±1.5% rule.
    ABSTAIN / unknown directions are not scored (return None).
    """
    if price_at_vote <= 0 or sigma <= 0:
        return None
    ret = (price_now - price_at_vote) / price_at_vote
    d = direction.upper()
    if d == "BUY":
        raw = ret / sigma
    elif d == "SELL":
        raw = -ret / sigma
    elif d == "HOLD":
        raw = 1.0 - abs(ret) / sigma
    else:
        return None
    return max(-1.0, min(1.0, raw))


def _node_score(vote_data: dict) -> Optional[float]:
    """Resolved score for a vote node: the new continuous score, or a legacy map."""
    score = vote_data.get("score")
    if score is not None:
        return float(score)
    return _LEGACY_SCORE.get(vote_data.get("status"))


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

    def evaluate_pending_votes(self, current_prices: Dict[str, float], volatility: Optional[Dict[str, float]] = None):
        """Grade votes that have reached the resolution horizon.

        current_prices: asset symbol (e.g. 'SOL') -> current price.
        volatility: optional asset symbol -> annualized volatility, used to
            vol-scale the score; falls back to a default expected move if absent.

        Each vote is graded exactly once, when it reaches RESOLVE_HORIZON_HOURS,
        with a continuous score in [-1, 1] (see _score_vote). No early resolution.
        """
        now = datetime.now(timezone.utc)
        vol = volatility or {}
        changed = False

        for node, data in self.graph.nodes(data=True):
            if data.get("type") != "vote" or data.get("status") != "PENDING":
                continue

            edges_out = list(self.graph.out_edges(node, data=True))
            if not edges_out:
                continue
            asset = edges_out[0][1]

            price_now = current_prices.get(asset.upper())
            if price_now is None:
                continue

            vote_price = data.get("price_at_vote")
            direction = data.get("direction")
            ts_str = data.get("timestamp")
            try:
                vote_time = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                self.graph.nodes[node]["status"] = "ERROR"
                changed = True
                continue

            if not vote_price or vote_price <= 0:
                continue

            hours_elapsed = (now - vote_time).total_seconds() / 3600.0
            if hours_elapsed < RESOLVE_HORIZON_HOURS:
                continue  # not yet at the horizon — leave PENDING, resolve once later

            sigma = _per_horizon_sigma(vol.get(asset.upper()))
            score = _score_vote(direction, vote_price, price_now, sigma)
            node_attrs = self.graph.nodes[node]
            node_attrs["resolved_at"] = now.isoformat()

            if score is None:
                # ABSTAIN / unscorable — resolved but no reputation impact.
                node_attrs["status"] = "IGNORED"
                changed = True
                continue

            ret = (price_now - vote_price) / vote_price
            node_attrs["status"] = "RESOLVED"
            node_attrs["score"] = score
            node_attrs["observed_return"] = ret
            node_attrs["sigma"] = sigma
            node_attrs["outcome"] = "HIT" if score > 0 else "MISS"
            changed = True
            logger.info(
                "Resolved vote %s: %s %s score=%.2f (return=%.2f%%, sigma=%.2f%%)",
                node, direction, asset, score, ret * 100.0, sigma * 100.0,
            )

        if changed:
            self.save()

    def _collect_scores(self) -> dict:
        """agent_id -> asset -> list of resolved vote scores (new + legacy)."""
        agent_stats = {
            node: {} for node, data in self.graph.nodes(data=True)
            if data.get("type") == "agent"
        }
        for u, v, edgedata in self.graph.edges(data=True):
            if edgedata.get("relation") != "cast_vote":
                continue
            score = _node_score(self.graph.nodes[v])
            if score is None:
                continue
            asset_edges = list(self.graph.out_edges(v))
            if not asset_edges:
                continue
            asset = asset_edges[0][1]
            agent_stats.setdefault(u, {}).setdefault(asset, []).append(score)
        return agent_stats

    def get_reputation_summary(self) -> str:
        """Human-readable per-agent win rate. A 'win' is any positively-scored
        call (correct direction for BUY/SELL, stayed flat for HOLD). Small
        samples are flagged so a lucky 1/1 doesn't read as established skill."""
        agent_stats = self._collect_scores()

        lines = []
        for agent, assets in agent_stats.items():
            asset_strs = []
            for asset, scores in assets.items():
                n = len(scores)
                if n == 0:
                    continue
                wins = sum(1 for s in scores if s > 0)
                win_rate = round(100 * wins / n)
                if n < _MIN_SAMPLES:
                    asset_strs.append(f"{asset} {win_rate}% (n={n}, low confidence)")
                else:
                    asset_strs.append(f"{asset} {win_rate}% ({wins}/{n})")
            if asset_strs:
                lines.append(f"- **{agent}** — " + " · ".join(asset_strs))

        if not lines:
            return "No historical predictions recorded yet."
        header = "_Win rate = share of calls scored correct; low-sample agents flagged._\n"
        return header + "\n".join(lines)

    def get_agent_weights(self) -> dict:
        """Returns agent_id -> asset -> weight in [-1.0, 1.0].

        The weight is the agent's mean vote score, shrunk toward neutral (0) by
        _PSEUDO_OBS pseudo-observations so an agent with few resolved votes can't
        swing the consensus. Magnitude-aware: a big correct call lifts the weight
        more than a marginal one (unlike the old binary hit/miss tally).
        """
        agent_stats = self._collect_scores()
        weights = {}
        for agent, assets in agent_stats.items():
            weights[agent] = {}
            for asset, scores in assets.items():
                # Mean score shrunk toward 0: sum / (n + pseudo). Few votes -> ~0.
                w = sum(scores) / (len(scores) + _PSEUDO_OBS)
                weights[agent][asset] = max(-1.0, min(1.0, w))
        return weights

reputation_graph = AgentReputationGraph()
