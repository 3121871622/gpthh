"""中文创作指令紧接分辨率/英文描述的解析回归，不访问网络。"""
import pytest
from core.commands import KIND_DRAW, KIND_EDIT, KIND_VIDEO, parse_command, strict_trigger_allows
from core.config import PluginConfig


@pytest.mark.parametrize("command,kind", [
    ("文生视频", KIND_VIDEO), ("图生视频", KIND_VIDEO),
    ("生成视频", KIND_VIDEO), ("视频生成", KIND_VIDEO),
    ("绘画", KIND_DRAW), ("图片编辑", KIND_EDIT),
])
@pytest.mark.parametrize("prompt", [
    "4K电影级推广视频，广州塔夜景", "1080p城市夜景", "3D风格，镜头推进",
    "16:9横屏画面", "60fps smooth camera movement", "cinematic city skyline",
])
def test_chinese_content_command_accepts_ascii_prompt_head(command, kind, prompt):
    cfg = PluginConfig({"strict_trigger": True})
    parsed = parse_command(command + prompt, cfg)
    assert parsed is not None
    assert (parsed.kind, parsed.arg, parsed.command) == (kind, prompt, command)
    assert strict_trigger_allows(command, prompt)


@pytest.mark.parametrize("text", ["vid4K", "vid1080p夜景", "vid2猫", "img2cat", "edit3D夜景"])
def test_ascii_alias_keeps_word_boundary(text):
    cfg = PluginConfig({"strict_trigger": True, "video_commands": ["vid"],
                        "draw_commands": ["img"], "edit_commands": ["edit"]})
    assert parse_command(text, cfg) is None


@pytest.mark.parametrize("separator", [" ", "\n", "\t", "　"])
def test_ascii_alias_with_separator_still_works(separator):
    cfg = PluginConfig({"strict_trigger": True, "video_commands": ["vid"]})
    parsed = parse_command("vid" + separator + "4K night skyline", cfg)
    assert parsed is not None
    assert (parsed.kind, parsed.arg) == (KIND_VIDEO, "4K night skyline")


@pytest.mark.parametrize("text", ["我想文生视频4K夜景", "文生视频怎么用", "绘画的好看", "文生视频吗"])
def test_existing_non_command_chat_is_not_triggered(text):
    assert parse_command(text, PluginConfig({"strict_trigger": True})) is None


def test_strict_helper_agrees_with_ascii_prefix_guard():
    assert not strict_trigger_allows("vid", "4K")
    assert not strict_trigger_allows("img", "2猫")
    assert strict_trigger_allows("vid", " 4K night skyline")
