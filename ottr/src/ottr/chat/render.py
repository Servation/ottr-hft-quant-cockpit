"""
Discord text for the chat bot: plain-code views of API data, and message splitting.

What it does: turns /api/health, /book, /council, /positions and /meetings replies into
short Discord markdown (Discord's <t:...> timestamps render in the reader's own time
zone), and splits long text into messages under Discord's 2000-character limit at
paragraph, line or sentence boundaries, re-opening a code block that a split cuts through.

Why plain code: /status, /positions and /meeting must work when every model CLI is
signed out or the hourly budget is spent, and they must say exactly what the API says.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from ottr.chat.client import ApiError, ApiUnreachable
from ottr.market.calendar import ET
from ottr.notify import clip, no_em_dash

DISCORD_LIMIT = 2000
MAX_CHUNKS = 4            # a reply longer than ~8000 characters is cut, with a note
VERDICTS = {0: "OK", 1: "DEGRADED", 2: "LEDGER PROBLEM", 3: "ENGINE DOWN"}
# How /status words an analyst's state in a running meeting, in this order.
ANALYST_STATES = (("done", "done"), ("dropped", "dropped out"), ("thinking", "thinking"),
                  ("waiting", "waiting"), ("stopped", "stopped"))


# -- small formatters ----------------------------------------------------------------------
def usd(x: Any) -> str:
    return "n/a" if x is None else f"${float(x):,.2f}"


def signed_usd(x: Any) -> str:
    if x is None:
        return "n/a"
    v = float(x)
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def pct(x: Optional[float], signed: bool = False) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.1f}%" if signed else f"{x * 100:.1f}%"


def when(ts: Optional[float], style: str = "R") -> str:
    """A Discord timestamp: R = relative ("in 2 hours"), f = date and time."""
    return "n/a" if ts is None else f"<t:{int(ts)}:{style}>"


def et(ts: Optional[float]) -> str:
    """New York time, the desk's clock: "Wed 2026-10-21 15:15 ET"."""
    return "n/a" if ts is None else datetime.fromtimestamp(ts, tz=ET).strftime("%a %Y-%m-%d %H:%M ET")


def market_line(m: Optional[dict]) -> str:
    """The session from /api/book's `market` (None from an engine without a calendar)."""
    if not m:
        return "Market: unknown"
    if m.get("open"):
        s = m.get("session") or {}
        early = " (early close)" if s.get("early_close") else ""
        return f"Market: **open**, closes {when(s.get('close_ts'))}{early}"
    return f"Market: closed, opens {when(m.get('next_open_ts'))}"


def names(items: List[str]) -> str:
    """claude / claude and gpt / claude, gpt and gemini"""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


# -- views ---------------------------------------------------------------------------------
def api_error(e: ApiError) -> str:
    if isinstance(e, ApiUnreachable):
        return (f"Can't reach the OTTR engine: {e.message}. It may be stopped or restarting; "
                "`ottr status` on the host says which.")
    if e.status == 401:
        return (f"The engine refused the request ({e.message}). OTTR_API_KEY in ottr/.env must be "
                "the same for the engine and the chat bot.")
    if e.status == 404:
        return (f"The running engine doesn't know this request ({e.message}): it is older than the chat "
                "bot. Restart it to load the new code (`ottr service restart`).")
    return f"The engine answered with an error ({e.status}): {e.message}"


def kill_line(k: Optional[dict]) -> str:
    if not k:
        return "kill switch: unknown"
    if not k.get("effective_dry_run"):
        return "kill switch off, paper fills on"
    bits = []
    if k.get("operator_engaged"):
        why = f": {clip(k['reason'], 120)}" if k.get("reason") else ""
        bits.append(f"**KILL SWITCH ENGAGED** by {k.get('by') or '?'} {when(k.get('since'))}{why}")
    if k.get("config_dry_run"):
        bits.append("**DRY RUN** forced by config or OTTR_DRY_RUN")
    return f"{'; '.join(bits)}. Nothing fills"


