"""
PR C — full-universe coverage in meetings.

Two guarantees:
1. The agent context presents the WATCHLIST (the full tradeable universe) distinctly
   from holdings, with an explicit instruction to take a stance on each member.
2. The closing "Net Asset Scores" table spans every watchlist asset, so an asset no
   agent voted on is an explicit HOLD/NEUTRAL — never a silent omission.
"""

import pytest
from unittest.mock import AsyncMock

from bot.meetings import MeetingEngine, MEETING_TYPES
from bot.universe import tradeable_universe


@pytest.fixture
def engine():
    return MeetingEngine()


def test_agent_context_shows_watchlist_and_demands_a_stance(engine):
    msgs = engine._build_agent_context(
        agent_id="technical_analyst",
        meeting_type=MEETING_TYPES["strategy_session"],
        conversation_log=[],
        price_data="BTC: $60,000",
        portfolio_summary="**Cash:** $8,000\n**Holdings (owned):**\n  - BTC: 0.1",
        ceo_directives="",
        memory_context="",
    )
    blob = "\n".join(m["content"] for m in msgs)
    assert "Watchlist (analyzed + tradeable)" in blob
    # Every universe member is named, and the holdings block is labelled as owned-only.
    for sym in tradeable_universe():
        assert sym in blob
    assert "Holdings (owned)" in blob
    # The instruction must push a per-asset stance, not "prioritize current holdings".
    assert "stance" in blob.lower()
    assert "prioritize our active positions first" not in blob


@pytest.mark.asyncio
async def test_closing_scores_span_the_full_universe(engine, mocker):
    """Agents vote only BTC + ETH; the Net Asset Scores table must still list all 8
    watchlist assets, the un-voted ones as 0B/0S/0H."""
    mocker.patch(
        "bot.meetings.agent_llm.generate_response",
        new=AsyncMock(return_value=("closing summary", None)),
    )
    posted = {}

    async def capture(agent, msg):
        posted.setdefault(agent, []).append(msg)

    contributions = {
        "technical_analyst": "Trend is up. [DEBATE]: I'm constructive. Final Vote: BUY BTC",
        "trader": "Momentum fading. [DEBATE]: I'd lighten. Final Vote: SELL ETH",
    }

    await engine._build_closing(
        MEETING_TYPES["strategy_session"],
        conversation_log=["discussion"],
        price_data="prices",
        portfolio_summary="portfolio",
        agent_contributions=contributions,
        post_message_fn=capture,
    )

    app_msgs = "\n".join(posted.get("APP", []))
    assert "Net Asset Scores" in app_msgs
    scores_section = app_msgs.split("Net Asset Scores", 1)[1]
    # Voted assets present…
    assert "BTC" in scores_section and "ETH" in scores_section
    # …and so is every other watchlist asset (no silent skips).
    for sym in tradeable_universe():
        assert sym in scores_section, f"{sym} missing from the consensus table"
    # An un-voted asset is an explicit neutral tally, not absent.
    assert "0B/0S/0H" in scores_section
