"""生成配套 txt 标注。

三级来源，按质量从高到低：

1. **PNG 元数据**（ComfyUI 节点图 / NAI 角色分组）—— 出图时真实用过的词，
   是 ground truth，免费且瞬时。有就用它。
2. **WD14** —— 看图猜的，本地 GPU 约 37ms/张。适合没有元数据的图
   （手拍、网图、被转存过的图）。
3. **在线 API** —— 兜底，要联网花钱。

原则：**元数据优先，WD14 兜底**。有元数据还用 WD14 属于主动降级，
因为 WD14 会漏角色名、会加你没要的词。

写盘策略
--------
- 没有 txt 的图：生成后直接落盘（用户明确要这个）
- 已有 txt 的图：默认 **跳过，不覆盖**；要覆盖必须显式指定
- 无论哪种，覆盖前都会备份原文件
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import captions, comfy_meta

ProgressFn = Callable[[int, int, str], None]
StopFn = Callable[[], bool]

SOURCE_META = "meta"
SOURCE_WD14 = "wd14"
SOURCE_EXISTING = "existing"
SOURCE_EMPTY = "empty"

SOURCE_LABEL = {
    SOURCE_META: "元数据",
    SOURCE_WD14: "WD14",
    SOURCE_EXISTING: "已有 txt",
    SOURCE_EMPTY: "无",
}

POLICY_SKIP = "skip"          # 已有 txt 就不动
POLICY_OVERWRITE = "overwrite"  # 已有 txt 也重新生成
POLICY_FILL_ONLY = "fill"     # 只补空的 txt（内容为空白的）


@dataclass
class GenerateItem:
    image_id: int
    path: str
    filename: str
    source: str = SOURCE_EMPTY
    tags: list[str] = field(default_factory=list)
    written: bool = False
    skipped_reason: str = ""
    error: str = ""


@dataclass
class GenerateReport:
    considered: int = 0
    written: int = 0
    skipped_existing: int = 0
    from_meta: int = 0
    from_wd14: int = 0
    empty: int = 0
    failed: int = 0
    items: list[GenerateItem] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"处理 {self.considered} 张"]
        if self.written:
            parts.append(f"写入 {self.written}")
        if self.from_meta:
            parts.append(f"元数据 {self.from_meta}")
        if self.from_wd14:
            parts.append(f"WD14 {self.from_wd14}")
        if self.skipped_existing:
            parts.append(f"跳过已有 {self.skipped_existing}")
        if self.empty:
            parts.append(f"无内容 {self.empty}")
        if self.failed:
            parts.append(f"失败 {self.failed}")
        return "，".join(parts)


def tags_from_metadata(path: str | Path, parser: str = "auto") -> tuple[list[str], str]:
    """从 PNG 元数据取标签。返回 (标签, 来源说明)。

    NAI 图会把每个角色组分别摊平后合并去重（训练集里一份 caption
    对应一张图，多角色时按出现顺序合并最稳妥）。
    """
    path = Path(path)
    parsed = comfy_meta.extract(path, parser=parser)
    if not parsed.get("has_meta"):
        return [], ""

    prompt = (parsed.get("prompt") or "").strip()
    if not prompt:
        return [], ""

    source = str(parsed.get("source") or "")
    tags: list[str] = []
    if parsed.get("char_groups"):
        # NAI：多角色分组，逐组摊平
        tags = comfy_meta.split_prompt_to_tags(
            "\n".join(
                ", ".join(group) if isinstance(group, list) else str(group)
                for group in parsed["char_groups"]
            )
        )
    else:
        tags = comfy_meta.split_prompt_to_tags(prompt)
    return captions.dedupe(tags), source or "meta"


def generate_for_images(
    db,
    images: Sequence[dict],
    *,
    parser: str = "auto",
    use_wd14: bool = False,
    wd14_config=None,
    policy: str = POLICY_SKIP,
    make_backup: bool = True,
    progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
    source_counts: Counter | None = None,
) -> GenerateReport:
    """给一批图生成 txt。

    ``images`` 每项至少要有 ``id`` / ``path`` / ``filename``。
    """
    report = GenerateReport()
    total = len(images)

    # 需要 WD14 的图先挑出来，攒一批一次性跑（GPU 批量效率高）
    pending: list[GenerateItem] = []
    need_wd14: list[GenerateItem] = []

    for index, row in enumerate(images, start=1):
        if should_stop and should_stop():
            break
        path = str(row.get("path") or "")
        if not path:
            continue
        report.considered += 1
        item = GenerateItem(
            image_id=int(row.get("id") or 0),
            path=path,
            filename=str(row.get("filename") or Path(path).name),
        )
        if progress:
            progress(index, total, item.filename)

        caption_file = captions.caption_path_for(path)
        exists = caption_file.is_file()
        existing_tags = captions.read_caption(path) if exists else []

        if exists and policy == POLICY_SKIP:
            item.source = SOURCE_EXISTING
            item.tags = existing_tags
            item.skipped_reason = "已有 txt"
            report.skipped_existing += 1
            report.items.append(item)
            continue
        if exists and policy == POLICY_FILL_ONLY and existing_tags:
            item.source = SOURCE_EXISTING
            item.tags = existing_tags
            item.skipped_reason = "已有内容"
            report.skipped_existing += 1
            report.items.append(item)
            continue

        tags, source = tags_from_metadata(path, parser=parser)
        if tags:
            item.source = SOURCE_META
            item.tags = tags
            report.from_meta += 1
            pending.append(item)
        else:
            need_wd14.append(item)
        report.items.append(item)

    # ---- WD14 补跑 ----
    if need_wd14 and not use_wd14:
        # 没开 WD14：这些图既没元数据也没本地打标，老实计入「无内容」
        for item in need_wd14:
            item.source = SOURCE_EMPTY
            item.error = "没有元数据，且未启用 WD14"
            report.empty += 1

    if need_wd14 and use_wd14 and not (should_stop and should_stop()):
        from . import wd14 as wd14_module

        tagger = wd14_module.Wd14Tagger(wd14_config)
        status = tagger.load()
        if status.available:
            paths = [item.path for item in need_wd14]
            results = tagger.tag_batch(paths)
            for item, tags in zip(need_wd14, results):
                if tags:
                    item.source = SOURCE_WD14
                    item.tags = tags
                    report.from_wd14 += 1
                    pending.append(item)
                else:
                    item.source = SOURCE_EMPTY
                    report.empty += 1
        else:
            for item in need_wd14:
                item.source = SOURCE_EMPTY
                item.error = status.error
                report.empty += 1

    # ---- 落盘 ----
    for item in pending:
        if should_stop and should_stop():
            break
        ok, note = captions.write_caption(
            item.path, item.tags, make_backup=make_backup
        )
        if ok:
            item.written = True
            report.written += 1
            db._set_caption_only(item.image_id, True)  # noqa: SLF001
            if source_counts is not None:
                source_counts[SOURCE_LABEL.get(item.source, item.source)] += 1
        else:
            item.error = note
            report.failed += 1

    return report


def preview_report(report: GenerateReport, limit: int = 40) -> str:
    """生成前的文字预览，让用户先看清楚会发生什么。"""
    lines = [report.summary(), ""]
    shown = 0
    for item in report.items:
        if item.skipped_reason:
            continue
        if shown >= limit:
            lines.append(f"... 还有 {sum(1 for i in report.items if not i.skipped_reason) - limit} 张")
            break
        source = SOURCE_LABEL.get(item.source, item.source)
        preview = ", ".join(item.tags[:8]) if item.tags else "（无内容）"
        lines.append(f"[{source}] {item.filename}")
        lines.append(f"    {preview}")
        shown += 1
    return "\n".join(lines)


def count_missing_captions(db, dataset_id: int | None = None) -> int:
    return db.images_missing_caption(dataset_id)


def iter_missing_caption_images(
    db, dataset_id: int | None = None
) -> Iterable[dict]:
    """列出没有配套 txt 的图（生成标注的目标）。"""
    if dataset_id is None:
        rows = db._query(  # noqa: SLF001
            "SELECT id, path, filename FROM images WHERE has_caption = 0"
        )
    else:
        rows = db._query(  # noqa: SLF001
            "SELECT id, path, filename FROM images WHERE has_caption = 0 AND dataset_id = ?",
            (dataset_id,),
        )
    for row in rows:
        yield {
            "id": int(row["id"]),
            "path": str(row["path"]),
            "filename": str(row["filename"]),
        }
