"""供应商名称在设置保存、会话和图片通道中的端到端选路回归。"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import PluginConfig
from core.models import resolve_choice
from core.state import SessionState


class StoredConfig(dict):
    def save_config(self):
        self.persisted = copy.deepcopy(dict(self))


def make_config(names, active=None):
    raw = StoredConfig(
        suppliers=[
            {"name": name, "base_url": f"https://supplier-{index}.invalid/v1", "api_key": f"fixture-{index}"}
            for index, name in enumerate(names, 1)
        ],
        active_supplier=active or names[0],
    )
    return raw, PluginConfig(raw)


def load_webapi():
    web = types.ModuleType("astrbot.api.web")
    web.error_response = lambda *args, **kwargs: (args, kwargs)
    web.json_response = lambda value: value
    web.request = types.SimpleNamespace()
    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.web": web,
    }
    spec = importlib.util.spec_from_file_location("core._supplier_selection_test", ROOT / "core/webapi.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("names", [["One", "Two"], ["One", "1"], ["2", "1"], ["One", "22"], ["One", "0"], ["one", "One"]])
def test_web_save_reload_routes_images_to_exact_selected_supplier(names):
    raw, cfg = make_config(names)
    api = load_webapi()
    selected = names[1]
    plan = api._build_plan(cfg, {"active_supplier": selected})
    api._apply_plan(cfg, plan)
    assert cfg.save()
    cfg.reload()

    assert raw["active_supplier"] == selected
    assert cfg.to_public_dict()["active_supplier"] == selected
    channel = cfg.build_channel()
    assert channel is not None
    assert (channel.name, channel.base_url, channel.api_key) == (
        selected, "https://supplier-2.invalid/v1", "fixture-2",
    )
    restarted = PluginConfig(StoredConfig(raw.persisted))
    assert restarted.active_supplier_name() == selected
    assert restarted.build_channel().base_url == "https://supplier-2.invalid/v1"


@pytest.mark.parametrize("name", ["1", "22", "0"])
def test_stored_numeric_supplier_survives_config_initialization(name):
    _, cfg = make_config(["One", name], active=name)
    assert cfg.active_supplier_name() == name
    assert cfg.active_supplier().name == name
    assert cfg.build_channel().base_url == "https://supplier-2.invalid/v1"


@pytest.mark.parametrize("name", ["1", "22", "0"])
def test_session_override_keeps_exact_supplier_after_global_change(name):
    _, cfg = make_config(["One", name, "Three"])
    storage = {}

    class Store:
        async def get_kv_data(self, key, default):
            return copy.deepcopy(storage.get(key, default))

        async def put_kv_data(self, key, value):
            storage[key] = copy.deepcopy(value)

    async def run():
        state = SessionState(Store())
        await state.set("session", "supplier", name)
        assert cfg.set_active_supplier("Three")
        resolved = await SessionState(Store()).resolved("session", cfg)
        assert resolved["supplier"] == name
        assert resolved["supplier_index"] == 2
        assert resolved["channel"] is not None
        assert resolved["channel"].name == name
        assert resolved["channel"].base_url == "https://supplier-2.invalid/v1"
        assert cfg.active_supplier_name() == "Three"

    asyncio.run(run())


@pytest.mark.parametrize("name", ["1", "22", "0"])
def test_bot_override_routes_images_to_numeric_supplier(name):
    _, cfg = make_config(["One", name])
    assert cfg.set_bot_override("robot", "supplier", name)
    channel = cfg.channel_for("robot")
    assert channel is not None
    assert (channel.name, channel.base_url) == (name, "https://supplier-2.invalid/v1")
    assert cfg.build_channel().name == "One"


def test_chat_numeric_input_still_means_list_position_before_name_storage():
    _, cfg = make_config(["2", "1"])
    index, actual_name = resolve_choice("2", cfg.supplier_names())
    assert (index, actual_name) == (1, "1")
    assert cfg.set_active_supplier(actual_name)
    assert cfg.active_supplier_name() == "1"
    assert cfg.build_channel().base_url == "https://supplier-2.invalid/v1"


def test_legacy_lookup_forms_remain_available_when_no_exact_name_exists():
    _, cfg = make_config(["One supplier", "Two supplier"])
    for target in ("2", "two SUPPLIER", "Two"):
        assert cfg.get_supplier(target).name == "Two supplier"
    assert cfg.get_supplier("missing") is None
    assert cfg.get_supplier("") is None
