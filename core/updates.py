"""固定仓库的版本检测与宿主插件维护任务。

任务保留在 AstrBot Context 上，重载旧插件时不会随插件实例销毁。
只调用宿主 PluginManager；下载、依赖安装和模块替换由宿主负责。
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable
import uuid

REPOSITORY_URL = "https://github.com/3121871622/gpthh"
CHANGELOG_URL = "https://raw.githubusercontent.com/3121871622/gpthh/main/CHANGELOG.md"
MANIFEST_URL = "https://raw.githubusercontent.com/3121871622/gpthh/main/update-manifest.json"
ARCHIVE_URL = REPOSITORY_URL + "/archive/refs/heads/main.zip"
DEFAULT_VERSION = "1.1.0"
STATE_ATTRIBUTE = "_gpthh_maintenance_v1"
RUNTIME_ATTRIBUTE = "_gpthh_runtime_v1"
ACTIVE_STATUSES = frozenset({"queued", "checking", "installing", "reloading", "verifying"})
START_DELAY = 0.3
OPERATION_TIMEOUT = 300.0
VERIFY_TIMEOUT = 30.0
VERIFY_INTERVAL = 0.2
FetchText = Callable[[str], Awaitable[str]]


class MaintenanceError(RuntimeError):
    """可公开显示的维护错误，不包含供应商配置或宿主异常详情。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def version_key(value: Any) -> tuple[int, ...]:
    match = re.fullmatch(r"[vV]?(\d+(?:\.\d+){1,3})", str(value or "").strip())
    if not match:
        raise ValueError("版本号格式无效")
    parts = tuple(int(part) for part in match.group(1).split("."))
    return parts + (0,) * (4 - len(parts))


def _manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("更新清单必须是 JSON 对象")
    version = str(value.get("version", "")).strip().lstrip("vV")
    version_key(version)
    build = value.get("build")
    if isinstance(build, bool) or not isinstance(build, int) or not 0 < build < 2**53:
        raise ValueError("更新清单缺少有效的 build 修订号")
    if value.get("repository", REPOSITORY_URL) != REPOSITORY_URL:
        raise ValueError("更新清单的仓库不匹配")
    if value.get("branch", "main") != "main":
        raise ValueError("更新清单的分支不匹配")
    return {"version": version, "build": build}


def _read_local_release() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    version = DEFAULT_VERSION
    try:
        text = (root / "metadata.yaml").read_text(encoding="utf-8-sig")
        match = re.search(r"^\s*version\s*:\s*[\"']?([^\"'\s#]+)", text, re.MULTILINE)
        if match:
            version = match.group(1).lstrip("vV")
            version_key(version)
    except (OSError, ValueError):
        pass
    build = 0
    try:
        release = _manifest(json.loads((root / "update-manifest.json").read_text(encoding="utf-8-sig")))
        if version_key(release["version"]) == version_key(version):
            build = release["build"]
    except (OSError, ValueError, TypeError):
        pass
    return {"version": version, "build": build}


def bind_plugin_runtime(plugin: Any) -> dict[str, Any]:
    """只在实例首次注册时快照文件版本，避免安装后旧实例冒充新代码。"""
    runtime = getattr(plugin, RUNTIME_ATTRIBUTE, None)
    if not isinstance(runtime, dict):
        runtime = {**_read_local_release(), "instance_id": uuid.uuid4().hex, "ready": False}
        setattr(plugin, RUNTIME_ATTRIBUTE, runtime)
    return runtime


def runtime_info(plugin: Any) -> dict[str, Any]:
    if plugin is None:
        return {**_read_local_release(), "instance_id": "", "ready": False}
    return dict(bind_plugin_runtime(plugin))


def mark_plugin_ready(plugin: Any) -> None:
    """由 initialize 最后一行调用；注册 API 并不代表初始化已经完成。"""
    bind_plugin_runtime(plugin)["ready"] = True


