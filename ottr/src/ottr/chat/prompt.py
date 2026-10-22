"""
The /ask prompt: desk data from the API, the channel's recent exchanges, the question.

What it does: renders a DeskContext (health, kill switch, book, positions, council
schedule, the latest meeting with every analyst's stance and thesis, recent fills) as
plain text, replays the last few answered questions in the channel (the model CLIs are
stateless, so this is the conversation's only memory), and states what the assistant
can't do: it explains, it never trades.

Why the size cap: a short prompt keeps an answer quick (chat runs at low effort). History
goes first when space runs short, then the data section is cut, so the question always
fits.
"""

from __future__ import annotations

from typing import List

from ottr.chat.client import DeskContext
from ottr.chat.log import Turn
from ottr.chat.render import clip, et, pct, signed_usd, usd
from ottr.health import fmt_age

MAX_PROMPT_CHARS = 24_000
HISTORY_ANSWER_CHARS = 1_200   # an old answer is context, not the point: keep it short

INSTRUCTIONS = """You are the desk assistant for OTTR, a US-markets PAPER-trading desk: ETFs across \
stocks, bonds, commodities and real estate, plus a few single stocks. A council of three models (Claude, \
GPT, Gemini) meets at fixed New York times on trading days and proposes trades; deterministic code sizes, \
risk-checks and paper-fills them during market hours. You answer the desk owner's questions in Discord.

What you can and can't do:
- You only explain. You cannot place, change or cancel trades, change settings or touch the kill \
switch, and nothing you write is executed. If asked to trade, say so plainly: trades only come from \
council meetings. The owner's own commands: /kill stops all fills, /release turns them back on.
- Base answers on the DESK DATA below. You may add general market knowledge, labeled as such. If the \
data doesn't answer the question, say what is missing. Never invent prices, fills or positions.
- The data is a snapshot taken {now}. Earlier exchanges may quote older numbers; the DESK DATA wins.

Style: Discord markdown, short paragraphs or bullets, no tables, under about 250 words unless the \
owner asks for detail. Money as $1,234.56, times in New York time (ET). Never use em dashes (the long \
dash); use commas, colons or parentheses instead."""


def _health(ctx: DeskContext, now: float) -> List[str]:
    h = ctx.health
    if not h:
        return [f"HEALTH: unavailable ({ctx.errors.get('health', 'no answer')})"]
    verdict = {0: "OK", 1: "DEGRADED (an alarm is active)", 2: "LEDGER PROBLEM", 3: "ENGINE DOWN"}
    lines = [f"HEALTH: {verdict.get(h.get('exit_code'), 'unknown')}"]
    for a in h.get("alarms") or []:
        lines.append(f"  alarm [{a.get('severity')}]: {clip(a.get('message'), 200)}")
    return lines


def _kill(ctx: DeskContext) -> str:
    k = (ctx.book or {}).get("kill_switch") or (ctx.health or {}).get("kill_switch")
    if not k:
        return "KILL SWITCH: unknown"
    if not k.get("effective_dry_run"):
        return "KILL SWITCH: off (paper fills enabled)"
    # Two independent sources; name each, since only one of them can be released from chat.
    parts = []
    if k.get("operator_engaged"):
        why = f" (reason: {clip(k.get('reason'), 150)})" if k.get("reason") else ""
        parts.append(f"operator switch engaged by {k.get('by')} at {et(k.get('since'))}{why}; the owner "
                     "can release it with /release")
    if k.get("config_dry_run"):
        parts.append("config or OTTR_DRY_RUN forces dry run on the host; chat can't change that")
    return f"KILL SWITCH: ON. {'; '.join(parts)}. Nothing fills; council verdicts are recorded as dry run."