def meeting_progress(p: Optional[dict]) -> str:
    """How far a running meeting has got, e.g. "claude and gpt done, gemini thinking".

    Empty when the engine sent no progress (an engine older than the bot)."""
    if not p:
        return ""
    if p.get("phase") == "market":
        return "reading the market"
    by_state: Dict[str, List[str]] = {}
    for a in p.get("analysts") or []:
        by_state.setdefault(str(a.get("status")), []).append(str(a.get("name")))
    parts = [f"{names(by_state[s])} {word}" for s, word in ANALYST_STATES if by_state.get(s)]
    c = p.get("chair") or {}
    chair = {"thinking": "chair deliberating", "done": "chair decided",
             "failed": "chair failed"}.get(c.get("status"))
    if chair and c.get("replaced"):
        chair += f" ({c.get('name')} standing in for {(c['replaced'] or {}).get('name')})"
    return ", ".join(parts + ([chair] if chair else []))


def council_line(c: Optional[dict]) -> str:
    if not c:
        return "Council: unknown"
    if c.get("running"):
        where = meeting_progress(c.get("progress"))
        return f"Council: **in session** (started {when(c.get('started_at'))})" + (f": {where}" if where else "")
    last = c.get("last_meeting")
    tail = f" | last {when(last['ts'])} ({last['status']})" if last else " | no meeting yet"
    line = f"Council: next meeting {when(c.get('next_meeting_at'))}{tail}"
    w = c.get("watch") or {}
    if w.get("enabled"):
        conds = [x["condition"] for x in w.get("conditions") or []]
        line += (f"\nWatcher: {'watching ' + ', '.join(conds) if conds else 'no conditions set'}; "
                 f"{w.get('called_today', 0)} of {w.get('max_called_per_day')} called meetings used today")
    return line


def status(health: Optional[dict], book: Optional[dict], council: Optional[dict],
           errors: Dict[str, str], now: float) -> str:
    verdict = VERDICTS.get(health.get("exit_code"), "UNKNOWN") if health else "UNKNOWN"
    kill = (book or {}).get("kill_switch") or (health or {}).get("kill_switch")
    lines = [f"**OTTR {verdict}** | {kill_line(kill)}"]
    if book and "market" in book:
        lines.append(market_line(book.get("market")))
    if book:
        eq, cash = book.get("equity"), float(book["cash"])
        if eq:
            lines.append(f"Equity **{usd(eq)}** | cash {usd(cash)} ({pct(cash / eq)}) | "
                         f"invested {pct(book.get('invested_pct'))}")
            lines.append(f"P&L: unrealized {signed_usd(book.get('unrealized_pnl'))} | realized "
                         f"{signed_usd(book.get('realized_pnl'))} | fees {usd(book.get('fees'))}")
        else:
            lines.append(f"Equity unknown (no price: {book.get('prices_error') or 'a holding is unpriced'}) | "
                         f"cash {usd(cash)}")
            lines.append(f"P&L: realized {signed_usd(book.get('realized_pnl'))} | fees {usd(book.get('fees'))}")
        b = book.get("benchmarks") or {}
        if b.get("spy_hold") is not None or b.get("sixty_forty") is not None:
            lines.append(f"Benchmarks from day 0: SPY hold {usd(b.get('spy_hold'))} | 60/40 "
                         f"{usd(b.get('sixty_forty'))}")
        r = book.get("risk") or {}
        state = f"**HALTED** {when(r.get('halted_since'))} (BUYs blocked)" if r.get("halted") else "normal"
        dd = f" | drawdown {pct(r['drawdown'])} from peak {usd(r.get('peak_equity'))}" \
            if r.get("drawdown") is not None else ""
        cools = sorted(a for a, until in (r.get("cooldowns") or {}).items() if until and until > now)
        lines.append(f"Risk: {state}{dd}" + (f" | cooling down: {', '.join(cools)}" if cools else ""))
        lines.append(f"Positions: {book.get('positions', 0)} (`/positions` for detail)")
    elif "book" in errors:
        lines.append(f"Book unavailable: {errors['book']}")
    lines.append(council_line(council))
    alarms = (health or {}).get("alarms") or []
    if alarms:
        lines.append("Alarms:")
        lines += [f"- [{a.get('severity')}] {clip(a.get('message'), 200)}" for a in alarms]
    elif health:
        lines.append("Alarms: none")
    else:
        lines.append(f"Health unavailable: {errors.get('health', 'no answer')}")
    return "\n".join(lines)


