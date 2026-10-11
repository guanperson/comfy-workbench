"""图库：虚拟化网格 + 过滤器。

用 QListView + 自定义 delegate，而不是逐张建 widget —— 几千张图也不卡。
缩略图交给 ThumbPool 异步生成，paint 时先画占位。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QPushButton,
    QSlider,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .base import BasePage
from ... import captions, comfy_meta, paths, trash
from .. import theme
from ..widgets import muted

ROW_ROLE = int(Qt.ItemDataRole.UserRole) + 1

PAGE_SIZE = 3000


class _ZoomImageView(QGraphicsView):
    """支持鼠标位置缩放和左键拖动的大图画布。"""

    zoom_changed = Signal(int)

    def __init__(self, pixmap: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._scene.addItem(self._item)
        self._pixmap = pixmap
        self._fit_mode = True
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QColor(theme.BG))
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if pixmap.isNull():
            text = self._scene.addText("图片无法读取或文件已移动")
            text.setDefaultTextColor(QColor(theme.TEXT_DIM))
        else:
            self._item.setPixmap(pixmap)
            self._scene.setSceneRect(self._item.boundingRect())

    def fit_image(self) -> None:
        if self._pixmap.isNull():
            return
        self.resetTransform()
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
        self._fit_mode = True
        self._emit_zoom()

    def actual_size(self) -> None:
        if self._pixmap.isNull():
            return
        self.resetTransform()
        self.centerOn(self._item)
        self._fit_mode = False
        self._emit_zoom()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._pixmap.isNull() or not event.angleDelta().y():
            return super().wheelEvent(event)
        factor = 1.18 if event.angleDelta().y() > 0 else (1 / 1.18)
        target = self.transform().m11() * factor
        if 0.04 <= target <= 24.0:
            self.scale(factor, factor)
            self._fit_mode = False
            self._emit_zoom()
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit_mode:
            self.fit_image()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _emit_zoom(self) -> None:
        self.zoom_changed.emit(int(round(self.transform().m11() * 100)))


class _ImagePreviewDialog(QDialog):
    """大图预览：滚轮缩放、按住左键拖动，也能一键适应或回到 1:1。"""

    def __init__(self, row: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(str(row.get("filename") or "图片预览"))
        self.setModal(True)
        self.resize(1100, 800)
        self._pixmap = QPixmap(str(row.get("path") or ""))

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        self.view = _ZoomImageView(self._pixmap)
        root.addWidget(self.view, 1)

        footer = QHBoxLayout()
        size_text = (
            f"{self._pixmap.width()} × {self._pixmap.height()}"
            if not self._pixmap.isNull() else "图片无法读取"
        )
        info = muted(f"{row.get('filename', '')}   ·   {size_text}")
        footer.addWidget(info)
        footer.addWidget(muted("滚轮缩放 · 按住左键拖动"))
        footer.addStretch(1)
        self.zoom_label = muted("100%")
        self.zoom_label.setMinimumWidth(48)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.zoom_changed.connect(
            lambda value: self.zoom_label.setText(f"{value}%")
        )
        footer.addWidget(self.zoom_label)
        fit_btn = QPushButton("适应窗口")
        fit_btn.setObjectName("Primary")
        fit_btn.clicked.connect(self.view.fit_image)
        footer.addWidget(fit_btn)
        actual_btn = QPushButton("1:1 原始尺寸")
        actual_btn.clicked.connect(self.view.actual_size)
        footer.addWidget(actual_btn)
        reveal_btn = QPushButton("打开所在文件夹")
        reveal_btn.setObjectName("Segment")
        reveal_btn.clicked.connect(
            lambda: paths.open_in_explorer(Path(str(row.get("path") or "")).parent)
        )
        footer.addWidget(reveal_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        root.addLayout(footer)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._pixmap.isNull():
            self.view.fit_image()

    # 兼容已有离屏检查调用的内部方法名。
    def _fit(self) -> None:
        self.view.fit_image()

    def _actual(self) -> None:
        self.view.actual_size()


class ImageListModel(QAbstractListModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return str(row.get("filename", ""))
        if role == ROW_ROLE:
            return row
        if role == Qt.ItemDataRole.ToolTipRole:
            prompt = str(row.get("prompt") or "").strip()
            head = str(row.get("filename", ""))
            return f"{head}\n\n{prompt[:600]}" if prompt else head
        return None

    def row_at(self, index: QModelIndex) -> dict | None:
        if index.isValid() and index.row() < len(self.rows):
            return self.rows[index.row()]
        return None

    def index_of_id(self, image_id: int) -> int:
        for i, row in enumerate(self.rows):
            if int(row.get("id") or 0) == image_id:
                return i
        return -1

    def update_row(self, image_id: int, row: dict) -> None:
        idx = self.index_of_id(image_id)
        if idx < 0:
            return
        self.rows[idx].update(row)
        model_index = self.index(idx, 0)
        self.dataChanged.emit(model_index, model_index)


class ImageCardDelegate(QStyledItemDelegate):
    """卡片：缩略图 + 文件名 + 标签数 + 状态点 + 标签胶囊。"""

    def __init__(self, pool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pool = pool
        self.cell = QSize(190, 210)
        self.padding = 8
        self.caption_h = 26
        # 标签胶囊显示开关（由图库页面按设置同步）
        self.show_tags = True
        self.show_meta = True
        self.show_caption = False

    def set_zoom(self, width: int) -> None:
        self.cell = QSize(width, int(width * 1.06) + self.caption_h)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return self.cell

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        row = index.data(ROW_ROLE) or {}
        rect = option.rect.adjusted(4, 4, -4, -4)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 卡片底
        painter.setBrush(QColor(theme.BG_PANEL_2 if hovered else theme.BG_PANEL))
        painter.setPen(QPen(QColor(theme.ACCENT if selected else theme.BORDER_SOFT), 2 if selected else 1))
        painter.drawRoundedRect(rect, 9, 9)

        # 图片区域
        image_rect = QRect(
            rect.x() + self.padding,
            rect.y() + self.padding,
            rect.width() - self.padding * 2,
            rect.height() - self.padding * 2 - self.caption_h,
        )
        path = str(row.get("path", ""))
        cached: QImage | None = self.pool.cached(path)
        if cached is None:
            self.pool.request(path, float(row.get("mtime") or 0))

        if cached is not None and not cached.isNull():
            scaled = cached.scaled(
                image_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = image_rect.x() + (image_rect.width() - scaled.width()) // 2
            y = image_rect.y() + (image_rect.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
        else:
            painter.setBrush(QColor(theme.BG))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(image_rect, 6, 6)
            painter.setPen(QColor(theme.TEXT_FAINT))
            font = QFont(painter.font())
            font.setPointSize(9)
            painter.setFont(font)
            painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, "载入中")

        # 标签数角标
        tag_count = int(row.get("tag_count") or 0)
        badge_text = f"{tag_count}"
        painter.setFont(QFont(painter.font().family(), 8))
        metrics = painter.fontMetrics()
        badge_w = max(20, metrics.horizontalAdvance(badge_text) + 12)
        badge_rect = QRect(
            image_rect.right() - badge_w + 1, image_rect.y() + 1, badge_w, 17
        )
        painter.setBrush(QColor(theme.GREEN if tag_count else theme.ORANGE))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(badge_rect, 8, 8)
        painter.setPen(QColor("#0b0f14"))
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)

        # 文件名
        caption_rect = QRect(
            rect.x() + self.padding,
            image_rect.bottom() + 5,
            rect.width() - self.padding * 2,
            self.caption_h - 5,
        )
        painter.setPen(QColor(theme.TEXT if selected else theme.TEXT_DIM))
        font = QFont(painter.font().family(), 8)
        painter.setFont(font)
        name = str(row.get("filename", ""))
        painter.drawText(
            caption_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, caption_rect.width()),
        )

        # 标签胶囊：元数据（和/或配套 txt）
        if self.show_tags:
            self._paint_tag_pills(painter, image_rect, rect, row)

        painter.restore()

    # ------------------------------------------------------------------ #
    def _paint_tag_pills(
        self, painter: QPainter, image_rect: QRect, card_rect: QRect, row: dict
    ) -> None:
        """在图片下沿叠一层标签胶囊，超出部分裁掉。

        胶囊 = 圆角矩形：外框一个颜色，内壁另一个颜色，和打标台一致。
        """
        tags: list[str] = []
        if self.show_meta:
            tags.extend(row.get("meta_tags") or [])
        if self.show_caption:
            tags.extend(row.get("caption_tags") or [])
        if not tags:
            return

        # 去重保序
        seen: set[str] = set()
        unique: list[str] = []
        for tag in tags:
            key = str(tag).strip().lower()
            if key and key not in seen:
                seen.add(key)
                unique.append(str(tag).strip())
        if not unique:
            return

        font = QFont(painter.font().family(), 7)
        painter.setFont(font)
        metrics = painter.fontMetrics()

        pad_x, pad_y, gap = 6, 2, 3
        line_h = metrics.height() + pad_y * 2 + 1

        # 三条：够显示常见标签密度，再多就省略
        max_lines = 3
        area = QRect(
            image_rect.x() + 2,
            image_rect.bottom() - line_h * max_lines - 4,
            image_rect.width() - 4,
            line_h * max_lines,
        )
        if area.height() <= 0 or area.width() <= 0:
            return

        painter.save()
        painter.setClipRect(area)
        # 底部渐隐，让胶囊压在图上有层次
        gradient = QLinearGradient(
            float(area.left()), float(area.top()), float(area.left()), float(area.bottom())
        )
        gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
        gradient.setColorAt(0.35, QColor(11, 15, 20, 170))
        gradient.setColorAt(1.0, QColor(11, 15, 20, 225))
        painter.fillRect(area, QBrush(gradient))

        x = area.x() + 2
        y = area.y() + 2
        shown = 0
        total = len(unique)

        for tag in unique:
            text = metrics.elidedText(tag, Qt.TextElideMode.ElideRight, int(area.width() * 0.8))
            pill_w = metrics.horizontalAdvance(text) + pad_x * 2
            if x + pill_w > area.right() - 2:
                x = area.x() + 2
                y += line_h + gap
            if y + line_h > area.bottom():
                remaining = total - shown
                if remaining > 0:
                    more = f"+{remaining}"
                    more_w = metrics.horizontalAdvance(more) + pad_x * 2
                    if x + more_w <= area.right() - 2:
                        pill = QRect(x, y, more_w, line_h)
                        painter.setPen(QPen(QColor(theme.PILL_BORDER), 1))
                        painter.setBrush(QColor(20, 26, 34, 220))
                        painter.drawRoundedRect(pill, 7, 7)
                        painter.setPen(QColor(theme.TEXT_DIM))
                        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, more)
                break

            pill = QRect(x, y, pill_w, line_h)
            painter.setPen(QPen(QColor(theme.PILL_BORDER), 1))
            painter.setBrush(QColor(theme.BG_PANEL_2))
            painter.drawRoundedRect(pill, 7, 7)
            painter.setPen(QColor(theme.TEXT))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
            x += pill_w + gap
            shown += 1

        painter.restore()


class GalleryPage(BasePage):
    title = "图库"
    subtitle = "浏览、筛选、批量处理出图"

    # 通知主窗口刷新右侧检查器
    request_inspector = Signal(object)
    selection_count_changed = Signal(int)

    def build(self) -> None:
        # ---- 工具栏 ----
        bar = QFrame()
        bar.setObjectName("Panel")
        bar_col = QVBoxLayout(bar)
        bar_col.setContentsMargins(12, 10, 12, 10)
        bar_col.setSpacing(8)
        primary_row = QHBoxLayout()
        primary_row.setSpacing(9)

        primary_row.addWidget(muted("数据集"))
        self.dataset_combo = QComboBox()
        self.dataset_combo.setMinimumWidth(170)
        self.dataset_combo.currentIndexChanged.connect(lambda _: self.reload())
        primary_row.addWidget(self.dataset_combo)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索文件名、提示词或标签…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_changed)
        primary_row.addWidget(self.search_edit, 1)

        primary_row.addWidget(muted("排序"))
        self.order_combo = QComboBox()
        for key, text in (
            ("mtime_desc", "最新优先"),
            ("mtime_asc", "最早优先"),
            ("name", "按文件名"),
            ("tags_desc", "标签最多"),
            ("tags_asc", "标签最少"),
        ):
            self.order_combo.addItem(text, key)
        self.order_combo.currentIndexChanged.connect(lambda _: self.reload())
        primary_row.addWidget(self.order_combo)
        bar_col.addLayout(primary_row)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_row.addWidget(muted("状态"))

        self.status_group: list[QPushButton] = []
        for key, text in (("", "全部"), ("tagged", "已打标"), ("pending", "待打标"), ("skip", "已跳过")):
            btn = QPushButton(text)
            btn.setObjectName("Segment")
            btn.setCheckable(True)
            btn.setProperty("status_key", key)
            btn.clicked.connect(self._on_status_click)
            filter_row.addWidget(btn)
            self.status_group.append(btn)
        self.status_group[0].setChecked(True)
        filter_row.addStretch(1)

        filter_row.addWidget(muted("缩略图"))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(110, 320)
        self.zoom.setValue(int(self.settings.gallery_zoom or 190))
        self.zoom.setFixedWidth(96)
        self.zoom.valueChanged.connect(self._on_zoom)
        self.zoom.setToolTip("调整图库缩略图大小")
        filter_row.addWidget(self.zoom)
        bar_col.addLayout(filter_row)

        self.root.addWidget(bar)

        # ---- 网格 ----
        self.model = ImageListModel(self)
        self.delegate = ImageCardDelegate(self.ctx.thumb_pool, self)

        self.view = QListView()
        self.view.setModel(self.model)
        self.view.setItemDelegate(self.delegate)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setFlow(QListView.Flow.LeftToRight)
        self.view.setWrapping(True)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(2)
        self.view.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.view.setMouseTracking(True)
        self.view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.delegate.set_zoom(int(self.settings.gallery_zoom or 190))
        self.view.setGridSize(self.delegate.cell)
        self.view.selectionModel().currentChanged.connect(self._on_current_changed)
        self.view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.view.doubleClicked.connect(self._on_double_clicked)

        wrap = QFrame()
        wrap.setObjectName("Panel")
        wrap_col = QVBoxLayout(wrap)
        wrap_col.setContentsMargins(6, 6, 6, 6)
        wrap_col.addWidget(self.view)
        self.root.addWidget(wrap, 1)

        # ---- 底栏 ----
        footer = QHBoxLayout()
        self.count_label = muted("0 张")
        footer.addWidget(self.count_label)
        preview_hint = muted("双击图片查看大图")
        footer.addWidget(preview_hint)
        footer.addStretch(1)

        self.delete_btn = QPushButton("删除选中图片")
        self.delete_btn.setObjectName("GhostDanger")
        self.delete_btn.setEnabled(False)
        self.delete_btn.setToolTip("图片和同名 txt 会一起移入安全垃圾桶，可在设置页还原")
        self.delete_btn.clicked.connect(self._trash_selected)
        footer.addWidget(self.delete_btn)

        self.preprocess_btn = QPushButton("预处理选中项")
        self.preprocess_btn.setEnabled(False)
        self.preprocess_btn.clicked.connect(self._preprocess_selected)
        footer.addWidget(self.preprocess_btn)

        self.pick_btn = QPushButton("打标选中项")
        self.pick_btn.setObjectName("Primary")
        self.pick_btn.setEnabled(False)
        self.pick_btn.clicked.connect(self._tag_selected)
        footer.addWidget(self.pick_btn)

        to_workbench = QPushButton("进打标工作台  →")
        to_workbench.clicked.connect(lambda: self.ctx.go("workbench"))
        footer.addWidget(to_workbench)
        self.root.addLayout(footer)

        self.ctx.thumb_pool.loaded.connect(self._on_thumb_loaded)
        self.ctx.datasets_changed.connect(self._reload_datasets)
        self.ctx.images_changed.connect(self._on_images_changed)

        self._search_timer = None
        self._reload_datasets()

    # ------------------------------------------------------------------ #
    def _attach_tags(self, rows: list[dict]) -> None:
        """给每行补上要在卡片上画的标签。

        元数据标签来自 PNG（ComfyUI / NAI），这是「出图时用的词」，
        默认显示。配套 txt 是「训练要用的标注」，按需打开。
        """
        want_meta = self.delegate.show_meta
        want_caption = self.delegate.show_caption
        if not (want_meta or want_caption):
            return

        for row in rows:
            path = str(row.get("path") or "")
            if want_meta:
                cached = row.get("meta_tags")
                if cached is None:
                    cached = comfy_meta.quick_tags(path)
                    row["meta_tags"] = cached
            if want_caption:
                row["caption_tags"] = captions.read_caption(path)

    def _reload_datasets(self) -> None:
        current = self.dataset_combo.currentData()
        self.dataset_combo.blockSignals(True)
        self.dataset_combo.clear()
        self.dataset_combo.addItem("全部数据集", None)
        for ds in self.db.list_datasets():
            self.dataset_combo.addItem(
                f"{ds['name']}（{ds['total']}）", int(ds["id"])
            )
        if current is not None:
            idx = self.dataset_combo.findData(current)
            if idx >= 0:
                self.dataset_combo.setCurrentIndex(idx)
        self.dataset_combo.blockSignals(False)
        self.reload()

    def _on_images_changed(self) -> None:
        if self.isVisible():
            self.reload()

    def reload(self) -> None:
        if not self._loaded:
            return
        rows = self.db.list_images(
            dataset_id=self.dataset_combo.currentData(),
            search=self.search_edit.text().strip(),
            status=self._current_status(),
            order=self.order_combo.currentData() or "mtime_desc",
            limit=PAGE_SIZE,
        )
        self._attach_tags(rows)
        self.model.set_rows(rows)
        self._on_selection_changed()
        shown = len(rows)
        total_in_filter = self.db.count_images(
            dataset_id=self.dataset_combo.currentData()
        )
        self.count_label.setText(
            f"显示 {shown:,} 张 / 该范围共 {total_in_filter:,} 张"
            if shown != total_in_filter
            else f"{shown:,} 张"
        )

    def _current_status(self) -> str:
        for btn in self.status_group:
            if btn.isChecked():
                return str(btn.property("status_key") or "")
        return ""

    def _on_status_click(self) -> None:
        sender = self.sender()
        for btn in self.status_group:
            btn.setChecked(btn is sender)
        self.reload()

    def _on_search_changed(self, _text: str) -> None:
        # 简单防抖：用 QTimer 更正规，这里直接重置模型代价也不大
        from PySide6.QtCore import QTimer  # noqa: PLC0415

        if getattr(self, "_search_timer", None) is None:
            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.setInterval(260)
            self._search_timer.timeout.connect(self.reload)
        self._search_timer.start()

    def _on_zoom(self, value: int) -> None:
        self.settings.gallery_zoom = int(value)
        self.delegate.set_zoom(int(value))
        self.view.setGridSize(self.delegate.cell)
        self.view.reset()

    def _on_thumb_loaded(self, _path: str, _image) -> None:
        self.view.viewport().update()

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        row = self.model.row_at(current)
        if row is not None:
            self.ctx.selected_image = row
            self.ctx.selection_changed.emit(row)

    def _on_selection_changed(self, *_args) -> None:
        count = len(self.selected_rows())
        self.selection_count_changed.emit(count)
        if hasattr(self, "pick_btn"):
            self.pick_btn.setEnabled(count > 0)
        if hasattr(self, "delete_btn"):
            self.delete_btn.setEnabled(count > 0)
        if hasattr(self, "preprocess_btn"):
            self.preprocess_btn.setEnabled(count > 0)

    def _on_double_clicked(self, index: QModelIndex) -> None:
        row = self.model.row_at(index)
        if row is None:
            return
        self.ctx.selected_image = row
        self.ctx.selection_changed.emit(row)
        _ImagePreviewDialog(row, self).exec()

    # ------------------------------------------------------------------ #
    # 给主窗口调用的公开方法
    # ------------------------------------------------------------------ #
    def set_filter(self, text: str) -> None:
        """顶栏搜索框同步过来（不触发二次防抖回环）。"""
        if self.search_edit.text() == text:
            return
        self.search_edit.setText(text)
        self.reload()

    def open_details(self) -> None:
        """Enter 键：预览当前图片（与双击一致）。"""
        self._on_double_clicked(self.view.currentIndex())

    def selected_rows(self) -> list[dict]:
        return [
            self.model.row_at(i)
            for i in self.view.selectionModel().selectedIndexes()
            if self.model.row_at(i) is not None
        ]

    def select_all(self) -> None:
        self.view.selectAll()

    def clear_selection(self) -> None:
        self.view.clearSelection()

    def select_image(self, image_id: int) -> None:
        idx = self.model.index_of_id(image_id)
        if idx < 0:
            return
        model_index = self.model.index(idx, 0)
        self.view.setCurrentIndex(model_index)
        self.view.scrollTo(model_index, QListView.ScrollHint.PositionAtCenter)

    def _tag_selected(self) -> None:
        rows = self.selected_rows()
        if not rows:
            self.ctx.notify("没有选中任何图片")
            return
        self.ctx.go("workbench", {"images": rows})

    def _preprocess_selected(self) -> None:
        rows = self.selected_rows()
        if rows:
            self.ctx.go("preprocess", {"images": rows})

    def _trash_selected(self) -> None:
        """从图库直接把选中图片及同名 txt 移进安全垃圾桶。"""
        rows = self.selected_rows()
        if not rows:
            self.ctx.notify("没有选中任何图片")
            return

        n_txt = sum(
            1 for row in rows
            if Path(str(row.get("path") or "")).with_suffix(".txt").exists()
        )
        try:
            target = trash.trash_root()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "垃圾桶不可用", str(exc))
            return

        box = QMessageBox(self)
        box.setWindowTitle("删除选中图片")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"把选中的 {len(rows)} 张图片移进垃圾桶？")
        detail = (
            f"配套的 {n_txt} 个 txt 标注会一起移走。\n\n"
            f"这不是永久删除，可以在设置页还原。\n位置：{target}"
        )
        if len(rows) >= 5 and len(rows) >= self.model.rowCount():
            detail += "\n\n注意：你选中了当前图库中的全部图片，请确认没有误按全选。"
        box.setInformativeText(detail)
        yes = box.addButton("移进垃圾桶", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(QMessageBox.StandardButton.NoButton)
        box.exec()
        if box.clickedButton() is not yes:
            self.ctx.notify("已取消，什么都没删")
            return

        dataset_names: dict[int, str] = {}
        items: list[tuple[str, str]] = []
        for row in rows:
            dataset_id = int(row.get("dataset_id") or 0)
            if dataset_id not in dataset_names:
                ds = self.db.get_dataset(dataset_id)
                dataset_names[dataset_id] = str(ds["name"]) if ds else "未命名"
            path = str(row.get("path") or "")
            if path:
                items.append((path, dataset_names[dataset_id]))

        moved, errors = trash.trash_images(
            items,
            note="图库删除",
            keep_structure=bool(getattr(self.settings, "trash_keep_structure", False)),
        )
        deleted = 0
        deleted_ids: set[int] = set()
        for row in rows:
            image_path = Path(str(row.get("path") or ""))
            if image_path.exists():
                continue
            image_id = int(row.get("id") or 0)
            try:
                self.db.discard_caption(image_id)
            except AttributeError:
                pass
            self.db.delete_image(image_id)
            deleted_ids.add(image_id)
            deleted += 1

        current_id = int((self.ctx.selected_image or {}).get("id") or 0)
        if current_id in deleted_ids:
            self.ctx.selected_image = None
            self.ctx.selection_changed.emit(None)
        self.ctx.images_changed.emit()
        QApplication.processEvents()

        if errors:
            QMessageBox.warning(
                self,
                "部分图片没能移走",
                f"成功移走 {deleted} 张图片，但有 {len(errors)} 个失败：\n\n"
                + "\n".join(errors[:8]),
            )
        self.ctx.notify(
            f"已把 {deleted} 张图片（共 {moved} 个文件）移进垃圾桶"
        )

    # ------------------------------------------------------------------ #
    def _sync_pill_flags(self) -> None:
        """把设置里的标签显示开关同步到卡片绘制。"""
        self.delegate.show_meta = bool(self.settings.gallery_show_meta)
        self.delegate.show_caption = bool(self.settings.gallery_show_caption)
        self.delegate.show_tags = (
            self.delegate.show_meta or self.delegate.show_caption
        )

    def refresh(self) -> None:
        self._sync_pill_flags()
        self._reload_datasets()
