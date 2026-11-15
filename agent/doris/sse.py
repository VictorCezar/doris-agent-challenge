"""SSE no formato OpenAI Chat Completions + extensão `doris` (README, "Contrato do agente").

Observação: no proxy, um `error_chunk` encerra o turno como ERRO. Use-o apenas em falha total; para falhas
parciais prefira texto explicativo + `feedback_chunk(..., "error")` e feche normalmente.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Literal, Optional

FeedbackState = Literal["running", "done", "error"]
FEEDBACK_STATES = ("running", "done", "error")
OBJECT = "chat.completion.chunk"
MODEL = "doris-agent"


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def text_chunk(content: str) -> str:
    return _sse({"object": OBJECT, "model": MODEL,
                 "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})


def feedback_chunk(id: str, text: str, state: FeedbackState = "running") -> str:
    if state not in FEEDBACK_STATES:
        raise ValueError(f"state inválido: {state}")
    return _sse({"object": OBJECT, "model": MODEL, "choices": [],
                 "doris": {"type": "feedback", "id": id, "text": text, "state": state}})


def error_chunk(message: str) -> str:
    return _sse({"error": {"message": message, "type": "agent_error"}})


def finish_chunk() -> str:
    return _sse({"object": OBJECT, "model": MODEL,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})


def done_chunk() -> str:
    return "data: [DONE]\n\n"


_CLOSE = object()


class TurnEmitter(asyncio.Queue):
    """Fila de eventos SSE do turno. O handler escreve; o endpoint consome via `stream()`.

    `close()` é idempotente e sempre enfileira `finish_chunk` + `done_chunk` (a menos que um erro fatal já
    tenha sido emitido, caso em que só o `[DONE]` é enviado).
    """

    def __init__(self) -> None:
        super().__init__()
        self.closed = False
        self.fatal_error = False
        self.text_parts: list[str] = []

    # ── escrita ──
    def text(self, content: str) -> None:
        if self.closed or not content:
            return
        self.text_parts.append(content)
        self.put_nowait(text_chunk(content))

    def feedback(self, id: str, text: str, state: FeedbackState = "running") -> None:
        if not self.closed:
            self.put_nowait(feedback_chunk(id, text, state))

    def error(self, message: str) -> None:
        """Erro fatal: o proxy encerra o turno como erro."""
        if not self.closed:
            self.fatal_error = True
            self.put_nowait(error_chunk(message))

    def close(self) -> None:
        if self.closed:
            return
        if not self.fatal_error:
            self.put_nowait(finish_chunk())
        self.put_nowait(done_chunk())
        self.closed = True
        self.put_nowait(_CLOSE)

    @property
    def full_text(self) -> str:
        return "".join(self.text_parts)

    # ── leitura ──
    async def stream(self) -> AsyncIterator[str]:
        while True:
            item = await self.get()
            if item is _CLOSE:
                return
            yield item


async def run_turn(emitter: TurnEmitter, coro, *, on_error_text: Optional[str] = None) -> AsyncIterator[str]:
    """Roda `coro` (que escreve no emitter) em uma task e devolve o stream SSE.

    Garantia: todo turno termina com `finish_reason="stop"` + `[DONE]`, mesmo com exceção ou cancelamento.
    """
    fallback = on_error_text or "Desculpa, tive um problema aqui e não consegui concluir. Pode tentar de novo?"

    async def _runner() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — nunca deixar o turno sem fechamento
            import logging
            logging.getLogger("doris").exception("falha no turno")
            emitter.feedback("turn", "Algo deu errado", "error")
            emitter.text(("\n\n" if emitter.text_parts else "") + fallback)
        finally:
            emitter.close()

    task = asyncio.create_task(_runner())
    try:
        async for chunk in emitter.stream():
            yield chunk
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
