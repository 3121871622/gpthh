"""插件更新来源回归测试；隔离 AstrBot 宿主，不请求远程服务。"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_video_config import load_webapi

WEBAPI = load_webapi()
REPO_URL = "https://github.com/3121871622/gpthh"
CHANGELOG_URL = "https://raw.githubusercontent.com/3121871622/gpthh/main/CHANGELOG.md"
CHANGELOG = """# 更新日志

## v1.0.9 - 2026-11-08
旧版本说明。

## v1.0.10 - 2026-11-08
独立视频入口与更新来源修复。

## v1.0.8 - 2026-09-27
历史版本说明。
"""


class UpdateSourceTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, current, changelog=CHANGELOG, error=None):
        fetch = AsyncMock(return_value=changelog, side_effect=error)
        with patch.object(WEBAPI, "_read_version", return_value=current), \
             patch.object(WEBAPI, "_fetch_text", fetch), \
             patch.object(WEBAPI, "json_response", lambda payload: payload):
            result = await WEBAPI._api_update_check(None)
        fetch.assert_awaited_once_with(CHANGELOG_URL)
        return result

    async def test_source_constants_and_metadata_point_to_project(self):
        self.assertEqual(WEBAPI.DEFAULT_VERSION, "1.0.10")
        self.assertEqual(WEBAPI.UPDATE_BASE_URL, REPO_URL)
        self.assertEqual(WEBAPI.UPDATE_CHANGELOG_URL, CHANGELOG_URL)
        self.assertIn("repo: " + REPO_URL, (ROOT / "metadata.yaml").read_text(encoding="utf-8"))
        self.assertEqual(WEBAPI._read_version(), "1.0.10")

    async def test_semantic_109_to_1010_and_versioned_archive_url(self):
        result = await self.check("1.0.9")
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertTrue(data["update_available"])
        self.assertEqual(data["latest_version"], "1.0.10")
        self.assertEqual(data["download_url"], REPO_URL + "/archive/refs/tags/v1.0.10.zip")
        self.assertEqual(data["changelog_url"], CHANGELOG_URL)
        self.assertTrue(data["changelog"].startswith("## v1.0.10 - 2026-11-08"))
        self.assertNotIn("## v1.0.8", data["changelog"])
        self.assertNotIn("docs.317ak.com", str(result))

    async def test_current_version_is_already_latest(self):
        result = await self.check("1.0.10")
        data = result["data"]
        self.assertFalse(data["update_available"])
        self.assertEqual(data["current_version"], "1.0.10")
        self.assertEqual(data["latest_version"], "1.0.10")
        self.assertIn("当前已是最新版本", data["message"])
        self.assertEqual(data["download_url"], REPO_URL + "/archive/refs/tags/v1.0.10.zip")

    async def test_failure_does_not_offer_old_download(self):
        result = await self.check("1.0.10", error=TimeoutError("read timeout"))
        self.assertFalse(result["ok"])
        data = result["data"]
        self.assertFalse(data["update_available"])
        self.assertEqual(data["latest_version"], "1.0.10")
        self.assertEqual(data["error"], "read timeout")
        self.assertNotIn("download_url", data)
        self.assertEqual(data["changelog_url"], CHANGELOG_URL)

    async def test_real_local_changelog_headings_and_retained_history(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        result = await self.check("1.0.9", text)
        self.assertEqual(result["data"]["latest_version"], "1.0.10")
        self.assertIn("独立「视频生成」分区", result["data"]["changelog"])
        self.assertIn("main/CHANGELOG.md", result["data"]["changelog"])
        self.assertNotIn("## v1.0.9", result["data"]["changelog"])
        self.assertIn("## v1.0.9 - 2026-11-08", text)
        self.assertIn("## v1.0.8 - 2026-09-27", text)

    async def test_no_version_heading_retains_current_version(self):
        result = await self.check("1.0.10", "暂时没有版本标题")
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["update_available"])
        self.assertEqual(result["data"]["latest_version"], "1.0.10")
        self.assertEqual(result["data"]["download_url"], REPO_URL + "/archive/refs/tags/v1.0.10.zip")

    async def test_fetch_user_agent_uses_current_default_version(self):
        response = Mock()
        response.read.return_value = b"\xef\xbb\xbf# Changelog"
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with patch.object(WEBAPI, "urlopen", return_value=context) as request:
            result = await WEBAPI._fetch_text(CHANGELOG_URL)
        self.assertEqual(result, "# Changelog")
        request.assert_called_once()
        sent_request = request.call_args.args[0]
        self.assertEqual(sent_request.full_url, CHANGELOG_URL)
        self.assertEqual(sent_request.get_header("User-agent"), "astrbot-plugin-gpt-image/" + WEBAPI.DEFAULT_VERSION)
        self.assertEqual(request.call_args.kwargs["timeout"], 8)


if __name__ == "__main__":
    unittest.main()
