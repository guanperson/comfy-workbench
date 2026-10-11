"""Image preprocessing and safe paired image/caption file operations."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image

from . import paths
from .db import Database

RENAME_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg", ".tif", ".tiff", ".heic", ".heif", ".jfif", ".ico"}
EDIT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
Progress = Callable[[int, int, str], None]


@dataclass
class RenameOptions:
    prefix: str = ""
    suffix: str = ""
    separator: str = "_"
    start: int = 1
    digits: int = 3
    step: int = 1
    keep_original: bool = False
    number_last: bool = False
    case: str = "keep"
    group: str = "all"
    date_source: str = "mtime"
    date_format: str = "%Y%m%d"
    auto_suffix: bool = False


@dataclass
class RenameEntry:
    source: Path
    target: Path
    image_id: int | None = None
    starred: bool = False


def _date(path: Path, options: RenameOptions) -> str:
    stamp = time.time() if options.date_source == "now" else path.stat().st_mtime
    if options.date_source == "exif":
        try:
            with Image.open(path) as image:
                raw = image.getexif().get(36867) or image.getexif().get(306)
                if raw:
                    import datetime
                    stamp = datetime.datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S").timestamp()
        except (OSError, ValueError, KeyError):
            pass
    return time.strftime(options.date_format, time.localtime(stamp))


def build_rename_plan(sources: Iterable[Path | str], options: RenameOptions,
                      *, stars: set[str] | None = None, mode: str = "rename",
                      output: Path | None = None, split: str = "single",
                      split_size: int = 100, image_ids: dict[str, int] | None = None) -> list[RenameEntry]:
    """Construct a preview; the sequence of sources is the order of numbering."""
    if mode not in {"rename", "copy", "move"}:
        raise ValueError("未知重命名模式")
    if mode != "rename" and output is None:
        raise ValueError("复制或移动需要目标目录")
    stars, image_ids = stars or set(), image_ids or {}
    counters: dict[str, int] = {}
    entries: list[RenameEntry] = []
    for index, raw in enumerate(sources):
        source = paths.norm_path(raw)
        if not source.is_file() or source.suffix.lower() not in RENAME_EXTENSIONS:
            continue
        starred = str(source) in stars
        group = {"folder": str(source.parent), "parent": source.parent.name,
                 "star": "star" if starred else "other"}.get(options.group, "all")
        number = str(counters.get(group, options.start)).zfill(options.digits)
        counters[group] = counters.get(group, options.start) + options.step
        date = _date(source, options)
        tokens = {"序号": number, "原名": source.stem, "文件名": source.name,
                  "扩展名": source.suffix.lstrip("."), "日期": date, "拍摄日期": date,
                  "组名": source.parent.name, "文件夹": source.parent.name,
                  "上级目录": source.parent.parent.name}
        def expand(value: str) -> str:
            return re.sub(r"\{([^{}]+)\}", lambda m: tokens.get(m.group(1), m.group(0)), value)
        parts = [expand(options.prefix)]
        if not options.number_last and "{序号}" not in options.prefix + options.suffix:
            parts.append(number)
        if options.keep_original and "{原名}" not in options.prefix + options.suffix:
            parts.append(source.stem)
        if options.number_last and "{序号}" not in options.prefix + options.suffix:
            parts.append(number)
        parts.append(expand(options.suffix))
        stem = INVALID.sub("_", options.separator.join(p for p in parts if p)).rstrip(" .")[:200] or "image"
        suffix = source.suffix
        if options.case == "lower":
            stem, suffix = stem.lower(), suffix.lower()
        elif options.case == "upper":
            stem, suffix = stem.upper(), suffix.upper()
        if mode == "rename":
            parent = source.parent
        else:
            sub = {"folder": source.parent.name, "parent": source.parent.parent.name,
                   "star": "已标星" if starred else "未标星",
                   "batch": f"{index // max(1, split_size) + 1:03d}"}.get(split, "")
            parent = paths.norm_path(output) / sub
        entries.append(RenameEntry(source, parent / f"{stem}{suffix}", image_ids.get(str(source)), starred))
    if options.auto_suffix:
        used: set[str] = set()
        sources_set = {str(e.source).casefold() for e in entries} if mode == "rename" else set()
        for entry in entries:
            candidate, n = entry.target, 2
            while str(candidate).casefold() in used or (candidate.exists() and str(candidate).casefold() not in sources_set):
                candidate = entry.target.with_name(f"{entry.target.stem} ({n}){entry.target.suffix}")
                n += 1
            entry.target = candidate
            used.add(str(candidate).casefold())
    return entries


def validate_rename_plan(entries: list[RenameEntry], mode: str) -> list[str]:
    errors: list[str] = []
    source_names = {str(e.source).casefold() for e in entries}
    source_txt = {str(e.source.with_suffix('.txt')).casefold() for e in entries}
    targets: set[str] = set()
    for entry in entries:
        if not entry.source.is_file():
            errors.append(f"源文件不存在：{entry.source}")
        if mode != "rename" and entry.source == entry.target:
            errors.append(f"目标不能是源文件本身：{entry.source}")
        for src, dst, sources in ((entry.source, entry.target, source_names),
                                  (entry.source.with_suffix('.txt'), entry.target.with_suffix('.txt'), source_txt)):
            if src != entry.source and not src.is_file():
                continue
            key = str(dst).casefold()
            if key in targets:
                errors.append(f"目标重名：{dst}")
            targets.add(key)
            if dst.exists() and (mode != "rename" or key not in sources):
                if src != dst:
                    errors.append(f"目标已存在：{dst}")
    return errors


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _move_staged(temp: Path, target: Path) -> None:
    """Commit a staged file without exposing a partial cross-volume copy."""
    if target.exists():
        raise FileExistsError(target)
    try:
        temp.rename(target)  # atomic on the same volume
        return
    except OSError as exc:
        if getattr(exc, "winerror", None) != 17 and getattr(exc, "errno", None) != 18:
            raise
    partial = target.with_name(f".{uuid.uuid4().hex}.preprocess-copy")
    committed = False
    try:
        with temp.open("rb") as inp, partial.open("xb") as out:
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(temp, partial)
        if target.exists():
            raise FileExistsError(target)
        partial.rename(target)
        committed = True
        temp.unlink()
    except BaseException:
        if committed:
            target.unlink(missing_ok=True)
        raise
    finally:
        partial.unlink(missing_ok=True)


def execute_rename(db: Database | None, entries: list[RenameEntry], mode: str,
                   progress: Progress | None = None, should_stop: Callable[[], bool] | None = None) -> int | None:
    errors = validate_rename_plan(entries, mode)
    if errors:
        raise ValueError("\n".join(errors[:20]))
    pairs: list[tuple[Path, Path]] = []
    for e in entries:
        if e.source != e.target:
            pairs.append((e.source, e.target))
            if e.source.with_suffix(".txt").is_file():
                pairs.append((e.source.with_suffix(".txt"), e.target.with_suffix(".txt")))
    staged: list[tuple[Path, Path, Path]] = []
    committed: list[tuple[Path, Path, Path]] = []
    copied: list[Path] = []
    original_datasets = {
        e.image_id: int(db.get_image(e.image_id)["dataset_id"])
        for e in entries if db and e.image_id is not None and db.get_image(e.image_id)
    }
    db_relocated = False
    try:
        if mode == "copy":
            for i, (src, dst) in enumerate(pairs, 1):
                if should_stop and should_stop():
                    raise InterruptedError("操作已取消")
                dst.parent.mkdir(parents=True, exist_ok=True)
                with src.open("rb") as inp, dst.open("xb") as out:
                    copied.append(dst)
                    shutil.copyfileobj(inp, out, 1024 * 1024)
                if progress:
                    progress(i, len(pairs), dst.name)
        else:
            for src, dst in pairs:
                if should_stop and should_stop():
                    raise InterruptedError("操作已取消")
                tmp = src.with_name(f".{uuid.uuid4().hex}.preprocess-tmp")
                src.rename(tmp)
                staged.append((src, tmp, dst))
            for i, item in enumerate(staged, 1):
                src, tmp, dst = item
                if should_stop and should_stop():
                    raise InterruptedError("操作已取消")
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    raise FileExistsError(dst)
                _move_staged(tmp, dst)
                committed.append(item)
                if progress:
                    progress(i, len(staged), dst.name)
            if db:
                db.relocate_images([(e.image_id, str(e.target), mode == "move") for e in entries
                                    if e.image_id is not None and e.source != e.target])
                db_relocated = True
        if db and pairs:
            detail = {"mode": mode,
                      "files": [{"source": str(src), "target": str(dst), "fingerprint": _fingerprint(dst)} for src, dst in pairs],
                      "images": [{"id": e.image_id, "source": str(e.source), "target": str(e.target),
                                  "dataset_id": original_datasets.get(e.image_id)}
                                 for e in entries if e.image_id is not None and e.source != e.target]}
            return db.record_preprocess_op("rename", detail)
        return None
    except BaseException:
        if mode == "copy":
            for dst in reversed(copied):
                dst.unlink(missing_ok=True)
        else:
            for src, tmp, dst in reversed(committed):
                if dst.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    _move_staged(dst, src)
            for src, tmp, dst in reversed(staged):
                if tmp.exists():
                    tmp.rename(src)
            if db and db_relocated:
                db.relocate_images([(e.image_id, str(e.source), False) for e in entries
                                    if e.image_id is not None and e.source != e.target], original_datasets)
        raise


def undo_rename(db: Database, op_id: int) -> None:
    found = [o for o in db.recent_preprocess_ops(100) if o["id"] == op_id]
    if not found or found[0]["kind"] != "rename" or found[0]["undone_at"]:
        raise ValueError("操作记录不存在或已撤销")
    detail = found[0]["detail"]
    files = detail["files"]
    mode = detail["mode"]
    target_names = {str(Path(item["target"])).casefold() for item in files}
    for item in files:
        src, dst = Path(item["source"]), Path(item["target"])
        if not dst.is_file() or _fingerprint(dst) != item["fingerprint"]:
            raise ValueError(f"文件已变化，不能安全撤销：{dst}")
        if mode != "copy" and src.exists() and str(src).casefold() not in target_names:
            raise FileExistsError(f"原位置已被占用：{src}")
    if mode == "copy":
        for item in files:
            Path(item["target"]).unlink()
    else:
        staged: list[tuple[Path, Path, Path]] = []
        for item in files:
            dst, src = Path(item["target"]), Path(item["source"])
            tmp = dst.with_name(f".{uuid.uuid4().hex}.preprocess-undo")
            dst.rename(tmp)
            staged.append((dst, tmp, src))
        try:
            for dst, tmp, src in staged:
                src.parent.mkdir(parents=True, exist_ok=True)
                _move_staged(tmp, src)
            db.relocate_images([(e["id"], e["source"], False) for e in detail["images"]],
                               {e["id"]: e["dataset_id"] for e in detail["images"]
                                if e.get("dataset_id") is not None})
        except BaseException:
            for dst, tmp, src in reversed(staged):
                if src.exists() and not dst.exists():
                    _move_staged(src, dst)
                elif tmp.exists():
                    tmp.rename(dst)
            raise
    db.mark_preprocess_undone(op_id)


def centered_crop_box(width: int, height: int, ratio_w: int, ratio_h: int) -> tuple[int, int, int, int]:
    if min(width, height, ratio_w, ratio_h) <= 0:
        raise ValueError("尺寸和比例必须大于零")
    if width * ratio_h > height * ratio_w:
        crop_w, crop_h = max(1, round(height * ratio_w / ratio_h)), height
    else:
        crop_w, crop_h = width, max(1, round(width * ratio_h / ratio_w))
    left, top = (width - crop_w) // 2, (height - crop_h) // 2
    return left, top, left + crop_w, top + crop_h


def ratio_drag_box(start: tuple[int, int], end: tuple[int, int],
                   image_size: tuple[int, int], ratio: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Fit a fixed-aspect crop inside the user's dragged rectangle."""
    rw, rh = ratio
    if rw <= 0 or rh <= 0:
        raise ValueError("裁剪比例必须大于零")
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0 or dy == 0:
        return None
    width = min(abs(dx), int(abs(dy) * rw / rh))
    height = int(width * rh / rw)
    if width < 1 or height < 1:
        return None
    x2 = start[0] + (width if dx > 0 else -width)
    y2 = start[1] + (height if dy > 0 else -height)
    left, right = sorted((start[0], x2))
    top, bottom = sorted((start[1], y2))
    if not (0 <= left < right <= image_size[0] and 0 <= top < bottom <= image_size[1]):
        return None
    return left, top, right, bottom


