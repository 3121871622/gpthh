# -*- coding: utf-8 -*-
"""内置预设库：提示词预设、抽卡画风池、出图文案与安全占位符渲染。

本模块只包含**纯数据与纯函数**：不读配置、不写文件、不发网络请求，
因此可以被 ``core/config.py``（回落内置库）、``main.py``（菜单与指令）
以及前端说明安全复用；即使配置层缺失也能独立导入。

设计约定
--------
* ``DEFAULT_PROMPTS`` / ``DEFAULT_GACHA_STYLES`` 的条目都是 ``dict``，
  调用方按字段名取值，不要依赖列表顺序（顺序只用于菜单展示）。
* ``render_preset`` 只替换白名单里的占位符，未命中的 ``{xxx}`` 原样保留，
  所以预设文本里可以安全地写花括号参数说明，不会触发 KeyError。
* 所有文案均为简体中文，避免出现平台敏感词、真人姓名与品牌名。
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "DEFAULT_PROMPTS",
    "DEFAULT_GACHA_STYLES",
    "DEFAULT_FLAVOR_LINES",
    "GACHA_RARITY_WEIGHTS",
    "RARITY_LABELS",
    "PRESET_PLACEHOLDERS",
    "PRESET_FALLBACKS",
    "PLACEHOLDER_ALIASES",
    "render_preset",
    "find_prompt",
    "prompt_names",
    "pick_gacha_style",
    "pick_flavor_line",
    "normalize_name",
]


# --------------------------------------------------------------------------- 工具
def normalize_name(value: Any) -> str:
    """归一化名称：去首尾空白、去掉用于展示的 ``#`` 前缀、全角空格转半角。"""
    text = str(value or "").replace("\u3000", " ").strip()
    while text[:1] in ("#", "\uff03"):
        text = text[1:].strip()
    return text


