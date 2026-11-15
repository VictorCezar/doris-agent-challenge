import base64
import json

import httpx
import pytest

from doris.deadline import Deadline
from doris.proxy_client import ProxyClient, ToolError

BASE = "http://proxy:3000"


def make_client(handler):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    client = ProxyClient(BASE, transport=httpx.MockTransport(handler), sleep=fake_sleep)
    return client, sleeps


def err(status, code, retryable):
    return httpx.Response(status, json={"ok": False, "error": {"code": code, "message": "x", "retryable": retryable}})


async def test_search_unwraps_envelope_and_sends_body():
    seen = {}

    def handler(request: httpx.Request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"products": [
            {"search_item_id": "p01", "name": "Camisa de linho branca", "price": 249.9, "store": "Maré"}]}})

    client, _ = make_client(handler)
    products = await client.search_by_text("c1", "camisa", Deadline(10), limit=50)
    assert seen["path"] == "/tools/search_by_text"
    assert seen["body"] == {"conversation_id": "c1", "query": "camisa", "limit": 12}
    assert products[0].search_item_id == "p01" and products[0].price == 249.9


async def test_retry_only_when_retryable_true():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return err(503, "upstream_timeout", True)
        return httpx.Response(200, json={"ok": True, "data": {"garment_id": "g1", "type": "TOP"}})

    client, sleeps = make_client(handler)
    g = await client.garment_creation("c1", "p01", Deadline(30))
    assert g.garment_id == "g1"
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]


async def test_gives_up_after_two_retries():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return err(503, "upstream_timeout", True)

    client, sleeps = make_client(handler)
    with pytest.raises(ToolError) as ei:
        await client.tryon("c1", "g1", Deadline(30))
    assert calls["n"] == 3 and sleeps == [0.5, 1.0]
    assert ei.value.retryable and ei.value.status_code == 503


@pytest.mark.parametrize("status,code", [(400, "invalid_request"), (404, "conversation_not_found"),
                                         (422, "not_found"), (400, "unsupported_combination")])
async def test_no_retry_on_client_errors(status, code):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return err(status, code, True)  # mesmo que o servidor diga retryable, 4xx nunca re-tenta

    client, sleeps = make_client(handler)
    with pytest.raises(ToolError) as ei:
        await client.show_artifact("c1", "PRODUCT", "p01", Deadline(10))
    assert calls["n"] == 1 and sleeps == []
    assert ei.value.code == code and ei.value.status_code == status and not ei.value.retryable


async def test_no_retry_when_envelope_says_not_retryable():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return err(503, "upstream_down", False)

    client, sleeps = make_client(handler)
    with pytest.raises(ToolError):
        await client.looks("c1", Deadline(10))
    assert calls["n"] == 1 and sleeps == []


async def test_retry_on_502_without_envelope_and_on_timeout():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        if calls["n"] == 2:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True, "data": {"garments": []}})

    client, sleeps = make_client(handler)
    assert await client.wardrobe("c1", Deadline(30)) == []
    assert calls["n"] == 3 and sleeps == [0.5, 1.0]


async def test_no_retry_when_deadline_too_short():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return err(503, "upstream_timeout", True)

    client, sleeps = make_client(handler)
    with pytest.raises(ToolError):
        await client.avatar("c1", Deadline(0.6))
    assert calls["n"] == 1 and sleeps == []


async def test_tryon_sends_exactly_one_garment():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"tryon_id": "t1", "look_id": "l1",
                                                               "garment_ids": ["g1"]}})

    client, _ = make_client(handler)
    t = await client.tryon("c1", "g1", Deadline(30))
    assert seen["body"]["garment_ids"] == ["g1"] and t.tryon_id == "t1"
    with pytest.raises(ValueError):
        await client.tryon("c1", "", Deadline(30))


async def test_expired_deadline_does_not_call():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True, "data": {}})

    client, _ = make_client(handler)
    with pytest.raises(ToolError) as ei:
        await client.looks("c1", Deadline(0))
    assert ei.value.code == "deadline_exceeded" and calls["n"] == 0


# ── fetch_image_as_data_uri ──────────────────────────────────────────────────

JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"


@pytest.mark.parametrize("url", [
    "http://proxy:3000/seed-images/products/p01.jpg",
    "/seed-images/products/p01.jpg",
    "https://raw.githubusercontent.com/dorisopensource/doris-agent-challenge/main/seed-images/products/p01.jpg",
])
async def test_fetch_internal_image_converts_to_base64(url):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    client, _ = make_client(handler)
    out = await client.fetch_image_as_data_uri(url, Deadline(10))
    assert out == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    assert seen["url"] == "http://proxy:3000/seed-images/products/p01.jpg"


async def test_fetch_defaults_to_jpeg_mime_when_unknown():
    client, _ = make_client(lambda r: httpx.Response(200, content=JPEG, headers={"content-type": "application/octet-stream"}))
    out = await client.fetch_image_as_data_uri("/seed-images/x.jpg", Deadline(10))
    assert out.startswith("data:image/jpeg;base64,")


async def test_fetch_passthrough_data_uri_and_public_url():
    def handler(request):  # pragma: no cover - não deve ser chamado
        raise AssertionError("não deveria baixar")

    client, _ = make_client(handler)
    data_uri = "data:image/png;base64,AAAA"
    assert await client.fetch_image_as_data_uri(data_uri, Deadline(10)) == data_uri
    public = "https://cdn.exemplo.com/products/p01.jpg"
    assert await client.fetch_image_as_data_uri(public, Deadline(10)) == public


async def test_fetch_refuses_admin_paths_and_errors():
    client, _ = make_client(lambda r: httpx.Response(404))
    with pytest.raises(ToolError):
        await client.fetch_image_as_data_uri("/admin/log", Deadline(10))
    with pytest.raises(ToolError) as ei:
        await client.fetch_image_as_data_uri("/seed-images/missing.jpg", Deadline(10))
    assert ei.value.code == "image_fetch_failed"
