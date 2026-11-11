"""
The /ask tool loop: Claude answers a question with read-only lookups, through the Claude API.

What it does: sends the instructions, the desk snapshot and the question, with the
lookups (chat/lookup.py) as tools. While the model asks for lookups, code runs them (one
turn's lookups concurrently, all their results in the next message, as the API expects)
and sends the results back. When the model answers, the reply is schema-validated JSON:
the answer, and whether it proposes a council meeting and why. A question gets at most
MAX_LOOKUPS lookups over MAX_ROUNDS model turns; past either, the model is told to answer
with what it has (tools off for the last turn).

Why a loop OTTR owns rather than the SDK's beta tool runner: the loop is the guardrail
(the lookup cap, the forced last turn, every lookup recorded for chat.db), and owning it
keeps it model-agnostic, so tests drive it with a scripted fake model and a fake engine.
Why a proposal is a field of the answer and not a tool: the model's only output that can
lead anywhere is data a person must act on. Code shows the owner a button; only the
owner can press it, and code's limits decide whether a meeting runs.

Why it is only on the Claude API (`council.claude_backend = "api"`): the CLIs run with
their tools turned off on purpose, and the API is where code, not the CLI, decides which
tools exist. On the CLI backend /ask makes one plain call over the snapshot, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, List, Optional, Protocol, Tuple

import anthropic

from ottr.chat.lookup import TOOLS, Done, Lookups
from ottr.config import Config
from ottr.council.models import FALLBACK_BETA, ModelError, _sdk, api_message

logger = logging.getLogger(__name__)

MAX_LOOKUPS = 8          # per question: enough for "compare the last three meetings", not a crawl
MAX_ROUNDS = 5           # model turns per question, the last one with tools off
ASK_MAX_TOKENS = 16000   # thinking plus a Discord-sized answer; under the SDK's non-streaming limit
REASON_MAX = 300         # a proposal's reason, as the meeting packet will carry it

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "the reply to post in Discord (markdown)"},
        "propose_meeting": {"type": "boolean",
                            "description": "true to offer the owner a button that calls a council meeting"},
        "meeting_reason": {"type": "string",
                           "description": "what the council should weigh, in a sentence or two; \"\" if not proposing"},
    },
    "required": ["answer", "propose_meeting", "meeting_reason"],
    "additionalProperties": False,
}
OVER_LIMIT = (f"Not run: this question has used its {MAX_LOOKUPS} lookups. Answer with what you have, "
              "and say what you couldn't check.")


@dataclass
class ToolCall:
    id: str
    name: str
    input: Any


@dataclass
class Step:
    """One model turn."""
    content: Any                  # the assistant content, sent back unchanged on the next request
    stop_reason: str
    calls: List[ToolCall]
    text: str                     # the text blocks, joined
    model: str = ""               # who answered (a fallback model after a refusal)
    tokens: Tuple[int, int, int, int] = (0, 0, 0, 0)   # input, output, cache read, cache write


class ToolSession(Protocol):
    async def step(self, system: str, messages: List[dict], tools: List[dict], final: bool,
                   timeout: float) -> Step: ...


class ToolModel(Protocol):
    """What the loop needs from a model: a session (one per question) that takes turns."""
    name: str
    model: str

    def session(self) -> contextlib.AbstractAsyncContextManager: ...


@dataclass
class Proposal:
    why: str


@dataclass
class Answer:
    text: str
    proposal: Optional[Proposal]
    lookups: List[Done] = field(default_factory=list)
    rounds: int = 0
    tokens: Tuple[int, int, int, int] = (0, 0, 0, 0)
    model: str = ""

    def detail(self) -> str:
        """The lookups and token counts, for the exchange's row in chat.db. Cache writes are
        named apart: with automatic caching most of a question's input is billed as one."""
        i, o, r, w = self.tokens
        parts = [f"{self.rounds} turn(s), {len(self.lookups)} lookup(s); tokens {i:,} in, {o:,} out, "
                 f"{r:,} cache read, {w:,} cache write" + (f"; answered by {self.model}" if self.model else "")]
        parts += [d.describe() for d in self.lookups]
        if self.proposal:
            parts.append(f"proposed a meeting: {self.proposal.why}")
        return "\n".join(parts)


def parse_answer(text: str) -> Tuple[str, Optional[Proposal]]:
    """The answer and the proposal, if any, from the model's JSON. Structured outputs make the
    shape a guarantee on the API; a reply that still isn't that JSON (a fake, a future
    change) is shown as it is, and proposes nothing."""
    try:
        obj = json.loads(text)
    except ValueError:
        return text.strip(), None
    if not isinstance(obj, dict) or not isinstance(obj.get("answer"), str):
        return text.strip(), None
    reason = obj.get("meeting_reason")
    reason = " ".join(reason.split())[:REASON_MAX] if isinstance(reason, str) else ""
    wants = obj.get("propose_meeting") is True and bool(reason)
    return obj["answer"].strip(), Proposal(reason) if wants else None