# ----------------------------------------------------------------- 内置提示词库
DEFAULT_PROMPTS: List[Dict[str, Any]] = [
    {
        "name": "日系唯美奇幻插画",
        "text": (
            "参考图是角色人设图，为参考图的少女绘制一副日系唯美奇幻风格插画。 【构图】这是一个宏大的中景日系奇幻插画构图，"
            "画面中心是完全保留了完整细节的可爱少女，她站立在无边的、如镜面般平滑的水面中心。天空呈现出高饱和度的粉紫与深蓝交织，"
            "一条耀眼的蓝色巨型流星划破天际，配合着边缘发光的瑰丽层云。女孩处于背光状态，形成一个暗调但依然清晰可辨其服装和紫色明亮眼眸的剪影，"
            "被流星和星空的边缘光细腻勾勒，她微微仰头，一只手轻轻张开。下方的水面完美、对称地反射出整个壮丽的星空、流星、云彩，以及女孩清晰的倒影，"
            "点缀着微小的发光点，营造出天人合一、空灵静谧的唯美梦境意境。 【日系唯美奇幻风格说明】该风格以高饱和度的粉紫冷暖色调交织出浩瀚星空，"
            "并辅以壮丽的流星与边缘发光的层云作为视觉奇观；画面巧妙利用“天空之镜”般的完美水面反射，将宏大的宇宙背景与孤独静立的人物剪影相融合，"
            "通过极具电影感的光影渲染与高对比度的表现手法，营造出一种空灵、静谧且带有超现实宿命感的梦境氛围。 【要求】生成图片的比例9:16，分辨率 4k。"
            "这个则是插画艺术创作图"
        ),
        "tags": ["插画", "奇幻", "日系", "唯美", "竖版"],
    },
    {
        "name": "手办化",
        "text": (
            "把参考图中的角色做成收藏级手办：1/7 比例 PVC 材质，人物站在透明圆形底座上，底座边缘有细微的镭射反光；"
            "棚拍纯色背景，主光从左上角 45 度打下，轮廓光勾出头发与衣摆边缘；皮肤呈现哑光质感，服装褶皱与金属配件保留高光；"
            "画面锐利、细节丰富，4K 商业产品摄影质感。保持角色五官、发型与服装配色与参考图一致。"
        ),
        "tags": ["手办", "模型", "产品", "人像"],
    },
    {
        "name": "电影感光影",
        "text": (
            "为画面加入电影级光影：黄昏逆光，暖橙色夕阳从主体背后低角度射入，空气中有可见的浮尘与轻微体积光；"
            "前景压暗形成画框式构图，主体面部保留柔和补光；整体青橙色调，高光轻微过曝，暗部保留细节而非死黑；"
            "加入浅景深与细微胶片颗粒，像 35mm 电影胶片的定格画面。"
        ),
        "tags": ["光影", "电影", "氛围", "调色"],
    },
    {
        "name": "赛博朋克",
        "text": (
            "赛博朋克夜景：高密度霓虹招牌与全息广告铺满背景，主色调为品红、青蓝与电紫；地面刚下过雨，"
            "积水反射出拉长的霓虹光带；人物穿机能风外套，身上有细小的发光配件；镜头带轻微光晕与色散，边缘泛紫边；"
            "低角度仰拍增强压迫感，整体冷色调、高对比、8K 细节。"
        ),
        "tags": ["赛博朋克", "科幻", "夜景", "霓虹"],
    },
    {
        "name": "国风水墨",
        "text": (
            "中国水墨风格：宣纸底纹，墨色浓淡分出远近层次，远山以淡墨晕染、留白成云海；近景几笔枯墨勾出松枝与飞鸟，"
            "主体用熟宣工笔勾勒轮廓再以淡彩罩染；画面一侧留出题款位置；整体气韵清冷，笔触可见飞白，"
            "像一幅当代水墨长卷的局部。"
        ),
        "tags": ["国风", "水墨", "东方", "意境"],
    },
    {
        "name": "像素风",
        "text": (
            "16-bit 像素艺术：限定 32 色调色板，主体与场景全部用清晰的方形像素块堆叠，边缘做轻微抗锯齿；"
            "背景用网格化渐变区分远近，加入两三个动态光点暗示；构图为斜 45 度等距视角，"
            "带一点复古掌机的扫描线质感；放大后依然锐利，不做模糊处理。"
        ),
        "tags": ["像素", "复古", "游戏", "等距"],
    },
    {
        "name": "二次元头像",
        "text": (
            "二次元头像：正面或四分之三侧脸，日系赛璐璐上色，头发分层高光带月牙形反光；眼睛用多段渐变与高光点表現通透感；"
            "背景做成柔和的糖果色渐变或半透明几何图形，主体居中且与背景保留清晰边缘；线条干净、色彩明快，"
            "正方形构图，适合直接作为社交头像。"
        ),
        "tags": ["头像", "二次元", "人像", "方图"],
    },
    {
        "name": "电商产品图",
        "text": (
            "电商主图风格：纯白或浅灰无缝背景，产品居中，四周留出约 15% 边距；两灯棚拍布光（主光加柔光箱、侧光压出高级感阴影），"
            "底部有自然的接触阴影；材质细节清晰可见（金属拉丝、玻璃通透、布料纹理）；"
            "整体明亮通透、无色偏，1:1 比例，可直接上架使用。"
        ),
        "tags": ["产品", "电商", "棚拍", "方图"],
    },
    {
        "name": "儿童绘本",
        "text": (
            "儿童绘本插画：手绘蜡笔与彩铅质感，线条圆润不平滑，色彩是低饱和的奶油色系；"
            "造型把主体夸张成圆头短肢的可爱比例，表情憨态可掬；背景简单留白，只点缀几片云、几朵花；"
            "整体温暖治愈，带一点点纸张颗粒感，横构图方便排版配文字。"
        ),
        "tags": ["绘本", "儿童", "手绘", "治愈"],
    },
    {
        "name": "古典油画",
        "text": (
            "古典油画：伦勃朗式明暗对照，暗褐色背景里只有一束柔光落在主体侧脸与肩部；笔触可见、颜料有厚度，"
            "暗部用透明罩染做出温润的层次；色彩以赭石、深红与灰蓝为主；画面带轻微老化纹理与清漆反光，"
            "像博物馆里一幅十七世纪写实肖像的局部。"
        ),
        "tags": ["油画", "古典", "肖像", "写实"],
    },
    {
        "name": "极简扁平",
        "text": (
            "极简扁平插画：只用三到四个纯色色块加一两个点缀色，去掉所有渐变与阴影；主体用几何图形概括，边缘圆角统一；"
            "大量留白，主体放在三分线交点；线条粗细一致，画面干净现代，适合做海报或应用启动页。"
        ),
        "tags": ["极简", "扁平", "海报", "矢量"],
    },
    {
        "name": "3D 渲染",
        "text": (
            "3D 卡通渲染：电影级三维软件风格，主体材质柔和、色彩高饱和，表面带轻微磨砂质感；"
            "使用三点布光加一块大面积柔光，阴影软而不散；背景为低饱和渐变或纯色，加入几个漂浮的小几何体增强空间感；"
            "整体像动画电影的静帧，画面干净、体积感强，8K 输出。"
        ),
        "tags": ["3D", "渲染", "卡通", "立体"],
    },
    {
        "name": "蒸汽波复古海报",
        "text": (
            "蒸汽波复古海报：粉紫渐变天空，居中一个大号网格地平线，太阳用粉橙同心圆表现；"
            "叠加录像带扫描线、轻微色差与噪点，整体像八十年代录像带画面的定格；"
            "画面下方留出一条干净的横排文字区域，方便后期排版。"
        ),
        "tags": ["蒸汽波", "复古", "海报", "合成"],
    },
    {
        "name": "治愈系微缩景观",
        "text": (
            "治愈系微缩景观：把主体缩小放进一个玻璃罩或苔藓小岛里，周围点缀迷你路灯、小树与石阶；"
            "移轴摄影效果，上下边缘做虚化处理，焦点只落在中间的主体上；光线是清晨的柔光，空气里有淡淡雾气；"
            "色彩清新、细节丰富，像桌面上一件可以捧在手心的小小世界。"
        ),
        "tags": ["微缩", "治愈", "移轴", "场景"],
    },
]


