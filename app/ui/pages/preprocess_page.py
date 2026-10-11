"""Native image preparation workspace: rename, mosaic and crop."""

from __future__ import annotations

import io
import random
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTabWidget, QVBoxLayout, QWidget, QInputDialog,
)

from ... import paths, preprocess
from ...config import save_settings
from .base import BasePage
from .. import theme


class TaskWorker(QThread):
    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.stopping = False

    def run(self):
        try:
            result = self.operation(lambda a, b, c: self.progress.emit(a, b, c),
                                    lambda: self.stopping)
            self.succeeded.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ImageCanvas(QWidget):
    """Loads only the current full-resolution image. Edited drafts live on disk."""
    edited = Signal()
    previous_requested = Signal()
    next_requested = Signal()
    save_requested = Signal()
    brush_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 340)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.path: Path | None = None
        self.original: Image.Image | None = None
        self.work: Image.Image | None = None
        self.crop_box: tuple[int, int, int, int] | None = None
        self.ratio = (1, 1)
        self.tool = "crop"
        self.shape = "brush"
        self.effect = "pixel"
        self.brush_size = 32
        self.strength = 16
        self.start: tuple[int, int] | None = None
        self.points: list[tuple[int, int]] = []
        self._preview = QPixmap()
        self._image_rect = None
        self._temp = tempfile.TemporaryDirectory(prefix="preprocess-", dir=paths.data_dir())
        self._drafts: dict[str, Path] = {}
        self._histories: dict[str, list[Path]] = {}
        self._counter = 0

    @property
    def modified_paths(self) -> list[Path]:
        self.stash()
        return [Path(p) for p in self._drafts]

    def draft_for(self, path: Path) -> Path:
        self.stash()
        return self._drafts[str(path)]

    def stash(self) -> None:
        if self.path is not None and self.work is not None and str(self.path) in self._drafts:
            draft = self._drafts[str(self.path)]
            self.work.save(draft, format="PNG")

    def load_path(self, path: Path) -> None:
        if self.path == path:
            return
        self.stash()
        self.path = path
        self.crop_box = None
        self.original = self.work = None
        if path.suffix.lower() not in preprocess.EDIT_EXTENSIONS:
            self._preview = QPixmap()
            self.update()
            return
        with Image.open(path) as image:
            self.original = ImageOps.exif_transpose(image).convert("RGBA")
        draft = self._drafts.get(str(path))
        if draft and draft.is_file():
            with Image.open(draft) as image:
                self.work = image.convert("RGBA")
        else:
            self.work = self.original.copy()
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        if self.work is None:
            self._preview = QPixmap()
        else:
            thumb = self.work.copy()
            thumb.thumbnail((1700, 1200))
            buffer = io.BytesIO()
            thumb.save(buffer, "PNG")
            self._preview = QPixmap()
            self._preview.loadFromData(buffer.getvalue())
        self.update()

    def set_ratio(self, ratio: tuple[int, int]) -> None:
        self.ratio = ratio
        if self.work:
            self.crop_box = preprocess.centered_crop_box(*self.work.size, *ratio)
            self.update()

    def _map_point(self, position) -> tuple[int, int] | None:
        if self.work is None or self._image_rect is None:
            return None
        rect = self._image_rect
        x = (position.x() - rect.x()) / max(1, rect.width())
        y = (position.y() - rect.y()) / max(1, rect.height())
        if not (0 <= x <= 1 and 0 <= y <= 1):
            return None
        return (min(self.work.width - 1, int(x * self.work.width)),
                min(self.work.height - 1, int(y * self.work.height)))

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202932"))
        if self._preview.isNull():
            painter.setPen(QColor("#e2e8ed"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "选择图片以预览\n支持 PNG / JPEG / WebP / BMP 像素编辑")
            return
        scaled = self._preview.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
        x, y = (self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2
        from PySide6.QtCore import QRect
        self._image_rect = QRect(x, y, scaled.width(), scaled.height())
        painter.drawPixmap(x, y, scaled)
        if self.tool == "crop" and self.crop_box and self.work:
            left, top, right, bottom = self.crop_box
            sx = scaled.width() / self.work.width
            sy = scaled.height() / self.work.height
            painter.setPen(QPen(QColor("#ff5578"), 2))
            painter.setBrush(QColor(255, 255, 255, 35))
            painter.drawRect(x + int(left*sx), y + int(top*sy),
                             int((right-left)*sx), int((bottom-top)*sy))
        elif self.tool == "mosaic" and len(self.points) > 1:
            painter.setPen(QPen(QColor("#ff5578"), 2))
            for a, b in zip(self.points, self.points[1:]):
                painter.drawLine(x + int(a[0]*scaled.width()/self.work.width),
                                 y + int(a[1]*scaled.height()/self.work.height),
                                 x + int(b[0]*scaled.width()/self.work.width),
                                 y + int(b[1]*scaled.height()/self.work.height))

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        point = self._map_point(event.position())
        if point:
            self.start = point
            self.points = [point]

    def mouseMoveEvent(self, event):  # noqa: N802
        if self.start is None:
            return
        point = self._map_point(event.position())
        if point:
            if self.tool == "crop":
                self.crop_box = preprocess.ratio_drag_box(
                    self.start, point, self.work.size, self.ratio
                )
            else:
                self.points.append(point)
            self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self.start is None:
            return
        point = self._map_point(event.position()) or self.start
        if self.tool == "mosaic" and self.work is not None:
            if self.shape == "rect":
                self.points = [self.start, point]
            else:
                self.points.append(point)
            self._apply_effect()
        elif self.tool == "crop" and self.crop_box is None and self.work:
            self.crop_box = preprocess.centered_crop_box(*self.work.size, *self.ratio)
        self.start = None
        self.points = []
        self.update()

    def _snapshot(self) -> None:
        if self.work is None or self.path is None:
            return
        self._counter += 1
        snap = Path(self._temp.name) / f"undo-{self._counter}.png"
        self.work.save(snap, "PNG")
        history = self._histories.setdefault(str(self.path), [])
        history.append(snap)
        if len(history) > 30:
            history.pop(0).unlink(missing_ok=True)

    def _apply_effect(self) -> None:
        if self.work is None or self.original is None or self.path is None:
            return
        self._snapshot()
        mask = Image.new("L", self.work.size)
        draw = ImageDraw.Draw(mask)
        if self.shape == "rect":
            a, b = self.points[0], self.points[-1]
            draw.rectangle((min(a[0],b[0]), min(a[1],b[1]), max(a[0],b[0]), max(a[1],b[1])), fill=255)
        else:
            radius = max(1, self.brush_size // 2)
            draw.line(self.points, fill=255, width=self.brush_size)
            for x, y in (self.points[0], self.points[-1]):
                draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=255)
        if self.effect == "erase":
            effected = self.original
        elif self.effect == "blur":
            effected = self.work.filter(ImageFilter.GaussianBlur(max(1, self.strength)))
            mask = mask.filter(ImageFilter.GaussianBlur(max(1, self.strength // 3)))
        elif self.effect == "black":
            effected = Image.new("RGBA", self.work.size, "black")
        elif self.effect == "white":
            effected = Image.new("RGBA", self.work.size, "white")
        else:
            block = max(4, self.strength)
            small = self.work.resize((max(1,self.work.width//block), max(1,self.work.height//block)),
                                     Image.Resampling.BOX)
            effected = small.resize(self.work.size, Image.Resampling.NEAREST)
        self.work = Image.composite(effected, self.work, mask)
        draft = self._drafts.get(str(self.path))
        if draft is None:
            draft = Path(self._temp.name) / f"draft-{len(self._drafts)}.png"
            self._drafts[str(self.path)] = draft
        self._refresh_preview()
        self.edited.emit()

    def undo(self) -> bool:
        if self.path is None:
            return False
        history = self._histories.get(str(self.path), [])
        if not history:
            return False
        snap = history.pop()
        with Image.open(snap) as image:
            self.work = image.convert("RGBA")
        snap.unlink(missing_ok=True)
        if not history and self.original and self.work.tobytes() == self.original.tobytes():
            self._drafts.pop(str(self.path), None)
        self._refresh_preview()
        return True

    def wheelEvent(self, event):  # noqa: N802
        if self.tool == "mosaic":
            delta = 5 if event.angleDelta().y() > 0 else -5
            self.brush_changed.emit(max(5, min(250, self.brush_size + delta)))
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_A, Qt.Key.Key_Left):
            self.previous_requested.emit()
        elif key in (Qt.Key.Key_D, Qt.Key.Key_Right):
            self.next_requested.emit()
        elif key == Qt.Key.Key_BracketLeft:
            self.brush_changed.emit(max(5, self.brush_size - 5))
        elif key == Qt.Key.Key_BracketRight:
            self.brush_changed.emit(min(250, self.brush_size + 5))
        elif key == Qt.Key.Key_Z and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.undo()
        elif key == Qt.Key.Key_S and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.save_requested.emit()
        else:
            super().keyPressEvent(event)


class PreprocessPage(BasePage):
    title = "预处理"
    subtitle = "图片排序重命名、马赛克与裁剪"

    def build(self) -> None:
        self.setAcceptDrops(True)
        self._worker: TaskWorker | None = None
        self._rows: dict[str, dict] = {}
        self._plan: list[preprocess.RenameEntry] = []
        top = QHBoxLayout()
        choose = QPushButton("打开外部文件夹")
        choose.clicked.connect(self._open_folder)
        top.addWidget(choose)
        self.source_label = QLabel("从图库选中图片后点击“预处理”，或打开外部文件夹")
        top.addWidget(self.source_label, 1)
        clear = QPushButton("清空列表")
        clear.clicked.connect(self._clear_items)
        top.addWidget(clear)
        self.root.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        controls = QFrame()
        controls.setObjectName("Panel")
        controls.setMinimumWidth(290)
        controls.setMaximumWidth(380)
        controls_col = QVBoxLayout(controls)
        controls_col.setContentsMargins(7,7,7,7)
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.addTab(self._rename_controls(), "重命名")
        self.tabs.addTab(self._mosaic_controls(), "马赛克")
        self.tabs.addTab(self._crop_controls(), "裁剪")
        controls_col.addWidget(self.tabs, 1)
        self.items = QListWidget()
        self.items.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.items.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.items.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.items.currentItemChanged.connect(self._current_changed)
        controls_col.addWidget(QLabel("图片顺序（可拖动；Ctrl/Shift 多选）"))
        controls_col.addWidget(self.items, 1)
        splitter.addWidget(controls)

        center = QWidget()
        center_col = QVBoxLayout(center)
        self.canvas = ImageCanvas()
        self.canvas.tool = "view"
        self.canvas.edited.connect(self._update_draft_count)
        self.canvas.previous_requested.connect(lambda: self._step_image(-1))
        self.canvas.next_requested.connect(lambda: self._step_image(1))
        self.canvas.save_requested.connect(
            lambda: self._save_mosaic() if self.tabs.currentIndex() == 1 else self._save_crop()
            if self.tabs.currentIndex() == 2 else None
        )
        self.canvas.brush_changed.connect(self.brush.setValue)
        center_col.addWidget(self.canvas, 1)
        self.info = QLabel("预览只加载当前图片；原图默认不变")
        center_col.addWidget(self.info)
        splitter.addWidget(center)

        right = QFrame()
        right.setObjectName("Panel")
        right.setMinimumWidth(220)
        right.setMaximumWidth(360)
        right_col = QVBoxLayout(right)
        right_col.setContentsMargins(9,9,9,9)
        right_col.addWidget(QLabel("导出位置"))
        self.output = QLineEdit(self.settings.preprocess_export_dir or self.settings.export_dir)
        right_col.addWidget(self.output)
        browse = QPushButton("选择目录…")
        browse.clicked.connect(self._choose_output)
        right_col.addWidget(browse)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("重命名前先预览所有目标和冲突")
        right_col.addWidget(QLabel("改名预览 / 执行结果"))
        right_col.addWidget(self.preview, 1)
        right_col.addWidget(QLabel("操作记录（重命名可跨重启撤销）"))
        self.history = QListWidget()
        right_col.addWidget(self.history, 1)
        self.undo_op = QPushButton("撤销选中的重命名记录")
        self.undo_op.clicked.connect(self._undo_operation)
        right_col.addWidget(self.undo_op)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([315, 730, 270])
        self.root.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        footer.addWidget(self.progress, 1)
        self.cancel_btn = QPushButton("停止")
        self.cancel_btn.clicked.connect(self._stop)
        self.cancel_btn.hide()
        footer.addWidget(self.cancel_btn)
        self.root.addLayout(footer)
        self._refresh_history()

    def _scroll_form(self) -> tuple[QWidget, QFormLayout]:
        holder = QWidget()
        form = QFormLayout(holder)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        return scroll, form

    def _rename_controls(self) -> QWidget:
        scroll, form = self._scroll_form()
        self.prefix = QLineEdit()
        self.suffix = QLineEdit()
        self.separator = QLineEdit("_")
        form.addRow("前缀", self.prefix)
        form.addRow("后缀", self.suffix)
        form.addRow("分隔符", self.separator)
        self.start_no = QSpinBox(); self.start_no.setRange(0, 9999999); self.start_no.setValue(1)
        self.digits = QSpinBox(); self.digits.setRange(1, 12); self.digits.setValue(3)
        self.step = QSpinBox(); self.step.setRange(1, 9999); self.step.setValue(1)
        form.addRow("起始序号", self.start_no)
        form.addRow("补零位数", self.digits)
        form.addRow("序号步长", self.step)
        self.keep_name = QCheckBox("保留原文件名")
        self.number_last = QCheckBox("序号放在原名之后")
        self.auto_suffix = QCheckBox("重名自动加 (2)")
        form.addRow(self.keep_name); form.addRow(self.number_last); form.addRow(self.auto_suffix)
        self.case = QComboBox()
        for key, label in (("keep","保持大小写"),("lower","小写"),("upper","大写")):
            self.case.addItem(label, key)
        form.addRow("大小写", self.case)
        self.group = QComboBox()
        for key, label in (("all","全局连续"),("folder","按来源文件夹"),("parent","按上级目录"),("star","按星标")):
            self.group.addItem(label, key)
        form.addRow("编号分组", self.group)
        self.date_source = QComboBox()
        for key, label in (("mtime","修改日期"),("exif","EXIF 拍摄日期"),("now","当前日期")):
            self.date_source.addItem(label, key)
        self.date_format = QComboBox()
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y%m%d_%H%M%S", "%y%m%d"):
            self.date_format.addItem(fmt, fmt)
        form.addRow("日期来源", self.date_source)
        form.addRow("日期格式", self.date_format)
        form.addRow(QLabel("可用字段：{序号} {原名} {文件名}\n{扩展名} {日期} {组名} {上级目录}"))
        self.rename_mode = QComboBox()
        for key, label in (("rename","原地成对改名"),("copy","复制导出"),("move","移动分流")):
            self.rename_mode.addItem(label, key)
        form.addRow("处理方式", self.rename_mode)
        self.split = QComboBox()
        for key, label in (("single","单一目录"),("folder","来源文件夹"),("parent","上级目录"),("star","按星标"),("batch","每 N 张")):
            self.split.addItem(label, key)
        self.split_size = QSpinBox(); self.split_size.setRange(1,100000); self.split_size.setValue(100)
        form.addRow("导出分流", self.split); form.addRow("每组数量", self.split_size)
        sort_row = QHBoxLayout()
        for label, key in (("名称↑","name"),("时间↓","time"),("随机","random"),("倒序","reverse")):
            button = QPushButton(label); button.clicked.connect(lambda _=False, k=key: self._sort(k))
            sort_row.addWidget(button)
        form.addRow(sort_row)
        micro = QHBoxLayout()
        for label, offset in (("↑",-1),("↓",1),("⤒",-1000000),("⤓",1000000)):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, n=offset: self._move_relative(n))
            micro.addWidget(button)
        form.addRow(micro)
        move_row = QHBoxLayout()
        star = QPushButton("★ 星标/取消")
        star.clicked.connect(self._toggle_star)
        move_row.addWidget(star)
        reorder = QPushButton("批量移动…")
        reorder.clicked.connect(self._move_selected)
        move_row.addWidget(reorder)
        form.addRow(move_row)
        exact = QPushButton("按序号重排选中图片…")
        exact.clicked.connect(self._reorder_selected)
        form.addRow(exact)
        preview = QPushButton("预览改动与冲突")
        preview.clicked.connect(self._preview_rename)
        form.addRow(preview)
        run = QPushButton("确认执行重命名")
        run.setObjectName("Primary")
        run.clicked.connect(self._run_rename)
        form.addRow(run)
        return scroll

    def _mosaic_controls(self) -> QWidget:
        scroll, form = self._scroll_form()
        self.shape = QComboBox()
        self.shape.addItem("手涂画笔", "brush"); self.shape.addItem("矩形拉框", "rect")
        self.shape.currentIndexChanged.connect(self._sync_canvas)
        form.addRow("工具", self.shape)
        self.effect = QComboBox()
        for key, label in (("pixel","像素马赛克"),("blur","高斯模糊"),("black","黑色遮挡"),
                           ("white","白色遮挡"),("erase","擦除还原")):
            self.effect.addItem(label, key)
        self.effect.currentIndexChanged.connect(self._sync_canvas)
        form.addRow("效果", self.effect)
        self.brush = QSpinBox(); self.brush.setRange(5,250); self.brush.setValue(32)
        self.brush.valueChanged.connect(self._sync_canvas)
        self.strength = QSpinBox(); self.strength.setRange(4,65); self.strength.setValue(16)
        self.strength.valueChanged.connect(self._sync_canvas)
        form.addRow("画笔粗细", self.brush); form.addRow("效果强度", self.strength)
        undo = QPushButton("撤销上一笔（最多 30 步）")
        undo.clicked.connect(lambda: self.canvas.undo())
        form.addRow(undo)
        restore = QPushButton("还原当前图片")
        restore.clicked.connect(self._restore_canvas)
        form.addRow(restore)
        self.overwrite = QCheckBox("直接覆盖原图（先备份）")
        form.addRow(self.overwrite)
        self.auto_export = QCheckBox("切图时自动导出已修改图（仅副本）")
        form.addRow(self.auto_export)
        save = QPushButton("保存当前图")
        save.setObjectName("Primary"); save.clicked.connect(self._save_mosaic)
        form.addRow(save)
        batch = QPushButton("批量导出所有已修改图片")
        batch.clicked.connect(lambda: self._save_mosaic(batch=True))
        form.addRow(batch)
        form.addRow(QLabel("切图时草稿自动保存；仅处理已修改的图。"))
        return scroll

    def _crop_controls(self) -> QWidget:
        scroll, form = self._scroll_form()
        self.crop_ratio = QComboBox()
        for ratio in ((1,1),(4,3),(3,4),(3,2),(2,3),(16,9),(9,16)):
            self.crop_ratio.addItem(f"{ratio[0]}:{ratio[1]}", ratio)
        self.crop_ratio.currentIndexChanged.connect(self._ratio_changed)
        form.addRow("裁剪比例", self.crop_ratio)
        self.custom_w = QSpinBox(); self.custom_w.setRange(1,10000); self.custom_w.setValue(1)
        self.custom_h = QSpinBox(); self.custom_h.setRange(1,10000); self.custom_h.setValue(1)
        custom = QHBoxLayout(); custom.addWidget(self.custom_w); custom.addWidget(QLabel(":")); custom.addWidget(self.custom_h)
        form.addRow("自定义宽高比", custom)
        custom_btn = QPushButton("使用自定义比例")
        custom_btn.clicked.connect(lambda: self.canvas.set_ratio((self.custom_w.value(), self.custom_h.value())))
        form.addRow(custom_btn)
        center_btn = QPushButton("当前图：最大居中区域")
        center_btn.clicked.connect(lambda: self.canvas.set_ratio(self.canvas.ratio))
        form.addRow(center_btn)
        form.addRow(QLabel("单图：在预览上拖动裁剪框。\n批量：统一比例，逐图最大居中裁剪。"))
        save = QPushButton("裁剪当前图")
        save.setObjectName("Primary"); save.clicked.connect(self._save_crop)
        form.addRow(save)
        batch = QPushButton("生成批量裁剪预览")
        batch.clicked.connect(self._preview_batch_crop)
        form.addRow(batch)
        run = QPushButton("确认批量裁剪")
        run.clicked.connect(self._run_batch_crop)
        form.addRow(run)
        return scroll

    def _paths(self) -> list[Path]:
        return [Path(self.items.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.items.count())]

    def _current_path(self) -> Path | None:
        item = self.items.currentItem()
        return Path(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):  # noqa: N802
        if not event.mimeData().hasUrls():
            return super().dropEvent(event)
        files = []
        for url in event.mimeData().urls():
            local = Path(url.toLocalFile())
            if local.is_dir():
                files.extend(p for p in local.rglob("*") if p.is_file())
            elif local.is_file():
                files.append(local)
        self._add_rows({"path": str(p)} for p in files)
        event.acceptProposedAction()

    def on_payload(self, payload: object) -> None:
        if isinstance(payload, dict) and payload.get("images"):
            self._add_rows(payload["images"])

    def _add_rows(self, rows) -> None:
        first = None
        seen = {str(p) for p in self._paths()}
        for row in rows:
            path = paths.norm_path(row["path"])
            if str(path) in seen or not path.is_file() or path.suffix.lower() not in preprocess.RENAME_EXTENSIONS:
                continue
            seen.add(str(path))
            item = QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setData(Qt.ItemDataRole.UserRole + 1, False)
            item.setToolTip(str(path))
            self.items.addItem(item)
            self._rows[str(path)] = dict(row)
            if first is None:
                first = item
        if first:
            self.items.setCurrentItem(first)
        self.source_label.setText(f"已载入 {self.items.count():,} 张图片；列表可拖动排序")

    def _open_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "打开图片目录", self.settings.dataset_dir)
        if not folder:
            return
        self.settings.dataset_dir = folder
        save_settings(self.settings)
        root = Path(folder)
        self._add_rows({"path": str(p)} for p in sorted(root.rglob("*")) if p.is_file())

    def _clear_items(self) -> None:
        self.canvas.stash()
        self.items.clear(); self._rows.clear(); self._plan.clear()
        self.source_label.setText("列表已清空；磁盘文件未删除")

    def _current_changed(self, current, previous) -> None:
        if current is None:
            return
        if (previous is not None and self.tabs.currentIndex() == 1
                and self.auto_export.isChecked()):
            previous_path = Path(previous.data(Qt.ItemDataRole.UserRole))
            if str(previous_path) in self.canvas._drafts:
                self._auto_export_mosaic(previous_path)
        path = Path(current.data(Qt.ItemDataRole.UserRole))
        try:
            self.canvas.load_path(path)
            if self.tabs.currentIndex() == 2:
                self.canvas.set_ratio(self._ratio())
            self.info.setText(f"{path}  ·  {self.canvas.work.size if self.canvas.work else '不支持像素编辑'}")
        except Exception as exc:  # noqa: BLE001
            self.info.setText(f"读取失败：{exc}")

    def _tab_changed(self, index: int) -> None:
        if not hasattr(self, "canvas"):
            return
        self.canvas.tool = "crop" if index == 2 else "mosaic" if index == 1 else "view"
        if index == 2:
            self.canvas.set_ratio(self._ratio())
        self.canvas.update()

    def _sync_canvas(self) -> None:
        if not hasattr(self, "canvas"):
            return
        self.canvas.shape = self.shape.currentData()
        self.canvas.effect = self.effect.currentData()
        self.canvas.brush_size = self.brush.value()
        self.canvas.strength = self.strength.value()

    def _ratio(self) -> tuple[int, int]:
        return tuple(self.crop_ratio.currentData())

    def _ratio_changed(self) -> None:
        if hasattr(self, "canvas"):
            self.canvas.set_ratio(self._ratio())

    def _choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择导出目录", self.output.text() or str(self.settings.export_root()))
        if folder:
            self.output.setText(folder)
            self.settings.preprocess_export_dir = folder
            save_settings(self.settings)

    def _output(self) -> Path:
        value = self.output.text().strip()
        if not value:
            self._choose_output()
            value = self.output.text().strip()
        if not value:
            raise ValueError("请先选择导出目录")
        out = paths.norm_path(value)
        self.settings.preprocess_export_dir = str(out)
        save_settings(self.settings)
        return out

    def _sort(self, key: str) -> None:
        items = [self.items.takeItem(0) for _ in range(self.items.count())]
        if key == "name":
            items.sort(key=lambda i: Path(i.data(Qt.ItemDataRole.UserRole)).name.casefold())
        elif key == "time":
            items.sort(key=lambda i: Path(i.data(Qt.ItemDataRole.UserRole)).stat().st_mtime, reverse=True)
        elif key == "random":
            random.shuffle(items)
        else:
            items.reverse()
        for item in items:
            self.items.addItem(item)

    def _step_image(self, offset: int) -> None:
        row = self.items.currentRow()
        if row >= 0:
            self.items.setCurrentRow(max(0, min(self.items.count()-1, row+offset)))

    def _move_relative(self, offset: int) -> None:
        selected = self.items.selectedItems()
        if not selected:
            return
        rows = sorted(self.items.row(item) for item in selected)
        picked = [self.items.item(row) for row in rows]
        before = rows[0]
        for item in picked:
            self.items.takeItem(self.items.row(item))
        target = 0 if offset < -1000 else self.items.count() if offset > 1000 else before + offset
        target = max(0, min(self.items.count(), target))
        for index, item in enumerate(picked):
            self.items.insertItem(target + index, item)
            item.setSelected(True)
        self.items.setCurrentItem(picked[0])

    def _reorder_selected(self) -> None:
        selected = sorted(self.items.selectedItems(), key=lambda item: self.items.row(item))
        if not selected:
            return
        example = ",".join(str(i+1) for i in range(len(selected)))
        text, ok = QInputDialog.getText(self, "按序号重排", f"输入选中项的新顺序，例如 {example}：")
        if not ok:
            return
        try:
            order = [int(part.strip())-1 for part in text.split(",")]
            if sorted(order) != list(range(len(selected))):
                raise ValueError("请把每个序号恰好写一次")
            first = self.items.row(selected[0])
            for item in selected:
                self.items.takeItem(self.items.row(item))
            for index, selected_index in enumerate(order):
                self.items.insertItem(first+index, selected[selected_index])
        except ValueError as exc:
            QMessageBox.warning(self, "序号无效", str(exc))

    def _toggle_star(self) -> None:
        for item in self.items.selectedItems():
            marked = not bool(item.data(Qt.ItemDataRole.UserRole + 1))
            item.setData(Qt.ItemDataRole.UserRole + 1, marked)
            item.setText(("★ " if marked else "") + Path(item.data(Qt.ItemDataRole.UserRole)).name)

    def _move_selected(self) -> None:
        selected = self.items.selectedItems()
        if not selected:
            return
        position, ok = QInputDialog.getInt(self, "批量移动", "插入到第几张之前？", 1, 1, self.items.count()+1)
        if not ok:
            return
        ordered = sorted(selected, key=lambda item: self.items.row(item))
        for item in ordered:
            self.items.takeItem(self.items.row(item))
        for offset, item in enumerate(ordered):
            self.items.insertItem(min(self.items.count(), position-1+offset), item)

    def _rename_options(self) -> preprocess.RenameOptions:
        return preprocess.RenameOptions(
            prefix=self.prefix.text(), suffix=self.suffix.text(), separator=self.separator.text(),
            start=self.start_no.value(), digits=self.digits.value(), step=self.step.value(),
            keep_original=self.keep_name.isChecked(), number_last=self.number_last.isChecked(),
            case=self.case.currentData(), group=self.group.currentData(),
            date_source=self.date_source.currentData(), date_format=self.date_format.currentData(),
            auto_suffix=self.auto_suffix.isChecked(),
        )

    def _preview_rename(self) -> bool:
        try:
            mode = self.rename_mode.currentData()
            output = self._output() if mode != "rename" else None
            stars = {str(p) for i, p in enumerate(self._paths())
                     if self.items.item(i).data(Qt.ItemDataRole.UserRole + 1)}
            ids = {key: int(row["id"]) for key, row in self._rows.items() if row.get("id") is not None}
            self._plan = preprocess.build_rename_plan(self._paths(), self._rename_options(),
                stars=stars, mode=mode, output=output, split=self.split.currentData(),
                split_size=self.split_size.value(), image_ids=ids)
            errors = preprocess.validate_rename_plan(self._plan, mode)
            lines = [f"共 {len(self._plan)} 张；模式：{self.rename_mode.currentText()}"]
            lines.extend(f"{e.source}  →  {e.target}" for e in self._plan)
            if errors:
                lines += ["", "⚠ 冲突：", *errors]
            self.preview.setPlainText("\n".join(lines))
            return not errors and bool(self._plan)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "预览失败", str(exc))
            return False

    def _start(self, operation, on_success) -> None:
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "任务正在运行", "请等待当前任务结束")
            return
        worker = TaskWorker(operation, self)
        self._worker = worker
        self.progress.setValue(0); self.progress.show(); self.cancel_btn.show()
        worker.progress.connect(lambda done, total, name: self._progress(done, total, name))
        worker.succeeded.connect(on_success)
        worker.failed.connect(lambda message: QMessageBox.warning(self, "处理失败", message))
        worker.finished.connect(lambda: (self.progress.hide(), self.cancel_btn.hide()))
        worker.start()

    def _progress(self, done: int, total: int, name: str) -> None:
        self.progress.setValue(round(done * 100 / max(1, total)))
        self.info.setText(f"{done}/{total}  {name}")

    def _stop(self) -> None:
        if self._worker:
            self._worker.stopping = True

    def shutdown(self) -> None:
        """Do not destroy a worker or its draft files while a write is in flight."""
        if self._worker and self._worker.isRunning():
            self._worker.stopping = True
            self._worker.wait()

    def _run_rename(self) -> None:
        if not self._preview_rename():
            return
        mode = self.rename_mode.currentData()
        text = f"确认对 {len(self._plan)} 张图片执行“{self.rename_mode.currentText()}”？\n同名 txt 会一起处理。"
        if QMessageBox.question(self, "确认文件操作", text) != QMessageBox.StandardButton.Yes:
            return
        plan = list(self._plan)
        self._start(lambda progress, stop: preprocess.execute_rename(self.db, plan, mode, progress, stop),
                    lambda op_id: self._rename_done(op_id, mode, plan))

    def _rename_done(self, op_id, mode, plan) -> None:
        if mode != "copy":
            self._clear_items()
            self._add_rows({"path": str(e.target), "id": e.image_id} for e in plan)
            self.ctx.datasets_changed.emit(); self.ctx.images_changed.emit()
        self.ctx.notify(f"已处理 {len(plan)} 张图片")
        self._refresh_history()

    def _refresh_history(self) -> None:
        self.history.clear()
        for op in self.db.recent_preprocess_ops():
            state = "已撤销" if op["undone_at"] else "可撤销" if op["kind"] == "rename" else "已完成"
            item = QListWidgetItem(f"#{op['id']} {op['kind']} · {state}")
            item.setData(Qt.ItemDataRole.UserRole, op["id"])
            self.history.addItem(item)

    def _undo_operation(self) -> None:
        selected = self.history.currentItem()
        if not selected:
            return
        op_id = int(selected.data(Qt.ItemDataRole.UserRole))
        if QMessageBox.question(self, "撤销重命名", f"撤销记录 #{op_id}？\n只有目标文件未变化时才会执行。") != QMessageBox.StandardButton.Yes:
            return
        try:
            preprocess.undo_rename(self.db, op_id)
            self._refresh_history(); self.ctx.datasets_changed.emit(); self.ctx.images_changed.emit()
            self.ctx.notify("已撤销重命名")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法安全撤销", str(exc))

    def _update_draft_count(self) -> None:
        self.info.setText(f"已修改 {len(self.canvas._drafts)} 张；切图会保存草稿")

    def _auto_export_mosaic(self, source: Path) -> None:
        self.canvas.stash()
        if self._worker and self._worker.isRunning():
            self.ctx.notify("已有任务运行；当前图草稿仍保留，可稍后批量导出")
            return
        try:
            output = paths.norm_path(self.output.text().strip()) if self.output.text().strip() else self.settings.export_root()
            target = output / f"{source.stem}_masked.png"
            draft = self.canvas.draft_for(source)
        except Exception as exc:  # noqa: BLE001
            self.ctx.notify(f"自动导出未开始，草稿已保留：{exc}")
            return
        def operation(progress, stop):
            if stop():
                return [], ["操作已取消，草稿仍保留"]
            with Image.open(draft) as image:
                saved = preprocess.save_processed(source, image, target)
            progress(1, 1, source.name)
            return [str(saved)], []
        self._start(operation, lambda result: self._processed_done("切图自动导出", result, False))

    def _restore_canvas(self) -> None:
        if self.canvas.original is None or self.canvas.path is None:
            return
        self.canvas._snapshot()
        self.canvas.work = self.canvas.original.copy()
        self.canvas._refresh_preview()

    def _save_mosaic(self, batch: bool = False) -> None:
        sources = self.canvas.modified_paths if batch else ([self._current_path()] if self._current_path() else [])
        sources = [p for p in sources if p and str(p) in self.canvas._drafts]
        if not sources:
            QMessageBox.information(self, "没有修改", "先在图片上涂抹，再保存")
            return
        overwrite = self.overwrite.isChecked()
        if overwrite:
            if QMessageBox.question(self, "覆盖原图", f"将覆盖 {len(sources)} 张原图。\n每张会先备份，且新文件会清除内嵌生成信息。继续？") != QMessageBox.StandardButton.Yes:
                return
        try:
            output = None if overwrite else self._output()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "导出目录", str(exc)); return
        drafts = [(src, self.canvas.draft_for(src)) for src in sources]
        backup_root = paths.data_dir() / "preprocess-backups"
        def operation(progress, stop):
            done, errors = [], []
            for index, (src, draft) in enumerate(drafts, 1):
                if stop(): break
                try:
                    with Image.open(draft) as img:
                        target = src if overwrite else output / f"{src.stem}_masked.png"
                        saved = preprocess.save_processed(src, img, target, overwrite=overwrite,
                                                             backup_root=backup_root if overwrite else None)
                    if overwrite:
                        self.db.refresh_overwritten_image(str(src))
                    done.append(str(saved))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{src.name}: {exc}")
                progress(index, len(drafts), src.name)
            return done, errors
        self._start(operation, lambda result: self._processed_done("马赛克", result, overwrite))

    def _ratio_box(self) -> tuple[int, int, int, int] | None:
        if self.canvas.work is None:
            return None
        return self.canvas.crop_box or preprocess.centered_crop_box(*self.canvas.work.size, *self.canvas.ratio)

    def _save_crop(self) -> None:
        source, box = self._current_path(), self._ratio_box()
        if not source or not box:
            return
        try:
            output = self._output()
            target = output / f"{source.stem}_crop.png"
            preprocess.crop_image(source, target, box)
            self.db.record_preprocess_op("crop", {"files": [str(target)]})
            self._processed_done("裁剪", ([str(target)], []), False)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "裁剪失败", str(exc))

    def _preview_batch_crop(self) -> None:
        paths_ = self._paths()
        ratio = self.canvas.ratio
        self.preview.setPlainText(f"批量居中裁剪预览：{len(paths_)} 张，比例 {ratio[0]}:{ratio[1]}\n"
            + "\n".join(f"{p.name} → {p.stem}_crop.png" for p in paths_))
        if paths_:
            self.canvas.set_ratio(ratio)

    def _run_batch_crop(self) -> None:
        sources = [p for p in self._paths() if p.suffix.lower() in preprocess.EDIT_EXTENSIONS]
        if not sources:
            return
        self._preview_batch_crop()
        if QMessageBox.question(self, "确认批量裁剪", f"按统一比例居中裁剪 {len(sources)} 张？\n原图不变，输出新 PNG。") != QMessageBox.StandardButton.Yes:
            return
        try: output = self._output()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "导出目录", str(exc)); return
        ratio = self.canvas.ratio
        self._start(lambda progress, stop: preprocess.batch_crop(sources, output, ratio, progress, stop),
                    lambda result: self._processed_done("批量裁剪", result, False))

    def _processed_done(self, kind: str, result, overwrite: bool) -> None:
        done, errors = result
        if done:
            self.db.record_preprocess_op(kind, {"files": [str(p) for p in done], "overwritten": overwrite})
        self._refresh_history()
        self.preview.setPlainText(f"{kind}完成：{len(done)} 成功，{len(errors)} 失败\n"
                                  + ("原图备份：\n" if overwrite else "输出文件：\n")
                                  + "\n".join(str(p) for p in done[:50])
                                  + ("\n\n失败：\n" + "\n".join(errors[:50]) if errors else ""))
        self.ctx.notify(f"{kind}完成：{len(done)} 张")
        if overwrite:
            self.ctx.images_changed.emit()
