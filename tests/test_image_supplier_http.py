"""真实配置、会话选择与图片协议的 localhost 回归，不调用外部供应商。"""

from __future__ import annotations

import ast
import asyncio
import base64
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import re
import sys
import time
import types
from unittest.mock import AsyncMock, Mock

from aiohttp import web
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.commands import KIND_DRAW, KIND_EDIT, ParsedCommand, render_template
from core.config import PluginConfig
from core.images import _cfg_flag, extract_image_urls
from core.models import GenerateRequest, ImageInput, ProtocolError
from core.protocols import create_protocol
from core.state import SessionState
from core.video_progress import VideoImageConfig


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6Os8AAAAASUVORK5CYII="
)
PROMPT = "local regression image"


@asynccontextmanager
async def local_suppliers():
    calls = []

    async def receive(request):
        supplier = request.match_info["supplier"]
        kind = request.match_info["kind"]
        assert request.headers.get("Authorization") == "Bearer fixture-" + supplier
        image_fields = []
        if kind == "generations":
            body = await request.json()
            assert not {"images", "image", "image[]"}.intersection(body)
        else:
            body = {}
            reader = await request.multipart()
            async for part in reader:
                if part.filename:
                    image_fields.append(part.name)
                    assert bytes(await part.read()) == PNG
                    assert part.headers.get("Content-Type") == "image/png"
                else:
                    body[part.name] = await part.text()
            assert image_fields and set(image_fields) == {"image[]"}
            assert not {"images", "image"}.intersection(body)
        assert body["model"] == ("fixture-edit" if kind == "edits" else "fixture-text")
        assert body["prompt"] == PROMPT
        calls.append({
            "supplier": supplier,
            "path": request.path,
            "image_count": len(image_fields),
            "model": body["model"],
        })
        return web.json_response({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    app = web.Application()
    app.router.add_post("/{supplier}/v1/images/{kind:generations|edits}", receive)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        cfg = PluginConfig({
            "suppliers": [
                {"name": name, "base_url": f"http://127.0.0.1:{port}/{name}/v1", "api_key": "fixture-" + name}
                for name in ("first", "second")
            ],
            "active_supplier": "first",
            "active_protocol": "openai",
        })
        cfg.set_model_for("openai", "fixture-text")
        cfg.set_edit_model_for("openai", "fixture-edit")
        state = SessionState(types.SimpleNamespace(
            get_kv_data=AsyncMock(return_value={}), put_kv_data=AsyncMock(),
        ))
        yield cfg, state, calls
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("supplier", ["first", "second"])
@pytest.mark.parametrize("image_count", [0, 1, 2])
def test_session_selected_supplier_image_http(supplier, image_count):
    async def run():
        async with local_suppliers() as (cfg, state, calls):
            await state.set("image-session", "supplier", supplier)
            resolved = await state.resolved("image-session", cfg)
            assert resolved["supplier"] == supplier
            assert cfg.active_supplier_name() == "first"
            client = create_protocol(resolved["channel"], timeout=5)
            try:
                result = await client.generate(GenerateRequest(
                    prompt=PROMPT,
                    images=[ImageInput(data=PNG) for _ in range(image_count)],
                    size="1024x1024",
                ))
            finally:
                await client.close()
            assert result.count == 1 and result.images[0].data == PNG
            expected_model = "fixture-edit" if image_count else "fixture-text"
            assert result.model == expected_model
            assert calls == [{
                "supplier": supplier,
                "path": f"/{supplier}/v1/images/" + ("edits" if image_count else "generations"),
                "image_count": image_count,
                "model": expected_model,
            }]

    asyncio.run(run())


def test_real_config_keeps_video_strict_collection_separate():
    cfg = PluginConfig({})
    assert _cfg_flag(cfg, "strict_input_images", False) is False
    assert _cfg_flag(VideoImageConfig(cfg), "strict_input_images", False) is True


def load_drawing_methods():
    """与现有视频入口测试一致，仅去掉 AstrBot 启动依赖，保留被测函数体。"""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Main")
    names = {"_draw_impl", "_state_view", "_parse_batch", "_split_prompts"}
    body = [node for node in original.body if
            (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names) or
            (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_BATCH_RE" for target in node.targets))]
    harness = ast.ClassDef(name="DrawingHarness", bases=[], keywords=[], decorator_list=[], body=body)
    constants = {"_MENTION_RE", "_SPACE_RE", "_REQUIRED_CHANNEL_HINT", "_SETTINGS_PATH"}
    globals_ast = [node for node in tree.body if
                   (isinstance(node, ast.FunctionDef) and node.name in {"_clean_prompt", "_channel_ready", "_auth_hint"}) or
                   (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets))]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + globals_ast + [harness], type_ignores=[])
    namespace = dict(
        asyncio=asyncio, time=time, re=re, logger=logging.getLogger("test-image-http"),
        _text_of=lambda value: "" if value is None else str(value), _should_retry=lambda error: False,
        _COOLDOWN_HINT="冷却 {seconds} 秒", KIND_DRAW=KIND_DRAW, KIND_EDIT=KIND_EDIT,
        ProtocolError=ProtocolError, GenerateRequest=GenerateRequest,
        create_protocol=create_protocol, extract_image_urls=extract_image_urls,
        render_template=render_template,
    )
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["DrawingHarness"], namespace


@pytest.mark.parametrize("image_count", [0, 1, 2])
def test_drawing_entry_uses_second_session_supplier(image_count):
    async def run():
        async with local_suppliers() as (cfg, state, calls):
            await state.set("image-session", "supplier", "second")
            harness, namespace = load_drawing_methods()
            namespace["collect_input_images"] = AsyncMock(return_value=[ImageInput(data=PNG) for _ in range(image_count)])
            namespace["send_result"] = AsyncMock(return_value=True)
            plugin = harness()
            plugin.cfg, plugin.state, plugin.stats, plugin.fun = cfg, state, Mock(), Mock()
            plugin._last_requests = {}
            plugin._cooldown_left = lambda event: 0
            plugin._mark_cooldown = lambda event: None
            plugin._resolve_size = lambda info, prompt: ("1024x1024", prompt, "")
            plugin._protocol_of = lambda info: info["protocol"]
            plugin._template_vars = lambda event, info, **values: values
            plugin._acquire = lambda: asyncio.Semaphore(1)
            plugin._retry_times = lambda: 0
            plugin._flavor_line = lambda: ""
            plugin._reply = AsyncMock()
            plugin._maybe_translate = AsyncMock(side_effect=lambda channel, prompt, is_edit: prompt)
            event = types.SimpleNamespace(
                unified_msg_origin="image-session", send=AsyncMock(), plain_result=lambda text: text,
                get_group_id=lambda: "", get_platform_id=lambda: "",
                get_sender_id=lambda: "test-user", get_sender_name=lambda: "测试用户",
            )
            parsed = ParsedCommand(kind=KIND_EDIT if image_count else KIND_DRAW, arg=PROMPT)
            await plugin._draw_impl(event, parsed, "image-http-task")
            assert calls == [{
                "supplier": "second",
                "path": "/second/v1/images/" + ("edits" if image_count else "generations"),
                "image_count": image_count,
                "model": "fixture-edit" if image_count else "fixture-text",
            }]
            namespace["send_result"].assert_awaited_once()
            delivered = namespace["send_result"].await_args.args[1]
            assert delivered.count == 1 and delivered.images[0].data == PNG
            plugin.stats.record_success.assert_called_once()
            plugin.stats.record_failure.assert_not_called()
            plugin._reply.assert_not_awaited()
            if image_count:
                namespace["collect_input_images"].assert_awaited_once_with(event, cfg, include_mentions=True)
            else:
                namespace["collect_input_images"].assert_not_awaited()

    asyncio.run(run())
