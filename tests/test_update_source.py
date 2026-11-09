"""固定 main 更新清单与同版本修订检测；全部网络响应来自测试替身。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import updates
from test_video_config import load_webapi

WEBAPI = load_webapi()
CHANGELOG = """# 更新日志

## v1.0.9 - 2026-11-08
旧版本说明。

## v1.0.10 - 2026-11-09
图片供应商与插件维护修复。

## v1.0.8 - 2026-09-27
历史版本说明。
"""


class UpdateSourceTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, current="1.0.10", current_build=2026110801, remote=None, changelog=CHANGELOG, error=None):
        manifest = remote if remote is not None else {"version": "1.0.10", "build": 2026110901}
        async def fetch(url):
            if error:
                raise error
            return json.dumps(manifest) if url == updates.MANIFEST_URL else changelog
        mocked = AsyncMock(side_effect=fetch)
        with patch.object(updates, "_read_local_release", return_value={"version": current, "build": current_build}), patch.object(WEBAPI, "_fetch_text", mocked), patch.object(WEBAPI, "json_response", lambda payload: payload):
            result = await WEBAPI._api_update_check(None)
        self.assertEqual(mocked.await_args_list[0].args, (updates.MANIFEST_URL,))
        return result

    async def test_source_constants_and_metadata_point_to_project(self):
        self.assertEqual(WEBAPI.DEFAULT_VERSION, "1.1.0")
        self.assertEqual(WEBAPI.UPDATE_BASE_URL, updates.REPOSITORY_URL)
        self.assertEqual(WEBAPI.UPDATE_MANIFEST_URL, updates.MANIFEST_URL)
        self.assertIn("repo: " + updates.REPOSITORY_URL, (ROOT / "metadata.yaml").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "update-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "1.1.0")
        self.assertEqual(manifest["build"], 2026111003)
        self.assertEqual(updates._read_local_release(), {"version": "1.1.0", "build": 2026111003})

    async def test_semantic_version_comparison_and_fixed_main_archive(self):
        result = await self.check("1.0.9", 9999999999)
        data = result["data"]
        self.assertTrue(result["ok"] and data["ok"] and data["update_available"])
        self.assertEqual(data["latest_version"], "1.0.10")
        self.assertEqual(data["download_url"], updates.REPOSITORY_URL + "/archive/refs/heads/main.zip")
        self.assertTrue(data["changelog"].startswith("## v1.0.10 - 2026-11-09"))
        self.assertNotIn("## v1.0.8", data["changelog"])

    async def test_same_version_new_build_is_an_update(self):
        data = (await self.check())["data"]
        self.assertTrue(data["update_available"])
        self.assertEqual(data["current_build"], 2026110801)
        self.assertEqual(data["target_build"], 2026110901)

    async def test_identical_build_is_latest(self):
        data = (await self.check(current_build=2026110901))["data"]
        self.assertFalse(data["update_available"])
        self.assertIn("当前已是最新版本", data["message"])

    async def test_older_remote_version_is_not_an_update(self):
        data = (await self.check(remote={"version": "1.0.9", "build": 9999999999}))["data"]
        self.assertFalse(data["update_available"])

    async def test_network_failure_has_nested_failure_and_no_download(self):
        result = await self.check(error=TimeoutError("private response content"))
        self.assertFalse(result["ok"] or result["data"]["ok"])
        self.assertFalse(result["data"]["update_available"])
        self.assertNotIn("download_url", result["data"])
        self.assertNotIn("private response content", str(result))

    async def test_invalid_manifest_cannot_offer_an_install(self):
        cases = [[], {"version": "oops", "build": 1}, {"version": "1.0.10", "build": True}, {"version": "1.0.10", "build": 0}, {"version": "1.0.10", "build": 1.5}, {"version": "1.0.10", "build": 2, "repository": "https://example.invalid/repo"}, {"version": "1.0.10", "build": 2, "branch": "other"}]
        for manifest in cases:
            with self.subTest(manifest=manifest):
                result = await self.check(remote=manifest)
                self.assertFalse(result["data"]["ok"])
                self.assertNotIn("download_url", result["data"])

    async def test_changelog_failure_does_not_lose_manifest_result(self):
        current = updates._read_local_release()
        newer = {**current, "build": current["build"] + 1}
        async def fetch(url):
            if url == updates.CHANGELOG_URL:
                raise TimeoutError()
            return json.dumps(newer)
        data = await updates.check_update(None, fetch)
        self.assertTrue(data["ok"] and data["update_available"])
        self.assertIn("日志暂时读取失败", data["changelog"])

    async def test_real_changelog_retains_old_versions(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        result = await self.check(changelog=changelog)
        self.assertIn("1.0.10", result["data"]["changelog"])
        self.assertNotIn("## v1.0.9", result["data"]["changelog"])
        self.assertIn("## v1.0.9 - 2026-11-08", changelog)
        self.assertIn("## v1.0.8 - 2026-09-27", changelog)

    async def test_runtime_version_is_a_snapshot_not_changed_files(self):
        plugin = types.SimpleNamespace()
        with patch.object(updates, "_read_local_release", return_value={"version": "1.0.10", "build": 5}):
            before = updates.runtime_info(plugin)
        with patch.object(updates, "_read_local_release", return_value={"version": "1.0.11", "build": 6}):
            self.assertEqual(updates.runtime_info(plugin), before)

    async def test_fetch_uses_bounded_read_and_current_user_agent(self):
        response = Mock()
        response.read.return_value = b"\xef\xbb\xbf# Changelog"
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch.object(WEBAPI, "urlopen", return_value=context) as request:
            result = await WEBAPI._fetch_text(updates.CHANGELOG_URL)
        self.assertEqual(result, "# Changelog")
        response.read.assert_called_once_with(512 * 1024 + 1)
        sent_request = request.call_args.args[0]
        self.assertEqual(sent_request.full_url, "https://api.github.com/repos/3121871622/gpthh/contents/CHANGELOG.md?ref=main")
        self.assertEqual(sent_request.get_header("Accept"), "application/vnd.github.raw+json")
        self.assertIsNone(sent_request.get_header("Authorization"))
        self.assertEqual(sent_request.get_header("User-agent"), "astrbot-plugin-gpt-image/1.1.0")
        self.assertEqual(request.call_args.kwargs["timeout"], 8)

    async def test_manifest_reads_fixed_official_api_without_credentials(self):
        response = Mock()
        response.read.return_value = b'{"version":"1.0.10","build":2}'
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch.object(WEBAPI, "urlopen", return_value=context) as request:
            content = await WEBAPI._fetch_text(updates.MANIFEST_URL)
        self.assertEqual(json.loads(content)["build"], 2)
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api.github.com/repos/3121871622/gpthh/contents/update-manifest.json?ref=main")
        self.assertEqual(sent.get_header("Accept"), "application/vnd.github.raw+json")
        self.assertIsNone(sent.get_header("Authorization"))

    async def test_fetch_rejects_other_sources_and_oversized_content(self):
        with self.assertRaises(ValueError):
            await WEBAPI._fetch_text("https://example.invalid/manifest.json")
        response = Mock()
        response.read.return_value = b"x" * (512 * 1024 + 1)
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch.object(WEBAPI, "urlopen", return_value=context), self.assertRaises(ValueError):
            await WEBAPI._fetch_text(updates.MANIFEST_URL)


if __name__ == "__main__":
    unittest.main()