# ------------------------------------------------------------------- 抽卡画风池
#: 稀有度权重（越稀有数值越小，抽卡时按权重随机）
GACHA_RARITY_WEIGHTS: Dict[str, int] = {
    "common": 70,
    "rare": 25,
    "epic": 5,
}

#: 稀有度中文标签（用于抽卡结果播报）
RARITY_LABELS: Dict[str, str] = {
    "common": "普通",
    "rare": "稀有",
    "epic": "史诗",
}

DEFAULT_GACHA_STYLES: List[Dict[str, Any]] = [
    {
        "name": "水彩小清新",
        "text": "水彩小清新插画：湿画法晕染出通透的色块，边缘自然洇开，颜色以薄荷绿、浅黄与樱粉为主；纸张纹理清晰，留白多，画面轻盈透气。",
        "rarity": "common",
    },
    {
        "name": "蜡笔涂鸦",
        "text": "蜡笔涂鸦风：笔触粗糙有力，颜色叠涂出颗粒感，造型随意可爱，背景是一张有折痕的牛皮纸，整体像小朋友的得意画作。",
        "rarity": "common",
    },
    {
        "name": "速写线稿",
        "text": "铅笔速写线稿：只用黑白灰，线条有轻重变化，保留辅助线与擦除痕迹；重点部位用排线加强明暗，背景大面积留白。",
        "rarity": "common",
    },
    {
        "name": "复古胶片",
        "text": "复古胶片摄影：柯达金 200 色调，暖黄偏绿，高光柔和溢出，带明显颗粒与轻微漏光；构图像随手抓拍，自然不摆拍。",
        "rarity": "common",
    },
    {
        "name": "糖果色卡通",
        "text": "糖果色卡通：高饱和的糖果配色，粗黑描边，扁平上色加一层硬阴影；造型圆润夸张，画面活泼，像儿童频道动画的截图。",
        "rarity": "common",
    },
    {
        "name": "赛博霓虹",
        "text": "赛博霓虹：紫红与青蓝双色打光，主体边缘被霓虹勾出轮廓，背景是虚化的城市灯牌；画面高对比、带轻微辉光与色差，未来感强烈。",
        "rarity": "rare",
    },
    {
        "name": "蒸汽朋克",
        "text": "蒸汽朋克：黄铜、皮革与深棕木料构成主体，齿轮与表盘做精细纹样；暖黄汽灯打光，空气中有蒸汽与煤烟，整体像维多利亚时代的机械幻想。",
        "rarity": "rare",
    },
    {
        "name": "国风水墨",
        "text": "国风水墨：宣纸底，墨色分五色，远山淡染、留白成雾；主体以工笔勾轮廓再淡彩罩染，画面一角留题款与印章位置。",
        "rarity": "rare",
    },
    {
        "name": "立体几何拼贴",
        "text": "立体几何拼贴：主体被拆解成圆形、三角与多面体，用硬边阴影堆出体积感；配色遵循互补色原则，背景是纯色大色块，构成感极强。",
        "rarity": "rare",
    },
    {
        "name": "二次元赛璐璐",
        "text": "二次元赛璐璐：清晰线稿加两段式阴影，头发有月牙形高光，眼睛用多段渐变表現通透感；背景是柔和渐变的天空与云。",
        "rarity": "rare",
    },
    {
        "name": "博物馆级油画",
        "text": "博物馆级油画：厚涂笔触清晰可见，明暗对照强烈，暗部透明罩染出温润层次；色彩沉稳，画面带清漆反光与细微老化纹理，像名画真迹的局部。",
        "rarity": "epic",
    },
    {
        "name": "星云幻想",
        "text": "星云幻想：主体置身于粉紫与深蓝交织的宇宙星云中，周围漂浮着发光尘埃与碎星；背光勾勒出剪影，下方是镜面般的水面倒影，宏大、空灵、梦幻。",
        "rarity": "epic",
    },
]