def positions(d: dict, now: float) -> str:
    rows = d.get("positions") or []
    if not rows:
        return "No open positions: the book is all cash."
    head = f"**Positions** ({len(rows)}) {when(d.get('as_of'))}"
    if not d.get("marked"):
        head += f"\nUnmarked: {d.get('prices_error') or 'a holding has no price'}"
    table = [f"{'ASSET':<6} {'VALUE':>11} {'WEIGHT':>6} {'P&L':>7} {'TO STOP':>7} {'PRICE':<7}"]
    for p in rows:
        table.append(f"{p['asset']:<6} {usd(p.get('value')):>11} {pct(p.get('weight')):>6} "
                     f"{pct(p.get('unrealized_pct'), signed=True):>7} {pct(p.get('stop_distance_pct')):>7} "
                     f"{p.get('price_basis') or 'none':<7}")
    notes = []
    for p in rows:
        if p.get("risk_blind_since"):
            notes.append(f"! {p['asset']}: no live quote {when(p['risk_blind_since'])}, its stop isn't enforced")
    body = "\n".join([head, "```", *table, "```"])
    footer = ("-# PRICE: live (the bid), delayed (a minute close 16+ min old) or close (the last official "
              "close). TO STOP: how far the price is above the trailing stop. P&L is unrealized, fees included.")
    return "\n".join([body, *notes, footer])


def _trade(t: dict) -> str:
    if t.get("action") == "BUY":
        return f"BUY {t.get('asset')} (conviction {float(t.get('conviction') or 0):.2f})"
    return f"SELL {t.get('asset')} ({float(t.get('fraction_of_position') or 0) * 100:.0f}% of the position)"


def meeting(m: Optional[dict], now: float) -> str:
    if not m:
        return "No council meeting yet."
    out = [f"**Council meeting** {when(m.get('ts'), 'f')} ({when(m.get('ts'))}), status {m.get('status')}"]
    if m.get("summary"):
        out.append(f"**Chair:** {m['summary']}")
    if m.get("dissent_notes"):
        out.append(f"**Dissent:** {m['dissent_notes']}")
    orders = {o.get("asset"): o for o in m.get("orders") or []}
    if m.get("trades"):
        out.append("**Decided:**")
        for t in m["trades"]:
            o = orders.get(t.get("asset"))
            result = ""
            if o:
                result = f": {o.get('status')}"
                if o.get("notional"):
                    result += f" {usd(o['notional'])}"
                if o.get("reason") and o.get("status") != "filled":
                    result += f" ({clip(o['reason'], 120)})"
            out.append(f"- {_trade(t)}{result}")
    else:
        out.append("**Decided:** no trades")
    for x in m.get("dropped") or []:
        out.append(f"- dropped by code: {_trade(x.get('trade') or {})}: {clip(x.get('why'), 160)}")
    for name, err in (m.get("errors") or {}).items():
        out.append(f"- {name} dropped out: {clip(err, 160)}")
    ch = m.get("chair") or {}
    if ch.get("replaced"):
        out.append(f"- chaired by {ch.get('name')}: {ch['replaced']} failed ({clip(ch.get('error'), 160)})")
    for name, a in (m.get("analysts") or {}).items():
        lat = (f", {a['latency_sec']:.0f}s" if isinstance(a.get("latency_sec"), (int, float)) else "")
        out.append(f"\n**{name}** (analyst {a.get('label') or '?'}{lat}): {clip(a.get('overall_view'), 400)}")
        stances = a.get("stances") or []
        out.append("`" + " | ".join(f"{s['asset']} {s['stance']} {float(s['conviction']):.2f}"
                                    for s in stances) + "`")
        for s in stances:
            if s.get("stance") != "HOLD":
                out.append(f"> {s['asset']} {s['stance']}: {clip(s.get('thesis'), 220)} "
                           f"Risk: {clip(s.get('key_risk'), 140)}")
    return no_em_dash("\n".join(out))  # most of this view is model text


