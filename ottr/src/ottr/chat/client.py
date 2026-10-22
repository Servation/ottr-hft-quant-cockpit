"""
HTTP client for the engine's loopback API, as the chat bot uses it.

What it does: GETs the read endpoints and POSTs the kill switch (the only write the bot
can make; the API has no trade endpoint). Transport failures and timeouts become
`ApiUnreachable`, error statuses become `ApiError` with the API's own message, so
callers can tell "the engine is down" from "the engine said no".

Why httpx with an injectable transport: tests swap in a fake engine (httpx.MockTransport)
or point it at a real ApiServer on an ephemeral port, with no other change.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx

DEFAULT_TIMEOUT = 10.0  # /book and /positions wait up to 5s for quotes before answering unmarked


class ApiError(RuntimeError):
    """The engine API answered with an error (or something that isn't JSON)."""

    def __init__(self, status: Optional[int], message: str) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status, self.message = status, message


class ApiUnreachable(ApiError):
    """No answer: the engine is stopped, restarting, or too slow to reply in time."""

    def __init__(self, message: str) -> None:
        super().__init__(None, message)


@dataclass
class DeskContext:
    """Everything /ask shows the model. A section is None when its read failed (`errors`)."""
    health: Optional[dict] = None
    book: Optional[dict] = None
    positions: Optional[dict] = None
    meeting: Optional[dict] = None     # the latest council meeting, if any
    fills: Optional[dict] = None
    council: Optional[dict] = None
    errors: Dict[str, str] = field(default_factory=dict)


class ApiClient:
    def __init__(self, base_url: str, api_key: Optional[str] = None, *,
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                       json: Any = None, headers: Optional[dict] = None,
                       timeout: Optional[float] = None) -> dict:
        url = self.base_url + path
        try:
            r = await self._http.request(method, url, params=params, json=json, headers=headers,
                                         timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TimeoutException:
            raise ApiUnreachable(f"the engine API at {self.base_url} did not answer in time")
        except httpx.TransportError as e:
            raise ApiUnreachable(f"can't connect to the engine API at {self.base_url} ({type(e).__name__})")
        try:
            body = r.json()
        except ValueError:
            raise ApiError(r.status_code, f"non-JSON reply from {path}")
        if not r.is_success:
            msg = body.get("error") if isinstance(body, dict) else None
            raise ApiError(r.status_code, msg or r.reason_phrase or "error")
        if not isinstance(body, dict):
            raise ApiError(r.status_code, f"unexpected reply shape from {path}")
        return body

    async def get(self, path: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        return await self._request("GET", path, params=params, timeout=timeout)

    # -- reads -----------------------------------------------------------------------------
    async def health(self) -> dict:
        return await self.get("/health")

    async def book(self) -> dict:
        return await self.get("/book")

    async def positions(self) -> dict:
        return await self.get("/positions")

    async def council(self, timeout: Optional[float] = None) -> dict:
        return await self.get("/council", timeout=timeout)

    async def latest_meeting(self) -> Optional[dict]:
        meetings = (await self.get("/meetings", {"limit": 1})).get("meetings") or []
        return meetings[0] if meetings else None

    async def fills(self, limit: int = 10) -> dict:
        return await self.get("/fills", {"limit": limit})

    async def kill_state(self, timeout: Optional[float] = None) -> dict:
        return await self.get("/kill-switch", timeout=timeout)

    async def desk_context(self) -> DeskContext:
        """All /ask sections, read concurrently. Partial failures are kept as `errors`;
        only an engine that answers nothing at all raises (ApiUnreachable)."""
        names = ("health", "book", "positions", "meeting", "fills", "council")
        results = await asyncio.gather(self.health(), self.book(), self.positions(), self.latest_meeting(),
                                       self.fills(10), self.council(), return_exceptions=True)
        ctx = DeskContext()
        for name, res in zip(names, results):
            if isinstance(res, ApiError):
                ctx.errors[name] = res.message
            elif isinstance(res, BaseException):
                raise res  # a bug, not an outage: let it surface
            else:
                setattr(ctx, name, res)
        if all(isinstance(r, ApiUnreachable) for r in results):
            raise results[0]
        return ctx

    # -- the one write ---------------------------------------------------------------------
    async def set_kill(self, engaged: bool, reason: str = "", by: str = "discord") -> dict:
        # The key goes in a header, never the URL, so it can't land in a log line.
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return await self._request("POST", "/kill-switch", headers=headers,
                                   json={"engaged": engaged, "reason": reason[:500], "by": by})
