"""Cliente LLM assíncrono para OpenRouter (ADR-002 / ADR-004).

- `complete_json`: força saída JSON validada por Pydantic v2; faz 1 retry com erro de validação e fallback para
  `FALLBACK_MODEL` se falhar novamente.
- `stream_chat`: streaming SSE de tokens com fallback automático se falhar antes do 1º token.
- Emite log JSON estruturado (`model`, `latency_ms`, `prompt_tokens`, `completion_tokens`, `cost`).
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, AsyncIterator, Optional, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .deadline import Deadline

log = logging.getLogger("doris.llm")

T = TypeVar("T", bound=BaseModel)

DEFAULT_LLM_TIMEOUT_S = 25.0
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class LLMError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _extract_json_str(raw: str) -> str:
    text = (raw or "").strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _log_call(
    *,
    model: str,
    latency_ms: int,
    usage: Optional[dict[str, Any]] = None,
    kind: str = "chat",
    error: Optional[str] = None,
) -> None:
    u = usage or {}
    entry = {
        "event": "llm_call",
        "kind": kind,
        "model": model,
        "latency_ms": latency_ms,
        "prompt_tokens": u.get("prompt_tokens", 0),
        "completion_tokens": u.get("completion_tokens", 0),
        "cost": u.get("cost", u.get("total_cost", 0.0)),
    }
    if error:
        entry["error"] = error
    log.info(json.dumps(entry, ensure_ascii=False))


class LLMClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        fallback_model: str = "openai/gpt-6-luna",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.fallback_model = fallback_model
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            transport=transport,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://doris.fashion",
                "X-Title": "Doris Agent",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post_completion(
        self,
        messages: list[dict[str, Any]],
        model: str,
        deadline: Deadline,
        *,
        json_mode: bool = False,
        temperature: float = 0.1,
        max_tokens: int = 1800,
    ) -> tuple[str, dict[str, Any]]:
        if deadline.is_expired():
            raise LLMError("deadline_exceeded")
        timeout = deadline.cap_timeout(DEFAULT_LLM_TIMEOUT_S)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reasoning": {"effort": "low"},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        try:
            resp = await self._http.post("/chat/completions", json=payload, timeout=timeout)
        except httpx.HTTPError as e:
            ms = int((time.monotonic() - t0) * 1000)
            _log_call(model=model, latency_ms=ms, kind="json" if json_mode else "chat", error=type(e).__name__)
            raise LLMError(f"transport_error: {type(e).__name__}") from e

        ms = int((time.monotonic() - t0) * 1000)
        if resp.status_code // 100 != 2:
            _log_call(model=model, latency_ms=ms, kind="json" if json_mode else "chat",
                      error=f"HTTP {resp.status_code}")
            raise LLMError(f"OpenRouter HTTP {resp.status_code}", resp.status_code)

        try:
            body = resp.json()
        except ValueError as e:
            _log_call(model=model, latency_ms=ms, kind="json" if json_mode else "chat", error="invalid_json_body")
            raise LLMError("invalid_json_body", resp.status_code) from e

        usage = body.get("usage") if isinstance(body, dict) else {}
        choices = body.get("choices") if isinstance(body, dict) else None
        if not choices or not isinstance(choices, list):
            _log_call(model=model, latency_ms=ms, usage=usage, kind="json" if json_mode else "chat",
                      error="empty_choices")
            raise LLMError("empty_choices", resp.status_code)

        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        _log_call(model=model, latency_ms=ms, usage=usage, kind="json" if json_mode else "chat")
        return str(content), usage or {}

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        schema: type[T],
        model: str,
        deadline: Deadline,
        *,
        temperature: float = 0.0,
    ) -> T:
        """Retorna instância validada de `schema`. Faz 1 retry passando o erro e fallback para `FALLBACK_MODEL`."""
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        schema_instruction = (
            "Responda ESTRITAMENTE com um único objeto JSON válido que obedeça a este JSON Schema "
            f"(sem markdown, sem texto fora do JSON):\n{schema_json}"
        )
        base_msgs = [{"role": "system", "content": schema_instruction}] + list(messages)

        models_to_try = [model]
        if self.fallback_model and self.fallback_model != model:
            models_to_try.append(self.fallback_model)

        last_err: Optional[Exception] = None
        for idx, current_model in enumerate(models_to_try):
            # Para o modelo primário tentamos até 2 vezes (1 retry com erro); para o fallback 1 vez.
            max_attempts = 2 if idx == 0 else 1
            attempt_msgs = list(base_msgs)
            for attempt in range(max_attempts):
                try:
                    raw, _ = await self._post_completion(
                        attempt_msgs, current_model, deadline, json_mode=True, temperature=temperature
                    )
                    cleaned = _extract_json_str(raw)
                    return schema.model_validate_json(cleaned)
                except (ValidationError, ValueError) as val_err:
                    last_err = val_err
                    log.warning("complete_json falhou validação (%s, tentativa %d): %s",
                                current_model, attempt + 1, val_err)
                    attempt_msgs = list(base_msgs) + [
                        {"role": "assistant", "content": raw if "raw" in locals() else "{}"},
                        {
                            "role": "user",
                            "content": f"Seu JSON falhou na validação com o erro:\n{val_err}\n"
                                       "Corrija e retorne APENAS o JSON válido.",
                        },
                    ]
                except LLMError as http_err:
                    last_err = http_err
                    log.warning("complete_json falhou HTTP (%s, tentativa %d): %s",
                                current_model, attempt + 1, http_err)
                    break  # erro HTTP/timeout pula direto para o fallback_model

        raise LLMError(f"complete_json falhou para {schema.__name__}: {last_err}")

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        deadline: Deadline,
        *,
        temperature: float = 0.4,
        max_tokens: int = 1800,
    ) -> AsyncIterator[str]:
        """Stream de tokens de texto. Faz fallback automático se falhar antes do 1º token."""
        models_to_try = [model]
        if self.fallback_model and self.fallback_model != model:
            models_to_try.append(self.fallback_model)

        last_err: Optional[Exception] = None
        for current_model in models_to_try:
            if deadline.is_expired():
                raise LLMError("deadline_exceeded")
            timeout = deadline.cap_timeout(DEFAULT_LLM_TIMEOUT_S)
            payload = {
                "model": current_model,
                "messages": messages,
                "stream": True,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "reasoning": {"effort": "low"},
            }
            t0 = time.monotonic()
            emitted_any = False
            usage: dict[str, Any] = {}
            try:
                async with self._http.stream("POST", "/chat/completions", json=payload, timeout=timeout) as resp:
                    if resp.status_code // 100 != 2:
                        ms = int((time.monotonic() - t0) * 1000)
                        _log_call(model=current_model, latency_ms=ms, kind="stream",
                                  error=f"HTTP {resp.status_code}")
                        raise LLMError(f"OpenRouter stream HTTP {resp.status_code}", resp.status_code)

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except ValueError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        if isinstance(chunk.get("usage"), dict):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            if not isinstance(choice, dict):
                                continue
                            delta = choice.get("delta") or {}
                            token = delta.get("content")
                            if isinstance(token, str) and token:
                                emitted_any = True
                                yield token

                ms = int((time.monotonic() - t0) * 1000)
                _log_call(model=current_model, latency_ms=ms, usage=usage, kind="stream")
                if emitted_any:
                    return
                # Stream terminou 200 mas sem nenhum token emitido -> tenta fallback
                last_err = LLMError("empty_stream")
            except (httpx.HTTPError, LLMError) as e:
                last_err = e
                ms = int((time.monotonic() - t0) * 1000)
                _log_call(model=current_model, latency_ms=ms, kind="stream", error=str(e))
                if emitted_any:
                    log.warning("stream interrompido após emissão parcial (%s): %s", current_model, e)
                    return
                log.warning("stream falhou antes do 1º token em %s (%s); tentando fallback", current_model, e)

        if last_err:
            raise LLMError(f"stream_chat falhou: {last_err}")
