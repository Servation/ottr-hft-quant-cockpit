"""Tests for the belief-revision round's dissent detection (_prep_revision)."""

from bot.meetings import meeting_engine


def test_finds_dissenter_against_majority():
    contribs = {
        "technical_analyst": "report\n\n[DEBATE]: bullish. Final Vote: BUY BTC",
        "sentiment_analyst": "report\n\n[DEBATE]: agree. Final Vote: BUY BTC",
        "risk_auditor": "report\n\n[DEBATE]: too risky here. Final Vote: SELL BTC",
    }
    dissenters, consensus, counter = meeting_engine._prep_revision(contribs)
    assert dissenters == ["risk_auditor"]
    assert "BUY BTC" in consensus
    assert "2 of 3" in consensus
    assert "bullish" in counter  # the consensus-aligned case the dissenter must rebut


def test_skips_when_unanimous():
    contribs = {
        "a": "x\n\n[DEBATE]: Final Vote: BUY BTC",
        "b": "x\n\n[DEBATE]: Final Vote: BUY BTC",
    }
    assert meeting_engine._prep_revision(contribs) == ([], "", "")


def test_skips_on_tie():
    contribs = {
        "a": "x\n\n[DEBATE]: Final Vote: BUY BTC",
        "b": "x\n\n[DEBATE]: Final Vote: SELL BTC",
    }
    assert meeting_engine._prep_revision(contribs) == ([], "", "")


def test_no_votes_returns_empty():
    assert meeting_engine._prep_revision({"a": "no debate segment here"}) == ([], "", "")


def test_abstain_is_not_a_dissent():
    # ABSTAIN doesn't count as a directional vote, so a lone abstainer isn't a dissenter.
    contribs = {
        "technical_analyst": "x\n\n[DEBATE]: Final Vote: BUY BTC",
        "sentiment_analyst": "x\n\n[DEBATE]: Final Vote: BUY BTC",
        "risk_auditor": "x\n\n[DEBATE]: Final Vote: ABSTAIN BTC",
    }
    dissenters, _, _ = meeting_engine._prep_revision(contribs)
    assert dissenters == []  # unanimous among those who took a side
