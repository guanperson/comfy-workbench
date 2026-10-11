"""导出：把标签落成训练可直接用的数据集。

产出：
  <name>/            每个数据集一个子目录
    xxx.png          （可选）复制图片
    xxx.txt          每图一个标签文件
  dataset.jsonl      汇总清单（路径 + 标签 + 全部元数据）
  tags.txt           标签频率表
  captions.csv       CSV 版清单

所有开关都在 Settings 里（export_*）。
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .config import Settings
from .db import Database

ProgressFn = Callable[[int, int, str], None]


@dataclass
class ExportResult:
    export_root: Path
    dataset_name: str
    target: Path
    images: int = 0
    txt_files: int = 0
    copied: int = 0
    jsonl: str = ""
    csv_file: str = ""
    tags_file: str = ""
    triggered_words: str = ""
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"导出 {self.images} 张图", f"标签文件 {self.txt_files} 个"]
        if self.copied:
            bits.append(f"复制 {self.copied} 张")
        if self.jsonl:
            bits.append("JSONL")
        if self.csv_file:
            bits.append("CSV")
        if self.tags_file:
            bits.append("频率表")
        return "，".join(bits)


def sanitize_filename(name: str, fallback: str = "untitled") -> str:
    """清掉 Windows 不允许的字符。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return name or fallback


def build_trigger(image: dict, settings: Settings) -> list[str]:
    """决定这张图要额外加哪些触发词（LoRA 训练的关键）。"""
    triggers: list[str] = []
    if settings.export_trigger.strip():
        triggers.append(settings.export_trigger.strip())

    if settings.export_use_ckpt:
        meta = image.get("meta") or {}
        ckpt = meta.get("checkpoint_short") or meta.get("checkpoint")
        if ckpt:
            tag = str(ckpt).strip().lower().replace(" ", "_")
            if tag and tag not in triggers:
                triggers.append(tag)

    # 工作流里的 LoRA 名作为可选触发词
    meta = image.get("meta") or {}
    for lora in meta.get("loras") or []:
        short = str(lora.get("short") or lora.get("name") or "").strip().lower()
        if not short:
            continue
        tag = short.replace(" ", "_")
        if tag and tag not in triggers:
            triggers.append(tag)

    return triggers


def compose_tags(image: dict, db: Database, settings: Settings) -> str:
    tags = db.tag_names_for_image(int(image["id"]))
    parts = build_trigger(image, settings) + tags
    joiner = settings.export_join or ", "
    return joiner.join(parts)


