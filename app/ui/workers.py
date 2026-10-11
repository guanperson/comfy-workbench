"""后台线程：扫描目录、批量打标、导出。

界面只跟信号打交道，数据库写入在 worker 线程里做（Database 自带锁）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Sequence

from PySide6.QtCore import QThread, Signal

from ..config import Settings
from ..db import Database
from ..exporter import ExportResult, export_dataset
from ..scanner import ScanResult, scan_folder
from ..tagger import TagRequest, VisionTagger


class ScanWorker(QThread):
    """扫描目录并入库。"""

    progress = Signal(int, int, str)      # 当前, 总数, 文件名
    finished_ok = Signal(object)          # ScanResult
    failed = Signal(str)

    def __init__(
        self,
        db: Database,
        dataset_id: int,
        root: str,
        recursive: bool = True,
        parse_meta: bool = True,
        reparse: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.dataset_id = dataset_id
        self.root = root
        self.recursive = recursive
        self.parse_meta = parse_meta
        self.reparse = reparse
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        try:
            result = scan_folder(
                self.db,
                self.dataset_id,
                self.root,
                recursive=self.recursive,
                parse_meta=self.parse_meta,
                reparse=self.reparse,
                progress=lambda cur, total, name: self.progress.emit(cur, total, name),
                should_stop=lambda: self._stop,
            )
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class TagWorker(QThread):
    """批量自动打标。

    内部用线程池并发请求，``concurrency`` 来自设置页。
    """

    progress = Signal(int, int, str)              # 完成数, 总数, 文件名
    tagged = Signal(int, list, str)               # image_id, tags, error
    log = Signal(str)
    finished_ok = Signal(dict)                    # {"ok": n, "fail": n, "stopped": bool}

    def __init__(
        self,
        db: Database,
        settings: Settings,
        images: Sequence[dict],
        retag: bool = False,
        use_prompt_hint: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self.images = list(images)
        self.retag = retag
        self.use_prompt_hint = use_prompt_hint
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        summary = {"ok": 0, "fail": 0, "stopped": False, "skipped": 0}
        try:
            tagger = VisionTagger(self.settings)
            tagger.set_banned(self.db.banned_tag_names())
            tagger.invalidate_prompt()

            todo = []
            for image in self.images:
                if self._stop:
                    break
                if not self.retag and int(image.get("tag_count") or 0) > 0:
                    summary["skipped"] += 1
                    continue
                todo.append(image)

            total = len(todo)
            if total == 0:
                summary["stopped"] = self._stop
                self.finished_ok.emit(summary)
                return

            done = 0
            workers = max(1, min(int(self.settings.concurrency or 1), 16))
            self.log.emit(f"开始打标：{total} 张，并发 {workers}，模型 {self.settings.model}")

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {}
                for image in todo:
                    if self._stop:
                        break
                    hint = ""
                    if self.use_prompt_hint:
                        hint = str(image.get("prompt") or "")
                    futures[pool.submit(tagger.tag_image, TagRequest(str(image["path"]), hint))] = image

                for future in as_completed(futures):
                    image = futures[future]
                    done += 1
                    if self._stop:
                        continue
                    try:
                        response = future.result()
                    except Exception as exc:  # noqa: BLE001
                        summary["fail"] += 1
                        self.tagged.emit(int(image["id"]), [], f"{type(exc).__name__}: {exc}")
                        self.progress.emit(done, total, str(image.get("filename", "")))
                        continue

                    if response.ok:
                        added = self.db.add_tags(
                            int(image["id"]), response.tags, source="auto"
                        )
                        summary["ok"] += 1
                        self.tagged.emit(int(image["id"]), added, "")
                    else:
                        summary["fail"] += 1
                        self.tagged.emit(int(image["id"]), [], response.error)

                    self.progress.emit(done, total, str(image.get("filename", "")))

            summary["stopped"] = self._stop
            self.finished_ok.emit(summary)
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"打标线程异常：{type(exc).__name__}: {exc}")
            summary["stopped"] = True
            self.finished_ok.emit(summary)


class ExportWorker(QThread):
    """导出数据集。"""

    progress = Signal(int, int, str)
    finished_ok = Signal(object)     # ExportResult
    failed = Signal(str)

    def __init__(
        self,
        db: Database,
        dataset_id: int,
        settings: Settings,
        root: str | Path | None = None,
        only_tagged: bool = True,
        min_tags: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.dataset_id = dataset_id
        self.settings = settings
        self.root = root
        self.only_tagged = only_tagged
        self.min_tags = min_tags
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        try:
            result: ExportResult = export_dataset(
                self.db,
                self.dataset_id,
                self.settings,
                root=self.root,
                only_tagged=self.only_tagged,
                min_tags=self.min_tags,
                progress=lambda cur, total, name: self.progress.emit(cur, total, name),
                should_stop=lambda: self._stop,
            )
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class PingWorker(QThread):
    """设置页的“测试连接”。"""

    finished_ok = Signal(bool, str)

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings

    def run(self) -> None:  # noqa: D102
        try:
            ok, message = VisionTagger(self.settings).ping()
        except Exception as exc:  # noqa: BLE001
            ok, message = False, f"{type(exc).__name__}: {exc}"
        self.finished_ok.emit(ok, message)


class PromptParseWorker(QThread):
    """批量重解析元数据（改了图但 mtime 没变时手动触发）。"""

    progress = Signal(int, int, str)
    finished_ok = Signal(int)
    failed = Signal(str)

    def __init__(self, db: Database, images: Sequence[dict], parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.images = list(images)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        from .. import comfy_meta  # noqa: PLC0415

        try:
            total = len(self.images)
            count = 0
            for index, image in enumerate(self.images, start=1):
                if self._stop:
                    break
                path = Path(str(image["path"]))
                self.progress.emit(index, total, path.name)
                if not path.exists():
                    continue
                parsed = comfy_meta.extract(path)
                if parsed["has_meta"]:
                    self.db.update_image_meta(
                        int(image["id"]),
                        parsed["prompt"],
                        parsed["negative"],
                        parsed["meta"],
                        width=int(parsed["meta"].get("width") or 0),
                        height=int(parsed["meta"].get("height") or 0),
                    )
                    count += 1
            self.finished_ok.emit(count)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class Wd14Worker(QThread):
    """给一批图跑 WD14 本地打标。

    只算标签，不写库 —— 写库交给主线程，避免两个线程动同一个连接。
    """

    progress = Signal(int, int, str)      # 已处理, 总数, 当前文件名
    loaded = Signal(str)                  # "GPU (CUDA)" / "CPU"
    finished_ok = Signal(object, str)     # {image_id: [标签]}, 错误说明
    failed = Signal(str)

    def __init__(
        self,
        images: Sequence[dict],
        config,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.images = list(images)
        self.config = config
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D102
        from .. import wd14 as wd14_module  # noqa: PLC0415

        try:
            tagger = wd14_module.Wd14Tagger(self.config)
            status = tagger.load()
            if not status.available:
                self.finished_ok.emit({}, status.error or "WD14 不可用")
                return
            self.loaded.emit(status.provider)

            results: dict[int, list[str]] = {}
            total = len(self.images)
            batch_size = max(1, int(self.config.batch_size))

            for start in range(0, total, batch_size):
                if self._stop:
                    break
                chunk = self.images[start : start + batch_size]
                paths = [str(item["path"]) for item in chunk]
                self.progress.emit(start, total, Path(paths[0]).name if paths else "")
                labels = tagger.tag_batch(paths)
                for item, tags in zip(chunk, labels):
                    if tags:
                        results[int(item["id"])] = tags

            self.finished_ok.emit(results, "")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
