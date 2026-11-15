import asyncio
import json

import pytest

from doris.sse import (TurnEmitter, done_chunk, error_chunk, feedback_chunk, finish_chunk, run_turn,
                       text_chunk)


def _payload(chunk: str):
    assert chunk.startswith("data: ") and chunk.endswith("\n\n")
    body = chunk[len("data: "):].strip()
    return body if body == "[DONE]" else json.loads(body)


def test_chunk_formats():
    t = _payload(text_chunk("olá"))
    assert t["object"] == "chat.completion.chunk"
    assert t["choices"] == [{"index": 0, "delta": {"content": "olá"}, "finish_reason": None}]

    f = _payload(feedback_chunk("tryon-1", "Gerando…", "running"))
    assert f["choices"] == []
    assert f["doris"] == {"type": "feedback", "id": "tryon-1", "text": "Gerando…", "state": "running"}

    assert _payload(error_chunk("x")) == {"error": {"message": "x", "type": "agent_error"}}
    assert _payload(finish_chunk())["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "stop"}
    assert done_chunk() == "data: [DONE]\n\n"


def test_feedback_rejects_invalid_state():
    with pytest.raises(ValueError):
        feedback_chunk("a", "b", "loading")


async def _collect(gen):
    return [c async for c in gen]


async def test_run_turn_happy_path_ends_with_stop_and_done():
    em = TurnEmitter()

    async def handler():
        em.feedback("s", "Buscando…")
        em.text("Oi")
        em.feedback("s", "Pronto", "done")

    chunks = await _collect(run_turn(em, handler()))
    assert _payload(chunks[-2])["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1] == done_chunk()
    assert em.full_text == "Oi"


async def test_run_turn_closes_with_done_even_on_exception():
    em = TurnEmitter()

    async def handler():
        em.text("parcial")
        raise RuntimeError("boom")

    chunks = await _collect(run_turn(em, handler()))
    payloads = [_payload(c) for c in chunks]
    assert payloads[-1] == "[DONE]"
    assert payloads[-2]["choices"][0]["finish_reason"] == "stop"
    states = [p["doris"]["state"] for p in payloads if isinstance(p, dict) and "doris" in p]
    assert "error" in states
    assert not any(isinstance(p, dict) and "error" in p for p in payloads)  # não marca o turno como erro fatal


async def test_close_is_idempotent_and_fatal_error_skips_finish():
    em = TurnEmitter()
    em.error("fatal")
    em.close()
    em.close()
    em.text("ignorado")
    chunks = await _collect(em.stream())
    assert [_payload(c) for c in chunks] == [{"error": {"message": "fatal", "type": "agent_error"}}, "[DONE]"]


async def test_stream_is_real_time():
    em = TurnEmitter()
    gate = asyncio.Event()

    async def handler():
        em.feedback("s", "Começando…")
        await gate.wait()

    gen = run_turn(em, handler())
    first = await asyncio.wait_for(gen.__anext__(), timeout=1)
    assert _payload(first)["doris"]["text"] == "Começando…"
    gate.set()
    rest = [c async for c in gen]
    assert rest[-1] == done_chunk()