async def answer(model: ToolModel, lookups: Lookups, system: str, user: str, timeout: float,
                 on_lookup: Optional[Callable[[Done], None]] = None) -> Answer:
    """Run the loop for one question. Raises ModelError when the model fails or never
    answers; `timeout` caps the whole loop, lookups included (the caller enforces it)."""
    messages: List[dict] = [{"role": "user", "content": user}]
    done: List[Done] = []
    tokens = (0, 0, 0, 0)
    async with model.session() as s:
        for rnd in range(1, MAX_ROUNDS + 1):
            final = rnd == MAX_ROUNDS or len(done) >= MAX_LOOKUPS
            step = await s.step(system, messages, TOOLS, final, timeout)
            tokens = tuple(a + b for a, b in zip(tokens, step.tokens))
            if step.stop_reason != "tool_use" or not step.calls:
                if not step.text.strip():
                    raise ModelError(f"empty reply (stop reason {step.stop_reason})")
                text, proposal = parse_answer(step.text)
                if not text:
                    raise ModelError("the reply had an empty answer")
                return Answer(text, proposal, done, rnd, tokens,
                              step.model if step.model and step.model != model.model else "")
            if final:
                raise ModelError(f"still asking for lookups after {rnd} turns and {len(done)} lookups")
            messages.append({"role": "assistant", "content": step.content})
            room = MAX_LOOKUPS - len(done)
            ran = await asyncio.gather(*(lookups.run(c.name, c.input) for c in step.calls[:room]))
            results = []
            for c, (text, is_error, rec) in zip(step.calls, ran):
                done.append(rec)
                if on_lookup:
                    on_lookup(rec)
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": text,
                                **({"is_error": True} if is_error else {})})
            for c in step.calls[room:]:     # every tool_use needs its result, even one not run
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": OVER_LIMIT,
                                "is_error": True})
            messages.append({"role": "user", "content": results})
    raise ModelError(f"no answer after {MAX_ROUNDS} turns")  # unreachable: the last turn is final


# -- Claude through the API ------------------------------------------------------------------
class _ClaudeSession:
    def __init__(self, owner: "ClaudeTools", client: Any) -> None:
        self.owner, self.client = owner, client

    async def step(self, system: str, messages: List[dict], tools: List[dict], final: bool,
                   timeout: float) -> Step:
        o = self.owner
        try:
            msg = await self.client.beta.messages.create(
                model=o.model, max_tokens=ASK_MAX_TOKENS, system=system, messages=messages, tools=tools,
                # tools stay declared on the last turn (the history holds tool_use blocks), but off.
                tool_choice={"type": "none"} if final else {"type": "auto"},
                output_config={"effort": o.effort, "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
                # The prefix (tools, instructions, snapshot, earlier turns) is re-read each turn:
                # cached, a turn after the first costs a tenth for it.
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA], fallbacks="default", timeout=timeout)
        except anthropic.AnthropicError as e:
            raise ModelError(f"claude api: {api_message(e)}") from None
        u = msg.usage
        if msg.stop_reason == "refusal":
            why = getattr(getattr(msg, "stop_details", None), "category", None)
            raise ModelError(f"claude api: declined to answer (refusal{f': {why}' if why else ''})")
        if msg.stop_reason == "max_tokens":
            raise ModelError(f"claude api: the reply was cut off at {ASK_MAX_TOKENS} tokens")
        calls = [ToolCall(b.id, b.name, b.input) for b in msg.content if b.type == "tool_use"]
        text = "".join(b.text for b in msg.content if b.type == "text")
        return Step(content=msg.content, stop_reason=msg.stop_reason or "", calls=calls, text=text,
                    model=msg.model, tokens=(u.input_tokens, u.output_tokens, u.cache_read_input_tokens or 0,
                                             u.cache_creation_input_tokens or 0))


class ClaudeTools:
    """Claude for /ask's tool loop: one non-streamed Messages API call per turn (answers are
    short, so a stream buys nothing, and the server validates each tool input in full).
    Thinking is always on for Fable 5.2 (and Opus 5.5); `effort` sets how hard (chat runs low)."""

    name = "claude"

    def __init__(self, model: str, effort: str, client: Optional[Callable[[], Any]] = None) -> None:
        # `client` builds the AsyncAnthropic; tests hand in one on a mock transport.
        self.model, self.effort, self._client = model, effort, client

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[_ClaudeSession]:
        # One client per question: its turns share a connection, and nothing outlives the
        # event loop it was made on.
        async with (self._client or _sdk)() as client:
            yield _ClaudeSession(self, client)


def tool_model(cfg: Config) -> Optional[ClaudeTools]:
    """The tool loop's model, or None when /ask runs without lookups: [chat] lookups is off,
    /ask runs a model other than Claude, or Claude runs on the CLI backend."""
    c = cfg.chat
    if not c.lookups or c.model != "claude" or cfg.council.claude_backend != "api":
        return None
    return ClaudeTools(cfg.council.claude_model, c.effort)