# --------------------------------------------------------------------- 出图文案
DEFAULT_FLAVOR_LINES: List[str] = [
    "这构图，摄影系看了要连夜改毕业作品。",
    "光影绝了，建议直接打印出来挂客厅。",
    "色彩调得比我心情还稳定。",
    "这张的细节多到可以拿放大镜看半天。",
    "说实话，这质量有点超出我的工资水平了。",
    "氛围感拉满，配首音乐就能当 MV 封面。",
    "主体突出，背景干净，教科书级别。",
    "画质清晰到能数清头发丝。",
    "这张要是发朋友圈，评论区要排队夸。",
    "构图稳、配色准、光影甜，三连。",
    "建议保存原图，别让压缩毁了它。",
    "这波属于是甲方看了都说好的那种。",
    "质感在线，放大十倍也不虚。",
    "看着就想给这张图配一段旁白。",
    "画风统一，细节克制，很有高级感。",
    "这张适合做壁纸，也适合做屏保。",
    "从构图到配色都很讲道理，我挑不出毛病。",
    "出图速度比我点外卖还快。",
    "这张的角度选得很聪明，主体一下就立住了。",
    "质感细腻，光影温柔，适合慢慢看。",
    "配色很舒服，眼睛一点都不累。",
    "画面干净又有层次，一看就是认真调的。",
    "这张的留白用得很好，呼吸感很足。",
    "细节都在该在的位置上，很稳。",
    "氛围到位，故事感已经有了。",
    "这一张，值得单独建个相册收藏。",
]


# ----------------------------------------------------------------- 占位符与渲染
#: 预设文本里可用的占位符 -> 中文说明（白名单，只有这里的 key 会被替换）
PRESET_PLACEHOLDERS: Dict[str, str] = {
    "{主体}": "本次要画的主体或内容（通常就是用户输入的描述）",
    "{参考图说明}": "本次是否带了参考图，例如「参考图已提供，请严格保持角色五官与配色一致」",
    "{风格}": "追加的风格描述，例如「日系唯美奇幻风格」",
    "{尺寸}": "本次使用的图片尺寸，例如 1024x1024",
    "{比例}": "本次使用的画面比例，例如 9:16",
    "{画质}": "画质要求，例如「4K、高细节、锐利对焦」",
    "{数量}": "本次预计出图张数",
    "{补充要求}": "用户在指令里额外写的要求（未填写时会被整句移除）",
}