def _state(context: Any) -> dict[str, Any]:
    if context is None:
        raise MaintenanceError("host_unavailable", "当前环境未连接 AstrBot 插件管理器")
    state = getattr(context, STATE_ATTRIBUTE, None)
    if not isinstance(state, dict):
        state = {"jobs": {}, "active_job_id": "", "last_job_id": "", "task": None, "last_check": None}
        setattr(context, STATE_ATTRIBUTE, state)
    state.setdefault("jobs", {})
    return state


def maintenance_message(plugin: Any) -> str:
    state = getattr(getattr(plugin, "context", None), STATE_ATTRIBUTE, None)
    if not isinstance(state, dict):
        return ""
    job = state.get("jobs", {}).get(state.get("active_job_id"), {})
    if job.get("status") in ACTIVE_STATUSES:
        return "插件正在更新或重载，请完成后再发起图片或视频生成。"
    return ""


def _unfinished(tasks: Any) -> bool:
    for task in tuple(tasks or ()):
        done = getattr(task, "done", None)
        if not callable(done) or not done():
            return True
    return False


def _busy_reason(plugin: Any) -> str:
    if _unfinished(getattr(plugin, "_image_tasks", ())):
        return "仍有图片任务正在执行或排队，请完成后再更新或重载。"
    if _unfinished(getattr(plugin, "_video_tasks", ())) or getattr(plugin, "_video_pending", None):
        return "仍有视频任务正在生成，请完成后再更新或重载。"
    for pending in (getattr(plugin, "_video_inputs", {}) or {}).values():
        if getattr(pending, "expires_at", float("inf")) > time.monotonic():
            return "仍有视频会话正在等待输入，请先完成或取消该会话。"
    stats = getattr(plugin, "stats", None)
    snapshot = getattr(stats, "snapshot", None)
    if callable(snapshot):
        totals = snapshot().get("totals", {})
        if int(totals.get("in_progress", 0) or 0) > 0:
            return "仍有生成任务正在执行，请完成后再更新或重载。"
    return ""


def _resolve_plugin(plugin: Any) -> tuple[Any, Any]:
    context = getattr(plugin, "context", None)
    manager = getattr(context, "_star_manager", None)
    all_stars = getattr(context, "get_all_stars", None)
    get_star = getattr(context, "get_registered_star", None)
    if manager is None or not callable(all_stars) or not callable(get_star):
        raise MaintenanceError("host_unavailable", "当前 AstrBot 缺少插件管理接口，请先升级宿主。")
    matches = [entry for entry in all_stars() if getattr(entry, "star_cls", None) is plugin]
    if len(matches) != 1:
        raise MaintenanceError("plugin_not_registered", "未找到当前插件实例的唯一注册记录，已停止操作。")
    metadata = matches[0]
    name = getattr(metadata, "name", None)
    if not isinstance(name, str) or not name or get_star(name) is not metadata:
        raise MaintenanceError("plugin_not_registered", "插件注册名称不匹配，已停止操作。")
    if not getattr(metadata, "root_dir_name", None) or getattr(metadata, "reserved", False):
        raise MaintenanceError("plugin_not_supported", "当前插件的安装目录或插件类型不支持此操作。")
    if not getattr(metadata, "activated", False):
        raise MaintenanceError("plugin_inactive", "当前插件尚未启用，已停止操作。")
    return manager, metadata


def _fixed_update_arguments(manager: Any, metadata: Any) -> dict[str, str]:
    method = getattr(manager, "update_plugin", None)
    if not callable(method):
        raise MaintenanceError("update_unsupported", "当前 AstrBot 不支持插件更新接口，请先升级宿主。")
    repo = str(getattr(metadata, "repo", "") or "").rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if repo != REPOSITORY_URL:
        raise MaintenanceError("repository_mismatch", "插件注册的仓库地址不是本项目，已停止自动更新。")
    parameters = inspect.signature(method).parameters
    arbitrary_keywords = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    kwargs = {}
    for key, value in {"proxy": "", "download_url": ARCHIVE_URL, "repo_url": REPOSITORY_URL}.items():
        if key in parameters or arbitrary_keywords:
            kwargs[key] = value
    # 只通过可指定 main 压缩包的官方接口更新，避免旧版接口选到其他分支。
    if "download_url" not in kwargs:
        raise MaintenanceError("update_unsupported", "当前 AstrBot 更新接口不支持固定下载源，请在宿主插件管理页更新，或升级 AstrBot。")
    inspect.signature(method).bind(metadata.name, **kwargs)
    return kwargs


