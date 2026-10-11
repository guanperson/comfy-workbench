"""配套 txt 标注文件的读写。

训练集的标注是 ``xxx.png`` 旁边同名的 ``xxx.txt``，整份按逗号切分。

这个模块只负责「文件这一层」：读、算 diff、备份、原子写。
所有的编辑都先在内存里攒着（见 db 里的 caption 暂存表），
用户点保存才真的落盘 —— 训练集标注误删一个 tag 可能毁掉一批训练，
不能边点边写。
"""

from __future__ import annotations

import difflib
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

# 文件名后缀对应关系：图 → 标注
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
CAPTION_SUFFIX = ".txt"


def caption_path_for(image_path: str | Path) -> Path:
    """图片路径 → 配套标注路径（同名同目录，后缀换 .txt）。"""
    return Path(image_path).with_suffix(CAPTION_SUFFIX)


def has_caption(image_path: str | Path) -> bool:
    return caption_path_for(image_path).is_file()


def split_caption(text: str) -> list[str]:
    """按逗号切分标注（保留原始书写，只做去空白）。

    分隔符放宽：半角/全角逗号、顿号、分号、换行都算。
    中文输入法下很容易打出「，」「、」，不该被当成标签内容。
    """
    normalized = text or ""
    for src, dst in (("，", ","), ("、", ","), ("；", ","), ("﹑", ",")):
        normalized = normalized.replace(src, dst)
    tags: list[str] = []
    for part in re.split(r"[,\n;]+", normalized):
        tag = part.strip()
        if tag:
            tags.append(tag)
    return tags


def join_caption(tags: list[str]) -> str:
    """标签列表 → 文件内容（逗号 + 空格，段落末尾换行）。"""
    return ", ".join(tags) + "\n" if tags else ""


# ====================================================================== #
# 标签写法转换
# ====================================================================== #
# Danbooru 系的角色词写成 `swire \(arknights\)`（反斜杠转义括号），
# 有些训练脚本/工具习惯 `swire (arknights)`（空格 + 裸括号）或
# `swire_(arknights)`（下划线）。这里提供互转。
#
# 只认带「命名空间括号」的词 —— 也就是形如 `xxx (yyy)` 的整体。
# 普通标签（`long hair`、`oiled body`）不会被碰。

# 一个「命名空间括号」单元：
#   \(arknights\)   (arknights)   _(arknights)
_NS_UNIT = r"\\?\(\s*[^()\\]+?\s*\\?\)"
# 角色词整体：名字 + 一个或多个命名空间括号。
# 单元之间允许 空格 / 下划线 / 直接相连（三种写法混用也能认）。
_ROLE_RE = re.compile(
    rf"^(?P<name>[^()\\]+?)"
    rf"[\s_]*(?P<namespaces>{_NS_UNIT}(?:[\s_]*{_NS_UNIT})*)$"
)
# 从命名空间串里逐个抠出内容
_NS_INNER_RE = re.compile(r"\\?\(\s*([^()\\]+?)\s*\\?\)")

# 已知作品名。文档《448style_角色名格式与括号转义》的判定规则要求
# 「括号内容含作品名」才转义，所以这里列一份。
# 下面的都来自用户数据集（448style）实际出现过的标签。
_KNOWN_WORKS = {
    "arknights",
    "azur lane",
    "honkai: star rail",
    "umamusume",
    "blue archive",
    "genshin impact",
    "wuthering waves",
    "elden ring",
    "fate",
    "fate/grand order",
    "nikke",
    "girls' frontline",
    "punishing: gray raven",
    "reverse: 1999",
    "path to nowhere",
    "tower of fantasy",
    "puella magi madoka magica",
    "the purple rabbit's marksmanship",   # 用户数据里的皮肤名
}
# 结尾像作品名的（换新作品时不用改代码也能认出来）
_WORK_SUFFIXES = (
    " lane", "lane", "arknights", "waves", "rail", "archive",
    "impact", "order", "frontline", "ring", "fantasy",
)
# 物件类型的命名空间。用户明确说过：物件也当角色处理
# （anima 就是这么打标的），所以这些也要转义。
_OBJECT_NAMESPACES = {
    "substance", "clothing", "food", "vehicle", "weapon", "material",
    "container", "furniture", "instrument", "tool",
}


def looks_like_role_tag(tag: str) -> bool:
    """是不是带命名空间的角色词。

    角色和物件都算（anima / Danbooru 就是这么标的）：
    `swire \\(arknights\\)`、`slime (substance)`、
    `zara \\(poolside coincidence\\) \\(azur lane\\)`（带变体名的双命名空间）。
    单纯带括号但不是命名空间形式的（比如 `(worst quality)`）不算。
    """
    return role_tag_parts(tag) is not None


