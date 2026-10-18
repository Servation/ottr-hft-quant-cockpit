"""
Chat service: who may do what, the /ask guardrails, and every command's answer.

What it does: everything the bot decides lives here, free of discord.py, so it can be
tested with fakes. Roles: the owner (CEO_DISCORD_ID) may do everything; allow-listed
users may read, /ask and /kill (the safe direction); only the owner may /release. /ask
answers one question with one model call over the desk data, inside guardrails checked
cheapest first: one call at a time, a rolling-hour budget, and silence while a council
meeting runs (the models are busy with it, and the book is about to change). Every
exchange, refusals included, is written to chat.db.

Why refusals before the deferral: Discord wants a first response within 3 seconds, and
a refusal sent as that first response can be private (ephemeral). The checks that come
first are local or one fast API read, so `defer` (the "thinking..." placeholder) is
called only once the question will really be answered.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Tuple

from ottr.chat import prompt, render
from ottr.chat.client import ApiClient, ApiError, ApiUnreachable
from ottr.chat.log import Asker, ChatLog
from ottr.config import Config
from ottr.council.models import ModelAdapter, ModelError
from ottr.health import fmt_age

logger = logging.getLogger(__name__)

HOUR = 3600.0
COUNCIL_CHECK_TIMEOUT = 1.5   # keeps the pre-checks inside Discord's 3-second window
OWNER, ALLOWED = "owner", "allowed"

STRANGER = "Sorry, OTTR only takes commands from its owner."
OWNER_ONLY = "Only the owner can release the kill switch."


@dataclass(frozen=True)
class Outcome:
    """What to send back. `ephemeral` replies are visible only to the person who asked."""
    text: str
    ephemeral: bool = False


class ChatService:
    def __init__(self, cfg: Config, api: ApiClient, model: ModelAdapter, log: ChatLog,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = cfg.chat
        self.owner_id = cfg.chat_owner_id
        self.allowed = frozenset(cfg.chat_allowed_ids)
        self.api, self.model, self.log, self.clock = api, model, log, clock
        self.model_label = f"{cfg.chat.model}, {cfg.chat.effort} effort"
        self._busy_since: Optional[float] = None

    # -- who -------------------------------------------------------------------------------
    def role(self, user_id: int) -> Optional[str]:
        if self.owner_id is not None and user_id == self.owner_id:
            return OWNER
        return ALLOWED if user_id in self.allowed else None

    def gate(self, asker: Asker, command: str) -> Optional[str]:
        """The refusal for a user who may not use the bot at all (recorded), else None."""
        if self.role(asker.user_id):
            return None
        logger.warning("refused /%s from %s (%s)", command, asker.name, asker.user_id)
        self.log.record(asker, command, None, "refused", detail="not the owner or allow-listed")
        return STRANGER

    # -- plain reads (no model) ------------------------------------------------------------
    async def _read(self, asker: Asker, command: str, fn: Callable[[], Awaitable[str]]) -> str:
        try:
            text, status, detail = await fn(), "ok", None
        except ApiError as e:
            text, status, detail = render.api_error(e), "error", e.message
        self.log.record(asker, command, None, status, reply=text, detail=detail)
        return text

    async def status(self, asker: Asker) -> str:
        async def view() -> str:
            names = ("health", "book", "council")
            res = await asyncio.gather(self.api.health(), self.api.book(), self.api.council(),
                                       return_exceptions=True)
            for r in res:
                if isinstance(r, BaseException) and not isinstance(r, ApiError):
                    raise r
            if all(isinstance(r, ApiUnreachable) for r in res):
                raise res[0]
            got = {n: (None if isinstance(r, BaseException) else r) for n, r in zip(names, res)}
            errors = {n: r.message for n, r in zip(names, res) if isinstance(r, ApiError)}
            return render.status(got["health"], got["book"], got["council"], errors, self.clock())
        return await self._read(asker, "status", view)

    async def positions(self, asker: Asker) -> str:
        async def view() -> str:
            return render.positions(await self.api.positions(), self.clock())
        return await self._read(asker, "positions", view)

    async def meeting(self, asker: Asker) -> str:
        async def view() -> str:
            return render.meeting(await self.api.latest_meeting(), self.clock())
        return await self._read(asker, "meeting", view)

    async def screen(self, asker: Asker) -> str:
        async def view() -> str:
            return render.screen(await self.api.screen(), self.clock())
        return await self._read(asker, "screen", view)

    # -- kill switch (the one write, through the API's authenticated endpoint) -------------
    async def _kill(self, asker: Asker, command: str, engaged: bool, reason: str) -> str:
        # by="discord" says where it came from; the reason says who (the ID can't be spoofed).
        why = f"/{command} by {asker.name} ({asker.user_id})" + (f": {reason}" if reason else "")
        try:
            state = await self.api.set_kill(engaged, why)
            text, status, detail = render.kill_result(state, engaged, asker.name), "ok", None
            logger.warning("kill switch %s via Discord by %s", "engaged" if engaged else "released", asker.name)
        except ApiError as e:
            text, status, detail = render.api_error(e), "error", e.message
        self.log.record(asker, command, reason or None, status, reply=text, detail=detail)
        return text

    async def kill(self, asker: Asker, reason: str = "") -> str:
        return await self._kill(asker, "kill", True, reason.strip())

    def may_release(self, asker: Asker) -> Optional[str]:
        """A refusal (recorded) unless `asker` is the owner."""
        if self.role(asker.user_id) == OWNER:
            return None
        self.log.record(asker, "release", None, "refused", detail="owner only")
        return OWNER_ONLY

    async def release_check(self, asker: Asker) -> Tuple[str, bool]:
        """(what to say, whether to ask for confirmation) for /release. No confirmation when
        there is nothing to do: not the owner, engine unreachable, switch not engaged."""
        refusal = self.may_release(asker)
        if refusal:
            return refusal, False
        try:
            k = await self.api.kill_state(timeout=COUNCIL_CHECK_TIMEOUT)
        except ApiError as e:
            return render.api_error(e), False
        if k.get("operator_engaged"):
            if k.get("config_dry_run"):
                return ("Release the kill switch? Config or OTTR_DRY_RUN still forces dry run on the host, "
                        "so nothing will fill until that changes too."), True
            return "Release the kill switch? Paper fills resume with the next council decision or risk action.", True
        if k.get("effective_dry_run"):
            return ("The kill switch isn't engaged, but config or OTTR_DRY_RUN forces dry run: nothing "
                    "fills until that changes on the host."), False
        return "The kill switch isn't engaged: paper fills are already on.", False

    async def release(self, asker: Asker) -> str:
        if self.may_release(asker):
            return OWNER_ONLY
        return await self._kill(asker, "release", False, "")

    def note_release_cancelled(self, asker: Asker, why: str) -> None:
        self.log.record(asker, "release", None, "refused", detail=why)

    # -- /ask ------------------------------------------------------------------------------
    def _refuse(self, asker: Asker, question: str, why: str, text: str, ephemeral: bool = True) -> Outcome:
        self.log.record(asker, "ask", question, "refused", reply=text, detail=why)
        return Outcome(text, ephemeral)

    async def ask(self, asker: Asker, question: str, defer: Callable[[], Awaitable[None]]) -> Outcome:
        """Answer one question. Calls `defer()` once the answer is really coming; any
        refusal before that is ephemeral."""
        question = question.strip()
        if not question:
            return self._refuse(asker, question, "empty", "Ask me something about the desk, for example "
                                "`/ask why did the council buy ETH?`")
        now = self.clock()
        if self._busy_since is not None:
            return self._refuse(asker, question, "busy", (
                f"I'm still answering the previous question (asked {fmt_age(now - self._busy_since)} ago). "
                "Ask again once that answer is posted."))
        calls = self.log.calls_since(now - HOUR)
        if len(calls) >= self.settings.per_hour:
            wait = calls[0] + HOUR - now
            return self._refuse(asker, question, "budget", (
                f"That's the hourly limit: {len(calls)} of {self.settings.per_hour} /ask calls in the last "
                f"hour. The next one frees up in {fmt_age(wait)}. /status, /positions and /meeting still "
                "work (they don't use a model)."))
        self._busy_since = now  # claimed before the first await: two asks can't both pass
        try:
            try:
                council = await self.api.council(timeout=COUNCIL_CHECK_TIMEOUT)
            except ApiError as e:
                return self._refuse(asker, question, f"api: {e.message}",
                                    render.api_error(e) + " I won't answer without current desk data.")
            if council.get("running"):
                started = council.get("started_at") or now
                return self._refuse(asker, question, "meeting", (
                    f"The council is in session right now (started {fmt_age(now - started)} ago), so I'm "
                    "staying quiet while the models work on it. Ask again once it ends; /meeting will "
                    "show what it decided."))
            await defer()
            return await self._answer(asker, question)
        finally:
            self._busy_since = None

    async def _answer(self, asker: Asker, question: str) -> Outcome:
        try:
            ctx = await self.api.desk_context()
        except ApiError as e:  # the engine went away between the check and the read
            text = render.api_error(e)
            self.log.record(asker, "ask", question, "error", reply=text, detail=e.message)
            return Outcome(text)
        now = self.clock()
        history = self.log.history(asker.channel_id, self.settings.history_turns)
        text_prompt = prompt.build(ctx, history, question, asker.name, now)
        row = self.log.start_call(asker, "ask", question, model=self.settings.model,
                                  effort=self.settings.effort, prompt=text_prompt)
        used = len(self.log.calls_since(now - HOUR))
        t0 = time.monotonic()
        try:
            raw = await self.model.complete(text_prompt, timeout=self.settings.timeout_sec)
        except ModelError as e:
            self.log.finish(row, "error", detail=str(e), latency=time.monotonic() - t0)
            return Outcome(f"The model call failed ({render.clip(str(e), 300)}). Nothing changed on the "
                           "desk; try again in a minute.")
        except Exception as e:
            logger.exception("/ask model call crashed")
            self.log.finish(row, "error", detail=f"{type(e).__name__}: {e}", latency=time.monotonic() - t0)
            return Outcome("The model call failed unexpectedly (details are in logs/chat.log). Nothing "
                           "changed on the desk.")
        latency = time.monotonic() - t0
        answer = render.no_em_dash(raw.strip()) or "(the model returned an empty answer)"
        self.log.finish(row, "ok", reply=answer, latency=latency)
        footer = (f"-# {self.model_label} | {latency:.0f}s | {used} of {self.settings.per_hour} "
                  "asks this hour | chat can't trade")
        return Outcome(f"> **{asker.name}:** {render.clip(question, 200)}\n\n{answer}\n\n{footer}")