def export_dataset(
    db: Database,
    dataset_id: int,
    settings: Settings,
    root: Path | str | None = None,
    tag_filter: str = "",
    only_tagged: bool = True,
    min_tags: int = 0,
    progress: ProgressFn | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ExportResult:
    """导出单个数据集。"""
    dataset = db.get_dataset(dataset_id)
    if dataset is None:
        raise ValueError(f"数据集不存在：{dataset_id}")

    root_path = Path(root) if root else settings.export_root()
    dataset_name = sanitize_filename(str(dataset["name"]), f"dataset_{dataset_id}")
    target = root_path / dataset_name
    target.mkdir(parents=True, exist_ok=True)

    result = ExportResult(
        export_root=root_path, dataset_name=dataset_name, target=target
    )

    images = db.list_images(
        dataset_id=dataset_id,
        status="tagged" if only_tagged else "",
        tag=tag_filter,
        order="mtime_asc",
        limit=100000,
    )
    total = len(images)

    jsonl_fp = open(target / "dataset.jsonl", "w", encoding="utf-8", newline="\n")
    csv_fp = open(target / "captions.csv", "w", encoding="utf-8", newline="")
    csv_writer = csv.writer(csv_fp)

    try:
        for index, image in enumerate(images, start=1):
            if should_stop and should_stop():
                break
            if progress and (index % 5 == 0 or index == total):
                progress(index, total, str(image["filename"]))

            tag_names = db.tag_names_for_image(int(image["id"]))
            if len(tag_names) < min_tags:
                continue

            try:
                tags_text = compose_tags(image, db, settings)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{image['filename']}: {exc}")
                continue

            stem = Path(str(image["filename"])).stem
            out_stem = sanitize_filename(stem, f"image_{image['id']}")

            if settings.export_write_txt:
                pattern = settings.export_filename_pattern or "{stem}.txt"
                try:
                    txt_name = pattern.format(stem=out_stem, index=index, id=image["id"])
                except (KeyError, IndexError, ValueError):
                    txt_name = f"{out_stem}.txt"
                (target / sanitize_filename(txt_name)).write_text(tags_text + "\n", encoding="utf-8")
                result.txt_files += 1

            stored_rel = ""
            if settings.export_copy_images:
                src = Path(str(image["path"]))
                if src.exists():
                    dst = target / sanitize_filename(str(image["filename"]))
                    try:
                        shutil.copy2(src, dst)
                        stored_rel = str(Path(dataset_name) / dst.name).replace("\\", "/")
                        result.copied += 1
                    except OSError as exc:
                        result.errors.append(f"复制失败 {src.name}: {exc}")
                else:
                    result.errors.append(f"文件已不存在：{src.name}")
            else:
                stored_rel = str(image["path"]).replace("\\", "/")

            meta = image.get("meta") or {}
            record = {
                "file_name": str(image["filename"]),
                "file_path": stored_rel,
                "caption": tags_text,
                "tags": tag_names,
                "width": image.get("width"),
                "height": image.get("height"),
                "prompt": image.get("prompt"),
                "negative": image.get("negative"),
                "checkpoint": meta.get("checkpoint_short") or meta.get("checkpoint"),
                "loras": meta.get("loras") or [],
                "seed": meta.get("seed"),
                "steps": meta.get("steps"),
                "cfg": meta.get("cfg"),
                "sampler": meta.get("sampler"),
                "scheduler": meta.get("scheduler"),
                "dataset": dataset_name,
                "tagged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            jsonl_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
            csv_writer.writerow([
                str(image["filename"]), stored_rel, tags_text,
                "|".join(tag_names), record["checkpoint"] or "",
            ])
            result.images += 1
    finally:
        jsonl_fp.close()
        csv_fp.close()

    if result.images == 0:
        result.jsonl = ""
        result.csv_file = ""
        result.tags_file = ""
        for junk in ("dataset.jsonl", "captions.csv"):
            (target / junk).unlink(missing_ok=True)
        return result

    result.jsonl = str(target / "dataset.jsonl")
    result.csv_file = str(target / "captions.csv")

    # 标签频率表
    freq = _dataset_tag_frequency(db, images)
    lines = ["\t".join([name, str(count)]) for name, count in freq]
    (target / "tags.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result.tags_file = str(target / "tags.txt")

    # 触发词汇总（LoRA 训练常要单独看）
    trigger_counter: dict[str, int] = {}
    for image in images:
        for trig in build_trigger(image, settings):
            trigger_counter[trig] = trigger_counter.get(trig, 0) + 1
    if trigger_counter:
        top = ", ".join(t for t, _ in sorted(trigger_counter.items(), key=lambda kv: -kv[1])[:20])
        result.triggered_words = top
        (target / "triggers.txt").write_text(
            "\n".join(f"{t}\t{c}" for t, c in sorted(trigger_counter.items(), key=lambda kv: -kv[1])),
            encoding="utf-8",
        )

    return result


def _dataset_tag_frequency(db: Database, images: list[dict]) -> list[tuple[str, int]]:
    counter: dict[str, int] = {}
    for image in images:
        for name in db.tag_names_for_image(int(image["id"])):
            counter[name] = counter.get(name, 0) + 1
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def export_prompt_captions(
    db: Database, dataset_id: int, target: Path | str
) -> ExportResult:
    """另一种思路：直接用原始 prompt 当 caption（很多 SD 微调就这么干）。"""
    dataset = db.get_dataset(dataset_id)
    if dataset is None:
        raise ValueError(f"数据集不存在：{dataset_id}")

    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)

    result = ExportResult(
        export_root=target.parent, dataset_name=str(dataset["name"]), target=target
    )
    images = db.list_images(dataset_id=dataset_id, order="mtime_asc", limit=100000)
    for image in images:
        prompt = (image.get("prompt") or "").strip()
        if not prompt:
            continue
        stem = sanitize_filename(Path(str(image["filename"])).stem, str(image["id"]))
        (target / f"{stem}.txt").write_text(prompt + "\n", encoding="utf-8")
        result.txt_files += 1
        result.images += 1
    return result


def export_readme(target: Path, result: ExportResult, settings: Settings) -> Path:
    """在导出目录里写一份说明，方便以后自己回看怎么导的。"""
    text = f"""# {result.dataset_name}

由 ComfyUI Workbench 导出于 {time.strftime('%Y-%m-%d %H:%M:%S')}

## 内容
- 图片：{result.images} 张
- 标签文件（.txt）：{result.txt_files} 个
- 清单：dataset.jsonl / captions.csv
- 标签频率表：tags.txt

## 导出设置
- 标签来源：{'仅已打标' if settings.export_write_txt else '全部'}
- 连接符：`{settings.export_join}`
- 固定触发词：`{settings.export_trigger or '（无）'}`
- 使用出图模型名做触发词：{'是' if settings.export_use_ckpt else '否'}
- 标签顺序：字母序（稳定，便于 diff）

## LoRA 训练提示
1. 触发词放在每个 caption 的最前面，保证模型稳定绑定。
2. `tags.txt` 是频率表，可用来剔除出现次数 <5 的噪声标签。
3. 若图里没有写元数据，caption 会是空的，记得补打标再导出。
"""
    path = target / "README.md"
    path.write_text(text, encoding="utf-8")
    return path