def _book(ctx: DeskContext, now: float) -> List[str]:
    b = ctx.book
    if not b:
        return [f"BOOK: unavailable ({ctx.errors.get('book', 'no answer')})"]
    eq = b.get("equity")
    cash = float(b["cash"])
    if eq:
        lines = [f"BOOK: equity {usd(eq)} | cash {usd(cash)} ({pct(cash / eq)}) | invested "
                 f"{pct(b.get('invested_pct'))} | unrealized P&L {signed_usd(b.get('unrealized_pnl'))} | "
                 f"realized P&L {signed_usd(b.get('realized_pnl'))} | fees paid {usd(b.get('fees'))}"]
    else:
        lines = [f"BOOK: equity unknown (no price: {b.get('prices_error')}) | cash {usd(cash)} | "
                 f"realized P&L {signed_usd(b.get('realized_pnl'))}"]
    r = b.get("risk") or {}
    risk = f"HALTED since {et(r.get('halted_since'))} (BUYs blocked)" if r.get("halted") else "normal"
    if r.get("drawdown") is not None:
        risk += f" | drawdown {pct(r['drawdown'])} from peak {usd(r.get('peak_equity'))}"
    cools = sorted(a for a, until in (r.get("cooldowns") or {}).items() if until and until > now)
    if cools:
        risk += f" | in cooldown (no new BUYs): {', '.join(cools)}"
    lines.append(f"RISK: {risk}")
    bm = b.get("benchmarks") or {}
    if bm.get("spy_hold") is not None or bm.get("sixty_forty") is not None:
        lines.append(f"BENCHMARKS (same starting equity, dividends reinvested): SPY buy-and-hold "
                     f"{usd(bm.get('spy_hold'))} | 60/40 SPY/IEF rebalanced monthly {usd(bm.get('sixty_forty'))}")
    m = b.get("market")
    if m:
        s = m.get("session") or {}
        lines.append(f"MARKET: open until {et(s.get('close_ts'))}" if m.get("open")
                     else f"MARKET: closed; next open {et(m.get('next_open_ts'))}")
    return lines


def _positions(ctx: DeskContext) -> List[str]:
    p = ctx.positions
    if not p:
        return [f"POSITIONS: unavailable ({ctx.errors.get('positions', 'no answer')})"]
    rows = p.get("positions") or []
    if not rows:
        return ["POSITIONS: none (all cash)"]
    lines = ["POSITIONS (priced live in session, else at a delayed print or the last close; the basis is "
             "given per row):"]
    for r in rows:
        stop = (f"trailing stop {r['trailing_stop']:,.2f} ({pct(r.get('stop_distance_pct'))} below the price)"
                if r.get("trailing_stop") is not None else "no stop (swept cash)")
        cap = "uncapped" if r.get("cap_pct") is None else f"cap {r['cap_pct']:g}%"
        lines.append(f"  {r['asset']} ({r.get('class') or 'not a member'}): qty {float(r['qty']):.6g} | value "
                     f"{usd(r.get('value'))} ({pct(r.get('weight'))} of book, {cap}) | price "
                     f"{usd(r.get('bid'))} {r.get('price_basis') or ''} | avg cost {usd(r.get('avg_cost'))} | "
                     f"unrealized {signed_usd(r.get('unrealized_pnl'))} ({pct(r.get('unrealized_pct'), signed=True)}) "
                     f"| {stop}" + (" | RISK MONITOR BLIND (no live quote)" if r.get("risk_blind_since") else ""))
    return lines


def _council(ctx: DeskContext, now: float) -> str:
    c = ctx.council
    if not c:
        return f"COUNCIL: schedule unavailable ({ctx.errors.get('council', 'no answer')})"
    nxt = c.get("next_meeting_at")
    when = f"next meeting {et(nxt)} (in {fmt_age(nxt - now)})" if nxt and nxt > now else "next meeting due now"
    times = " and ".join(c.get("meeting_times_et") or []) or "the scheduled times"
    return f"COUNCIL: meets at {times} ET on trading days | {when}"


