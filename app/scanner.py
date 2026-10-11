"""目录扫描：把出图目录同步进数据库。

纯逻辑，不依赖 Qt，方便单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import captions, comfy_meta, paths
from .db import IMAGE_EXTENSIONS, Database

ProgressFn = Callable[[int, int, str], None]


@dataclass
class ScanResult:
    scanned: int = 0        # 看到的图片文件数
    added: int = 0          # 新入库
    updated: int = 0        # 元数据被刷新
    skipped: int = 0        # 已在库且未变化
    failed: int = 0         # 解析失败
    removed: int = 0        # 文件已不存在，从库中清理
    with_meta: int = 0      # 解析出提示词的张数
    with_caption: int = 0   # 有配套 txt 标注的张数


def iter_image_files(
    root: Path,
    recursive: bool = True,
    excludes: Iterable[str] = (".git", "__pycache__", ".thumbnails"),
) -> Iterator[Path]:
    """遍历目录下的图片文件，跳过隐藏目录与常见垃圾目录。"""
    ex = set(excludes)
    if not root.is_dir():
        return
    if recursive:
        for child in sorted(root.rglob("*")):
            if not child.is_file():
                continue
            if child.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if ex.intersection(part for part in child.parts if part.startswith(".")):
                continue
            yield child
    else:
        for child in sorted(root.iterdir()):
            if child.is_file() and child.suffix.lower() in IMAGE_EXTENSIONS:
                yield child


def scan_folder(
    db: Database,
    dataset_id: int,
    root: str | Path,
    recursive: bool = True,
    parse_meta: bool = True,
    reparse: bool = False,
    prune_missing: bool = True,
    parser: str = "auto",
    progress: ProgressFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ScanResult:
    """扫描 ``root`` 并入库。

    parse_meta 打开时会读 PNG 文本块，这一步对几千张图会明显变慢，
    所以放在线程里跑，并通过 progress 回调刷 UI。

    ``parser`` 交给 :func:`comfy_meta.extract`，可选 auto / comfyui / nai。
    """
    root = paths.norm_path(root)
    result = ScanResult()
    files = list(iter_image_files(root, recursive=recursive))
    total = len(files)
    seen_paths: set[str] = set()

    for index, file in enumerate(files, start=1):
        if should_stop and should_stop():
            break
        if progress and (index % 8 == 0 or index == total):
            progress(index, total, file.name)

        # 用 paths.norm_path 而不是 file.resolve()：保证入库的路径和
        # 数据集路径、缩略图 cache key 用的是**同一套**规范化。
        # 否则同一个文件在库里会有两种写法，看起来像"图片重复/丢失"。
        abs_path = str(paths.norm_path(file))
        seen_paths.add(abs_path)
        result.scanned += 1

        existing = db.find_by_path(abs_path)
        record = existing

        try:
            stat = file.stat()
        except OSError:
            result.failed += 1
            continue

        if record is not None:
            changed = abs(float(record.get("mtime") or 0) - stat.st_mtime) > 0.5
            # 图片没变，但配套 txt 可能出现/消失了，这个也要跟上
            caption_now = captions.has_caption(file)
            if not changed and not reparse:
                if caption_now != bool(record.get("has_caption")):
                    db._set_caption_only(int(record["id"]), caption_now)  # noqa: SLF001
                    result.updated += 1
                else:
                    result.skipped += 1
                if caption_now:
                    result.with_caption += 1
                continue

        prompt = negative = ""
        meta: dict = {}
        has_meta = False
        has_caption = captions.has_caption(file)
        if parse_meta:
            parsed = comfy_meta.extract(file, parser=parser)
            prompt = parsed["prompt"]
            negative = parsed["negative"]
            meta = parsed["meta"]
            has_meta = bool(parsed["has_meta"])
            if has_meta:
                result.with_meta += 1
        if has_caption:
            result.with_caption += 1

        width = int(meta.get("width") or 0)
        height = int(meta.get("height") or 0)
        if not width or not height:
            size = _probe_size(file)
            if size:
                width, height = size

        if record is None:
            new_id = db.add_image(
                dataset_id=dataset_id,
                path=abs_path,
                filename=file.name,
                rel_path=str(file.relative_to(root)) if root in file.parents or file.parent == root else file.name,
                width=width,
                height=height,
                size=stat.st_size,
                mtime=stat.st_mtime,
                prompt=prompt,
                negative=negative,
                meta=meta,
                has_meta=has_meta,
                has_caption=has_caption,
            )
            if new_id is None:
                result.skipped += 1
            else:
                result.added += 1
        else:
            db.update_image_meta(
                int(record["id"]), prompt, negative, meta,
                width=width, height=height, has_caption=has_caption,
            )
            result.updated += 1

    if prune_missing and not (should_stop and should_stop()):
        result.removed = _prune_missing(db, dataset_id, seen_paths)

    return result


def _prune_missing(db: Database, dataset_id: int, seen: set[str]) -> int:
    """把已从磁盘消失的文件从库里清掉。"""
    rows = db._query(  # noqa: SLF001
        "SELECT id, path FROM images WHERE dataset_id = ?", (dataset_id,)
    )
    removed = 0
    for row in rows:
        path = str(row["path"])
        if path in seen:
            continue
        if Path(path).exists():
            continue
        db.delete_image(int(row["id"]))
        removed += 1
    if removed:
        db.prune_orphan_tags()
    return removed


def _probe_size(path: Path) -> tuple[int, int] | None:
    """不解析元数据时，用最小代价拿图片尺寸。"""
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None


def dataset_missing_files(db: Database, dataset_id: int) -> int:
    """统计库里有记录但磁盘上没了的图。"""
    rows = db._query(  # noqa: SLF001
        "SELECT path FROM images WHERE dataset_id = ?", (dataset_id,)
    )
    return sum(1 for r in rows if not Path(str(r["path"])).exists())
