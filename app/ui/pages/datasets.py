"""数据集：加入目录、扫描入库、查看进度、导出。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .base import BasePage
from .. import theme
from ..widgets import elide, hline, icon_badge, muted, progress_bar
from ..workers import ExportWorker, ScanWorker

DS_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class _DatasetCard(QFrame):
    scan_requested = Signal(int)
    reparse_requested = Signal(int)
    export_requested = Signal(int)
    remove_requested = Signal(int)
    browse_requested = Signal(int)

    def __init__(self, data: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.data = data
        self.setObjectName("Card")
        # 允许横向被压缩：卡片原本会被超长路径标签顶到 900+px 的最小宽度，
        # 导致滚动区出现横向滚动条、右侧按钮被推出视野。
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 13)
        outer.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(11)
        top.addWidget(icon_badge("▦", theme.ACCENT, 38))

        name_box = QVBoxLayout()
        name_box.setSpacing(2)
        self.name = QLabel(str(data.get("name", "")))
        self.name.setObjectName("CardTitle")
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.name.setMinimumWidth(0)
        self.name.setToolTip(str(data.get("name", "")))
        self.path = QLabel()
        self.path.setObjectName("Mono")
        self.path.setToolTip(str(data.get("path", "")))
        self._full_path = str(data.get("path", ""))
        # 路径按**像素**省略，而不是按字符数。
        # 原来写死 elide(path, 70)：中文路径一个字约占两倍宽，
        # 70 个字符能撑到 800px，把卡片顶到 949px —— 小窗口下
        # 于是出现横向滚动条、右侧按钮被推出视野。这里改成随宽度自适应。
        self.path.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.path.setMinimumWidth(0)
        name_box.addWidget(self.name)
        name_box.addWidget(self.path)
        top.addLayout(name_box, 1)

        ratio = int(round(float(data.get("progress") or 0) * 100))
        self.ratio = QLabel(f"{ratio}%")
        self.ratio.setStyleSheet(f"color: {theme.GREEN}; font-size: 15px; font-weight: 700;")
        top.addWidget(self.ratio, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(top)

        self.bar = progress_bar(float(data.get("progress") or 0.0))
        self.bar.setFixedHeight(6)
        outer.addWidget(self.bar)

        stats = QHBoxLayout()
        stats.setSpacing(16)
        stats.addWidget(muted(f"共 {int(data.get('total') or 0):,} 张"))
        stats.addWidget(muted(f"已打标 {int(data.get('tagged') or 0):,} 张"))
        stats.addWidget(muted(f"待打标 {int(data.get('pending') or 0):,} 张"))
        stats.addStretch(1)
        outer.addLayout(stats)

        outer.addWidget(hline())

        actions = QHBoxLayout()
        actions.setSpacing(8)
        # 按钮允许被压窄：小窗口下需要能挤进 ~530px 宽的卡片，
        # 否则卡片最小宽度会顶到 694px，逼出横向滚动条。
        scan = QPushButton("重新扫描")
        scan.setMinimumWidth(72)
        scan.clicked.connect(lambda: self.scan_requested.emit(int(self.data["id"])))
        actions.addWidget(scan)

        reparse = QPushButton("重新解析提示词")
        reparse.setObjectName("Segment")
        reparse.setToolTip(
            "把图库里的正向提示词重新解析一遍。\n"
            "用于老数据集：早期扫描存的是原始 JSON / NAI 权重语法，\n"
            "点这个刷成干净标签（girl, solo, ...）。\n"
            "只读 PNG、只更新数据库，不动图片和 txt。"
        )
        reparse.clicked.connect(lambda: self.reparse_requested.emit(int(self.data["id"])))
        actions.addWidget(reparse)

        export = QPushButton("导出数据集")
        export.setObjectName("Primary")
        export.clicked.connect(lambda: self.export_requested.emit(int(self.data["id"])))
        actions.addWidget(export)

        actions.addStretch(1)
        browse = QPushButton("打开目录")
        browse.setObjectName("Segment")
        browse.clicked.connect(lambda: self.browse_requested.emit(int(self.data["id"])))
        actions.addWidget(browse)
        remove = QPushButton("移除")
        remove.setObjectName("GhostDanger")
        remove.clicked.connect(lambda: self.remove_requested.emit(int(self.data["id"])))
        actions.addWidget(remove)
        outer.addLayout(actions)
        self._elide_labels()

    def set_busy(self, busy: bool, text: str = "") -> None:
        if busy and text:
            self.ratio.setText(text)
            self.ratio.setStyleSheet(f"color: {theme.ACCENT}; font-size: 12px; font-weight: 600;")
        else:
            ratio = int(round(float(self.data.get("progress") or 0) * 100))
            self.ratio.setText(f"{ratio}%")
            self.ratio.setStyleSheet(f"color: {theme.GREEN}; font-size: 15px; font-weight: 700;")

    # ------------------------------------------------------------------ #
    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """窗口变窄时把名称/路径按可用像素重新省略，杜绝撑宽卡片。"""
        super().resizeEvent(event)
        self._elide_labels()

    def _elide_labels(self) -> None:
        full_name = str(self.data.get("name", ""))
        for label, text in ((self.name, full_name), (self.path, self._full_path)):
            avail = max(label.width(), 0)
            if avail <= 0:
                continue
            metrics = label.fontMetrics()
            label.setText(metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, avail))


class DatasetsPage(BasePage):
    title = "数据集"
    subtitle = "管理出图目录，扫描入库与导出"

    def build(self) -> None:
        # ---- 顶部操作 ----
        bar = QFrame()
        bar.setObjectName("Panel")
        form = QVBoxLayout(bar)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(8)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        path_row.addWidget(muted("图片目录"))

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("选择 ComfyUI output 或任意图片文件夹")
        self.path_edit.setClearButtonEnabled(True)
        path_row.addWidget(self.path_edit, 1)

        pick = QPushButton("浏览文件夹…")
        pick.clicked.connect(self._browse)
        path_row.addWidget(pick)
        form.addLayout(path_row)

        option_row = QHBoxLayout()
        option_row.setSpacing(8)
        option_row.addWidget(muted("数据集名称"))

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("可选；留空时自动使用文件夹名")
        option_row.addWidget(self.name_edit, 1)

        self.recursive = QCheckBox("扫描子文件夹")
        self.recursive.setChecked(True)
        self.recursive.setToolTip("同时扫描这个目录下面的所有子文件夹")
        option_row.addWidget(self.recursive)

        add_btn = QPushButton("添加数据集并开始扫描")
        add_btn.setObjectName("Primary")
        add_btn.clicked.connect(self._add_dataset)
        option_row.addWidget(add_btn)
        form.addLayout(option_row)
        self.root.addWidget(bar)

        # ---- 扫描进度条 ----
        self.scan_panel = QFrame()
        self.scan_panel.setObjectName("Panel")
        self.scan_panel.setVisible(False)
        scan_row = QHBoxLayout(self.scan_panel)
        scan_row.setContentsMargins(12, 8, 12, 8)
        scan_row.setSpacing(10)
        self.scan_label = QLabel("扫描中…")
        self.scan_bar = progress_bar(0.0)
        self.scan_bar.setFixedHeight(6)
        scan_row.addWidget(self.scan_label, 0)
        scan_row.addWidget(self.scan_bar, 1)
        self.scan_cancel = QPushButton("停止")
        self.scan_cancel.setObjectName("Segment")
        self.scan_cancel.clicked.connect(self._on_cancel)
        scan_row.addWidget(self.scan_cancel)
        self.root.addWidget(self.scan_panel)

        # ---- 列表 ----
        # 卡片列表必须套滚动区：小窗口下超过 4~5 个数据集时，
        # 原来的裸 QWidget 会把超出的卡片直接裁掉，看不到也滚不到。
        self.list = QWidget()
        self.list_box = QVBoxLayout(self.list)
        self.list_box.setContentsMargins(0, 0, 2, 0)
        self.list_box.setSpacing(10)

        self.list_scroll = QScrollArea()
        self.list_scroll.setObjectName("DatasetScroll")
        self.list_scroll.setWidgetResizable(True)
        self.list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.list_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.list_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        # 卡片纵向排布，宽度跟着滚动区走，避免出现横向滚动条
        self.list.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.list_scroll.setWidget(self.list)
        self.root.addWidget(self.list_scroll, 1)

        self._cards: dict[int, _DatasetCard] = {}
        self._scan_worker: ScanWorker | None = None
        self._export_worker: ExportWorker | None = None
        self._cancel_cb = None

    # ------------------------------------------------------------------ #
    def _browse(self) -> None:
        start = self.settings.dataset_dir or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "选择出图目录", start)
        if folder:
            self.path_edit.setText(folder)
            self.settings.dataset_dir = folder
            if not self.name_edit.text().strip():
                self.name_edit.setText(Path(folder).name)

    def _add_dataset(self) -> None:
        raw = self.path_edit.text().strip().strip('"')
        if not raw:
            QMessageBox.warning(self, "缺少目录", "请先填写或浏览出图目录。")
            return
        folder = Path(raw).expanduser()
        if not folder.is_dir():
            QMessageBox.critical(
                self, "目录不存在", f"找不到这个目录：\n{folder}\n\n是不是写错了？"
            )
            return

        self.settings.dataset_dir = str(folder)
        name = self.name_edit.text().strip() or folder.name
        try:
            dataset_id = self.db.upsert_dataset(name, str(folder))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "添加失败", str(exc))
            return

        self.ctx.datasets_changed.emit()
        self.ctx.notify(f"已加入数据集「{name}」，开始扫描…")
        self._start_scan(dataset_id, str(folder))

    def scan_dataset(self, dataset_id: int, reparse: bool = False) -> None:
        row = self.db.get_dataset(dataset_id)
        if row is None:
            return
        self._start_scan(dataset_id, str(row["path"]), reparse=reparse)

    def reparse_dataset(self, dataset_id: int) -> None:
        """整库重解析 PNG 元数据（画师词/权重语法清理后要跑一次）。"""
        row = self.db.get_dataset(dataset_id)
        if row is None:
            return
        count = self.db.stats(dataset_id=dataset_id)["images"]
        box = QMessageBox(self)
        box.setWindowTitle("重新解析提示词")
        box.setText(f"重新解析 {count:,} 张图的 PNG 元数据？")
        box.setInformativeText(
            "用来把图库里老格式的正向提示词刷成干净的标签。\n\n"
            "会做这些事：\n"
            "  · 剥掉 NAI 权重语法（1.3::word:: → word）\n"
            "  · 丢掉画师词（by xxx / Artists:xxx）\n"
            "  · 丢掉负权重词（-5::word::）\n\n"
            "只读 PNG、只更新数据库，**不动图片，也不动 txt**。\n"
            f"990 张大约几秒。"
        )
        box.addButton("开始", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not box.buttons()[0]:
            return
        self._start_scan(dataset_id, str(row["path"]), reparse=True)

    def _start_scan(self, dataset_id: int, folder: str, reparse: bool = False) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            QMessageBox.information(self, "正在扫描", "上一个扫描还没结束，稍等一下。")
            return
        self.scan_panel.setVisible(True)
        self.scan_bar.set_value(0.0)
        self.scan_label.setText("正在枚举文件…")

        worker = ScanWorker(
            self.db,
            dataset_id,
            folder,
            recursive=self.recursive.isChecked(),
            parse_meta=True,
            reparse=reparse,
            parent=self,
        )
        worker.progress.connect(self._on_scan_progress)
        worker.finished_ok.connect(self._on_scan_done)
        worker.failed.connect(self._on_scan_failed)
        worker.finished.connect(self._on_scan_closed)
        self._scan_worker = worker
        self._cancel_cb = worker.stop
        self.scan_cancel.setText("停止")
        worker.start()

    def _stop_scan(self) -> None:
        if self._scan_worker is not None:
            self._scan_worker.stop()
            self.scan_label.setText("正在停止…")

    def _on_cancel(self) -> None:
        """「停止 / 取消」按钮统一入口。"""
        if self._cancel_cb is None:
            return
        self._cancel_cb()
        self.scan_label.setText("正在停止…")

    def _on_scan_progress(self, current: int, total: int, name: str) -> None:
        self.scan_bar.set_value(current / total if total else 0.0)
        self.scan_label.setText(f"{current}/{total}  {elide(name, 48)}")

    def _on_scan_done(self, result) -> None:
        self.scan_label.setText(
            f"完成：新增 {result.added}，更新 {result.updated}，"
            f"带提示词 {result.with_meta}，清理 {result.removed}"
        )
        self.scan_bar.set_value(1.0, theme.GREEN)
        if result.scanned == 0:
            self.ctx.notify("这个目录里没找到图片（支持 png/jpg/jpeg/webp/bmp/gif/avif/tif）")
        else:
            self.ctx.notify(
                f"扫描完成：共 {result.scanned} 张，新增 {result.added}，"
                f"其中 {result.with_meta} 张读到提示词"
            )
        self.ctx.datasets_changed.emit()
        self.ctx.images_changed.emit()
        if self.settings.auto_tag_on_scan and result.added:
            self.ctx.notify("已按设置自动进入打标队列")

    def _on_scan_failed(self, message: str) -> None:
        QMessageBox.critical(self, "扫描失败", message)
        self.ctx.notify(f"扫描失败：{message}")

    def _on_scan_closed(self) -> None:
        self.scan_panel.setVisible(False)

    # ------------------------------------------------------------------ #
    def export_dataset(self, dataset_id: int) -> None:
        if self._export_worker is not None and self._export_worker.isRunning():
            QMessageBox.information(self, "正在导出", "上一个导出还没结束。")
            return
        self.scan_panel.setVisible(True)
        self.scan_label.setText("导出中…")
        self.scan_bar.set_value(0.0)
        self.scan_cancel.setText("取消")

        worker = ExportWorker(self.db, dataset_id, self.settings, parent=self)
        worker.progress.connect(self._on_export_progress)
        worker.finished_ok.connect(self._on_export_done)
        worker.failed.connect(self._on_export_failed)
        worker.finished.connect(self._on_scan_closed)
        self._export_worker = worker
        self._cancel_cb = worker.stop
        self.scan_cancel.setText("取消")
        worker.start()

    def _on_export_progress(self, current: int, total: int, name: str) -> None:
        self.scan_bar.set_value(current / total if total else 0.0)
        self.scan_label.setText(f"导出 {current}/{total}  {elide(name, 44)}")

    def _on_export_done(self, result) -> None:
        from ... import paths  # noqa: PLC0415
        from ...exporter import export_readme  # noqa: PLC0415

        try:
            export_readme(result.target, result, self.settings)
        except Exception:
            pass
        self.scan_bar.set_value(1.0, theme.GREEN)
        self.scan_label.setText(result.summary())
        self.ctx.notify(f"已导出到 {result.target}")
        box = QMessageBox(self)
        box.setWindowTitle("导出完成")
        box.setText(result.summary())
        box.setInformativeText(f"目录：{result.target}")
        open_btn = box.addButton("打开目录", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("好", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            paths.open_in_explorer(result.target)

    def _on_export_failed(self, message: str) -> None:
        QMessageBox.critical(self, "导出失败", message)
        self.ctx.notify(f"导出失败：{message}")

    # ------------------------------------------------------------------ #
    def _browse_dataset(self, dataset_id: int) -> None:
        from ... import paths  # noqa: PLC0415

        row = self.db.get_dataset(dataset_id)
        if row is None:
            return
        folder = Path(str(row["path"]))
        if not folder.exists():
            QMessageBox.warning(self, "目录不存在", f"{folder}\n\n目录可能已被移动或删除。")
            return
        paths.open_in_explorer(folder)

    def _remove_dataset(self, dataset_id: int) -> None:
        row = self.db.get_dataset(dataset_id)
        if row is None:
            return
        total = self.db.count_images(dataset_id)
        box = QMessageBox(self)
        box.setWindowTitle("移除数据集")
        box.setText(f"要从工作台移除「{row['name']}」吗？")
        box.setInformativeText(
            f"会从库中删除 {total} 张图片的记录和它们的标签。\n"
            "磁盘上的原始图片不会被删除。"
        )
        yes = box.addButton("移除", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            return
        self.db.delete_dataset(dataset_id, drop_images=True)
        self.ctx.datasets_changed.emit()
        self.ctx.images_changed.emit()
        self.ctx.notify(f"已移除数据集「{row['name']}」")

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        if not self._loaded:
            return
        while self.list_box.count():
            item = self.list_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._cards.clear()

        rows = self.db.list_datasets()
        if not rows:
            empty = QFrame()
            empty.setObjectName("Panel")
            col = QVBoxLayout(empty)
            col.setContentsMargins(20, 40, 20, 40)
            col.setSpacing(8)
            title = QLabel("还没有数据集")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title.setObjectName("SectionTitle")
            desc = QLabel(
                "把 ComfyUI 的 output 目录（或它的子目录）加进来，\n"
                "程序会自动读取每张 PNG 里内嵌的提示词、模型和采样参数。"
            )
            desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
            desc.setObjectName("Muted")
            col.addWidget(title)
            col.addWidget(desc)
            self.list_box.addWidget(empty)
            return

        for data in rows:
            card = _DatasetCard(data)
            card.scan_requested.connect(self.scan_dataset)
            card.reparse_requested.connect(self.reparse_dataset)
            card.export_requested.connect(self.export_dataset)
            card.remove_requested.connect(self._remove_dataset)
            card.browse_requested.connect(self._browse_dataset)
            self.list_box.addWidget(card)
            self._cards[int(data["id"])] = card
        self.list_box.addStretch(1)

    # ------------------------------------------------------------------ #
    def on_payload(self, payload: object) -> None:
        if payload == "add":
            self.path_edit.setFocus()
