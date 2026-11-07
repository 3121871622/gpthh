"""供应商模型目录的隔离、刷新与宿主单层解包回归测试。"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import PluginConfig


def load_webapi():
    web = types.ModuleType("astrbot.api.web")
    web.error_response = lambda *args, **kwargs: (args, kwargs)
    web.json_response = lambda value: value
    web.request = types.SimpleNamespace(query={})
    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.web": web,
    }
    spec = importlib.util.spec_from_file_location("core._supplier_models_test", ROOT / "core/webapi.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


SUPPLIERS = [
    {"name": "One", "base_url": "https://one.invalid/v1", "api_key": "fixture-one"},
    {"name": "Two", "base_url": "https://two.invalid/v1", "api_key": "fixture-two"},
]


@pytest.fixture
def catalog(monkeypatch):
    module = load_webapi()
    cfg = PluginConfig({"suppliers": [dict(item) for item in SUPPLIERS], "active_supplier": "One"})
    fixture = types.SimpleNamespace(
        module=module, cfg=cfg, calls=[],
        models={"One": ["one-image-only"], "Two": ["two-image-only"]},
        errors={},
    )

    class Client:
        def __init__(self, channel):
            self.channel = channel

        async def list_models(self, **kwargs):
            channel = self.channel
            fixture.calls.append((channel.name, channel.base_url, channel.api_key, kwargs))
            if channel.name in fixture.errors:
                raise fixture.errors[channel.name]
            return fixture.models[channel.name]

        async def close(self):
            pass

    monkeypatch.setattr(module, "_create_protocol", lambda channel, *args, **kwargs: Client(channel))

    def query(supplier="Two", **params):
        module.request.query = dict(protocol="openai", **params)
        if supplier is not None:
            module.request.query["supplier"] = supplier
        return asyncio.run(module._api_list_models(types.SimpleNamespace(cfg=cfg)))

    fixture.query = query
    return fixture


def assert_result(response, supplier, models, ok):
    assert response["ok"] is ok
    data = response["data"]
    assert data["supplier"] == supplier
    assert data["models"] == models
    # 官方父页面会只传 response.data.data，成功标记必须同时存在于这一层。
    assert data["ok"] is ok
    return data


@pytest.mark.parametrize("params", [{}, {"include_all": "1"}, {"purpose": "text"}, {"purpose": "vision"}])
def test_explicit_supplier_uses_its_own_connection_and_directory(catalog, params):
    for name in ("One", "Two", "One", "Two"):
        assert_result(catalog.query(name, **params), name, catalog.models[name], True)
    assert [(item[0], item[1], item[2]) for item in catalog.calls] == [
        (item["name"], item["base_url"], item["api_key"]) for item in SUPPLIERS
    ]
    assert catalog.cfg.active_supplier_name() == "One"


@pytest.mark.parametrize("field,new_value", [("base_url", "https://moved.invalid/v1"), ("api_key", "fixture-rotated")])
def test_changed_connection_invalidates_only_its_catalog(catalog, field, new_value):
    assert_result(catalog.query("One"), "One", ["one-image-only"], True)
    assert_result(catalog.query("Two"), "Two", ["two-image-only"], True)
    suppliers = [dict(item) for item in SUPPLIERS]
    suppliers[1][field] = new_value
    assert catalog.cfg.replace_suppliers(suppliers)
    catalog.models["Two"] = ["two-new-connection"]
    assert_result(catalog.query("Two"), "Two", ["two-new-connection"], True)
    assert_result(catalog.query("One"), "One", ["one-image-only"], True)
    assert [item[0] for item in catalog.calls] == ["One", "Two", "Two"]


def test_refresh_replaces_removed_models_instead_of_merging_old_cache(catalog):
    catalog.models["Two"] = ["two-obsolete", "two-kept"]
    assert_result(catalog.query(), "Two", catalog.models["Two"], True)
    catalog.models["Two"] = ["two-kept", "two-new"]
    result = catalog.query(refresh="1")
    assert_result(result, "Two", ["two-kept", "two-new"], True)
    assert result["data"]["cached"] is False
    result = catalog.query()
    assert_result(result, "Two", ["two-kept", "two-new"], True)
    assert result["data"]["cached"] is True
    assert len(catalog.calls) == 2


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("cache_present", [False, True])
def test_empty_or_failed_directory_never_uses_other_supplier_or_global_models(catalog, failed, cache_present):
    catalog.query("One")
    if cache_present:
        catalog.query("Two")
    if failed:
        catalog.errors["Two"] = RuntimeError("fixture directory failed")
    else:
        catalog.models["Two"] = []
    result = catalog.query("Two", refresh="1")
    expected = ["two-image-only"] if cache_present else []
    data = assert_result(result, "Two", expected, False)
    assert data["cached"] is cache_present
    assert data["message"]


@pytest.mark.parametrize("name", ["Tw", "Two renamed", "2", "Missing"])
def test_unknown_name_does_not_fuzzily_select_another_supplier(catalog, name):
    data = assert_result(catalog.query(name), name, [], False)
    assert "供应商不存在" in data["message"]
    assert catalog.calls == []


def test_numeric_supplier_name_is_not_treated_as_list_position(catalog):
    suppliers = [dict(SUPPLIERS[0]), dict(SUPPLIERS[1], name="1")]
    assert catalog.cfg.replace_suppliers(suppliers)
    catalog.models["1"] = ["numeric-name-image"]
    assert_result(catalog.query("1"), "1", ["numeric-name-image"], True)
    assert catalog.calls[0][1:3] == (SUPPLIERS[1]["base_url"], SUPPLIERS[1]["api_key"])


def test_omitted_supplier_uses_active_supplier(catalog):
    assert_result(catalog.query(None), "One", ["one-image-only"], True)
    assert catalog.calls[0][0] == "One"


@pytest.mark.parametrize("params", [{"purpose": "not-supported"}, {"protocol": "not-supported"}])
def test_validation_errors_keep_ok_after_bridge_unwrap(catalog, params):
    catalog.module.request.query = dict(supplier="Two", **params)
    response = asyncio.run(catalog.module._api_list_models(types.SimpleNamespace(cfg=catalog.cfg)))
    assert_result(response, "Two", [], False)
    assert catalog.calls == []