def _capabilities(plugin: Any) -> dict[str, Any]:
    result = {"install_supported": False, "reload_supported": False, "capability_message": ""}
    try:
        manager, metadata = _resolve_plugin(plugin)
        result["reload_supported"] = callable(getattr(manager, "reload", None))
        _fixed_update_arguments(manager, metadata)
        result["install_supported"] = True
    except (MaintenanceError, TypeError, ValueError) as exc:
        result["capability_message"] = str(exc)
    return result


def extract_changelog(text: str, version: str) -> str:
    escaped = re.escape(version.lstrip("vV"))
    heading = r"^[ \t]*#{1,6}[ \t]*v?\d+(?:\.\d+)+(?:[ \t]*[-\u2013\u2014:][ \t]*[^\r\n]*)?$"
    pattern = re.compile(rf"(?ims)^[ \t]*#{{1,6}}[ \t]*v?{escaped}(?:[ \t]*[-\u2013\u2014:][ \t]*[^\r\n]*)?$.*?(?={heading}|\Z)")
    match = pattern.search(text or "")
    return (match.group(0) if match else text or "")[:12000].strip()


async def check_update(plugin: Any, fetch_text: FetchText) -> dict[str, Any]:
    current = runtime_info(plugin)
    data = {
        "ok": False, "status": "check_failed", "current_version": current["version"],
        "current_build": current["build"], "latest_version": current["version"],
        "target_build": current["build"], "update_available": False,
        "changelog_url": CHANGELOG_URL, "manifest_url": MANIFEST_URL,
        **_capabilities(plugin),
    }
    try:
        release = _manifest(json.loads(await fetch_text(MANIFEST_URL)))
        newer = (version_key(release["version"]), release["build"]) > (version_key(current["version"]), current["build"])
        data.update({
            "ok": True, "status": "checked", "latest_version": release["version"],
            "target_build": release["build"], "update_available": newer,
            "download_url": ARCHIVE_URL, "checked_at": time.time(),
            "message": ("发现更新 v%s · 修订 %s" % (release["version"], release["build"])) if newer else ("当前已是最新版本（v%s · 修订 %s）" % (current["version"], current["build"])),
        })
        try:
            data["changelog"] = extract_changelog(await fetch_text(CHANGELOG_URL), release["version"])
        except Exception:
            data["changelog"] = "更新日志暂时读取失败，版本检查已完成。"
        context = getattr(plugin, "context", None)
        if context is not None:
            _state(context)["last_check"] = copy.deepcopy(data)
    except Exception as exc:
        data.update({"message": "检查更新失败，请检查网络后重试。", "error": "更新清单读取失败（%s）" % type(exc).__name__})
    return data


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(job)


def get_update_status(plugin: Any, job_id: str = "") -> dict[str, Any]:
    state = _state(getattr(plugin, "context", None))
    selected = job_id or state.get("active_job_id") or state.get("last_job_id")
    if selected:
        job = state["jobs"].get(selected)
        if job is None:
            return {"ok": False, "status": "not_found", "job_id": selected, "message": "维护任务不存在或宿主已重新启动。"}
        return _public_job(job)
    current = runtime_info(plugin)
    return {"ok": True, "status": "idle", "job_id": "", "message": "当前没有更新或重载任务。", "current_version": current["version"], "current_build": current["build"], "target_build": current["build"]}


def _job_stage(job: dict[str, Any], status: str, message: str, **extra: Any) -> None:
    job.update({"status": status, "message": message, "updated_at": time.time(), **extra})


def _log_failure(plugin: Any, exc: BaseException) -> None:
    logger = getattr(plugin, "logger", None) or logging.getLogger(__name__)
    try:
        logger.error("插件维护任务失败：%s", exc, exc_info=(type(exc), exc, exc.__traceback__))
    except Exception:
        logging.getLogger(__name__).error("插件维护任务失败：%s", exc)