def _meeting(ctx: DeskContext, now: float) -> List[str]:
    m = ctx.meeting
    if not m:
        why = ctx.errors.get("meeting")
        return [f"LATEST COUNCIL MEETING: unavailable ({why})" if why else "LATEST COUNCIL MEETING: none yet"]
    lines = [f"LATEST COUNCIL MEETING {m.get('id')} at {et(m.get('ts'))} ({fmt_age(now - m['ts'])} ago), "
             f"status {m.get('status')}:"]
    if m.get("summary"):
        lines.append(f"  Chair summary: {clip(m['summary'], 800)}")
    if m.get("dissent_notes"):
        lines.append(f"  Chair's dissent notes: {clip(m['dissent_notes'], 500)}")
    orders = {o.get("asset"): o for o in m.get("orders") or []}
    for t in m.get("trades") or []:
        size = (f"conviction {float(t.get('conviction') or 0):.2f}" if t.get("action") == "BUY"
                else f"{float(t.get('fraction_of_position') or 0) * 100:.0f}% of the position")
        o = orders.get(t.get("asset")) or {}
        res = f"; result: {o.get('status')} {usd(o.get('notional')) if o.get('notional') else ''}".rstrip() \
            if o else ""
        if o.get("reason") and o.get("status") != "filled":
            res += f" ({clip(o['reason'], 150)})"
        lines.append(f"  Decided: {t.get('action')} {t.get('asset')} ({size}): {clip(t.get('reasoning'), 300)}{res}")
    if not m.get("trades"):
        lines.append("  Decided: no trades")
    for x in m.get("dropped") or []:
        tr = x.get("trade") or {}
        lines.append(f"  Dropped by code: {tr.get('action')} {tr.get('asset')}: {clip(x.get('why'), 200)}")
    for name, err in (m.get("errors") or {}).items():
        lines.append(f"  Analyst {name} dropped out: {clip(err, 200)}")
    for name, a in (m.get("analysts") or {}).items():
        lines.append(f"  Analyst {name} (shown to the chair as {a.get('label')}): {clip(a.get('overall_view'), 500)}")
        for s in a.get("stances") or []:
            lines.append(f"    {s['asset']} {s['stance']} conviction {float(s['conviction']):.2f}: "
                         f"{clip(s.get('thesis'), 300)} Key risk: {clip(s.get('key_risk'), 200)}")
    return lines


def _fills(ctx: DeskContext) -> List[str]:
    f = ctx.fills
    if not f:
        return [f"RECENT FILLS: unavailable ({ctx.errors.get('fills', 'no answer')})"]
    rows = f.get("fills") or []
    if not rows:
        return ["RECENT FILLS: none"]
    lines = ["RECENT FILLS (newest first):"]
    for r in rows:
        pnl = f", realized {signed_usd(r.get('realized_pnl'))}" if r.get("side") == "SELL" else ""
        lines.append(f"  {et(r['ts'])} {r['side']} {r['asset']} {usd(r.get('notional'))} at "
                     f"{float(r['price']):,.6g} ({r.get('origin')}{pnl}): {clip(r.get('reason'), 160)}")
    return lines


def desk_data(ctx: DeskContext, now: float) -> str:
    lines: List[str] = []
    lines += _health(ctx, now)
    lines.append(_kill(ctx))
    lines += _book(ctx, now)
    lines += _positions(ctx)
    lines.append(_council(ctx, now))
    lines += _meeting(ctx, now)
    lines += _fills(ctx)
    return "\n".join(lines)


def _turn(t: Turn, now: float) -> str:
    return (f"[{et(t.ts)}, {fmt_age(now - t.ts)} ago] {t.user_name} asked: {clip(t.question, 600)}\n"
            f"You answered: {clip(t.answer, HISTORY_ANSWER_CHARS)}")


def build(ctx: DeskContext, history: List[Turn], question: str, asker: str, now: float,
          max_chars: int = MAX_PROMPT_CHARS) -> str:
    head = INSTRUCTIONS.format(now=et(now))
    tail = f"Question from {asker} (now): {question.strip()}"
    data = desk_data(ctx, now)
    room = max_chars - len(head) - len(tail) - 200  # 200: section markers and blank lines
    if len(data) > room:
        data = data[:max(0, room - 40)].rstrip() + "\n(desk data cut to fit)"
    room -= len(data)
    turns: List[str] = []
    for t in reversed(history):  # newest first, so the oldest are what get left out
        block = _turn(t, now)
        if len(block) + 2 > room:
            break
        turns.insert(0, block)
        room -= len(block) + 2
    parts = [head, f"=== DESK DATA ({et(now)}) ===\n{data}\n=== END DESK DATA ==="]
    if turns:
        parts.append("=== EARLIER IN THIS CHANNEL (oldest first) ===\n" + "\n\n".join(turns)
                     + "\n=== END EARLIER ===")
    parts.append(tail)
    return "\n\n".join(parts)
