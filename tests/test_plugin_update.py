"""官方管理器契约的隔离回归测试；不下载仓库，不安装/重载真实 AstrBot。

fixture 对照 AstrBotDevs/AstrBot 提交 6b10af75e118714335aff3aab047bf7689fa1388：
star_manager.py 的 update_plugin(1888-1928)、reload(1018-1074)。
官方 update_plugin 返回 None，内部调用 reload，并忽略其返回二元组。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import updates
from core.config import PluginConfig
from test_video_config import load_webapi

WEBAPI = load_webapi()


class OfficialManagerFixture:
    """保留官方更新→重载、名称匹配、返回值语义，替换真正磁盘操作。"""

    def __init__(self, context, make_plugin):
        self.context = context
        self.make_plugin = make_plugin
        self.calls = []
        self.reload_calls = []
        self.reload_all_calls = 0
        self.reload_success = True
        self.replace_instance = True
        self.new_ready = True
        self.new_build = None
        self.fail = None
        self.block = None
        self.entered = asyncio.Event()
        self.cancelled = False

    async def update_plugin(self, plugin_name: str, proxy="", download_url: str = "", repo_url: str = "") -> None:
        self.calls.append((plugin_name, proxy, download_url, repo_url))
        plugin = self.context.get_registered_star(plugin_name)
        if plugin is None or plugin.reserved:
            raise RuntimeError("plugin not updateable")
        self.entered.set()
        if self.fail:
            raise self.fail
        if self.block is not None:
            try:
                await self.block.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        # 官方更新方法将仓库文件替换完成后，调用 reload，且不消费返回值。
        self.context.disk_release = dict(self.context.remote_release)
        await self.reload(plugin_name)

    async def reload(self, specified_plugin_name=None):
        self.reload_calls.append(specified_plugin_name)
        metadata = self.context.get_registered_star(specified_plugin_name)
        if metadata is None:
            self.reload_all_calls += 1
            return True, None
        if not self.reload_success:
            return False, "fixture initialize failed"
        if self.replace_instance:
            instance = self.make_plugin(self.new_ready)
            if self.new_build is not None:
                getattr(instance, updates.RUNTIME_ATTRIBUTE)["build"] = self.new_build
            self.context.metadata = types.SimpleNamespace(**{**vars(metadata), "star_cls": instance})
        return True, None


class PluginUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = types.SimpleNamespace(disk_release={"version": "1.0.10", "build": 2026110801}, remote_release={"version": "1.0.10", "build": 2026110901})
        self.local_patch = patch.object(updates, "_read_local_release", side_effect=lambda: dict(self.context.disk_release))
        self.local_patch.start()
        self.timing_patch = patch.multiple(updates, START_DELAY=0, VERIFY_TIMEOUT=0.02, VERIFY_INTERVAL=0.001, OPERATION_TIMEOUT=0.1)
        self.timing_patch.start()
        self.plugin = self.make_plugin()
        self.context.metadata = types.SimpleNamespace(name="registered_plugin_name", root_dir_name="different_install_folder", star_cls=self.plugin, activated=True, reserved=False, repo=updates.REPOSITORY_URL)
        self.context.get_all_stars = lambda: [self.context.metadata] if self.context.metadata else []
        self.context.get_registered_star = lambda name: self.context.metadata if self.context.metadata and self.context.metadata.name == name else None
        self.manager = OfficialManagerFixture(self.context, self.make_plugin)
        self.context._star_manager = self.manager
        self.fetch = AsyncMock(side_effect=self.fetch_source)

    def make_plugin(self, ready=True):
        plugin = types.SimpleNamespace(context=self.context, _image_tasks=set(), _video_tasks=set(), _video_pending=set(), _video_inputs={}, logger=Mock(), cfg=PluginConfig({}), stats=types.SimpleNamespace(snapshot=lambda: {"totals": {"in_progress": 0}}))
        updates.bind_plugin_runtime(plugin)
        if ready:
            updates.mark_plugin_ready(plugin)
        return plugin

    async def fetch_source(self, url):
        if url == updates.MANIFEST_URL:
            return json.dumps(self.context.remote_release)
        self.assertEqual(url, updates.CHANGELOG_URL)
        return "## v1.0.10 - 2026-11-09\n修复。"

    async def asyncTearDown(self):
        state = getattr(self.context, updates.STATE_ATTRIBUTE, {})
        if self.manager.block is not None:
            self.manager.block.set()
        tasks = [state.get(key) for key in ("task", "operation_task")]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        self.timing_patch.stop()
        self.local_patch.stop()

    async def complete(self, operation="install"):
        queued = updates.start_maintenance(self.plugin, operation, self.fetch)
        task = getattr(self.context, updates.STATE_ATTRIBUTE)["task"]
        await task
        return queued, updates.get_update_status(self.plugin, queued["job_id"])

    async def test_update_uses_registered_name_and_fixed_archive_then_verifies_new_instance(self):
        queued, done = await self.complete()
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(done["status"], "succeeded")
        self.assertTrue(done["ok"])
        self.assertEqual(done["current_build"], 2026110901)
        self.assertEqual(self.manager.calls, [("registered_plugin_name", "", updates.ARCHIVE_URL, updates.REPOSITORY_URL)])
        self.assertEqual(self.manager.reload_calls, ["registered_plugin_name"])
        self.assertEqual(self.manager.reload_all_calls, 0)
        self.assertNotEqual(done["instance_id"], updates.runtime_info(self.plugin)["instance_id"])
        self.assertFalse(self.plugin._video_tasks)

    async def test_start_returns_before_host_operation_and_rejects_duplicate(self):
        queued = updates.start_maintenance(self.plugin, "install", self.fetch)
        self.assertEqual(self.manager.calls, [])
        self.assertTrue(updates.maintenance_message(self.plugin))
        repeated = updates.start_maintenance(self.plugin, "reload", self.fetch)
        self.assertFalse(repeated["ok"])
        self.assertEqual(repeated["error_code"], "maintenance_busy")
        self.assertEqual(repeated["job_id"], queued["job_id"])
        await getattr(self.context, updates.STATE_ATTRIBUTE)["task"]

    async def test_install_rechecks_remote_instead_of_trusting_stale_check(self):
        await updates.check_update(self.plugin, self.fetch)
        self.context.remote_release = dict(self.context.disk_release)
        _, result = await self.complete()
        self.assertEqual(result["error_code"], "already_latest")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(sum(call.args == (updates.MANIFEST_URL,) for call in self.fetch.await_args_list), 2)

    async def test_no_registry_match_never_calls_reload_all(self):
        self.context.metadata.star_cls = object()
        with self.assertRaises(updates.MaintenanceError) as failure:
            updates.start_maintenance(self.plugin, "reload", self.fetch)
        self.assertEqual(failure.exception.code, "plugin_not_registered")
        self.assertFalse(self.manager.reload_calls)
        self.assertEqual(self.manager.reload_all_calls, 0)

    async def test_name_lookup_mismatch_never_calls_reload(self):
        self.context.get_registered_star = lambda name: None
        with self.assertRaises(updates.MaintenanceError):
            updates.start_maintenance(self.plugin, "reload", self.fetch)
        self.assertFalse(self.manager.reload_calls)

    async def test_wrong_repo_and_old_update_signature_fail_before_any_mutation(self):
        self.context.metadata.repo = "https://example.invalid/other"
        with self.assertRaises(updates.MaintenanceError) as failure:
            updates.start_maintenance(self.plugin, "install", self.fetch)
        self.assertEqual(failure.exception.code, "repository_mismatch")
        self.context.metadata.repo = updates.REPOSITORY_URL
        async def old_update(plugin_name, proxy=""):
            self.fail("old update must not execute")
        self.manager.update_plugin = old_update
        with self.assertRaises(updates.MaintenanceError) as failure:
            updates.start_maintenance(self.plugin, "install", self.fetch)
        self.assertEqual(failure.exception.code, "update_unsupported")

    async def test_unready_instance_cannot_start(self):
        getattr(self.plugin, updates.RUNTIME_ATTRIBUTE)["ready"] = False
        with self.assertRaises(updates.MaintenanceError) as failure:
            updates.start_maintenance(self.plugin, "reload", self.fetch)
        self.assertEqual(failure.exception.code, "plugin_not_ready")

    async def test_busy_generation_and_input_are_not_interrupted(self):
        pending_task = asyncio.create_task(asyncio.Event().wait())
        cases = [("_image_tasks", {pending_task}), ("_video_tasks", {pending_task}), ("_video_pending", {"session"}), ("_video_inputs", {"session": types.SimpleNamespace(expires_at=float("inf"))})]
        try:
            for attribute, value in cases:
                with self.subTest(attribute=attribute):
                    original = getattr(self.plugin, attribute)
                    setattr(self.plugin, attribute, value)
                    with self.assertRaises(updates.MaintenanceError) as failure:
                        updates.start_maintenance(self.plugin, "reload", self.fetch)
                    self.assertEqual(failure.exception.code, "generation_busy")
                    setattr(self.plugin, attribute, original)
            self.assertFalse(pending_task.cancelled())
        finally:
            pending_task.cancel()
            await asyncio.gather(pending_task, return_exceptions=True)
        self.assertFalse(self.manager.reload_calls)

    async def test_in_progress_stats_block_old_generation_tracking(self):
        self.plugin.stats.snapshot = lambda: {"totals": {"in_progress": 1}}
        with self.assertRaises(updates.MaintenanceError) as failure:
            updates.start_maintenance(self.plugin, "install", self.fetch)
        self.assertEqual(failure.exception.code, "generation_busy")

    async def test_state_is_visible_to_fresh_module_and_new_instance(self):
        queued, result = await self.complete()
        spec = importlib.util.spec_from_file_location("core._fresh_update_module", ROOT / "core/updates.py")
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        self.assertEqual(fresh.get_update_status(self.context.metadata.star_cls, queued["job_id"]), result)
        self.assertFalse(fresh.maintenance_message(self.context.metadata.star_cls))

    async def test_host_exception_is_logged_but_not_exposed(self):
        self.manager.fail = RuntimeError("private fixture detail")
        _, result = await self.complete()
        self.assertEqual(result["error_code"], "operation_failed")
        self.assertNotIn("private fixture detail", str(result))
        self.plugin.logger.error.assert_called_once()
        self.assertFalse(updates.maintenance_message(self.plugin))

    async def test_official_update_ignoring_false_reload_is_not_false_success(self):
        self.manager.reload_success = False
        _, result = await self.complete()
        self.assertEqual(result["error_code"], "reload_not_verified")
        self.assertFalse(result["ok"])

    async def test_reload_false_tuple_is_checked(self):
        self.manager.reload_success = False
        _, result = await self.complete("reload")
        self.assertEqual(result["error_code"], "reload_failed")

    async def test_new_instance_wrong_build_is_not_success(self):
        self.manager.new_build = 1
        _, result = await self.complete()
        self.assertEqual(result["error_code"], "version_not_loaded")

    async def test_new_instance_not_initialized_is_not_success(self):
        self.manager.new_ready = False
        _, result = await self.complete()
        self.assertEqual(result["error_code"], "reload_not_verified")

    async def test_same_instance_is_not_a_reload(self):
        self.manager.replace_instance = False
        _, result = await self.complete("reload")
        self.assertEqual(result["error_code"], "reload_not_verified")

    async def test_manual_reload_does_not_fetch_network(self):
        _, result = await self.complete("reload")
        self.assertTrue(result["ok"])
        self.fetch.assert_not_awaited()
        self.assertEqual(self.manager.reload_calls, ["registered_plugin_name"])

    async def test_slow_host_operation_is_not_cancelled_or_unlocked(self):
        self.manager.block = asyncio.Event()
        with patch.object(updates, "OPERATION_TIMEOUT", 0.005):
            queued = updates.start_maintenance(self.plugin, "install", self.fetch)
            worker = getattr(self.context, updates.STATE_ATTRIBUTE)["task"]
            await self.manager.entered.wait()
            await asyncio.sleep(0.02)
            status = updates.get_update_status(self.plugin, queued["job_id"])
            self.assertEqual(status["status"], "installing")
            self.assertTrue(status["slow"])
            self.assertTrue(updates.maintenance_message(self.plugin))
            self.assertFalse(self.manager.cancelled)
            self.manager.block.set()
            await worker
        self.assertEqual(updates.get_update_status(self.plugin, queued["job_id"])["status"], "succeeded")

    async def test_cancelled_worker_keeps_lock_until_host_really_finishes(self):
        self.manager.block = asyncio.Event()
        queued = updates.start_maintenance(self.plugin, "install", self.fetch)
        state = getattr(self.context, updates.STATE_ATTRIBUTE)
        worker = state["task"]
        await self.manager.entered.wait()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self.assertFalse(self.manager.cancelled)
        self.assertTrue(updates.maintenance_message(self.plugin))
        host_task = state["operation_task"]
        self.manager.block.set()
        await host_task
        await asyncio.sleep(0)
        self.assertFalse(updates.maintenance_message(self.context.metadata.star_cls))
        self.assertEqual(updates.get_update_status(self.plugin, queued["job_id"])["error_code"], "verification_interrupted")

    async def test_invalid_job_id_returns_explicit_nested_failure(self):
        result = updates.get_update_status(self.plugin, "missing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "not_found")

    async def test_mutating_api_does_not_accept_url_or_plugin_name(self):
        for payload in ({"repo": "https://example.invalid"}, {"name": "another_plugin"}, {"download_url": updates.ARCHIVE_URL}, []):
            with self.subTest(payload=payload), patch.object(WEBAPI, "request", types.SimpleNamespace(json=AsyncMock(return_value=payload))), patch.object(WEBAPI, "_host_error_response", lambda message, **kwargs: kwargs):
                result = await WEBAPI._api_update_install(self.plugin)
                self.assertFalse(result["data"]["ok"])
                self.assertEqual(result["data"]["error_code"], "invalid_request")
        self.assertFalse(self.manager.calls)

    async def test_saved_configuration_is_not_reported_as_plugin_reload(self):
        with patch.object(WEBAPI, "request", types.SimpleNamespace(json=AsyncMock(return_value={}))), patch.object(WEBAPI, "json_response", lambda payload: payload), patch.object(self.plugin.cfg, "save", return_value=True):
            result = await WEBAPI._api_save_config(self.plugin)
        self.assertTrue(result["data"]["ok"] and result["data"]["saved"])
        self.assertTrue(result["data"]["config_reloaded"])
        self.assertFalse(result["data"]["plugin_reloaded"])
        self.assertFalse(self.manager.reload_calls)

    async def test_save_error_keeps_nested_failure_after_bridge_unwrap(self):
        with patch.object(WEBAPI, "request", types.SimpleNamespace(json=AsyncMock(return_value={}))), patch.object(self.plugin.cfg, "save", return_value=False), patch.object(WEBAPI, "_host_error_response", lambda message, **kwargs: kwargs):
            result = await WEBAPI._api_save_config(self.plugin)
        self.assertFalse(result["data"]["ok"])
        self.assertEqual(result["status_code"], 400)

    async def test_new_routes_registered_and_success_data_contains_ok(self):
        registrations = []
        self.context.register_web_api = lambda *args: registrations.append(args)
        self.assertIs(WEBAPI.register_web_apis(self.plugin), True)
        names = {args[0].rsplit("/", 1)[-1]: args[2] for args in registrations}
        self.assertEqual(names["update-install"], ["POST"])
        self.assertEqual(names["plugin-reload"], ["POST"])
        self.assertEqual(names["update-status"], ["GET"])
        with patch.object(WEBAPI, "request", types.SimpleNamespace(json=AsyncMock(return_value={}), query={})), patch.object(WEBAPI, "json_response", lambda payload: payload), patch.object(WEBAPI, "_fetch_text", self.fetch):
            result = await WEBAPI._api_plugin_reload(self.plugin)
            self.assertTrue(result["ok"] and result["data"]["ok"])
            await getattr(self.context, updates.STATE_ATTRIBUTE)["task"]
            result = await WEBAPI._api_update_status(self.plugin)
            self.assertEqual(result["data"]["status"], "succeeded")

    async def test_registration_reports_missing_host_support(self):
        self.assertIs(WEBAPI.register_web_apis(self.plugin), False)

    async def test_registration_failure_does_not_return_success(self):
        self.context.register_web_api = Mock(side_effect=RuntimeError("fixture route registration failure"))
        with self.assertRaises(RuntimeError):
            WEBAPI.register_web_apis(self.plugin)


if __name__ == "__main__":
    unittest.main()