#: 各占位符的默认值：调用方没传参时可用它补全（``render_preset`` 本身不强制填充）
PRESET_FALLBACKS: Dict[str, str] = {
    "{主体}": "画面主体",
    "{参考图说明}": "参考图已提供，请严格保持角色五官、发型与配色一致",
    "{风格}": "高质量插画风格",
    "{尺寸}": "1024x1024",
    "{比例}": "1:1",
    "{画质}": "4K，高细节，锐利对焦",
    "{数量}": "1",
    "{补充要求}": "",
}

#: 允许用英文别名传参，方便 main.py 与前端调用
PLACEHOLDER_ALIASES: Dict[str, str] = {
    "subject": "{主体}",
    "ref_note": "{参考图说明}",
    "refer": "{参考图说明}",
    "style": "{风格}",
    "size": "{尺寸}",
    "ratio": "{比例}",
    "quality": "{画质}",
    "count": "{数量}",
    "extra": "{补充要求}",
}


#: 反向别名表：中文占位符 -> 允许的英文写法列表
_ALIAS_BY_PLACEHOLDER: Dict[str, List[str]] = {}
for _alias, _target in PLACEHOLDER_ALIASES.items():
    _ALIAS_BY_PLACEHOLDER.setdefault(_target, []).append(_alias)


def _resolve_placeholder_keys(kwargs: Dict[str, Any]) -> Dict[str, str]:
    """把调用方传入的关键字参数映射成白名单占位符，非白名单的键直接忽略。

    同时接受 ``{主体}`` 与 ``subject`` / ``{subject}`` 三种写法。
    """
    mapping: Dict[str, str] = {}
    for raw_key, value in (kwargs or {}).items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        if not key.startswith("{"):
            key = PLACEHOLDER_ALIASES.get(key.lower(), "{" + key + "}")
        elif key not in PRESET_PLACEHOLDERS:
            key = PLACEHOLDER_ALIASES.get(key.strip("{}").lower(), key)
        if key not in PRESET_PLACEHOLDERS:
            continue
        mapping[key] = "" if value is None else str(value)
    return mapping


def _replacement_pairs(mapping: Dict[str, str]) -> List[Tuple[str, str, bool]]:
    """把「占位符 -> 值」展开成可替换对，同时覆盖中文与英文别名写法。

    返回 ``[(显示文本, 替换值, 是否补充要求), ...]``，按 key 长度降序排列，
    避免短占位符先替换掉长占位符的一部分。
    """
    pairs: List[Any] = []
    for key, value in mapping.items():
        aliases = ["{" + alias + "}" for alias in _ALIAS_BY_PLACEHOLDER.get(key, [])]
        for form in [key] + aliases:
            pairs.append((form, value, key == "{补充要求}"))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def render_preset(text: Any, **kwargs: Any) -> str:
    """安全替换预设文本里的白名单占位符。

    * 只替换 ``PRESET_PLACEHOLDERS`` 里登记过的占位符，其余 ``{xxx}`` 原样保留；
    * 不使用 ``str.format``，因此用户输入里含花括号也不会报错；
    * ``{补充要求}`` 传空串时会把整行「补充要求：」一起抹掉，避免留下空条目。
    """
    result = "" if text is None else str(text)
    if not result:
        return ""
    mapping = _resolve_placeholder_keys(kwargs)
    if not mapping:
        return result
    for form, value, is_extra in _replacement_pairs(mapping):
        if is_extra and not value.strip():
            for prefix in ("补充要求：", "补充要求:", "【补充要求】"):
                result = result.replace(prefix + form, "")
            result = result.replace(form, "")
            continue
        result = result.replace(form, value)
    return result


# --------------------------------------------------------------------- 查找预设
def _is_index_text(text: str) -> bool:
    """判断一段文本是否只是序号（支持 ``3`` / ``#3`` / ``第3``）。"""
    candidate = normalize_name(text)
    for prefix in ("第", "序号"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):].strip()
    for suffix in ("个", "条", "号", "项"):
        if candidate.endswith(suffix):
            candidate = candidate[:-len(suffix)].strip()
    if not candidate:
        return False
    for char in candidate:
        if char not in "0123456789０１２３４５６７８９":
            return False
    return True