def save_processed(source: Path, result: Image.Image, target: Path, *, overwrite: bool = False,
                   backup_root: Path | None = None) -> Path:
    source, target = paths.norm_path(source), paths.norm_path(target)
    if source.suffix.lower() not in EDIT_EXTENSIONS:
        raise ValueError(f"暂不支持像素编辑：{source.suffix}")
    if not overwrite and target.exists():
        raise FileExistsError(target)
    if overwrite and source != target:
        raise ValueError("覆盖只允许保存回原路径")
    fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
           ".webp": "WEBP", ".bmp": "BMP"}.get(target.suffix.lower())
    if fmt is None:
        raise ValueError("目标格式不受支持")
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = result.convert("RGB" if fmt in {"JPEG", "BMP"} else
                           "RGBA" if result.mode == "RGBA" else "RGB")
    fd, temp_name = tempfile.mkstemp(prefix=".preprocess-", suffix=target.suffix, dir=target.parent)
    os.close(fd)
    temp = Path(temp_name)
    backup = None
    try:
        clean.save(temp, format=fmt, quality=95 if fmt in {"JPEG", "WEBP"} else None)
        with temp.open("rb+") as stream:
            os.fsync(stream.fileno())
        if overwrite:
            if backup_root is None:
                raise ValueError("覆盖前必须指定备份目录")
            backup_root.mkdir(parents=True, exist_ok=True)
            backup = backup_root / f"{uuid.uuid4().hex}{source.suffix}"
            shutil.copy2(source, backup)
            os.replace(temp, target)
        else:
            try:
                with temp.open("rb") as inp, target.open("xb") as out:
                    shutil.copyfileobj(inp, out, 1024 * 1024)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            sidecar = source.with_suffix(".txt")
            if sidecar.is_file():
                created_sidecar = False
                try:
                    with sidecar.open("rb") as inp, target.with_suffix(".txt").open("xb") as out:
                        created_sidecar = True
                        shutil.copyfileobj(inp, out)
                except BaseException:
                    target.unlink(missing_ok=True)
                    if created_sidecar:
                        target.with_suffix(".txt").unlink(missing_ok=True)
                    raise
        return backup or target
    finally:
        temp.unlink(missing_ok=True)


def crop_image(source: Path, target: Path, box: tuple[int, int, int, int]) -> Path:
    with Image.open(source) as image:
        width, height = image.size
        left, top, right, bottom = box
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise ValueError("裁剪区域超出图片范围")
        return save_processed(source, image.crop(box), target)


def batch_crop(sources: Iterable[Path], output: Path, ratio: tuple[int, int],
               progress: Progress | None = None,
               should_stop: Callable[[], bool] | None = None) -> tuple[list[Path], list[str]]:
    sources = list(sources)
    done: list[Path] = []
    errors: list[str] = []
    for index, source in enumerate(sources, 1):
        if should_stop and should_stop():
            break
        try:
            with Image.open(source) as image:
                box = centered_crop_box(*image.size, *ratio)
            done.append(crop_image(source, output / f"{source.stem}_crop.png", box))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source.name}: {exc}")
        if progress:
            progress(index, len(sources), source.name)
    return done, errors
