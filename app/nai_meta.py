"""NovelAI 出图元数据解析。

NAI 把参数放在 PNG 的 ``tEXt`` chunk 里，``keyword = "Comment"``，值是 JSON。
和 ComfyUI 完全不是一个格式，所以单独一个模块。

结构（Stealth PNG Info v3 / v4）::

    {
      "prompt": "...",                  # 正向
      "negative_prompt": "...",
      "seed": 123, "steps": 28, "scale": 5.0, "sampler": "k_euler_ancestral",
      "source": "NovelAI",
      "v4_prompt": {
        "caption": {
          "base_caption": "...",
          "char_captions": [{"char_caption": "...", "centers": [...]}, ...]
        }
      }
    }

关键点：``v4_prompt.caption.char_captions`` 是**多角色**分组，每组一个角色。
每组要单独成行，并且要判定这组是男是女 —— 见 :func:`is_male_group`。

读 chunk 时读完数据**必须再跳 4 字节 CRC**，漏跳会让后面所有 chunk 错位。
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from typing import Any

# 0 权重 = 禁用，直接丢掉
ZERO_WEIGHT = re.compile(r"^\s*0(?:\.0+)?::")

# 由脚本统一置顶的词，原标签里若有就去掉，避免重复
HEAD_WORDS = {
    "girl", "boy", "solo", "solo focus", "1girl", "1boy", "2girls", "2boys",
    "faceless", "faceless male", "male focus",
}

# 性别判定词表
MALE_BODY = {"penis", "testicles", "erection", "male pubic hair"}
MALE_HINT = {"faceless male", "male focus", "male pubic hair", "1boy", "2boys"}
FEMALE_BODY = {
    "breasts", "pussy", "large breasts", "huge breasts", "gigantic breasts",
    "medium breasts", "small breasts", "flat chest", "nipples", "large areolae",
}

# 粘连尺寸词：`hanging_breasts::cheerleader` 这种要切开
_SIZE_WORDS = (
    "gigantic|massive|huge|large|medium|small|pendulous|hanging|saggy"
)
_STUCK = re.compile(rf"({_SIZE_WORDS}) breasts(?=[a-z])", re.I)

# 角色系列词（用于识别角色标签）
SERIES_RE = re.compile(
    r"\(([^()]*(?:arknights|azur lane|honkai|wuthering waves|blue archive|"
    r"genshin|granblue|nikke)[^()]*)\)\s*$",
    re.I,
)

# 权重语法
_W_NUM = re.compile(r"-?\d+(?:\.\d+)?::")
_W_BRACES = re.compile(r"\{\{+|\}\}+")
_W_PAREN = re.compile(r"\(\s*([^()]*?)\s*:\s*-?\d+(?:\.\d+)?\s*\)")
_LEAD_NUM_DOT = re.compile(r"^-?\d+(?:\.\d+)?\.\s*")
_LEAD_NUM_COMMA = re.compile(r"^-?\d+(?:\.\d+)?,\s*")

# 标签开头的权重数字：`1.3::word::` / `-5.1::word::`
_LEAD_WEIGHT = re.compile(r"^(?P<w>-?\d+(?:\.\d+)?)\s*::")
# `by2.5::oving::` —— by 后面直接跟权重
_LEAD_WEIGHT_BY = re.compile(r"^by\s*(?P<w>-?\d+(?:\.\d+)?)\s*::", re.I)
# 画师分类前缀：`Artists:meion` / `artist:binggong asylum`（注意真实数据里有拼错的 artitst）
_ARTIST_PREFIX = re.compile(r"^(?:artists?|artitst|artisit)\s*:", re.I)
# by 前缀：`by swd3e2` / `by 2::akita hika::` / `by2.5::oving::`
_BY_PREFIX = re.compile(r"^by\b\s*(?:-?\d+(?:\.\d+)?\s*::)?", re.I)
# 一段里带权重的块，用来把 `by 2::a:: 1.63::b::` 切成一个一个
_WEIGHTED_CHUNK = re.compile(r"(?:by\s*)?-?\d+(?:\.\d+)?\s*::[^:]*?::", re.I)
# `::` 收尾处紧跟下一个词（前面不是逗号/空白）→ 补逗号
#   `2::breasts apart::autumn leaves` → `2::breasts apart::,autumn leaves`
_WEIGHT_END_BOUNDARY = re.compile(r"(?<=::)(?=[^\s,\n])")
# `::` 开头前面紧跟一个词（前面不是逗号/空白）→ 补逗号
#   `q,nude2::bent over::` → `q,nude,2::bent over::`
_WEIGHT_OPEN_BOUNDARY = re.compile(r"(?<=[^\s,\n])(?=-?\d+(?:\.\d+)?::)")


def read_png_comment(path: str | Path) -> dict[str, Any] | None:
    """读 PNG 里 tEXt chunk 的 Comment 字段并解析成 JSON。"""
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            while True:
                header = fh.read(8)
                if len(header) < 8:
                    return None
                length = struct.unpack(">I", header[:4])[0]
                kind = header[4:8]
                body = fh.read(length)
                fh.read(4)  # ★ CRC，必须跳，否则后面全错位
                if kind == b"tEXt":
                    keyword, _, value = body.partition(b"\x00")
                    if keyword == b"Comment":
                        try:
                            parsed = json.loads(value.decode("utf-8", "replace"))
                        except (ValueError, UnicodeDecodeError):
                            return None
                        return parsed if isinstance(parsed, dict) else None
                    if keyword == b"Description":
                        # 有些工具把 NAI 信息写这儿，值是 JSON 字符串
                        try:
                            parsed = json.loads(value.decode("utf-8", "replace"))
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if isinstance(parsed, dict) and "v4_prompt" in parsed:
                            return parsed
                elif kind == b"IEND":
                    return None
    except OSError:
        return None
    return None


def has_nai_metadata(path: str | Path) -> bool:
    """快速判断是不是 NAI 图（看有没有 v4_prompt / NAI 特征字段）。"""
    data = read_png_comment(path)
    if not data:
        return False
    return looks_like_nai(data)


def looks_like_nai(data: dict[str, Any]) -> bool:
    """已经解析出来的 JSON，判断是不是 NAI 格式。"""
    if "v4_prompt" in data or "v4_negative_prompt" in data:
        return True
    source = str(data.get("source") or "").lower()
    if "novelai" in source or "nai" == source:
        return True
    # NAI 独有：uc / qualityToggle / noise_schedule
    if any(key in data for key in ("noise_schedule", "qualityToggle", "uc")):
        return True
    return False


def _leading_weight(text: str) -> float | None:
    """取出标签开头的权重数字，没有就返回 None。

    ``1.3::Artists:meion::``        → 1.3
    ``-5.1::artist collaboration::`` → -5.1
    ``by2.5::oving::``              → 2.5
    """
    for pattern in (_LEAD_WEIGHT, _LEAD_WEIGHT_BY):
        match = pattern.match(text.strip())
        if match:
            try:
                return float(match.group("w"))
            except (TypeError, ValueError):
                return None
    return None


def is_artist_tag(text: str) -> bool:
    """是不是画师词。

    真实数据里的三种写法（先看词，权重不影响判断）：
      ``1.3::Artists:meion::``           → Artists: 前缀
      ``1.7::artist:binggong asylum::``  → artist: 前缀
      ``by2.5::oving::`` / ``by swd3e2`` → by 前缀
    """
    probe = (text or "").strip()
    if not probe:
        return False
    # by 后面粘着权重：`by2.5::oving::` → `by oving::`
    probe = _LEAD_WEIGHT_BY.sub("by ", probe)
    # 开头的纯权重：`1.3::Artists:meion::` → `Artists:meion::`
    probe = _LEAD_WEIGHT.sub("", probe)
    probe = probe.strip()
    if _ARTIST_PREFIX.match(probe):
        return True
    return bool(_BY_PREFIX.match(probe))


def norm_tag(raw: str) -> str:
    r"""标签规范化：剥 NAI 权重语法，把脏东西清掉。

    1. 空 → 丢
    2. 0 权重 / 负权重（``0::word::``、``-5.1::word::``）→ **整条丢弃**
       负权重是「不要什么」，不属于正向
    3. 画师词（``Artists:xxx`` / ``artist:xxx`` / ``by xxx``）→ **整条丢弃**
    4. 剥权重：``1.5::word::`` → ``word``
    5. 剥 ``{{word}}`` / ``(word:1.2)`` 外壳，保留 word
    6. 去转义反斜杠、下划线转空格、全角标点转半角
    7. 行首脏数据 ``0.`` ``1,`` 剥掉
    """
    if not raw:
        return ""
    text = raw.strip()
    if not text:
        return ""

    weight = _leading_weight(text)
    if weight is not None and weight <= 0:
        return ""
    if is_artist_tag(text):
        return ""

    text = _W_NUM.sub("", text)
    text = text.replace("::", "")
    text = _W_BRACES.sub("", text)
    text = _W_PAREN.sub(r"\1", text)
    text = _BY_PREFIX.sub("", text)      # 剥完权重可能剩个光杆 by

    text = text.replace("\\", "")
    text = text.replace("_", " ")
    text = text.replace("，", ",").replace("、", ",")
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("【", "").replace("】", "")
    text = re.sub(r"\s+", " ", text).strip(" ,")
    text = _LEAD_NUM_DOT.sub("", text)
    text = _LEAD_NUM_COMMA.sub("", text)
    return text.strip(" ,")


def split_tags(text: str) -> list[str]:
    """把一段 prompt 切成去重后的标签列表。

    处理真实数据里的三种粘连：
      - ``no text,skindentation``（缺空格）
      - ``hanging_breasts::cheerleader``
      - ``by 2::akita hika:: 1.63::(data in year 2025)::``（两坨挤在一起）
    """
    tags: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,\n]", text or ""):
        for chunk in _split_one_part(part):
            key = chunk.strip().lower()
            if key and key not in seen:
                seen.add(key)
                tags.append(chunk.strip())
    return tags


def _split_one_part(part: str) -> list[str]:
    """把一段（可能粘了好几坨）拆成若干干净标签。

    真实数据里 `::` 块的收尾经常**紧贴着下一个词**，没有逗号：
      ``2::gigantic breasts,breasts apart::autumn leaves``
    所以剥权重之前，先在每个 ``::`` 收尾处补一个逗号边界。
    """
    text = (part or "").strip()
    if not text:
        return []

    text = _WEIGHT_END_BOUNDARY.sub(",", text)
    text = _WEIGHT_OPEN_BOUNDARY.sub(",", text)

    # `by 2::akita hika:: 1.63::(data in year 2025)::`
    # 一段里挤了多组带权重的块，逐块切开再各自规范化
    weighted = _WEIGHTED_CHUNK.findall(text)
    if len(weighted) > 1:
        out: list[str] = []
        for piece in _WEIGHTED_CHUNK.split(text):
            out.extend(_split_one_part(piece))
        for piece in weighted:
            out.extend(_split_one_part(piece))
        return [x for x in out if x]

    tag = norm_tag(text)
    if not tag:
        return []

    result: list[str] = []
    pieces = [x.strip() for x in tag.split(",")] if "," in tag else [tag]
    for piece in pieces:
        # 粘连尺寸词：hanging breasts::cheerleader
        piece = _STUCK.sub(r"\1 breasts, ", piece)
        for sub in ([x.strip() for x in piece.split(",")] if "," in piece else [piece]):
            if sub:
                result.append(sub)
    return result


def is_male_group(tags: list[str], index: int = 0, n_groups: int = 1) -> bool:
    """判断角色组的性别。

    规则（按置信度）：
      1) 有男性专属词（penis / faceless male / male focus…）→ 男
      2) 有女性专属词（breasts / pussy / …）→ 女
      3) 两者都没有 + 不是第一组 → 判为男（NAI 双人图第二组通常是男伴）
      4) 其他 → 女
    """
    low = {t.lower() for t in tags}
    if low & MALE_BODY or low & MALE_HINT:
        return True
    if low & FEMALE_BODY:
        return False
    if index > 0:
        return True
    return False


def format_groups(groups: list[list[str]]) -> list[str]:
    """每角色一行：``girl, solo, ...`` / ``boy, solo, ...``"""
    lines: list[str] = []
    real = [g for g in groups if g]
    for index, group in enumerate(real):
        head = "boy, solo" if is_male_group(group, index, len(real)) else "girl, solo"
        body = [t for t in group if t.lower() not in HEAD_WORDS]
        lines.append(head + (", " + ", ".join(body) if body else ""))
    return lines


def parse(path: str | Path) -> dict[str, Any]:
    """解析一张 NAI 图，返回和 comfy_meta.extract 对齐的结构。"""
    data = read_png_comment(path)
    result: dict[str, Any] = {
        "format": "unknown",
        "positive": "",
        "negative": "",
        "loras": [],
        "models": [],
        "samplers": [],
        "has_metadata": False,
        "raw": None,
        "char_groups": [],
    }
    if not data:
        return result

    result["raw"] = data
    result["format"] = "nai"
    result["has_metadata"] = True

    caption = ((data.get("v4_prompt") or {}).get("caption") or {})
    char_captions = caption.get("char_captions") or []

    groups: list[list[str]] = []
    for entry in char_captions:
        text = entry.get("char_caption", "") if isinstance(entry, dict) else ""
        tags = split_tags(text)
        if tags:
            groups.append(tags)

    if not groups:
        # 没有多角色信息 → 退回 base_caption / prompt
        base = caption.get("base_caption") or data.get("prompt") or ""
        tags = split_tags(base)
        if tags:
            groups.append(tags)

    result["char_groups"] = groups
    # 正向 = 每角色一行的形式（也就是训练要的 caption 形态）
    lines = format_groups(groups)
    result["positive"] = "\n".join(lines)
    result["negative"] = str(data.get("negative_prompt") or "").strip()

    # 采样参数：塞进 models 让图库/打标台能显示
    sampler = str(data.get("sampler") or "").strip()
    if sampler:
        result["samplers"] = [sampler]
    source = str(data.get("source") or "").strip()
    if source:
        result["models"] = [source]

    result["nai_params"] = {
        "seed": data.get("seed"),
        "steps": data.get("steps"),
        "scale": data.get("scale"),
        "cfg_rescale": data.get("cfg_rescale"),
        "sampler": sampler,
        "noise_schedule": data.get("noise_schedule"),
        "vibe_count": len(data.get("reference_image_multiple") or []),
        "char_groups": len(groups),
    }
    return result


def flatten_tags(result: dict[str, Any]) -> list[str]:
    """把解析结果摊平成去重后的标签列表（跨所有角色组）。"""
    tags: list[str] = []
    seen: set[str] = set()
    for group in result.get("char_groups") or []:
        for tag in group:
            low = tag.lower()
            if low not in seen:
                seen.add(low)
                tags.append(tag)
    if not tags and result.get("positive"):
        for tag in split_tags(str(result["positive"]).replace("\n", ",")):
            low = tag.lower()
            if low not in seen:
                seen.add(low)
                tags.append(tag)
    return tags