def _as_number(text: str) -> int:
    """把（可能是全角的）序号文本转成 int，失败返回 -1。"""
    candidate = normalize_name(text)
    for prefix in ("第", "序号"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):].strip()
    for suffix in ("个", "条", "号", "项"):
        if candidate.endswith(suffix):
            candidate = candidate[:-len(suffix)].strip()
    table = {ord(full): ord(half) for full, half in zip("０１２３４５６７８９", "0123456789")}
    try:
        return int(candidate.translate(table))
    except Exception:  # noqa: BLE001
        return -1


def _entry_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or "").strip()
    return ""


def _copy_entry(entry: Any) -> Dict[str, Any]:
    """深拷贝一条提示词条目，避免调用方改动 tags 列表时污染内置库。"""
    result: Dict[str, Any] = {}
    if isinstance(entry, dict):
        for key, value in entry.items():
            if isinstance(value, list):
                result[key] = list(value)
            else:
                result[key] = value
    return result


def _entry_tags(entry: Any) -> List[str]:
    if not isinstance(entry, dict):
        return []
    raw = entry.get("tags")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def prompt_names(prompts: Sequence[Any]) -> List[str]:
    """返回提示词库里的全部名称（保持原顺序）。"""
    return [_entry_name(item) for item in (prompts or []) if _entry_name(item)]


def find_prompt(prompts: Sequence[Any], name: Any) -> Optional[Dict[str, Any]]:
    """按「名称精确 -> 1-based 序号 -> 唯一子串 -> 唯一标签」查找提示词。

    命中返回条目副本（``dict``），找不到或有歧义时返回 ``None``。
    """
    target = normalize_name(name)
    if not target:
        return None
    entries = [item for item in (prompts or []) if isinstance(item, dict) and _entry_name(item)]
    if not entries:
        return None

    lowered = target.lower()
    # 1) 名称精确匹配（先区分大小写，再忽略大小写）
    for entry in entries:
        if _entry_name(entry) == target:
            return _copy_entry(entry)
    for entry in entries:
        if _entry_name(entry).lower() == lowered:
            return _copy_entry(entry)

    # 2) 1-based 序号
    if _is_index_text(target):
        index = _as_number(target)
        if 1 <= index <= len(entries):
            return dict(entries[index - 1])
        return None

    # 3) 唯一子串匹配
    matched = [entry for entry in entries if lowered in _entry_name(entry).lower()]
    if len(matched) == 1:
        return _copy_entry(matched[0])
    if len(matched) > 1:
        return None

    # 4) 唯一标签匹配（兜底：例如「赛博」命中标签「赛博朋克」）
    tagged = [entry for entry in entries if any(lowered in tag.lower() for tag in _entry_tags(entry))]
    if len(tagged) == 1:
        return _copy_entry(tagged[0])
    return None


# --------------------------------------------------------------------- 随机辅助
def pick_gacha_style(
    styles: Optional[Sequence[Any]] = None,
    rng: Optional[random.Random] = None,
) -> Optional[Dict[str, Any]]:
    """按稀有度权重抽取一个画风，池子为空时返回 ``None``。"""
    pool: List[Dict[str, Any]] = []
    for item in (styles if styles is not None else DEFAULT_GACHA_STYLES) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        text = str(item.get("text") or "").strip()
        if not name or not text:
            continue
        rarity = str(item.get("rarity") or "common").strip().lower()
        if rarity not in GACHA_RARITY_WEIGHTS:
            rarity = "common"
        pool.append({"name": name, "text": text, "rarity": rarity})
    if not pool:
        return None
    weights = [GACHA_RARITY_WEIGHTS.get(item["rarity"], 1) for item in pool]
    generator = rng or random
    chosen = generator.choices(pool, weights=weights, k=1)
    return _copy_entry(chosen[0]) if chosen else None


def pick_flavor_line(
    lines: Optional[Sequence[Any]] = None,
    rng: Optional[random.Random] = None,
) -> str:
    """随机取一条出图文案；文案池为空时返回空串。"""
    pool = [str(item).strip() for item in (lines if lines is not None else DEFAULT_FLAVOR_LINES) or []]
    pool = [item for item in pool if item]
    if not pool:
        return ""
    generator = rng or random
    return generator.choice(pool)