def looks_like_work_namespace(ns: str) -> bool:
    """这个括号内容像不像「作品名 / 物件名」—— 也就是该不该转义。

    文档《448style_角色名格式与括号转义》的判定规则：
    只有括号内容含**作品名**才算角色名，才该转义。

    像 `(larger)`、`(cosplay)`、`(magical girl)`、`(sexual)` 这种是
    **修饰语**，不是作品名，转了反而把普通标签改成奇怪的样子。

    物件命名空间（`slime \\(substance\\)`）也算 —— 用户明确说过物件
    按角色处理，anima 就是这么打标的。
    """
    text = (ns or "").strip().lower()
    if not text:
        return False
    if text in _KNOWN_WORKS or text in _OBJECT_NAMESPACES:
        return True
    # 带冒号的（honkai: star rail）基本是作品名
    if ":" in text:
        return True
    # 结尾像作品名的
    return any(text.endswith(suffix) for suffix in _WORK_SUFFIXES)


def role_namespaces(tag: str, *, require_work: bool = False) -> tuple[str, list[str]] | None:
    """拆出 (角色名, [命名空间...])；不是角色词返回 None。

    支持多个命名空间：`zara \\(poolside coincidence\\) \\(azur lane\\)`
    → ('zara', ['poolside coincidence', 'azur lane'])

    ``require_work=True`` 时，要求至少有一个命名空间像作品名
    （转换按钮用这个，避免把 `alternate breast size (larger)` 也改了）。
    """
    text = (tag or "").strip()
    if not text or len(text) > 160:
        return None
    match = _ROLE_RE.match(text)
    if not match:
        return None
    name = match.group("name").strip()
    if not name:
        return None
    namespaces = [
        part.strip()
        for part in _NS_INNER_RE.findall(match.group("namespaces"))
        if part.strip()
    ]
    if not namespaces or any(len(ns) > 60 for ns in namespaces):
        return None
    if require_work and not any(looks_like_work_namespace(ns) for ns in namespaces):
        return None
    return name, namespaces


def role_tag_parts(tag: str) -> tuple[str, str] | None:
    """拆出 (角色名, 命名空间)。多个命名空间时用中间的点号连起来给人看。"""
    parsed = role_namespaces(tag)
    if parsed is None:
        return None
    name, namespaces = parsed
    return name, " / ".join(namespaces)


def to_plain_role_tag(tag: str) -> str:
    r"""角色词 → 空格 + 裸括号。

    `swire \(arknights\)`   → `swire (arknights)`
    `swire_(arknights)`    → `swire (arknights)`
    `zara \(poolside coincidence\) \(azur lane\)`
      → `zara (poolside coincidence) (azur lane)`

    命名空间里的下划线也换成空格；不是角色词的原样返回。
    """
    parsed = role_namespaces(tag)
    if parsed is None:
        return tag
    name, namespaces = parsed
    clean_name = name.strip().replace("_", " ")
    suffix = " ".join(f"({ns.strip().replace('_', ' ')})" for ns in namespaces)
    return f"{clean_name} {suffix}"


def to_underscore_role_tag(tag: str) -> str:
    """角色词 → 下划线写法（anima / Danbooru 推荐）。

    `swire \\(arknights\\)`  →  `swire_(arknights)`
    `zara \\(poolside coincidence\\) \\(azur lane\\)`
      →  `zara_(poolside coincidence)_(azur lane)`

    角色名和命名空间里的空格换下划线；不是角色词的原样返回。
    """
    parsed = role_namespaces(tag)
    if parsed is None:
        return tag
    name, namespaces = parsed
    name_part = name.strip().replace(" ", "_")
    suffix = "".join(f"_({ns.strip().replace(' ', '_')})" for ns in namespaces)
    return f"{name_part}{suffix}"


def to_escaped_role_tag(tag: str) -> str:
    r"""角色词 → Danbooru 转义写法（448style 的目标形态）。

    `swire (arknights)`   → `swire \(arknights\)`
    `swire_(arknights)`   → `swire \(arknights\)`
    `musashi (the purple rabbit's marksmanship) (azur lane)`
      → `musashi \(the purple rabbit's marksmanship\) \(azur lane\)`

    命名空间里的下划线也会换成空格（Anima 规范：除 score tag 外都用空格）。

    **只转「括号内容像作品名」的**，这样 `alternate breast size (larger)`、
    `kaname madoka (cosplay)` 这类修饰语不会被误改。
    """
    parsed = role_namespaces(tag, require_work=True)
    if parsed is None:
        return tag
    name, namespaces = parsed
    # 角色名和命名空间的下划线都还原成空格，再统一加转义括号
    clean_name = name.strip().replace("_", " ")
    suffix = " ".join(f"\\({ns.strip().replace('_', ' ')}\\)" for ns in namespaces)
    return f"{clean_name} {suffix}"



def letter_underscores_to_spaces(tag: str) -> str:
    r"""把标签里的下划线换成空格。

    角色词（带命名空间括号的）会把角色名和命名空间都转，并统一成
    「空格 + 裸括号」：male_rover_\(wuthering_waves\)
    → male rover (wuthering waves)。

    普通标签直接替换下划线，结尾多余的下划线也一并清掉。
    """
    text = (tag or "").strip()
    if not text or "_" not in text:
        return text

    parts = role_tag_parts(text)
    if parts is not None:
        # 角色词：角色名和命名空间都转，并统一成「空格 + 裸括号」
        # （原本可能是转义写法或下划线写法）
        name, namespace = parts
        name = _collapse_underscores(name)
        namespace = _collapse_underscores(namespace)
        return f"{name} ({namespace})"

    return _collapse_underscores(text)


