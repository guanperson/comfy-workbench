"""缩略图磁盘缓存。

命名规则：``<图片绝对路径 + mtime + 尺寸> 的 sha1`` -> ``<hash>.jpg``。
源文件改动后 hash 变化，旧缩略图自然失效（设置页可一键清理）。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from . import paths

_PLACEHOLDER = None


def cache_key(image_path: str | Path, mtime: float, size: int) -> str:
    # 用 paths.norm_path 而不是 Path().resolve()：会先剥掉 `\\?\` 设备前缀。
    # 不剥的话同一个文件会有两个 cache key（一张图两份缓存），
    # 而且 `\\?\` 那份和 `D:\...` 那份互不命中。
    raw = f"{paths.norm_path(image_path)}|{int(mtime)}|{int(size)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def thumb_path_for(image_path: str | Path, mtime: float, size: int = 512) -> Path:
    return paths.thumb_dir() / f"{cache_key(image_path, mtime, size)}.jpg"


def build_thumb(src: str | Path, dst: Path, size: int = 512, quality: int = 82) -> bool:
    """生成缩略图。成功返回 True。"""
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((size, size), Image.LANCZOS)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(".tmp.jpg")
            im.save(tmp, format="JPEG", quality=quality, optimize=True)
            os.replace(tmp, dst)
        return True
    except Exception:
        return False


def get_thumb(
    image_path: str | Path,
    mtime: float,
    size: int = 512,
    allow_build: bool = True,
) -> Path | None:
    """拿到缩略图路径，不存在就现做。失败返回 None。"""
    dst = thumb_path_for(image_path, mtime, size)
    if dst.exists():
        return dst
    if not allow_build:
        return None
    if build_thumb(image_path, dst, size):
        return dst
    return None


def cache_size_bytes() -> int:
    total = 0
    for entry in paths.thumb_dir().glob("*.jpg"):
        try:
            total += entry.stat().st_size
        except OSError:
            continue
    return total


def cache_count() -> int:
    return sum(1 for _ in paths.thumb_dir().glob("*.jpg"))


def clear_cache() -> int:
    """清空缩略图缓存，返回删除的文件数。"""
    removed = 0
    for entry in paths.thumb_dir().glob("*.jpg"):
        try:
            entry.unlink()
            removed += 1
        except OSError:
            continue
    for entry in paths.thumb_dir().glob("*.tmp.jpg"):
        try:
            entry.unlink()
        except OSError:
            continue
    return removed


def human_size(num: int) -> str:
    step = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} TB"


def touch(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def now() -> float:
    return time.time()
