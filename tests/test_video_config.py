"""视频配置、WebAPI 与旧菜单迁移的离线测试，仅使用 Python 标准库。"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import (
    DEFAULT_MENU_SECTIONS, DEFAULT_VIDEO_START_PROMPT,
    DEFAULT_VIDEO_PROGRESS_PROMPT, DEFAULT_VIDEO_DONE_PROMPT, PluginConfig,
)


def load_webapi():
    """只隔离 AstrBot Web 桥接层，运行真实配置校验与保存代码。"""
    web = types.ModuleType("astrbot.api.web")
    web.error_response = lambda *args, **kwargs: (args, kwargs)
    web.json_response = lambda *args, **kwargs: (args, kwargs)
    web.request = types.SimpleNamespace()
    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.web": web,
    }
    spec = importlib.util.spec_from_file_location("core._video_config_webapi_test", ROOT / "core/webapi.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


WEBAPI = load_webapi()
DEFAULTS = {
    "video_enabled": True,
    "video_commands": ["生成视频", "视频生成", "文生视频", "图生视频"],
    "video_model": "",
    "video_supplier": "",
    "video_api_mode": "legacy",
    "video_poll_interval": 5,
    "video_progress_interval": 10,
    "video_duration_timeout": 120,
    "video_start_prompt": DEFAULT_VIDEO_START_PROMPT,
    "video_progress_prompt": DEFAULT_VIDEO_PROGRESS_PROMPT,
    "video_done_prompt": DEFAULT_VIDEO_DONE_PROMPT,
    "video_timeout": 900,
    "video_max_concurrent": 2,
}
LEGACY_MENU = [
    {"name": "绘画与编辑", "items": ["绘画", "图片编辑", "重画", "抽卡", "预设列表", "图片转提示词"]},
    {"name": "模型与接口", "items": ["模型列表", "切换模型", "协议列表", "切换协议", "供应商列表", "切换供应商"]},
    {"name": "尺寸设置", "items": ["尺寸列表", "切换尺寸", "添加尺寸", "删除尺寸"]},
    {"name": "群与权限", "items": ["群列表", "群开关", "添加主人", "删除主人"]},
    {"name": "娱乐与统计", "items": ["排行", "运行统计", "我的ID"]},
    {"name": "插件维护", "items": ["菜单列表", "设置菜单", "删除菜单", "添加预设", "删除预设", "重载配置", "重置设置"]},
]


class VideoConfigTests(unittest.TestCase):
    def test_old_config_and_schema_have_consistent_defaults(self):
        cfg = PluginConfig({})
        public = cfg.to_public_dict()
        self.assertEqual(cfg.timeout(), 600)
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key, expected in DEFAULTS.items():
            with self.subTest(key=key):
                self.assertEqual(getattr(cfg, key)(), expected)
                self.assertEqual(public[key], expected)
                self.assertEqual(schema[key]["default"], expected)
        self.assertEqual(schema["menu_sections"]["default"], DEFAULT_MENU_SECTIONS)

    def test_nested_video_config_and_commands(self):
        video = {
            "video_enabled": False, "video_model": "nested-model",
            "video_supplier": "专用视频", "video_api_mode": "openai",
            "video_poll_interval": 7, "video_timeout": 120, "video_max_concurrent": 3,
            "video_progress_interval": 15, "video_duration_timeout": 180,
        }
        cfg = PluginConfig({"nested": {"video": video, "commands": {"video_commands": ["制作视频"]}}})
        for key, expected in video.items():
            with self.subTest(key=key):
                self.assertEqual(getattr(cfg, key)(), expected)
        self.assertEqual(cfg.video_commands(), ["制作视频"])

    def test_webapi_round_trip_preserves_image_settings_and_hides_key(self):
        raw = {"timeout": 240, "image_sizes": ["1024x1024"], "quality": "high"}
        cfg = PluginConfig(raw)
        image_model = cfg.model_for("openai")
        test_key = "placeholder-video-test-key"
        changes = {
            "video_enabled": False, "video_commands": ["制作视频", "短片"],
            "video_model": "fixture-video-model", "video_supplier": "视频测试站",
            "video_api_mode": "openai", "video_poll_interval": 6,
            "video_timeout": 900, "video_max_concurrent": 3,
            "video_progress_interval": 20, "video_duration_timeout": 180,
            "video_start_prompt": "开始 {model} {seconds} {task_id}",
            "video_progress_prompt": "进度 {progress_type}{progress}% {elapsed} {status}",
            "video_done_prompt": "完成 {progress}% {prompt}",
        }
        payload = dict(changes, suppliers=[{"name": "视频测试站", "base_url": "https://example.invalid/v1", "api_key": test_key}])
        original = copy.deepcopy(raw)
        plan = WEBAPI._build_plan(cfg, payload)
        self.assertEqual(raw, original)
        WEBAPI._apply_plan(cfg, plan)
        cfg.reload()
        public = cfg.to_public_dict()
        for key, expected in changes.items():
            with self.subTest(key=key):
                self.assertEqual(getattr(cfg, key)(), expected)
                self.assertEqual(public[key], expected)
        self.assertEqual(cfg.timeout(), 240)
        self.assertEqual(cfg.model_for("openai"), image_model)
        self.assertEqual(cfg.quality(), "high")
        self.assertNotIn(test_key, json.dumps(public, ensure_ascii=False))

    def test_webapi_rejects_invalid_video_inputs_without_mutation(self):
        cases = [
            {"video_api_mode": "other"}, {"video_model": 123},
            {"video_supplier": "不存在"}, {"video_commands": []},
            {"video_enabled": "false"}, {"video_poll_interval": 1},
            {"video_poll_interval": 61}, {"video_poll_interval": 2.5},
            {"video_timeout": 29}, {"video_timeout": 3601},
            {"video_timeout": 30.5}, {"video_max_concurrent": 0},
            {"video_max_concurrent": 9},
            {"video_progress_interval": 4}, {"video_progress_interval": 61},
            {"video_progress_interval": 5.5}, {"video_duration_timeout": 29},
            {"video_duration_timeout": 601}, {"video_duration_timeout": 30.5},
            {"video_start_prompt": False}, {"video_progress_prompt": []},
            {"video_done_prompt": 123},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                raw = {}
                cfg = PluginConfig(raw)
                with self.assertRaises(WEBAPI._ValidationError):
                    WEBAPI._build_plan(cfg, payload)
                self.assertEqual(raw, {})

    def test_getters_clamp_numeric_ranges_and_normalize_mode(self):
        for key, low, high in [("video_poll_interval", 2, 60), ("video_progress_interval", 5, 60), ("video_duration_timeout", 30, 600), ("video_timeout", 30, 3600), ("video_max_concurrent", 1, 8)]:
            with self.subTest(key=key):
                self.assertEqual(getattr(PluginConfig({key: -1}), key)(), low)
                self.assertEqual(getattr(PluginConfig({key: 99999}), key)(), high)
                self.assertEqual(getattr(PluginConfig({key: "invalid"}), key)(), DEFAULTS[key])
        self.assertEqual(PluginConfig({"video_api_mode": "unknown"}).video_api_mode(), "legacy")
        self.assertEqual(PluginConfig({"video_api_mode": " OpenAI "}).video_api_mode(), "openai")

    def test_blank_video_model_does_not_block_other_settings(self):
        raw = {"quality": "low"}
        cfg = PluginConfig(raw)
        plan = WEBAPI._build_plan(cfg, {"video_enabled": True, "video_model": "  ", "quality": "high"})
        WEBAPI._apply_plan(cfg, plan)
        cfg.reload()
        self.assertTrue(cfg.video_enabled())
        self.assertEqual(cfg.video_model(), "")
        self.assertEqual(cfg.quality(), "high")

    def test_old_default_aliases_gain_new_commands_but_custom_values_stay(self):
        self.assertEqual(PluginConfig({"video_commands": ["生成视频", "视频生成"]}).video_commands(), DEFAULTS["video_commands"])
        self.assertEqual(PluginConfig({"video_commands": ["制作视频"]}).video_commands(), ["制作视频"])

    def test_video_templates_support_nested_storage_and_blank_fallback(self):
        keys = ["video_start_prompt", "video_progress_prompt", "video_done_prompt"]
        messages = {key: "自定义 {model} {seconds} {task_id}" for key in keys}
        cfg = PluginConfig({"nested": {"messages": messages}})
        for key in keys:
            with self.subTest(key=key):
                self.assertEqual(getattr(cfg, key)(), messages[key])
                self.assertEqual(getattr(PluginConfig({key: "  "}), key)(), DEFAULTS[key])
        variables = dict(prompt="云缓慢移动", model="fixture-video-model", seconds=10, elapsed=0, progress=1, progress_type="估算进度 ", status="queued", task_id="fixture-task")
        start = DEFAULT_VIDEO_START_PROMPT.format_map(variables)
        self.assertIn("1%", start)
        self.assertIn("估算仅供参考，不代表剩余时间", start)
        done = DEFAULT_VIDEO_DONE_PROMPT.format_map(dict(variables, progress=100))
        self.assertIn("100%", done)

    def test_webapi_public_factory_defaults_include_video_templates(self):
        plugin = types.SimpleNamespace(cfg=PluginConfig({}))
        args, kwargs = asyncio.run(WEBAPI._api_get_config(plugin))
        data = args[0]["data"]
        for key in ["video_start_prompt", "video_progress_prompt", "video_done_prompt"]:
            self.assertEqual(data["defaults"][key], DEFAULTS[key])
        self.assertEqual(data["video_model"], "")

    def test_factory_menu_migration_adds_video(self):
        for raw in [{}, {"menu_sections": copy.deepcopy(LEGACY_MENU)}, {"nested": {"menus": {"menu_sections": copy.deepcopy(LEGACY_MENU)}}}]:
            with self.subTest(raw=raw):
                menu = PluginConfig(raw).menu_sections()
                self.assertEqual(menu, DEFAULT_MENU_SECTIONS)
                self.assertIn("生成视频", menu[0]["items"])

    def test_menu_migration_preserves_custom_and_empty_menus(self):
        edited = copy.deepcopy(LEGACY_MENU)
        edited[0]["items"].append("自定义指令")
        extended = copy.deepcopy(LEGACY_MENU) + [{"name": "我的菜单", "items": ["hello"]}]
        for menu in [[], [{"name": "个人菜单", "items": ["绘画"]}], edited, extended]:
            with self.subTest(menu=menu):
                cfg = PluginConfig({"menu_sections": copy.deepcopy(menu)})
                self.assertEqual(cfg.menu_sections(), menu)


if __name__ == "__main__":
    unittest.main()
