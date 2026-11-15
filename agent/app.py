"""Agente da Doris — API compatível com OpenAI Chat Completions em streaming (contrato no README da raiz)."""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from doris.config import get_settings
from doris.deadline import Deadline
from doris.graph import run_doris_turn
from doris.llm import LLMClient
from doris.memory import ConversationStore, Database, ProfileStore
from doris.proxy_client import ProxyClient
from doris.sse import TurnEmitter, done_chunk, error_chunk, run_turn
from doris.types import TurnRequest

_SECRET_RE = re.compile(r"(Bearer\s+[A-Za-z0-9._-]+|sk-or-v1-[A-Za-z0-9]+)", re.IGNORECASE)


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if _SECRET_RE.search(msg):
            record.msg = _SECRET_RE.sub("[REDACTED]", msg)
            record.args = ()
        return True


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RedactingFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("doris")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = Database.in_dir(settings.DATA_DIR)
    app.state.settings = settings
    app.state.proxy = ProxyClient(settings.PROXY_URL)
    app.state.llm = LLMClient(
        settings.OPENROUTER_API_KEY.get_secret_value(),
        base_url=settings.OPENROUTER_BASE_URL,
        fallback_model=settings.FALLBACK_MODEL,
    )
    app.state.conversations = ConversationStore(db)
    app.state.profiles = ProfileStore(db)
    try:
        yield
    finally:
        await app.state.llm.aclose()
        await app.state.proxy.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    st = request.app.state
    deadline = Deadline(st.settings.TURN_DEADLINE_S)
    try:
        req = TurnRequest.from_openai(await request.json())
    except Exception as e:  # noqa: BLE001
        log.warning("payload inválido: %s", e)

        async def bad():
            yield error_chunk("requisição inválida para o agente")
            yield done_chunk()
        return StreamingResponse(bad(), media_type="text/event-stream")

    emitter = TurnEmitter()
    coro = run_doris_turn(
        req,
        settings=st.settings,
        proxy=st.proxy,
        llm=st.llm,
        conversations=st.conversations,
        profiles=st.profiles,
        emitter=emitter,
        deadline=deadline,
    )
    return StreamingResponse(
        run_turn(emitter, coro),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