async def _await_host_operation(awaitable: Awaitable[Any], state: dict[str, Any], job: dict[str, Any]) -> Any:
    # 只给等待套超时，不取消宿主调用；pip/线程中的写盘必须真实结束后才放行。
    operation = asyncio.create_task(awaitable, name="gpthh-host-" + job["operation"])
    state["operation_task"] = operation
    try:
        return await asyncio.wait_for(asyncio.shield(operation), timeout=OPERATION_TIMEOUT)
    except asyncio.TimeoutError:
        _job_stage(job, job["status"], "宿主操作耗时较长，仍在后台处理中，请勿重复操作。", slow=True)
        return await asyncio.shield(operation)


def _settle_interrupted_job(plugin: Any, state: dict[str, Any], job: dict[str, Any], operation: Any) -> None:
    try:
        operation.result()
    except BaseException as exc:
        _log_failure(plugin, exc)
    _job_stage(job, "failed", "宿主管理操作已结束，但验证被中断；请检查插件状态后重试。", ok=False, error_code="verification_interrupted", completed_at=time.time())
    if state.get("active_job_id") == job["job_id"]:
        state["active_job_id"] = ""
    if state.get("operation_task") is operation:
        state["operation_task"] = None


async def _verify_new_instance(context: Any, name: str, previous: Any, target: dict[str, Any]) -> Any:
    deadline = time.monotonic() + VERIFY_TIMEOUT
    while True:
        metadata = context.get_registered_star(name)
        instance = getattr(metadata, "star_cls", None)
        runtime = getattr(instance, RUNTIME_ATTRIBUTE, None)
        if instance is not None and instance is not previous and getattr(metadata, "activated", False) and isinstance(runtime, dict) and runtime.get("ready"):
            if runtime.get("version") != target["version"] or runtime.get("build") != target["build"]:
                raise MaintenanceError("version_not_loaded", "新实例已启动，但实际版本或修订号与目标不一致；请重新检测更新。")
            return instance
        if time.monotonic() >= deadline:
            raise MaintenanceError("reload_not_verified", "未确认新插件实例初始化完成，请查看 AstrBot 插件日志后重载。")
        await asyncio.sleep(VERIFY_INTERVAL)


