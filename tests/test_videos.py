"""用本机 HTTP 服务验证视频协议；不依赖中转站、密钥或 AstrBot。"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
import sys

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import videos
from core.videos import VideoClient, VideoError, VideoTask, parse_task
from core.models import ImageInput

# 接口依据：QuantumNous/new-api v1.0.0-rc.48 的 dto/video.go 与
# plugins/tasks/sora/plugin.js；OpenAI videos.create 文档的 seconds/input_reference。
# 本文件只验证兼容协议编码，不声称所有上游模型接受同一组时长或图片尺寸。


@asynccontextmanager
async def server(handler):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield "http://127.0.0.1:%s" % port
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("mode,prefix", [("legacy", "/v1/video/generations"), ("openai", "/v1/videos")])
def test_submit_poll_preserves_endpoint_and_gateway_id(mode, prefix):
    async def scenario():
        calls = []
        polls = 0

        async def handler(request):
            nonlocal polls
            calls.append((request.method, request.path, request.headers.get("Authorization")))
            if request.method == "POST":
                payload = await request.json()
                assert payload["model"] == "test-video-model"
                assert payload["prompt"] == "A cat running"
                assert ("response_format" in payload) == (mode == "legacy")
                if mode == "legacy":
                    assert payload["duration"] == 5
                    assert "seconds" not in payload
                else:
                    assert payload["seconds"] == "5"
                    assert "duration" not in payload
                return web.json_response({"code": "success", "data": {
                    "task_id": "gateway-1", "status": "queued", "data": {"id": "upstream-1"}}})
            polls += 1
            if polls == 1:
                return web.json_response({"data": {"task_id": "gateway-1", "status": "IN_PROGRESS", "progress": "50%"}})
            return web.json_response({"data": {"task_id": "gateway-1", "status": "SUCCESS", "data": {
                "id": "upstream-1", "output": {"video_url": "https://cdn.example.test/movie.mp4"}}}})

        async with server(handler) as base:
            async with VideoClient(base + "/v1/", "test-only-key", api_mode=mode, timeout=2, poll_interval=0.01) as client:
                task = await client.submit(" A cat running ", "test-video-model", seconds=5)
                assert task.id == "gateway-1"
                result = await client.wait(task)
        assert result.status == "completed"
        assert result.url == "https://cdn.example.test/movie.mp4"
        assert calls == [("POST", prefix, "Bearer test-only-key"),
                         ("GET", prefix + "/gateway-1", "Bearer test-only-key"),
                         ("GET", prefix + "/gateway-1", "Bearer test-only-key")]

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["failed", "FAILURE", "cancelled", "canceled"])
def test_failed_or_cancelled_task_stops_without_new_request(status):
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            return web.json_response({"id": "t1", "status": status, "error": {"message": "upstream refused"}})

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", poll_interval=0.01) as client:
                task = await client.submit("test", "test-video-model", seconds=5)
                if status in ("cancelled", "canceled"):
                    result = await client.wait(task)
                    assert result.status == "cancelled"
                else:
                    with pytest.raises(VideoError, match="upstream refused"):
                        await client.wait(task)
        assert calls == ["POST"]

    asyncio.run(scenario())


@pytest.mark.parametrize("response,expected", [
    ({"status": "queued"}, "任务 ID"),
    ({"id": "t1", "status": "surprise"}, "未知任务状态"),
    ([{"id": "t1", "status": "queued"}], "不是任务对象"),
])
def test_rejects_bad_submission_objects(response, expected):
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            return web.json_response(response)

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key") as client:
                with pytest.raises(VideoError, match=expected):
                    await client.submit("test", "test-video-model", seconds=5)
        assert calls == ["POST"]

    asyncio.run(scenario())


def test_poll_rejects_different_task_id():
    async def scenario():
        async def handler(request):
            return web.json_response({"id": "someone-elses-task", "status": "completed", "url": "https://cdn.example.test/file.mp4"})

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", poll_interval=0.01) as client:
                with pytest.raises(VideoError, match="其他任务"):
                    await client.wait(VideoTask("my-task", "queued"))

    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["", "file:///private/movie.mp4", "javascript:alert(1)", "https://user:pass@example.test/video.mp4", "http://[invalid", "http://example.test:not-a-port/video.mp4", "http://example.test:99999/video.mp4"])
def test_legacy_completion_requires_valid_download_url(url):
    async def scenario():
        task = parse_task({"id": "t1", "status": "succeeded", "url": url})
        assert not task.url
        async with VideoClient("http://127.0.0.1", "test-only-key") as client:
            with pytest.raises(VideoError, match="未返回有效视频地址"):
                await client.wait(task)

    asyncio.run(scenario())


def test_query_429_retries_without_resubmitting():
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            if request.method == "POST":
                return web.json_response({"task_id": "t1", "status": "queued"})
            if len(calls) == 2:
                return web.Response(status=429, headers={"Retry-After": "0.02"})
            return web.json_response({"id": "t1", "status": "completed", "url": "https://cdn.example.test/movie.mp4"})

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", poll_interval=0.01) as client:
                result = await client.wait(await client.submit("test", "test-video-model", seconds=5))
        assert result.status == "completed"
        assert calls == ["POST", "GET", "GET"]

    asyncio.run(scenario())


def test_query_three_5xx_failures_stop():
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            return web.Response(status=503)

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", poll_interval=0.01) as client:
                with pytest.raises(VideoError, match="连续三次"):
                    await client.wait(VideoTask("t1", "queued"))
        assert calls == ["GET"] * 3

    asyncio.run(scenario())


def test_post_429_is_not_retried():
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            return web.json_response({"error": {"message": "rate limited"}}, status=429)

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", poll_interval=0.01) as client:
                with pytest.raises(VideoError, match="429"):
                    await client.submit("test", "test-video-model", seconds=5)
        assert calls == ["POST"]

    asyncio.run(scenario())


def test_successful_poll_resets_transient_failure_count():
    async def scenario():
        statuses = [503, 503, 200, 503, 503, 200]
        calls = []

        async def handler(request):
            calls.append(request.method)
            status = statuses.pop(0)
            if status != 200:
                return web.Response(status=status)
            return web.json_response({"id": "t1", "status": "in_progress" if statuses else "completed", "url": "https://cdn.example.test/movie.mp4"})

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", poll_interval=0.01) as client:
                assert (await client.wait(VideoTask("t1", "queued"))).status == "completed"
        assert calls == ["GET"] * 6

    asyncio.run(scenario())


def test_polling_deadline_does_not_submit_again():
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            return web.json_response({"id": "t1", "status": "in_progress"})

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", timeout=1, poll_interval=0.15) as client:
                with pytest.raises(VideoError, match="超时"):
                    await client.wait(VideoTask("t1", "queued"))
        assert calls and set(calls) == {"GET"}

    asyncio.run(scenario())


def test_post_timeout_submits_exactly_once():
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            await asyncio.sleep(1.15)
            return web.json_response({"id": "t1", "status": "queued"})

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", timeout=1, poll_interval=0.01) as client:
                with pytest.raises(VideoError, match="未自动重发"):
                    await client.submit("test", "test-video-model", seconds=5)
        assert calls == ["POST"]

    asyncio.run(scenario())


def test_cancellation_propagates_and_closes_session():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        client = None
        session = None

        async def handler(request):
            entered.set()
            await release.wait()
            return web.json_response({"id": "t1", "status": "in_progress"})

        async with server(handler) as base:
            async def operation():
                nonlocal client, session
                async with VideoClient(base, "test-only-key", poll_interval=0.01) as opened:
                    client, session = opened, opened._session
                    await client.wait(VideoTask("t1", "queued"))

            work = asyncio.create_task(operation())
            await asyncio.wait_for(entered.wait(), timeout=2)
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work
            release.set()
            assert session.closed
            assert client._session is None

    asyncio.run(scenario())


def test_openai_download_redirect_drops_bearer(tmp_path):
    async def scenario():
        seen = []
        data = b"\x00\x00\x00\x18ftypmp42test-video-content"

        async def target(request):
            seen.append(("cdn", request.path, request.headers.get("Authorization")))
            return web.Response(body=data, content_type="video/mp4")

        async with server(target) as cdn:
            async def origin(request):
                seen.append(("origin", request.path, request.headers.get("Authorization")))
                raise web.HTTPFound(cdn + "/movie.mp4?signature=test")

            async with server(origin) as base:
                async with VideoClient(base, "test-only-key", api_mode="openai") as client:
                    result = await client.download(VideoTask("t1", "completed"), tmp_path)
        assert result.read_bytes() == data
        assert seen == [("origin", "/v1/videos/t1/content", "Bearer test-only-key"), ("cdn", "/movie.mp4", None)]
        assert not list(tmp_path.glob("*.part"))

    asyncio.run(scenario())


def test_explicit_result_url_never_receives_api_key(tmp_path):
    async def scenario():
        seen = []

        async def handler(request):
            seen.append(request.headers.get("Authorization"))
            return web.Response(body=b"a-video", content_type="application/octet-stream")

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key") as client:
                result = await client.download(VideoTask("t1", "completed", base + "/result.mp4"), tmp_path)
        assert seen == [None]
        assert result.read_bytes() == b"a-video"

    asyncio.run(scenario())


def test_download_cancellation_removes_partial_file(tmp_path):
    async def scenario():
        sent = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
            await response.prepare(request)
            await response.write(b"initial-video-bytes")
            sent.set()
            await release.wait()
            return response

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", api_mode="openai") as client:
                operation = asyncio.create_task(client.download(VideoTask("t1", "completed"), tmp_path))
                await asyncio.wait_for(sent.wait(), timeout=2)
                await asyncio.sleep(0.01)
                operation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await operation
                release.set()
        assert not list(tmp_path.iterdir())

    asyncio.run(scenario())


@pytest.mark.parametrize("body,content_type,expected", [
    (b"<html>login page</html>", "text/html", "未返回 JSON"),
    (b"x" * (videos.MAX_JSON_BYTES + 1), "application/json", "响应过大"),
])
def test_invalid_or_oversized_json_is_rejected(body, content_type, expected):
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            return web.Response(body=body, content_type=content_type)

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key") as client:
                with pytest.raises(VideoError, match=expected):
                    await client.submit("test", "test-video-model", seconds=5)
        assert calls == ["POST"]

    asyncio.run(scenario())


@pytest.mark.parametrize("kind,expected", [("html", "非视频"), ("empty", "为空"), ("large", "下载上限")])
def test_download_rejects_invalid_content_and_removes_partial_files(tmp_path, monkeypatch, kind, expected):
    monkeypatch.setattr(videos, "MAX_VIDEO_BYTES", 16)

    async def scenario():
        async def handler(request):
            if kind == "html":
                return web.Response(text="not a movie", content_type="text/html")
            if kind == "empty":
                return web.Response(body=b"", content_type="video/mp4")
            response = web.StreamResponse(headers={"Content-Type": "video/mp4"})
            await response.prepare(request)
            await response.write(b"x" * 17)
            await response.write_eof()
            return response

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", api_mode="openai") as client:
                with pytest.raises(VideoError, match=expected):
                    await client.download(VideoTask("t1", "completed"), tmp_path)
        assert not list(tmp_path.iterdir())

    asyncio.run(scenario())


def test_error_redaction_keeps_credentials_out_of_messages():
    task = parse_task({"id": "t1", "status": "failed", "error": {"message":
        "credential=test-only-key Bearer dangerous-token https://cdn.example.test/movie?sig=secret"}}, api_key="test-only-key")
    assert "test-only-key" not in task.error
    assert "dangerous-token" not in task.error
    assert "sig=secret" not in task.error


def test_gateway_status_takes_precedence_over_upstream_completion():
    task = parse_task({"code": "success", "data": {
        "task_id": "gateway-1", "status": "IN_PROGRESS", "progress": "75%", "data": {
            "id": "upstream-1", "status": "completed", "video_url": "https://cdn.example.test/movie.mp4"}}})
    assert task.id == "gateway-1"
    assert task.status == "in_progress"
    assert task.progress == 75
    assert task.url == "https://cdn.example.test/movie.mp4"


@pytest.mark.parametrize("mode", ["legacy", "openai"])
@pytest.mark.parametrize("mime,extension,data", [
    ("image/png", "png", b"\x89PNG\r\n\x1a\nreference-bytes"),
    ("image/jpeg", "jpg", b"\xff\xd8\xffreference-bytes"),
    ("image/webp", "webp", b"RIFFreference-WEBP-bytes"),
])
def test_image_submission_wire_format(mode, mime, extension, data):
    async def scenario():
        calls = []

        async def handler(request):
            calls.append(request.method)
            if mode == "legacy":
                payload = await request.json()
                assert request.content_type == "application/json"
                assert payload["model"] == "test-video-model"
                assert payload["prompt"] == "animate the photo"
                assert payload["duration"] == 8
                assert "images" not in payload
                assert "input_reference" not in payload
                assert payload["image"] == "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
            else:
                assert request.content_type == "multipart/form-data"
                parts = await request.multipart()
                fields = {}
                async for part in parts:
                    if part.name == "input_reference":
                        assert part.filename == "reference." + extension
                        assert part.headers["Content-Type"] == mime
                        fields[part.name] = bytes(await part.read())
                    else:
                        fields[part.name] = await part.text()
                assert fields == {"model": "test-video-model", "prompt": "animate the photo", "seconds": "8", "input_reference": data}
            return web.json_response({"id": "t1", "status": "queued"})

        async with server(handler) as base:
            image = ImageInput(data=data, mime=mime, filename="private-local-path-do-not-send.jpg", source="private-local-source")
            async with VideoClient(base, "test-only-key", api_mode=mode) as client:
                task = await client.submit("animate the photo", "test-video-model", seconds=8, images=[image])
        assert task.id == "t1"
        assert calls == ["POST"]

    asyncio.run(scenario())


@pytest.mark.parametrize("seconds", [0, -1, 121, True, False, 5.5, "5", None])
def test_invalid_duration_is_rejected_before_http(seconds):
    async def scenario():
        async with VideoClient("http://127.0.0.1", "test-only-key") as client:
            with pytest.raises(VideoError, match="时长"):
                await client.submit("test", "test-video-model", seconds=seconds)

    asyncio.run(scenario())


@pytest.mark.parametrize("images,message", [
    ([ImageInput(data=b"one"), ImageInput(data=b"two")], "一张"),
    ([ImageInput(data=b"")], "为空"),
    ([ImageInput(data=b"GIF89a", mime="image/gif")], "JPEG"),
    ([ImageInput(data=b"svg", mime="image/svg+xml")], "JPEG"),
])
def test_invalid_images_are_rejected_before_http(images, message):
    async def scenario():
        async with VideoClient("http://127.0.0.1", "test-only-key") as client:
            with pytest.raises(VideoError, match=message):
                await client.submit("test", "test-video-model", seconds=5, images=images)

    asyncio.run(scenario())


def test_reference_image_size_limit(monkeypatch):
    monkeypatch.setattr(videos, "MAX_REFERENCE_BYTES", 16)

    async def scenario():
        async with VideoClient("http://127.0.0.1", "test-only-key") as client:
            with pytest.raises(VideoError, match="20 MiB"):
                await client.submit("test", "test-video-model", seconds=5, images=[ImageInput(data=b"x" * 17)])

    asyncio.run(scenario())


@pytest.mark.parametrize("value,known,expected", [
    (None, False, 0), (True, False, 0), (False, False, 0),
    ("NaN", False, 0), (float("nan"), False, 0),
    ("inf", False, 0), (float("inf"), False, 0), ("-inf", False, 0),
    ("unknown", False, 0), ("5%%", False, 0),
    (0, True, 0), ("0%", True, 0), (" 12.5% ", True, 12.5),
    (53, True, 53), (101, True, 100), (-1, True, 0),
])
def test_progress_known_only_for_finite_real_values(value, known, expected):
    task = parse_task({"id": "t1", "status": "in_progress", "progress": value})
    assert task.progress_known is known
    assert task.progress == expected


def test_missing_progress_is_unknown_and_dataclass_keeps_positional_compatibility():
    assert not parse_task({"id": "t1", "status": "queued"}).progress_known
    task = VideoTask("t1", "failed", "", 0, "error text")
    assert task.error == "error text"
    assert not task.progress_known


def test_callback_receives_initial_and_every_successful_poll_and_errors_are_isolated():
    async def scenario():
        seen = []
        replies = [{"id": "t1", "status": "in_progress", "progress": "0%"},
                   {"id": "t1", "status": "in_progress", "progress": 60},
                   {"id": "t1", "status": "completed", "progress": 100, "url": "https://cdn.example.test/video.mp4"}]

        async def handler(request):
            assert request.method == "GET"
            return web.json_response(replies.pop(0))

        async def callback(task):
            seen.append((task.status, task.progress, task.progress_known))
            raise RuntimeError("delivery unavailable")

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", poll_interval=0.01) as client:
                result = await client.wait(VideoTask("t1", "queued"), on_progress=callback)
        assert result.status == "completed"
        assert seen == [("queued", 0, False), ("in_progress", 0, True),
                        ("in_progress", 60, True), ("completed", 100, True)]

    asyncio.run(scenario())


def test_callback_cancellation_propagates_without_http():
    async def scenario():
        async def callback(task):
            raise asyncio.CancelledError()

        async with VideoClient("http://127.0.0.1", "test-only-key") as client:
            with pytest.raises(asyncio.CancelledError):
                await client.wait(VideoTask("t1", "queued"), on_progress=callback)

    asyncio.run(scenario())



def test_nested_upstream_timeout_reason_is_preserved():
    from core.videos import parse_task
    task = parse_task({"code": "success", "data": {
        "task_id": "gateway-timeout", "status": "FAILURE", "progress": "100%",
        "fail_reason": "task failed",
        "data": {"status": "failed", "error": "任务总等待时限已到，停止重试"},
    }})
    assert task.status == "failed"
    assert "任务总等待时限已到" in task.error


@pytest.mark.parametrize("count", [0, 1, 2, 10])
def test_images_mode_posts_exclusive_ordered_array(count):
    async def scenario():
        calls = []
        references = [ImageInput(data=("image-%s" % i).encode(),
                                 mime=("image/png", "image/jpeg", "image/webp")[i % 3])
                      for i in range(count)]
        forbidden = {"image", "image_url", "reference_image", "input_reference", "seconds"}

        async def handler(request):
            assert request.method == "POST" and request.path == "/v1/videos"
            assert request.content_type == "application/json"
            payload = await request.json()
            calls.append(payload)
            if forbidden.intersection(payload):
                return web.json_response({"error": {"message": "images 不能与旧版单图字段同时使用"}}, status=400)
            expected = {"model": "test-video-model", "prompt": "animate these references", "duration": 10}
            if references:
                expected["images"] = ["data:%s;base64,%s" % (
                    image.mime, base64.b64encode(image.data).decode("ascii")) for image in references]
            assert payload == expected
            return web.json_response({"id": "images-task", "status": "queued"})

        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", api_mode="images") as client:
                task = await client.submit("animate these references", "test-video-model", seconds=10, images=references)
        assert task.id == "images-task"
        assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("bad,message", [
    (None, "第 2 张"), (ImageInput(data=b""), "第 2 张"),
    (ImageInput(data=b"gif", mime="image/gif"), "JPEG"),
])
def test_images_mode_validates_every_reference_before_http(bad, message):
    async def scenario():
        async with VideoClient("http://127.0.0.1", "test-only-key", api_mode="images") as client:
            with pytest.raises(VideoError, match=message):
                await client.submit("test", "test-video-model", seconds=5,
                                    images=[ImageInput(data=b"good"), bad])
    asyncio.run(scenario())


def test_images_mode_rejects_too_many_references_before_http():
    async def scenario():
        async with VideoClient("http://127.0.0.1", "test-only-key", api_mode="images") as client:
            with pytest.raises(VideoError, match="10"):
                await client.submit("test", "test-video-model", seconds=5,
                                    images=[ImageInput(data=b"image")] * 11)
    asyncio.run(scenario())


def test_images_mode_checks_second_image_size(monkeypatch):
    monkeypatch.setattr(videos, "MAX_REFERENCE_BYTES", 16)
    async def scenario():
        async with VideoClient("http://127.0.0.1", "test-only-key", api_mode="images") as client:
            with pytest.raises(VideoError, match="第 2 张"):
                await client.submit("test", "test-video-model", seconds=5,
                                    images=[ImageInput(data=b"ok"), ImageInput(data=b"x" * 17)])
    asyncio.run(scenario())


def test_images_mode_checks_total_size(monkeypatch):
    monkeypatch.setattr(videos, "MAX_REFERENCE_TOTAL_BYTES", 10, raising=False)
    async def scenario():
        async with VideoClient("http://127.0.0.1", "test-only-key", api_mode="images") as client:
            with pytest.raises(VideoError, match="合计"):
                await client.submit("test", "test-video-model", seconds=5,
                                    images=[ImageInput(data=b"first!"), ImageInput(data=b"second")])
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 429, 500])
def test_images_mode_http_error_does_not_resubmit(status):
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request.method)
            return web.json_response({"error": {"message": "fixture submission refused"}}, status=status)
        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", api_mode="images") as client:
                with pytest.raises(VideoError, match="HTTP %s" % status):
                    await client.submit("test", "test-video-model", seconds=5,
                                        images=[ImageInput(data=b"one"), ImageInput(data=b"two")])
        assert calls == ["POST"]
    asyncio.run(scenario())


def test_images_mode_poll_and_download_preserve_paths_and_auth(tmp_path):
    async def scenario():
        calls = []
        async def handler(request):
            calls.append((request.method, request.path, request.headers.get("Authorization")))
            if request.method == "POST":
                return web.json_response({"id": "images-task", "status": "queued"})
            if request.path == "/v1/videos/images-task":
                return web.json_response({"id": "images-task", "status": "completed", "url": base + "/ready.mp4"})
            assert request.path == "/ready.mp4"
            return web.Response(body=b"fixture-video", content_type="video/mp4")
        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", api_mode="images", poll_interval=0.01) as client:
                task = await client.submit("test", "test-video-model", seconds=5, images=[ImageInput(data=b"reference")])
                task = await client.wait(task)
                downloaded = await client.download(task, tmp_path)
        assert downloaded.read_bytes() == b"fixture-video"
        assert calls == [("POST", "/v1/videos", "Bearer test-only-key"),
                         ("GET", "/v1/videos/images-task", "Bearer test-only-key"),
                         ("GET", "/ready.mp4", None)]
    asyncio.run(scenario())


def test_images_mode_completion_without_url_is_explicit_failure(tmp_path):
    async def scenario():
        async with VideoClient("http://127.0.0.1", "test-only-key", api_mode="images") as client:
            completed = VideoTask("images-task", "completed")
            with pytest.raises(VideoError, match="未返回有效视频地址"):
                await client.wait(completed)
            with pytest.raises(VideoError, match="没有下载地址"):
                await client.download(completed, tmp_path)
    asyncio.run(scenario())


def test_reference_field_conflict_explains_images_mode_without_retry():
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request.method)
            payload = await request.json()
            assert "image" in payload and "images" not in payload
            return web.json_response({"error": {"message": "images 不能与旧版单图字段同时使用"}}, status=400)
        async with server(handler) as base:
            async with VideoClient(base, "test-only-key", api_mode="legacy") as client:
                with pytest.raises(VideoError, match="接口模式.*images"):
                    await client.submit("test", "test-video-model", seconds=5, images=[ImageInput(data=b"reference")])
        assert calls == ["POST"]
    asyncio.run(scenario())