def screen(d: dict, now: float) -> str:
    """/screen: the satellites now, the latest screen's changes and the top of its ranking."""
    sats = d.get("satellites") or []
    out = [f"**Satellites** ({len([s for s in sats if s.get('state') == 'active'])} of {d.get('satellites_max')}, "
           f"screened {d.get('schedule')}):"]
    for s in sats:
        if s.get("state") == "active":
            out.append(f"- {s['symbol']}: since {s.get('promoted_day')}, score {float(s.get('score') or 0):.0f}")
        else:
            out.append(f"- {s['symbol']}: being sold off (demoted {s.get('demoted_day')}: {clip(s.get('why'), 120)})")
    if not sats:
        out.append("- none yet")
    last = d.get("screen")
    if not last:
        out.append("No screen has run yet.")
        return "\n".join(out)
    res = last.get("result") or {}
    k = res.get("counts") or {}
    kind = "applied" if last.get("applied") else "dry run, nothing changed"
    out.append(f"\n**Latest screen** {when(last.get('ts'), 'f')} ({when(last.get('ts'))}), {kind}: "
               f"{k.get('ranked', 0):,} of {k.get('measured', 0):,} measured stocks passed the gates as operating "
               f"companies ({k.get('liquid', 0):,} liquid).")
    dec = res.get("decision") or {}
    for label, key in (("Promoted", "promote"), ("Demoted", "demote"), ("Kept", "keep"), ("Passed over", "passed")):
        items = dec.get(key) or []
        if key == "passed":
            items = items[:5]
        for sym, why in items:
            out.append(f"- {label} {sym}: {clip(why, 160)}")
    top = res.get("top") or []
    if top:
        out.append("**Top of the ranking:** " + " | ".join(
            f"{r['rank']}. {r['symbol']} {float(r.get('score') or 0):.0f}"
            + (f" (12-1 {float(r['momentum_return']) * 100:+.0f}%)" if r.get("momentum_return") is not None else "")
            for r in top[:10]))
    return no_em_dash("\n".join(out))


def kill_result(state: dict, engaged: bool, who: str) -> str:
    if engaged:
        return (f"**Kill switch ENGAGED** by {who}: nothing fills from now on; council verdicts are "
                "recorded as dry run. `/release` turns paper fills back on.")
    if state.get("effective_dry_run"):
        return (f"**Kill switch released** by {who}, but config or OTTR_DRY_RUN still forces dry run: "
                "nothing fills until that changes.")
    return f"**Kill switch released** by {who}: paper fills resume with the next council decision."


# -- splitting -----------------------------------------------------------------------------
FENCE_RE = re.compile(r"^\s*```(.*)$")
CLOSE = "\n```"


def _open_fence(text: str) -> Optional[str]:
    """The language tag of a code block left open at the end of `text`, else None."""
    lang: Optional[str] = None
    for line in text.split("\n"):
        m = FENCE_RE.match(line)
        if m:
            lang = None if lang is not None else m.group(1).strip()
    return lang


def _cut(text: str, budget: int) -> tuple:
    """(end of this piece, start of the next) for a split at or before `budget`."""
    window = text[:budget]
    for sep in ("\n\n", "\n"):
        i = window.rfind(sep)
        if i > budget // 3:  # don't leave a tiny first piece just to honor a paragraph break
            return i, i + len(sep)
    m = None
    for m in re.finditer(r"[.!?;:](\s)", window):
        pass
    if m and m.start(1) > budget // 3:
        return m.start(1), m.end(1)
    i = window.rfind(" ")
    if i > budget // 3:
        return i, i + 1
    return budget, budget  # one huge word: hard cut


def split_message(text: str, limit: int = DISCORD_LIMIT, max_chunks: int = MAX_CHUNKS) -> List[str]:
    """Split `text` into Discord messages of at most `limit` characters."""
    text = text.strip()
    if not text:
        return []
    chunks: List[str] = []
    rest, lang = text, None
    while rest:
        # A code block cut by the previous split is closed there and re-opened here.
        prefix = f"```{lang}\n" if lang is not None else ""
        if len(prefix) + len(rest) <= limit:
            chunks.append(prefix + rest)
            break
        end, nxt = _cut(rest, limit - len(prefix) - len(CLOSE))
        piece = prefix + rest[:end].rstrip()
        rest = rest[nxt:].lstrip("\n")
        lang = _open_fence(piece)
        chunks.append(piece + CLOSE if lang is not None else piece)
    if len(chunks) > max_chunks:
        omitted = sum(len(c) for c in chunks[max_chunks:])
        note = f"\n-# (reply cut here: about {omitted:,} more characters not shown)"
        last = chunks[max_chunks - 1]
        if last.endswith(CLOSE) and _open_fence(last[:-len(CLOSE)]) is not None:
            last = last[:-len(CLOSE)]  # the fence the split added; re-closed below if still needed
        last = last[:limit - len(note) - len(CLOSE)].rstrip()
        if _open_fence(last) is not None:
            last += CLOSE
        chunks = chunks[:max_chunks - 1] + [last + note]
    return chunks
