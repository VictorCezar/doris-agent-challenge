"""Cliente SSE de avaliação E2E contra `POST /chat` e auditoria em `GET /admin/log`."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


@dataclass
class EvalTurnResult:
    conversation_id: str
    user_id: str
    message: str
    http_status: int
    ttft_ms: Optional[float]
    total_ms: float
    events: list[dict[str, Any]] = field(default_factory=list)
    full_text: str = ""
    admin_log: dict[str, Any] = field(default_factory=dict)
    turn_log: dict[str, Any] = field(default_factory=dict)


class EvalClient:
    def __init__(self, proxy_url: str = "http://localhost:3000", timeout_s: float = 110.0):
        self.proxy_url = proxy_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.proxy_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "EvalClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def reset(self) -> None:
        resp = await self._http.post("/admin/reset")
        resp.raise_for_status()

    async def get_log(self, conversation_id: str) -> dict[str, Any]:
        resp = await self._http.get("/admin/log", params={"conversation_id": conversation_id})
        resp.raise_for_status()
        return resp.json()

    async def send_turn(
        self,
        *,
        user_id: str,
        conversation_id: str,
        message: str,
        images: Optional[list[str]] = None,
    ) -> EvalTurnResult:
        payload = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "message": message,
            "images": images or [],
        }
        events: list[dict[str, Any]] = []
        text_parts: list[str] = []
        ttft_ms: Optional[float] = None
        t0 = time.monotonic()

        async with self._http.stream("POST", "/chat", json=payload) as resp:
            status = resp.status_code
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw_data = line[5:].strip()
                if not raw_data:
                    continue
                try:
                    ev = json.loads(raw_data)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                events.append(ev)
                if ev.get("type") == "text" and ev.get("delta"):
                    if ttft_ms is None:
                        ttft_ms = (time.monotonic() - t0) * 1000.0
                    text_parts.append(str(ev["delta"]))

        total_ms = (time.monotonic() - t0) * 1000.0
        admin_log = await self.get_log(conversation_id)
        turns = admin_log.get("turns") or []
        turn_log = turns[-1] if turns else {}

        return EvalTurnResult(
            conversation_id=conversation_id,
            user_id=user_id,
            message=message,
            http_status=status,
            ttft_ms=ttft_ms,
            total_ms=total_ms,
            events=events,
            full_text="".join(text_parts),
            admin_log=admin_log,
            turn_log=turn_log,
        )
