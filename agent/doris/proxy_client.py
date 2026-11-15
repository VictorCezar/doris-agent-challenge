"""Cliente tipado das tools do proxy (docs/proxy-api.md).

- Desembrulha o envelope `{ok, data, error}` e levanta `ToolError`.
- Retry exponencial (0.5s, 1.0s; máx. 2 retries) só para falhas transitórias (ADR-004):
  envelope com `retryable=true`; ou, sem envelope legível, HTTP 502/503/504 / timeout / erro de transporte.
  O `retryable` explícito do envelope sempre prevalece. Nunca há retry em 400/404/422.
- Todo timeout é limitado por `Deadline.cap_timeout()`.
- Nunca chama `/admin/*` nem `/conversations/*` (regra do repositório).
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urljoin, urlparse

import httpx

from .deadline import Deadline
from .types import ArtifactType, Avatar, Garment, LookItem, Product, TryonResult, WardrobeItem

log = logging.getLogger("doris.proxy")

RETRY_BACKOFF_S: tuple[float, ...] = (0.5, 1.0)
TRANSIENT_STATUS = {502, 503, 504}
NEVER_RETRY_STATUS = {400, 404, 422}
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Timeouts "normais" por tool (latência documentada + folga); sempre limitados pelo Deadline.
TOOL_TIMEOUT_S = {
    "search_by_text": 10.0, "garment_creation": 15.0, "tryon": 35.0,
    "avatar": 5.0, "looks": 5.0, "wardrobe": 5.0, "show_artifact": 5.0,
}
IMAGE_TIMEOUT_S = 10.0
_FORBIDDEN_PREFIXES = ("/admin", "/conversations")


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, status_code: Optional[int] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


def _parse_error(resp: httpx.Response) -> ToolError:
    status = resp.status_code
    try:
        body = resp.json()
    except ValueError:
        body = None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        retryable = bool(err.get("retryable", False))
        if status in NEVER_RETRY_STATUS:
            retryable = False
        return ToolError(str(err.get("code") or "tool_error"), str(err.get("message") or ""), retryable, status)
    # Sem envelope legível: decide pelo status HTTP.
    return ToolError("http_error", f"HTTP {status}", status in TRANSIENT_STATUS, status)


class ProxyClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        backoff: tuple[float, ...] = RETRY_BACKOFF_S,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport)
        self._sleep = sleep
        self._backoff = backoff

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "ProxyClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ── núcleo ────────────────────────────────────────────────────────────────

    async def _call(self, tool: str, body: dict[str, Any], deadline: Deadline) -> Any:
        path = f"/tools/{tool}"
        assert not path.startswith(_FORBIDDEN_PREFIXES)
        attempt = 0
        while True:
            if deadline.is_expired():
                raise ToolError("deadline_exceeded", f"sem tempo para chamar {tool}", False, None)
            timeout = deadline.cap_timeout(TOOL_TIMEOUT_S.get(tool, 10.0))
            try:
                resp = await self._http.post(path, json=body, timeout=timeout)
                if resp.status_code // 100 == 2:
                    try:
                        payload = resp.json()
                    except ValueError as e:
                        raise ToolError("invalid_response", f"{tool}: resposta não-JSON", False,
                                        resp.status_code) from e
                    if not isinstance(payload, dict) or payload.get("ok") is not True:
                        err = payload.get("error") if isinstance(payload, dict) else None
                        if isinstance(err, dict):
                            raise ToolError(str(err.get("code") or "tool_error"), str(err.get("message") or ""),
                                            bool(err.get("retryable", False)), resp.status_code)
                        raise ToolError("invalid_response", f"{tool}: envelope inválido", False, resp.status_code)
                    return payload.get("data")
                error = _parse_error(resp)
            except httpx.TimeoutException:
                error = ToolError("timeout", f"{tool}: timeout após {timeout:.1f}s", True, None)
            except httpx.TransportError as e:
                error = ToolError("transport_error", f"{tool}: {type(e).__name__}", True, None)

            if not error.retryable or attempt >= len(self._backoff):
                raise error
            wait = self._backoff[attempt]
            if deadline.remaining() <= wait + 0.5:
                raise error
            attempt += 1
            log.warning("tool %s falhou (%s, status=%s); retry %d em %.1fs",
                        tool, error.code, error.status_code, attempt, wait)
            await self._sleep(wait)

    # ── tools ─────────────────────────────────────────────────────────────────

    async def search_by_text(self, conversation_id: str, query: str, deadline: Deadline,
                             limit: int = 6) -> list[Product]:
        limit = max(1, min(int(limit), 12))
        data = await self._call("search_by_text",
                                {"conversation_id": conversation_id, "query": query, "limit": limit}, deadline)
        return [Product.model_validate(p) for p in (data or {}).get("products", []) or []]

    async def garment_creation(self, conversation_id: str, search_item_id: str, deadline: Deadline) -> Garment:
        data = await self._call("garment_creation",
                                {"conversation_id": conversation_id, "search_item_id": search_item_id}, deadline)
        return Garment.model_validate(data or {})

    async def tryon(self, conversation_id: str, garment_id: str, deadline: Deadline) -> TryonResult:
        """ADR-001: exatamente 1 peça por try-on."""
        if not isinstance(garment_id, str) or not garment_id:
            raise ValueError("try-on exige exatamente 1 garment_id")
        data = await self._call("tryon", {"conversation_id": conversation_id, "garment_ids": [garment_id],
                                          "avatar_pose": "NORMAL"}, deadline)
        return TryonResult.model_validate(data or {})

    async def avatar(self, conversation_id: str, deadline: Deadline) -> list[Avatar]:
        data = await self._call("avatar", {"conversation_id": conversation_id}, deadline)
        return [Avatar.model_validate(a) for a in (data or {}).get("avatars", []) or []]

    async def looks(self, conversation_id: str, deadline: Deadline, limit: int = 20) -> list[LookItem]:
        data = await self._call("looks", {"conversation_id": conversation_id, "limit": limit}, deadline)
        return [LookItem.model_validate(lk) for lk in (data or {}).get("looks", []) or []]

    async def wardrobe(self, conversation_id: str, deadline: Deadline,
                       garment_types: Optional[list[str]] = None) -> list[WardrobeItem]:
        body: dict[str, Any] = {"conversation_id": conversation_id}
        if garment_types:
            body["garment_types"] = garment_types
        data = await self._call("wardrobe", body, deadline)
        return [WardrobeItem.model_validate(g) for g in (data or {}).get("garments", []) or []]

    async def show_artifact(self, conversation_id: str, type: ArtifactType, value: str,
                            deadline: Deadline) -> None:
        if type not in ("PRODUCT", "GARMENT", "TRYON_CARD"):
            raise ValueError(f"tipo de artifact inválido: {type}")
        await self._call("show_artifact", {"conversation_id": conversation_id, "type": type, "value": value},
                         deadline)

    # ── imagens (ADR-002) ─────────────────────────────────────────────────────

    def is_internal_url(self, url: str) -> bool:
        if "/seed-images/" in url:
            return True
        if url.startswith("/") and not url.startswith("//"):
            return True
        parsed, base = urlparse(url), urlparse(self.base_url)
        if parsed.scheme not in ("http", "https"):
            return False
        return parsed.netloc == base.netloc or parsed.hostname in ("proxy", "localhost", "127.0.0.1")

    async def fetch_image_as_data_uri(self, image_url: str, deadline: Deadline) -> str:
        """Converte imagem interna do proxy (ou de `/seed-images/`) em data URI para a OpenRouter.

        - `data:image/...` → retorna direto.
        - Qualquer URL contendo `/seed-images/<path>` ou do próprio proxy → baixa do proxy local e converte para base64.
        - URL pública externa fora de `/seed-images/` → retorna sem baixar.
        """
        url = (image_url or "").strip()
        if url.startswith("data:image/"):
            return url
        if not self.is_internal_url(url):
            if urlparse(url).scheme in ("http", "https"):
                return url
            raise ToolError("invalid_image_url", "URL de imagem inválida", False, None)

        if "/seed-images/" in url:
            subpath = url.split("/seed-images/", 1)[1].lstrip("/")
            absolute = f"{self.base_url}/seed-images/{subpath}"
        elif url.startswith("/"):
            absolute = urljoin(self.base_url + "/", url.lstrip("/"))
        else:
            parsed = urlparse(url)
            absolute = f"{self.base_url}{parsed.path}"
        path = urlparse(absolute).path
        if path.startswith(_FORBIDDEN_PREFIXES):
            raise ToolError("forbidden_path", "caminho não permitido", False, None)
        if deadline.is_expired():
            raise ToolError("deadline_exceeded", "sem tempo para baixar a imagem", False, None)
        try:
            resp = await self._http.get(absolute, timeout=deadline.cap_timeout(IMAGE_TIMEOUT_S))
        except httpx.HTTPError as e:
            raise ToolError("image_fetch_failed", type(e).__name__, True, None) from e
        if resp.status_code != 200:
            raise ToolError("image_fetch_failed", f"HTTP {resp.status_code}", resp.status_code in TRANSIENT_STATUS,
                            resp.status_code)
        content = resp.content
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise ToolError("image_fetch_failed", "imagem vazia ou grande demais", False, resp.status_code)
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        mime = ctype if ctype.startswith("image/") else "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