def _collapse_underscores(text: str) -> str:
    """下划线 → 空格，并清理首尾和连续多出来的空格。"""
    out = text.replace("_", " ")
    out = re.sub(r"\s+", " ", out)
    return out.strip()


@dataclass
class ConvertReport:
    """一次批量转换的结果，用来告诉用户改了什么。"""

    images_touched: int = 0
    tags_changed: int = 0
    samples: list[tuple[str, str, str]] = field(default_factory=list)  # (文件, 旧, 新)

    def add_sample(self, filename: str, old: str, new: str, limit: int = 12) -> None:
        if len(self.samples) < limit:
            self.samples.append((filename, old, new))

    def summary(self, action: str) -> str:
        if not self.images_touched:
            return f"没有需要转换的标签（{action}）"
        return (
            f"{action}：改了 {self.images_touched} 张图，"
            f"共 {self.tags_changed} 个标签"
        )


def convert_tags(
    tags: list[str],
    converter,
    *,
    filename: str = "",
    report: ConvertReport | None = None,
) -> tuple[list[str], int]:
    """对一组标签做转换。

    返回 ``(新标签列表, 改动个数)``。去重按最终结果算 ——
    两个标签转换后可能撞成同一个（`a_b` 和 `a b`），只留一个。
    """
    out: list[str] = []
    seen: set[str] = set()
    changed = 0
    for tag in tags:
        new_tag = converter(tag)
        if new_tag != tag:
            changed += 1
            if report is not None:
                report.add_sample(filename, tag, new_tag)
        key = new_tag.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(new_tag)
    return out, changed


def dedupe(tags: list[str]) -> list[str]:
    """保序去重，大小写不敏感（训练集里大小写不同算同一个词）。"""
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        key = tag.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(tag.strip())
    return out


@dataclass
class CaptionEdit:
    """一张图的标注改动。"""

    image_id: int
    image_path: str
    original: list[str] = field(default_factory=list)
    current: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.original != self.current

    @property
    def added(self) -> list[str]:
        low = {t.lower() for t in self.original}
        return [t for t in self.current if t.lower() not in low]

    @property
    def removed(self) -> list[str]:
        low = {t.lower() for t in self.current}
        return [t for t in self.original if t.lower() not in low]

    @property
    def diff_text(self) -> str:
        before = join_caption(self.original).splitlines() or [""]
        after = join_caption(self.current).splitlines() or [""]
        return "\n".join(
            difflib.unified_diff(
                before, after, fromfile="当前文件", tofile="保存后", lineterm="", n=0
            )
        )


def read_caption(image_path: str | Path) -> list[str]:
    """读配套 txt，返回标签列表。文件不存在返回空列表。"""
    path = caption_path_for(image_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        # 有些老文件是 GBK
        try:
            text = path.read_text(encoding="gbk")
        except (OSError, UnicodeDecodeError):
            return []
    return split_caption(text)


def read_caption_text(image_path: str | Path) -> str:
    path = caption_path_for(image_path)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        try:
            return path.read_text(encoding="gbk")
        except (OSError, UnicodeDecodeError):
            return ""


def backup_path(image_path: str | Path) -> Path:
    path = caption_path_for(image_path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.name}.{stamp}.bak")


def write_caption(
    image_path: str | Path, tags: list[str], *, make_backup: bool = True
) -> tuple[bool, str]:
    """原子写入标注，并备份原文件。

    返回 ``(是否成功, 说明)``。
    """
    path = caption_path_for(image_path)
    content = join_caption(dedupe(tags))
    backup_note = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if make_backup and path.exists():
            target = backup_path(path)
            shutil.copy2(path, target)
            backup_note = f"，备份 {target.name}"
        # 先写临时文件再替换：中途失败不会留下半截文件把标注毁了
        temp = path.with_name(path.name + ".tmp-writing")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        return False, f"{path.name} 写入失败：{exc}"
    return True, f"{path.name} 已保存{backup_note}"


def preview_diff(edits: list[CaptionEdit]) -> str:
    """把所有改动拼成一份可读的预览，保存前给用户看。"""
    changed = [e for e in edits if e.changed]
    if not changed:
        return "没有需要保存的改动。"
    lines: list[str] = [f"将要保存 {len(changed)} 个标注文件：", ""]
    for edit in changed:
        lines.append(f"── {Path(edit.image_path).name}")
        if edit.added:
            lines.append(f"   + {', '.join(edit.added)}")
        if edit.removed:
            lines.append(f"   - {', '.join(edit.removed)}")
        lines.append(f"   共 {len(edit.original)} → {len(edit.current)} 个标签")
        lines.append("")
    return "\n".join(lines)
