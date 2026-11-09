"""供应商模型发现只读取目录，使用本机 fixture 验证匹配、分页和并发边界。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
import sys
import types
from unittest.mock import patch

from aiohttp import web
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import PluginConfig
from core.models import ProtocolError


def load_webapi():
    web_module = types.ModuleType("astrbot.api.web")
    web_module.error_response = lambda message, **kwargs: {"ok": False, "data": kwargs.get("data") or {"message": message}}
    web_module.json_response = lambda value: value
    web_module.request = types.SimpleNamespace(query={})
    modules = {"astrbot": types.ModuleType("astrbot"), "astrbot.api": types.ModuleType("astrbot.api"), "astrbot.api.web": web_module}
    spec = importlib.util.spec_from_file_location("core._supplier_model_check_test", ROOT / "core/webapi.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


def test_model_check_endpoint_exists():
    assert callable(getattr(load_webapi(), "_api_model_check", None)), "缺少不计费的供应商模型检测接口"


@pytest.fixture
def fixture(monkeypatch):
    module = load_webapi()
    cfg = PluginConfig({"suppliers": [
        {"name": "First", "base_url": "https://first.invalid/v1", "api_key": "fixture-secret-one"},
        {"name": "22", "base_url": "https://second.invalid/v1", "api_key": "fixture-secret-two"},
    ], "active_supplier": "First", "timeout": 600})
    state = types.SimpleNamespace(module=module, cfg=cfg, calls=[], closed=[], payloads={}, errors={}, delays={}, statuses={}, active=0, peak=0)

    class Result:
        status = 200
        def __init__(self, value, status=200): self.data = value; self.status = status
        def ensure_ok(self, message): pass

    class Client:
        def __init__(self, channel): self.channel = channel
        async def _request(self, method, url, **kwargs):
            assert method == "GET", "模型检测不得触发生成或其他写请求"
            name = self.channel.name
            state.calls.append((name, self.channel.protocol, method, url, kwargs.get("params")))
            state.active += 1
            state.peak = max(state.peak, state.active)
            try:
                await asyncio.sleep(state.delays.get(name, 0))
                if name in state.errors: raise state.errors[name]
                pages = state.payloads.get(name, [{"data": [{"id": "Fixture-Image"}]}])
                cursor = (kwargs.get("params") or {}).get("after") or (kwargs.get("params") or {}).get("pageToken")
                return Result(pages[0] if not cursor else pages[min(len(pages)-1, int(cursor.rsplit("-", 1)[-1]) + 1)], state.statuses.get(name, 200))
            finally: state.active -= 1
        async def list_models(self, **kwargs):
            assert kwargs.get("include_non_image") is True
            state.calls.append((self.channel.name, self.channel.protocol, "GET", "gemini", kwargs))
            return ["gemini-fixture-image"]
        def _url(self, suffix): return self.channel.base_url + "/" + suffix
        async def close(self): state.closed.append(self.channel.name)

    monkeypatch.setattr(module, "_create_protocol", lambda channel, *args, **kwargs: Client(channel))
    def query(supplier="22", model="Fixture-Image", protocol="openai", purpose="generate", **params):
        module.request.query = dict(supplier=supplier, model=model, protocol=protocol, purpose=purpose, **params)
        return asyncio.run(module._api_model_check(types.SimpleNamespace(cfg=cfg)))["data"]
    state.query = query
    return state


def test_exact_numeric_supplier_and_exact_model_id(fixture):
    result = fixture.query()
    assert result["status"] == "matched" and result["matched"] is True
    assert result["supplier"] == "22" and result["canonical_model"] == "Fixture-Image"
    assert result["connection"] == "online" and result["cached"] is False
    assert result["generation_verified"] is False and result["catalog_complete"] is True
    assert fixture.calls[0][0] == "22"
    assert fixture.closed == ["22"]
    assert fixture.cfg.active_supplier_name() == "First"
    assert "fixture-secret" not in json.dumps(result)


@pytest.mark.parametrize("supplier", ["2", "Fir", "missing"])
def test_supplier_names_are_not_indices_or_substrings(fixture, supplier):
    result = fixture.query(supplier=supplier)
    assert result["status"] == "invalid" and result["matched"] is None
    assert fixture.calls == []


@pytest.mark.parametrize("params", [{"model": ""}, {"protocol": "bad"}, {"purpose": "video"}])
def test_invalid_query_never_calls_supplier(fixture, params):
    result = fixture.query(**params)
    assert result["status"] == "invalid" and result["ok"] is False
    assert result["generation_verified"] is False and fixture.calls == []


def test_case_sensitive_match_wins_over_similar_ids(fixture):
    fixture.payloads["22"] = [{"data": [{"id": "fixture-image"}, {"id": "Fixture-Image"}]}]
    result = fixture.query()
    assert result["matched"] is True and result["match_type"] == "exact"
    assert result["canonical_model"] == "Fixture-Image"


def test_case_only_match_is_a_suggestion_not_availability(fixture):
    result = fixture.query(model="fixture-image")
    assert result["status"] == "case_mismatch" and result["matched"] is False
    assert result["canonical_model"] == "Fixture-Image"
    assert result["candidates"] == ["Fixture-Image"]


def test_complete_directory_over_200_is_not_truncated(fixture):
    fixture.payloads["22"] = [{"data": [{"id": f"model-{n}"} for n in range(405)]}]
    result = fixture.query(model="model-404")
    assert result["matched"] is True and result["catalog_total"] == 405
    assert result["catalog_complete"] is True


def test_standard_openai_pagination_reads_later_pages(fixture):
    fixture.payloads["22"] = [
        {"data": [{"id": f"model-{n}"} for n in range(240)], "has_more": True, "last_id": "cursor-0"},
        {"data": [{"id": "later-image"}], "has_more": False},
    ]
    result = fixture.query(model="later-image")
    assert result["matched"] is True and result["catalog_total"] == 241
    assert fixture.calls[1][4] == {"after": "cursor-0"}


@pytest.mark.parametrize("marker", [{"next": "/next"}, {"next_cursor": "opaque"}, {"has_more": "true"}, {"total": 999}, {"pagination": {"next_page": 2}}, {"next_page_token": "next"}, {"page": 1, "total_pages": 5}, {"limit": 1}, {"meta": {"next_cursor": "opaque"}}])
def test_unknown_or_incomplete_paging_never_claims_missing(fixture, marker):
    fixture.payloads["22"] = [dict(data=[{"id": "different"}], **marker)]
    result = fixture.query(model="missing")
    assert result["matched"] is None and result["catalog_complete"] is False
    assert result["status"] == "catalog_unavailable"
    assert len(fixture.calls) == 1


def test_repeated_page_cursor_stops_without_false_negative(fixture):
    fixture.payloads["22"] = [
        {"data": [{"id": "first"}], "has_more": True, "last_id": "cursor-0"},
        {"data": [{"id": "second"}], "has_more": True, "last_id": "cursor-0"},
    ]
    result = fixture.query(model="missing")
    assert result["matched"] is None and result["catalog_complete"] is False
    assert len(fixture.calls) == 2


def test_page_limit_preserves_unconfirmed_state(fixture):
    fixture.payloads["22"] = [{"data": [{"id": f"model-{n}"}], "has_more": True, "last_id": f"cursor-{n}"} for n in range(12)]
    result = fixture.query(model="missing")
    assert result["matched"] is None and result["catalog_complete"] is False
    assert len(fixture.calls) == 10


def test_cache_is_labeled_and_refresh_failure_does_not_use_old_success(fixture):
    assert fixture.query()["matched"] is True
    cached = fixture.query(purpose="edit")
    assert cached["cached"] is True and cached["connection"] == "cached"
    assert cached["purpose"] == "edit" and cached["generation_verified"] is False
    assert len(fixture.calls) == 1
    fixture.errors["22"] = ProtocolError("Authorization: Bearer fixture-secret-two", status=401, detail="fixture-secret-two")
    failed = fixture.query(refresh="1")
    assert failed["status"] == "auth_failed" and failed["matched"] is None
    assert failed["cached"] is False and failed["ok"] is False
    assert "fixture-secret" not in json.dumps(failed)


def test_cache_key_separates_protocol_supplier_and_credentials(fixture):
    assert fixture.query()["matched"] is True
    assert fixture.query(supplier="First")["cached"] is False
    assert fixture.query(protocol="grok")["cached"] is False
    suppliers = [{"name": item.name, "base_url": item.base_url, "api_key": item.api_key} for item in fixture.cfg.suppliers()]
    suppliers[1]["api_key"] = "fixture-rotated"
    fixture.cfg.replace_suppliers(suppliers)
    assert fixture.query()["cached"] is False
    assert len(fixture.calls) == 4


def test_gemini_reads_raw_pages_and_reuses_canonical_prefix(fixture):
    fixture.payloads["22"] = [
        {"models": [{"name": "models/first-model"}], "nextPageToken": "cursor-0"},
        {"models": [{"name": "models/gemini-fixture-image"}]},
    ]
    result = fixture.query(protocol="gemini", model="gemini-fixture-image")
    assert result["matched"] is True and result["catalog_complete"] is True
    assert len(fixture.calls) == 2 and fixture.calls[1][4] == {"pageToken": "cursor-0"}


@pytest.mark.parametrize("status,expected", [(401, "auth_failed"), (403, "auth_failed"), (404, "catalog_unavailable"), (500, "network_error")])
def test_http_failures_keep_status_and_hide_credentials(fixture, status, expected):
    fixture.errors["22"] = ProtocolError("secret fixture-secret-two Bearer other-secret", status=status, detail="private")
    result = fixture.query()
    assert result["status"] == expected and result["http_status"] == status
    assert result["matched"] is None and result["generation_verified"] is False
    assert "secret" not in json.dumps(result) and "private" not in json.dumps(result)


def test_timeout_does_not_block_another_supplier(fixture, monkeypatch):
    from core import model_discovery
    monkeypatch.setattr(model_discovery, "MODEL_CHECK_TIMEOUT", 0.08)
    fixture.delays["22"] = 10
    async def run():
        plugin = types.SimpleNamespace(cfg=fixture.cfg)
        fixture.module.request.query = dict(supplier="22", model="Fixture-Image", protocol="openai", purpose="generate")
        slow = asyncio.create_task(fixture.module._api_model_check(plugin))
        await asyncio.sleep(0)
        fixture.module.request.query = dict(supplier="First", model="Fixture-Image", protocol="openai", purpose="generate")
        fast = await fixture.module._api_model_check(plugin)
        assert fast["data"]["matched"] is True
        assert not slow.done()
        result = (await slow)["data"]
        assert result["status"] == "timeout" and result["matched"] is None
        assert result["elapsed_ms"] < 500
    asyncio.run(run())
    assert sorted(fixture.closed) == ["22", "First"]


def test_server_limits_catalog_requests_to_three(fixture):
    from core.model_discovery import check_model
    fixture.delays["22"] = 0.025
    async def run():
        channel = fixture.cfg.build_channel(supplier="22")
        return await asyncio.gather(*(check_model(fixture.cfg, channel, "Fixture-Image", "generate", factory=fixture.module._create_protocol, refresh=True) for _ in range(8)))
    results = asyncio.run(run())
    assert all(result["matched"] for result in results)
    assert fixture.peak == 3


@asynccontextmanager
async def local_directory():
    calls = []
    async def handle(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer fixture-local-key"
        calls.append({"method": request.method, "path": request.path, "after": request.query.get("after")})
        if request.query.get("after"):
            return web.json_response({"data": [{"id": "page-two-image"}], "has_more": False})
        return web.json_response({"data": [{"id": f"model-{n}"} for n in range(250)], "has_more": True, "last_id": "last-first-page"})
    app = web.Application()
    app.router.add_route("*", "/v1/models", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}/v1", calls
    finally: await runner.cleanup()


def test_real_http_reads_only_models_and_follows_pagination():
    async def run():
        async with local_directory() as (url, calls):
            module = load_webapi()
            cfg = PluginConfig({"suppliers": [{"name": "Local", "base_url": url, "api_key": "fixture-local-key"}]})
            module.request.query = dict(supplier="Local", protocol="openai", model="page-two-image", purpose="edit")
            result = (await module._api_model_check(types.SimpleNamespace(cfg=cfg)))["data"]
            assert result["matched"] is True and result["catalog_total"] == 251
            assert result["generation_verified"] is False and result["purpose"] == "edit"
            assert calls == [{"method": "GET", "path": "/v1/models", "after": None}, {"method": "GET", "path": "/v1/models", "after": "last-first-page"}]
    asyncio.run(run())



def test_nested_directory_pagination_is_not_silently_dropped(fixture):
    fixture.payloads["22"] = [{"data": {"items": [{"id": "first"}], "has_more": True, "last_id": "cursor"}}]
    result = fixture.query(model="missing")
    assert result["matched"] is None and result["catalog_complete"] is False
    assert result["status"] == "catalog_unavailable"


def test_cache_expiry_and_failed_refresh_do_not_reuse_stale_success(fixture, monkeypatch):
    from core import model_discovery
    assert fixture.query()["matched"] is True
    monkeypatch.setattr(model_discovery, "MODEL_CHECK_CACHE_TTL", -1)
    fixture.errors["22"] = OSError("Bearer fixture-secret-two")
    result = fixture.query()
    assert result["status"] == "network_error" and result["cached"] is False
    monkeypatch.setattr(model_discovery, "MODEL_CHECK_CACHE_TTL", 30)
    result = fixture.query()
    assert result["status"] == "network_error" and result["cached"] is False
    assert len(fixture.calls) == 3


def test_absent_tongyi_catalog_does_not_claim_connection_or_generate(fixture):
    result = fixture.query(protocol="tongyi")
    assert result["status"] == "catalog_unavailable" and result["connection"] == "unknown"
    assert result["matched"] is None and result["generation_verified"] is False
    assert fixture.calls == []


def test_bounded_cleanup_does_not_hold_a_slow_supplier_forever(fixture, monkeypatch):
    from core import model_discovery
    monkeypatch.setattr(model_discovery, "MODEL_CHECK_TIMEOUT", 0.1)
    original = fixture.module._create_protocol
    def factory(*args, **kwargs):
        client = original(*args, **kwargs)
        async def close(): await asyncio.sleep(10)
        client.close = close
        return client
    monkeypatch.setattr(fixture.module, "_create_protocol", factory)
    result = fixture.query()
    assert result["matched"] is True and result["elapsed_ms"] < 300


def test_model_check_is_registered_as_get(monkeypatch):
    module = load_webapi()
    monkeypatch.setattr(module.plugin_updates, "bind_plugin_runtime", lambda plugin: None)
    registered = []
    plugin = types.SimpleNamespace(context=types.SimpleNamespace(register_web_api=lambda *args: registered.append(args)))
    assert module.register_web_apis(plugin) is True
    route = next(item for item in registered if item[0].endswith("/suppliers/model-check"))
    assert route[2] == ["GET"]



@pytest.mark.parametrize("payload", [None, {"unexpected": "object"}, {"error": {"message": "invalid"}}, {"error": "invalid", "models": []}, {"models": [{"unexpected": "record"}]}])
def test_gemini_unrecognized_response_never_claims_model_absent(fixture, payload):
    fixture.payloads["22"] = [payload]
    result = fixture.query(protocol="gemini", model="missing")
    assert result["status"] == "catalog_unavailable" and result["matched"] is None
    assert result["catalog_complete"] is False


def test_gemini_stops_at_page_limit(fixture):
    fixture.payloads["22"] = [{"models": [{"name": f"models/model-{n}"}], "nextPageToken": f"cursor-{n}"} for n in range(12)]
    result = fixture.query(protocol="gemini", model="missing")
    assert result["status"] == "catalog_unavailable" and result["matched"] is None
    assert len(fixture.calls) == 10 and result["catalog_complete"] is False


def test_actual_http_status_is_preserved(fixture):
    fixture.statuses["22"] = 201
    result = fixture.query()
    assert result["matched"] is True and result["http_status"] == 201


def test_old_inflight_success_cannot_repopulate_cache_after_newer_auth_failure(fixture, monkeypatch):
    from core.model_discovery import check_model
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        request_count = 0
        class Client:
            async def _request(self, *args, **kwargs):
                nonlocal request_count
                request_count += 1
                if request_count == 1:
                    started.set()
                    await release.wait()
                    return types.SimpleNamespace(data={"data": [{"id": "Fixture-Image"}]}, status=200, ensure_ok=lambda _: None)
                raise ProtocolError("fixture-denied", status=401)
            async def close(self): pass
        factory = lambda *args, **kwargs: Client()
        channel = fixture.cfg.build_channel(supplier="22")
        old = asyncio.create_task(check_model(fixture.cfg, channel, "Fixture-Image", "generate", factory=factory, refresh=True))
        await started.wait()
        fresh = await check_model(fixture.cfg, channel, "Fixture-Image", "generate", factory=factory, refresh=True)
        assert fresh["status"] == "auth_failed"
        release.set()
        assert (await old)["matched"] is True
        next_result = await check_model(fixture.cfg, channel, "Fixture-Image", "generate", factory=factory)
        assert next_result["status"] == "auth_failed" and next_result["cached"] is False
        assert request_count == 3
    asyncio.run(run())