async def _run_job(plugin: Any, state: dict[str, Any], job: dict[str, Any], fetch_text: FetchText) -> None:
    try:
        await asyncio.sleep(START_DELAY)
        manager, metadata = _resolve_plugin(plugin)
        busy = _busy_reason(plugin)
        if busy:
            raise MaintenanceError("generation_busy", busy)
        name = metadata.name
        if job["operation"] == "install":
            _job_stage(job, "checking", "正在重新检测远程版本与修订号…")
            checked = await check_update(plugin, fetch_text)
            if not checked["ok"]:
                raise MaintenanceError("check_failed", checked["message"])
            target = {"version": checked["latest_version"], "build": checked["target_build"]}
            job.update({"target_version": target["version"], "target_build": target["build"]})
            if not checked["update_available"]:
                raise MaintenanceError("already_latest", checked["message"] + "，无需重复安装。")
            # 检测网络返回后再查一遍实例与任务，防止旧宿主或外部入口绕过维护提示。
            manager, metadata = _resolve_plugin(plugin)
            busy = _busy_reason(plugin)
            if busy:
                raise MaintenanceError("generation_busy", busy)
            kwargs = _fixed_update_arguments(manager, metadata)
            _job_stage(job, "installing", "正在通过 AstrBot 安装更新，完成后宿主将自动重载插件…")
            await _await_host_operation(manager.update_plugin(name, **kwargs), state, job)
        else:
            target = _read_local_release()
            job.update({"target_version": target["version"], "target_build": target["build"]})
            _job_stage(job, "reloading", "正在通过 AstrBot 重载当前插件…")
            result = await _await_host_operation(manager.reload(name), state, job)
            if not isinstance(result, tuple) or len(result) != 2 or result[0] is not True:
                raise MaintenanceError("reload_failed", "AstrBot 未确认插件重载成功，请查看宿主插件日志。")
        _job_stage(job, "verifying", "正在核对新插件实例、初始化状态与版本修订号…")
        instance = await _verify_new_instance(plugin.context, name, plugin, target)
        actual = runtime_info(instance)
        _job_stage(job, "succeeded", "插件更新并重载成功。" if job["operation"] == "install" else "插件重载成功。", ok=True, current_version=actual["version"], current_build=actual["build"], instance_id=actual["instance_id"], completed_at=time.time())
    except asyncio.CancelledError:
        operation = state.get("operation_task")
        if operation is not None and not operation.done():
            _job_stage(job, job["status"], "维护验证被中断，正在等待宿主管理操作实际结束。")
            operation.add_done_callback(lambda task: _settle_interrupted_job(plugin, state, job, task))
        else:
            _job_stage(job, "failed", "维护任务被中断，请检查插件状态后重试。", ok=False, error_code="cancelled", completed_at=time.time())
        raise
    except MaintenanceError as exc:
        _job_stage(job, "failed", str(exc), ok=False, error_code=exc.code, completed_at=time.time())
    except Exception as exc:
        _log_failure(plugin, exc)
        _job_stage(job, "failed", "插件维护失败（%s），请检查 AstrBot 插件日志后重试。" % type(exc).__name__, ok=False, error_code="operation_failed", completed_at=time.time())
    finally:
        operation = state.get("operation_task")
        operation_running = operation is not None and not operation.done()
        if state.get("active_job_id") == job["job_id"] and not operation_running:
            state["active_job_id"] = ""
        if not operation_running:
            state["operation_task"] = None
        if state.get("task") is asyncio.current_task():
            state["task"] = None


def start_maintenance(plugin: Any, operation: str, fetch_text: FetchText) -> dict[str, Any]:
    """原子登记维护状态后再让出事件循环，防止重复点击与新生成任务竞态。"""
    if operation not in {"install", "reload"}:
        raise MaintenanceError("invalid_operation", "不支持的插件维护操作。")
    state = _state(getattr(plugin, "context", None))
    active = state["jobs"].get(state.get("active_job_id"), {})
    if active.get("status") in ACTIVE_STATUSES:
        return {**_public_job(active), "ok": False, "error_code": "maintenance_busy", "message": "已有插件维护任务正在进行，请等待完成。"}
    manager, metadata = _resolve_plugin(plugin)
    if operation == "install":
        _fixed_update_arguments(manager, metadata)
    elif not callable(getattr(manager, "reload", None)):
        raise MaintenanceError("reload_unsupported", "当前 AstrBot 缺少插件重载接口。")
    current = runtime_info(plugin)
    if not current.get("ready"):
        raise MaintenanceError("plugin_not_ready", "插件尚未初始化完成，请稍后重试。")
    busy = _busy_reason(plugin)
    if busy:
        raise MaintenanceError("generation_busy", busy)
    checked = state.get("last_check") or {}
    job_id = uuid.uuid4().hex
    job = {
        "ok": True, "job_id": job_id, "operation": operation, "status": "queued",
        "message": "维护任务已排队，正在准备更新。" if operation == "install" else "重载任务已排队。",
        "current_version": current["version"], "current_build": current["build"],
        "previous_build": current["build"],
        "target_version": checked.get("latest_version", current["version"]) if operation == "install" else current["version"],
        "target_build": checked.get("target_build", current["build"]) if operation == "install" else current["build"],
        "created_at": time.time(), "updated_at": time.time(),
    }
    state["jobs"][job_id] = job
    state["active_job_id"] = state["last_job_id"] = job_id
    while len(state["jobs"]) > 8:
        oldest = next(iter(state["jobs"]))
        if oldest == job_id:
            break
        del state["jobs"][oldest]
    state["task"] = asyncio.create_task(_run_job(plugin, state, job, fetch_text), name="gpthh-plugin-" + operation)
    return _public_job(job)
