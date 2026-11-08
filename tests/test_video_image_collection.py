"""视频严格采图回归：运行真实采集器，只隔离素材读取和宿主消息组件。"""
import asyncio
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

from core import images
from core.models import ImageInput, ProtocolError
from core.video_progress import VideoImageConfig
import test_video_commands as video


class Image:
    def __init__(self, url):
        self.url = url


class Reply:
    def __init__(self, chain=(), id="12"):
        self.chain, self.id = list(chain), id


GOOD = "https://fixture.invalid/good.png"
BAD = "https://fixture.invalid/bad.png"
SECOND = "https://fixture.invalid/second.png"


def fixture(monkeypatch, source, *, broken=True):
    monkeypatch.setattr(images, "Comp", types.SimpleNamespace(Image=Image, Reply=Reply))
    parser = types.ModuleType("astrbot.core.utils.quoted_message_parser")
    parser.extract_quoted_message_images = AsyncMock(return_value=[])
    monkeypatch.setitem(sys.modules, "astrbot.core.utils.quoted_message_parser", parser)
    refs = [GOOD, BAD if broken else SECOND]
    event = video.FakeEvent(message="图生视频 镜头推进")
    components = []
    if source == "url":
        event.message += " " + " ".join(refs)
    elif source == "attachment":
        components = [Image(url) for url in refs]
    elif source == "reply_chain":
        components = [Reply([Image(url) for url in refs])]
    elif source == "quoted_parser":
        parser.extract_quoted_message_images.return_value = refs
    elif source == "onebot":
        components = [Reply()]
        event.get_platform_name = lambda: "aiocqhttp"
        event.bot = types.SimpleNamespace(api=types.SimpleNamespace(call_action=AsyncMock(return_value={
            "data": {"message": [{"type": "image", "data": {"url": ref}} for ref in refs]},
        })))
    event.get_messages = lambda: components

    async def fetch(url, **kwargs):
        if url == BAD:
            raise RuntimeError("fixture unavailable")
        return ImageInput(data=url.encode(), mime="image/png", source=url)

    reader = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(images, "fetch_image", reader)
    return event, reader


def config():
    return types.SimpleNamespace(max_input_images=lambda: 6, reply_reference_image=lambda: True)


@pytest.mark.parametrize("source", ["url", "attachment", "reply_chain", "quoted_parser", "onebot"])
def test_video_rejects_partial_reference_collection(monkeypatch, source):
    event, reader = fixture(monkeypatch, source)
    with pytest.raises(ProtocolError, match="参考图"):
        asyncio.run(images.collect_input_images(event, VideoImageConfig(config())))
    assert reader.await_count == 2


@pytest.mark.parametrize("source", ["url", "attachment", "reply_chain", "quoted_parser", "onebot"])
def test_ordinary_image_edit_keeps_best_effort_collection(monkeypatch, source):
    event, reader = fixture(monkeypatch, source)
    result = asyncio.run(images.collect_input_images(event, config()))
    assert [image.source for image in result] == [GOOD]
    assert reader.await_count == 2


@pytest.mark.parametrize("source", ["url", "attachment", "reply_chain", "quoted_parser", "onebot"])
def test_video_preserves_all_reference_order_without_refetch(monkeypatch, source):
    event, reader = fixture(monkeypatch, source, broken=False)
    result = asyncio.run(images.collect_input_images(event, VideoImageConfig(config())))
    assert [image.source for image in result] == [GOOD, SECOND]
    assert reader.await_count == 2


def test_plain_url_is_not_an_image_failure(monkeypatch):
    event, reader = fixture(monkeypatch, "attachment", broken=False)
    event.message += " https://fixture.invalid/article"
    result = asyncio.run(images.collect_input_images(event, VideoImageConfig(config())))
    assert [image.source for image in result] == [GOOD, SECOND]
    assert reader.await_count == 2


def test_video_preserves_deduplication(monkeypatch):
    event, reader = fixture(monkeypatch, "attachment", broken=False)
    event.get_messages = lambda: [Image(GOOD), Image(SECOND), Image(GOOD)]
    result = asyncio.run(images.collect_input_images(event, VideoImageConfig(config())))
    assert [image.source for image in result] == [GOOD, SECOND]
    assert reader.await_count == 3


def test_partial_failure_stops_video_before_duration_and_submission(monkeypatch):
    async def scenario():
        case = video.VideoLifecycleTests()
        await case.asyncSetUp()
        try:
            case.plugin.cfg.video_api_mode = lambda: "images"
            event, reader = fixture(monkeypatch, "url")
            monkeypatch.setitem(video.MAIN_GLOBALS, "extract_image_urls", images.extract_image_urls)
            monkeypatch.setitem(video.MAIN_GLOBALS, "collect_input_images", images.collect_input_images)
            await case.plugin._video(event, video.ParsedCommand(
                video.KIND_VIDEO, event.message.removeprefix("图生视频 "), command="图生视频"))
            assert "读取视频参考图失败" in case.plugin._reply.await_args.args[1]
            assert not case.plugin._video_inputs
            assert not case.plugin._video_tasks
            assert not video.FakeClient.instances
            assert reader.await_count == 2
        finally:
            await case.asyncTearDown()
    asyncio.run(scenario())
