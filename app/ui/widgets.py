"""可复用小部件：流式布局、统计卡、缩略图线程池、标签芯片栏。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QImageReader,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListView,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from PySide6.QtCore import QAbstractListModel, QModelIndex, QTimer

from .. import thumbs
from . import theme

# --------------------------------------------------------------------------- #
# 流式布局（标签芯片、卡片墙用）
# --------------------------------------------------------------------------- #

class FlowLayout(QLayout):
    """自动换行的水平布局，直接抄 Qt 官方示例的思路。"""

    def __init__(self, parent: QWidget | None = None, margin: int = 0, spacing: int = 6):
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self._spacing = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    # --- QLayout 接口 ---
    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    # --- 内部 ---
    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y = effective.x(), effective.y()
        line_height = 0

        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            space_x = self._spacing
            space_y = self._spacing
            next_x = x + item.sizeHint().width() + space_x
            if next_x - space_x > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x = next_x
            line_height = max(line_height, item.sizeHint().height())
        return y + line_height - rect.y() + margins.bottom()

    def clear(self) -> None:
        while self._items:
            item = self._items.pop()
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.invalidate()


# --------------------------------------------------------------------------- #
# 小工具函数
# --------------------------------------------------------------------------- #

def hline() -> QFrame:
    line = QFrame()
    line.setObjectName("HLine")
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    return line


def label(text: str, kind: str = "") -> QLabel:
    lb = QLabel(text)
    if kind:
        lb.setObjectName(kind)
    return lb


def muted(text: str) -> QLabel:
    return label(text, "Muted")


def mono(text: str) -> QLabel:
    lb = label(text, "Mono")
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lb.setWordWrap(True)
    return lb


def elide(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def icon_badge(glyph: str, color: str, size: int = 34) -> QLabel:
    """左侧那个彩色圆角方块图标。"""
    lb = QLabel(glyph)
    lb.setFixedSize(size, size)
    lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
    font = QFont()
    font.setPointSize(int(size * 0.42))
    lb.setFont(font)
    lb.setStyleSheet(
        f"background: {color}22; color: {color}; "
        f"border: 1px solid {color}55; border-radius: {int(size * 0.28)}px;"
    )
    return lb


class _ProgressBar(QWidget):
    """自制细进度条（QProgressBar 的圆角在这套配色下不好控）。"""

    def __init__(self, width: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if width:
            self.setFixedWidth(width)
        self.setFixedHeight(6)
        self._value = 0.0
        self._color = theme.ACCENT

    def set_value(self, value: float, color: str = "") -> None:
        value = max(0.0, min(1.0, float(value)))
        color = color or (theme.GREEN if value >= 0.999 else theme.ACCENT)
        if abs(value - self._value) < 0.001 and color == self._color:
            return
        self._value, self._color = value, color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BG))
        painter.drawRoundedRect(self.rect(), 3, 3)
        if self._value > 0:
            filled = QRect(0, 0, int(round(self.width() * self._value)), self.height())
            painter.setBrush(QColor(self._color))
            painter.drawRoundedRect(filled, 3, 3)


def progress_bar(value: float = 0.0, width: int | None = None) -> _ProgressBar:
    bar = _ProgressBar(width)
    bar.set_value(value)
    return bar



# --------------------------------------------------------------------------- #
# 统计卡
# --------------------------------------------------------------------------- #

class StatCard(QFrame):
    """图片总数 / 已标签 / 标签词条 / 待打标 那种卡片。"""

    clicked = Signal()

    def __init__(
        self,
        title: str,
        value: str,
        glyph: str = "",
        color: str = theme.ACCENT,
        hint: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(76)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(12)

        if glyph:
            row.addWidget(icon_badge(glyph, color, 40))

        col = QVBoxLayout()
        col.setSpacing(1)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("StatValue")
        self.title_label = QLabel(title)
        self.title_label.setObjectName("StatLabel")
        col.addWidget(self.value_label)
        col.addWidget(self.title_label)
        row.addLayout(col)
        row.addStretch(1)

        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("Faint")
        self.hint_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.hint_label)

    def set_value(self, value: str, hint: str | None = None) -> None:
        self.value_label.setText(value)
        if hint is not None:
            self.hint_label.setText(hint)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


# --------------------------------------------------------------------------- #
# 缩略图线程池
# --------------------------------------------------------------------------- #

class _ThumbTask(QRunnable):
    def __init__(self, pool: "ThumbPool", image_path: str, mtime: float, size: int) -> None:
        super().__init__()
        self.pool = pool
        self.image_path = image_path
        self.mtime = mtime
        self.size = size
        self.setAutoDelete(True)

    def run(self) -> None:  # noqa: D102
        try:
            target = thumbs.get_thumb(self.image_path, self.mtime, self.size)
            if target is None:
                self.pool._done(self.image_path, None)  # noqa: SLF001
                return
            reader = QImageReader(str(target))
            reader.setAutoTransform(True)
            image = reader.read()
            if image.isNull():
                base = QImage(str(self.image_path))
                if base.isNull():
                    self.pool._done(self.image_path, None)  # noqa: SLF001
                    return
                image = base.scaled(
                    self.size, self.size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self.pool._done(self.image_path, image)  # noqa: SLF001
        except Exception:
            self.pool._done(self.image_path, None)  # noqa: SLF001


class ThumbPool(QObject):
    """后台生成/读取缩略图，完成后在主线程发信号。

    QImage 可以跨线程传递，QPixmap 不行 —— 所以这里只发 QImage，
    由界面侧再转成 QPixmap。

    内部 ``_deliver`` 是主线程队列信号：工作线程只 emit 它，
    ``loaded`` / ``failed`` 一定在 GUI 线程里发出。
    """

    loaded = Signal(str, QImage)   # 图片路径, 缩略图
    failed = Signal(str)

    _deliver = Signal(str, QImage)

    def __init__(self, size: int = 512, max_threads: int = 4, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.size = size
        self._in_flight: set[str] = set()
        self._cache: dict[str, QImage] = {}
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, max_threads))
        self._max_cache = 900
        # 关键：把结果从工作线程搬到 GUI 线程
        self._deliver.connect(self._on_deliver, Qt.ConnectionType.QueuedConnection)

    def set_size(self, size: int) -> None:
        if size == self.size:
            return
        self.size = size
        self._cache.clear()

    def cached(self, image_path: str) -> QImage | None:
        return self._cache.get(image_path)

    def request(self, image_path: str, mtime: float = 0.0) -> QImage | None:
        """有缓存直接返回；否则排队并返回 None。"""
        cached = self._cache.get(image_path)
        if cached is not None:
            return cached
        if image_path in self._in_flight:
            return None
        self._in_flight.add(image_path)
        self._pool.start(_ThumbTask(self, image_path, mtime, self.size))
        return None

    # --- 工作线程调用 ---
    def _done(self, image_path: str, image: QImage | None) -> None:
        if image is None or image.isNull():
            self._deliver.emit(image_path, QImage())
        else:
            self._deliver.emit(image_path, image)

    # --- GUI 线程执行 ---
    def _on_deliver(self, image_path: str, image: QImage) -> None:
        self._in_flight.discard(image_path)
        if image.isNull():
            self.failed.emit(image_path)
            return
        if len(self._cache) >= self._max_cache:
            self._cache.clear()
        self._cache[image_path] = image
        self.loaded.emit(image_path, image)

    def clear_cache(self) -> None:
        self._cache.clear()


# --------------------------------------------------------------------------- #
# 缩略图小网格（打标台左侧队列、抽离视图都用它）
# --------------------------------------------------------------------------- #

class ThumbGridModel(QAbstractListModel):
    """缩略图网格的数据源：一行就是一张图。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.rows: list[dict] = []
        self.changed_ids: set[int] = set()   # 已改过的图（画 ● 用）

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return str(row.get("filename", ""))
        if role == Qt.ItemDataRole.ToolTipRole:
            path = str(row.get("path", ""))
            name = str(row.get("filename", ""))
            has_txt = bool(row.get("has_caption"))
            tail = "有配套 txt" if has_txt else "没有配套 txt"
            changed = "　·　已改动（未保存）" if int(row.get("id") or 0) in self.changed_ids else ""
            return f"{name}\n{tail}{changed}\n{path}"
        return None

    def set_rows(self, rows: list[dict], changed_ids: set[int] | None = None) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        if changed_ids is not None:
            self.changed_ids = set(changed_ids)
        self.endResetModel()
        self.refresh_marks()

    def refresh_marks(self) -> None:
        """只刷新「改过」标记，不重建整个列表。"""
        if not self.rows:
            return
        top = self.index(0, 0)
        bottom = self.index(len(self.rows) - 1, 0)
        self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.ToolTipRole])

    def index_of_id(self, image_id: int) -> int:
        for i, row in enumerate(self.rows):
            if int(row.get("id") or 0) == image_id:
                return i
        return -1

    def row_at(self, index: QModelIndex) -> dict | None:
        if index.isValid() and 0 <= index.row() < len(self.rows):
            return self.rows[index.row()]
        return None


class ThumbGridDelegate(QStyledItemDelegate):
    """小卡片：缩略图 + 文件名 + 角标（● 改动 / 无txt）。"""

    def __init__(self, pool: ThumbPool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pool = pool
        self.cell = QSize(112, 138)
        self.caption_h = 24
        self.icon = QSize(96, 96)

    def set_zoom(self, width: int) -> None:
        self.cell = QSize(width, width + self.caption_h + 16)
        self.icon = QSize(max(40, width - 16), max(40, width - 16))

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return self.cell

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        row = index.data(Qt.ItemDataRole.DisplayRole)
        model = index.model()
        data = model.row_at(index) if hasattr(model, "row_at") else {}
        rect = option.rect.adjusted(3, 3, -3, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        current = bool(option.state & QStyle.StateFlag.State_HasFocus)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 卡片底
        if selected:
            bg = theme.PILL_SEL_FILL
            border, width = theme.PILL_SEL_BORDER, 2
        elif hovered:
            bg, border, width = theme.BG_PANEL_2, theme.BORDER_SOFT, 1
        else:
            bg, border, width = theme.BG_PANEL, theme.BORDER_SOFT, 1
        painter.setBrush(QColor(bg))
        painter.setPen(QPen(QColor(border), width))
        painter.drawRoundedRect(rect, 7, 7)

        # 缩略图区
        pad = 4
        thumb_rect = QRect(
            rect.x() + pad,
            rect.y() + pad,
            rect.width() - pad * 2,
            rect.height() - pad * 2 - self.caption_h,
        )
        path = str((data or {}).get("path", ""))
        image: QImage | None = self.pool.cached(path) if path else None
        if image is None and path:
            self.pool.request(path, float((data or {}).get("mtime") or 0))

        if image is not None and not image.isNull():
            scaled = image.scaled(
                thumb_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = thumb_rect.x() + (thumb_rect.width() - scaled.width()) // 2
            y = thumb_rect.y() + (thumb_rect.height() - scaled.height()) // 2
            painter.drawImage(x, y, scaled)
        else:
            painter.setBrush(QColor(theme.BG))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(thumb_rect, 5, 5)
            painter.setPen(QColor(theme.TEXT_FAINT))
            painter.setFont(QFont(painter.font().family(), 7))
            painter.drawText(thumb_rect, Qt.AlignmentFlag.AlignCenter, "载入…")

        # 文件名
        painter.setPen(QColor(theme.TEXT if selected else theme.TEXT_DIM))
        font = QFont(painter.font().family(), 7)
        painter.setFont(font)
        text_rect = QRect(
            rect.x() + pad, thumb_rect.bottom() + 1,
            rect.width() - pad * 2, self.caption_h,
        )
        metrics = painter.fontMetrics()
        name = metrics.elidedText(
            str(row or ""), Qt.TextElideMode.ElideMiddle, text_rect.width()
        )
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, name)

        # 角标
        badge = ""
        if not bool((data or {}).get("has_caption")):
            badge = "无txt"
        changed_ids = getattr(model, "changed_ids", set())
        if int((data or {}).get("id") or 0) in changed_ids:
            badge = "● 改动" if not badge else "● " + badge
        if badge:
            painter.setFont(QFont(painter.font().family(), 6))
            bm = painter.fontMetrics()
            bw = bm.horizontalAdvance(badge) + 8
            bh = bm.height() + 2
            bx = rect.right() - bw - 5
            by = rect.y() + 5
            painter.setBrush(QColor(theme.ACCENT_DEEP))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRect(bx, by, bw, bh), 4, 4)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(QRect(bx, by, bw, bh), Qt.AlignmentFlag.AlignCenter, badge)

        painter.restore()


class ThumbGridView(QListView):
    """缩略图网格的视图：上下键当「上一张 / 下一张」用。

    Qt 默认上下键在 IconMode 里是跨行跳（一趟跳一整行），
    打标的时候更想要「一张一张翻」。
    """

    step_requested = Signal(int)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_PageUp):
            self.step_requested.emit(-1)
            event.accept()
            return
        if key in (Qt.Key.Key_Down, Qt.Key.Key_PageDown):
            self.step_requested.emit(1)
            event.accept()
            return
        if key == Qt.Key.Key_Home:
            self.step_requested.emit(-10**9)
            event.accept()
            return
        if key == Qt.Key.Key_End:
            self.step_requested.emit(10**9)
            event.accept()
            return
        super().keyPressEvent(event)


class ThumbGrid(QWidget):
    """缩略图小网格：点选 + 多选（Ctrl/Shift）+ 可调整大小。

    打标台左侧队列和「该标签全部图」抽离视图都用它，
    这样两处外观一致，用户不用重新学。
    """

    current_changed = Signal(int)      # 当前图 id
    pick_changed = Signal()            # 多选变化

    def __init__(self, pool: ThumbPool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pool = pool
        self.model = ThumbGridModel(self)
        self.delegate = ThumbGridDelegate(pool, self)
        self.view = ThumbGridView(self)
        self.view.step_requested.connect(self.step)
        self.view.setModel(self.model)
        self.view.setItemDelegate(self.delegate)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setWrapping(True)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(2)
        self.view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.view.setSelectionRectVisible(True)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        # 方向键直接翻图（上下 = 上一张/下一张）
        self.view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.view.selectionModel().currentChanged.connect(self._on_current)
        self.view.selectionModel().selectionChanged.connect(self._on_selection)
        pool.loaded.connect(self._on_thumb_loaded)

        self.zoom = 112
        self.apply_zoom(self.zoom)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)

    # --- 数据 ---
    def set_rows(
        self,
        rows: list[dict],
        changed_ids: set[int] | None = None,
        *,
        keep_view: bool = True,
    ) -> None:
        """换一批行。

        ``keep_view=True`` 时会尽量保持滚动位置和当前图 —— 改完标签重新
        筛列表时，用户不希望视野跳回最顶上。
        当前图要是被筛掉了（正好是它被改了），就选它原来位置附近的那张，
        而不是一头栽回第一张。
        """
        old_rows = self.model.rows
        anchor_id = self.current_id() if keep_view else 0
        anchor_pos = 0
        if keep_view and anchor_id:
            for i, r in enumerate(old_rows):
                if int(r.get("id") or 0) == anchor_id:
                    anchor_pos = i
                    break
        scroll = self.view.verticalScrollBar().value()

        self.model.set_rows(rows, changed_ids)

        if keep_view:
            # 先恢复滚动位置，再挑当前图（顺序反了会被 scrollTo 覆盖）
            self.view.verticalScrollBar().setValue(scroll)
            target = 0
            if anchor_id and self.model.index_of_id(anchor_id) >= 0:
                target = self.model.index_of_id(anchor_id)
            elif self.model.rowCount():
                # 原图没了 → 落在它原来的位置（夹到末尾）
                target = min(anchor_pos, self.model.rowCount() - 1)
            if self.model.rowCount():
                index = self.model.index(target, 0)
                self.view.setCurrentIndex(index)

        # 布局重排是异步的，等它做完再补一次，防止被重算冲掉
        QTimer.singleShot(0, lambda: self._restore_scroll(scroll))

    def _restore_scroll(self, value: int) -> None:
        if value:
            self.view.verticalScrollBar().setValue(value)

    def refresh_marks(self, changed_ids: set[int] | None = None) -> None:
        if changed_ids is not None:
            self.model.changed_ids = set(changed_ids)
        self.model.refresh_marks()
        self.view.viewport().update()

    def rows(self) -> list[dict]:
        return self.model.rows

    def set_zoom(self, width: int) -> None:
        self.apply_zoom(width)

    def apply_zoom(self, width: int) -> None:
        self.zoom = max(72, min(220, int(width)))
        self._relayout()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        """按可用宽度算出能排几列，把格子铺满整栏。"""
        viewport = self.view.viewport().width()
        # 纵向滚动条会吃掉宽度，先把它算进去，否则最后一列会被挤掉
        bar = self.view.verticalScrollBar()
        if bar is not None and bar.isVisible():
            viewport -= bar.sizeHint().width()
        avail = viewport
        if avail <= 0:
            avail = self.width() - 24
        if avail <= 0:
            return
        gap = 6
        cols = max(1, (avail + gap) // (self.zoom + gap))
        cell_w = max(56, (avail - gap * (cols - 1)) // cols)
        self.delegate.set_zoom(cell_w)
        self.view.setGridSize(
            QSize(cell_w + gap, cell_w + self.delegate.caption_h + gap + 4)
        )
        self.view.scheduleDelayedItemsLayout()
        self.view.viewport().update()

    # --- 选中 ---
    def current_id(self) -> int:
        index = self.view.currentIndex()
        row = self.model.row_at(index)
        return int((row or {}).get("id") or 0)

    def selected_ids(self) -> list[int]:
        ids: list[int] = []
        for index in self.view.selectionModel().selectedIndexes():
            row = self.model.row_at(index)
            if row:
                ids.append(int(row.get("id") or 0))
        return ids

    def select_all(self) -> None:
        self.view.selectAll()
        self.pick_changed.emit()

    def clear_selection(self) -> None:
        self.view.clearSelection()
        self.pick_changed.emit()

    def select_image(self, image_id: int, *, scroll: bool = True) -> bool:
        """选中某张图。``scroll=False`` 时不滚动视野（保住用户的位置）。"""
        row_index = self.model.index_of_id(image_id)
        if row_index < 0:
            return False
        index = self.model.index(row_index, 0)
        self.view.setCurrentIndex(index)
        if scroll:
            self.view.scrollTo(index, QListView.ScrollHint.PositionAtCenter)
        return True

    def step(self, delta: int) -> None:
        """上下切换当前图，跨行也顺；越界就夹到两端。"""
        total = self.model.rowCount()
        if not total:
            return
        index = self.view.currentIndex()
        if not index.isValid():
            target = 0
        else:
            target = max(0, min(total - 1, index.row() + delta))
        new_index = self.model.index(target, 0)
        self.view.setCurrentIndex(new_index)
        self.view.scrollTo(new_index, QListView.ScrollHint.EnsureVisible)

    def _on_current(self, current: QModelIndex, _prev) -> None:
        row = self.model.row_at(current)
        self.current_changed.emit(int((row or {}).get("id") or 0))

    def _on_selection(self, *_args) -> None:
        self.pick_changed.emit()

    def _on_thumb_loaded(self, image_path: str, _image) -> None:
        # 只重画，不重建布局
        if self.model.rows:
            self.view.viewport().update()


# --------------------------------------------------------------------------- #
# 标签芯片栏
# --------------------------------------------------------------------------- #

class TagChip(QPushButton):
    """可点击的标签芯片。"""

    removed = Signal(str)
    toggled_banned = Signal(str, bool)

    def __init__(
        self,
        name: str,
        count: int | None = None,
        banned: bool = False,
        removable: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        text = name if count is None else f"{name}  {count}"
        super().__init__(text, parent)
        self.name = name
        self.setObjectName("Chip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setCheckable(not removable)
        self.setChecked(False)
        if removable:
            self.setToolTip(f"{name} —— 右键移除")
        if banned:
            self.setStyleSheet(
                f"QPushButton#Chip {{ background: rgba(255,96,96,0.10);"
                f" border: 1px solid rgba(255,96,96,0.45); color: {theme.RED};"
                f" border-radius: 11px; padding: 3px 11px; font-size: 12px; }}"
                f"QPushButton#Chip:hover {{ background: rgba(255,96,96,0.22); }}"
            )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.removed.emit(self.name)
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtWidgets import QMenu  # noqa: PLC0415

        menu = QMenu(self)
        act_remove = menu.addAction("从这张图移除")
        act_ban = menu.addAction("加入屏蔽词")
        chosen = menu.exec(event.globalPos())
        if chosen is act_remove:
            self.removed.emit(self.name)
        elif chosen is act_ban:
            self.toggled_banned.emit(self.name, True)


class TagChipBar(QWidget):
    """一组标签芯片，自动换行 + 输入框添加。"""

    tags_changed = Signal(list)          # 当前全部标签
    tag_added = Signal(str)
    tag_removed = Signal(str)
    tag_banned = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QLineEdit  # noqa: PLC0415

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        self._container = QWidget()
        self._flow = FlowLayout(self._container, margin=0, spacing=6)
        self._container.setLayout(self._flow)
        outer.addWidget(self._container)

        self.add_edit = QLineEdit()
        self.add_edit.setPlaceholderText("输入标签，回车或逗号添加（支持一次粘贴多个，用逗号分隔）")
        self.add_edit.returnPressed.connect(self._commit_edit)
        self.add_edit.textChanged.connect(self._maybe_split)
        outer.addWidget(self.add_edit)

        self._sizes: list[TagChip] = []

    # ------------------------------------------------------------------ #
    def set_tags(self, tags: Iterable[dict], removable: bool = True) -> None:
        self._flow.clear()
        self._sizes = []
        for item in tags:
            chip = TagChip(
                str(item.get("name", "")),
                banned=bool(item.get("banned")),
                removable=removable,
            )
            chip.removed.connect(self.tag_removed.emit)
            chip.toggled_banned.connect(self.tag_banned.emit)
            self._flow.addWidget(chip)
            self._sizes.append(chip)
        self._container.updateGeometry()

    def current_tags(self) -> list[str]:
        return [chip.name for chip in self._sizes]

    # ------------------------------------------------------------------ #
    def _commit_edit(self) -> None:
        text = self.add_edit.text().strip()
        if not text:
            return
        names = [t.strip() for t in text.replace("，", ",").split(",") if t.strip()]
        self.add_edit.clear()
        for name in names:
            self.tag_added.emit(name)

    def _maybe_split(self, text: str) -> None:
        """粘贴一堆逗号分隔的标签时自动拆。"""
        if "," in text or "，" in text:
            normalized = text.replace("，", ",")
            if normalized.endswith(","):
                parts = [t.strip() for t in normalized.split(",") if t.strip()]
                if parts:
                    self.add_edit.blockSignals(True)
                    self.add_edit.clear()
                    self.add_edit.blockSignals(False)
                    for name in parts:
                        self.tag_added.emit(name)

    def focus_input(self) -> None:
        self.add_edit.setFocus()

    def sizeHint(self) -> QSize:  # noqa: N802
        return self._container.sizeHint()


# --------------------------------------------------------------------------- #
# 可点击的键值行（右侧信息面板用）
# --------------------------------------------------------------------------- #

class InfoRow(QWidget):
    def __init__(self, key: str, value: str, value_color: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(8)
        k = QLabel(key)
        k.setObjectName("Muted")
        k.setMinimumWidth(72)
        self.value = QLabel(value)
        self.value.setWordWrap(True)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        if value_color:
            self.value.setStyleSheet(f"color: {value_color};")
        row.addWidget(k, 0)
        row.addWidget(self.value, 1)

    def set_value(self, value: str) -> None:
        self.value.setText(value)


class ClickableLabel(QLabel):
    clicked = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class HoverFrame(QFrame):
    """带 hover 高亮的卡片容器。"""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None, object_name: str = "Card") -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event: QEvent) -> None:  # noqa: N802
        self.setStyleSheet(f"QFrame#{self.objectName()} {{ border: 1px solid {theme.BORDER}; }}")
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        self.setStyleSheet("")
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def pixmap_from_image(image: QImage) -> QPixmap:
    return QPixmap.fromImage(image)


def overlay_status_dot(color: str, size: int = 8) -> QLabel:
    dot = QLabel()
    dot.setFixedSize(size, size)
    dot.setStyleSheet(f"background: {color}; border-radius: {size // 2}px;")
    return dot


# ====================================================================== #
# 可选中的标签胶囊
# ====================================================================== #
class SelectableTagChip(QPushButton):
    """可点击的标签胶囊。

    两段配色：**外框**一个颜色，**内壁**另一个颜色。
    点一下内壁由紫变灰表示已选中，可以多选。

    这是打标台搬标签的核心：选中若干胶囊 → 右侧点复制/移动 →
    切到别的图 → 粘贴。
    """

    clicked_tag = Signal(str)          # 左键点一下（切换选中）
    remove_requested = Signal(str)     # 右键 / 菜单：从这张图移除
    ban_requested = Signal(str)        # 菜单：加入屏蔽词
    isolate_requested = Signal(str)    # 菜单 / 双击：查看所有带这个标签的图

    def __init__(
        self,
        name: str,
        *,
        count: int | None = None,
        selected: bool = False,
        readonly: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        text = name if count is None else f"{name}  {count}"
        super().__init__(text, parent)
        self.name = name
        self.readonly = readonly
        self._selected = selected
        self.setObjectName("TagPill")
        self.setCursor(
            Qt.CursorShape.ArrowCursor if readonly else Qt.CursorShape.PointingHandCursor
        )
        self.setFlat(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._apply_style()
        if not readonly:
            self.setToolTip(f"{name}\n左键选中 / 取消，右键更多操作")

    # ------------------------------------------------------------------ #
    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, value: bool) -> None:
        if self._selected == value:
            return
        self._selected = value
        self._apply_style()

    def _apply_style(self) -> None:
        """外框 + 内壁双色。选中时内壁转灰。"""
        if self._selected:
            border, fill, text = theme.PILL_SEL_BORDER, theme.PILL_SEL_FILL, theme.TEXT
        else:
            border, fill, text = theme.PILL_BORDER, theme.PILL_FILL, theme.TEXT
        hover_border = theme.ACCENT if not self._selected else theme.PILL_SEL_BORDER
        self.setStyleSheet(
            f"QPushButton#TagPill {{"
            f" background: {fill};"
            f" border: 1px solid {border};"
            f" border-radius: 10px;"
            f" padding: 3px 10px;"
            f" font-size: 12px;"
            f" color: {text};"
            f" text-align: center;"
            f"}}"
            f"QPushButton#TagPill:hover {{ border: 1px solid {hover_border}; }}"
        )

    # ------------------------------------------------------------------ #
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self.readonly:
            return super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.set_selected(not self._selected)
            self.clicked_tag.emit(self.name)
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.remove_requested.emit(self.name)
            return
        return super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """双击 = 查看所有带这个标签的图。"""
        if self.readonly:
            return super().mouseDoubleClickEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.isolate_requested.emit(self.name)
            return
        return super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        if self.readonly:
            return
        from PySide6.QtWidgets import QMenu  # noqa: PLC0415

        menu = QMenu(self)
        act_isolate = menu.addAction("查看所有该标签图片")
        menu.addSeparator()
        act_remove = menu.addAction("从这张图移除")
        act_ban = menu.addAction("加入屏蔽词")
        chosen = menu.exec(event.globalPos())
        if chosen is act_isolate:
            self.isolate_requested.emit(self.name)
        elif chosen is act_remove:
            self.remove_requested.emit(self.name)
        elif chosen is act_ban:
            self.ban_requested.emit(self.name, True)


class TagPillBox(QWidget):
    """一排可以点选的标签胶囊，自动换行。

    滚动：胶囊多了自动出现竖向滚动条（鼠标滚轮可用）。
    """

    selection_changed = Signal(list)     # 当前选中的标签名
    remove_requested = Signal(str)
    ban_requested = Signal(str)
    tag_clicked = Signal(str)
    isolate_requested = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        readonly: bool = False,
        max_height: int = 0,
        placeholder: str = "（没有标签）",
    ) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QScrollArea  # noqa: PLC0415

        self.readonly = readonly
        self._chips: list[SelectableTagChip] = []
        self._selected: list[str] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        if max_height:
            self._scroll.setMaximumHeight(max_height)
        # 滚轮直接翻，不用先点进来
        self._scroll.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._container = QWidget()
        self._container.setObjectName("PillCanvas")
        self._flow = FlowLayout(self._container, margin=2, spacing=6)
        self._container.setLayout(self._flow)
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll)

        self._empty = QLabel(placeholder)
        self._empty.setObjectName("Faint")
        self._empty.setWordWrap(True)
        outer.addWidget(self._empty)

        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._container.setStyleSheet("background: transparent;")

    # ------------------------------------------------------------------ #
    def set_tags(self, tags: Iterable[str], counts: dict[str, int] | None = None) -> None:
        """重建胶囊。会清空选中状态。"""
        self._flow.clear()
        self._chips = []
        self._selected = []

        names = [t for t in tags if str(t).strip()]
        for name in names:
            count = counts.get(name) if counts else None
            chip = SelectableTagChip(
                str(name),
                count=count,
                readonly=self.readonly,
            )
            chip.clicked_tag.connect(self._on_chip_clicked)
            chip.remove_requested.connect(self.remove_requested)
            chip.ban_requested.connect(self.ban_requested)
            chip.isolate_requested.connect(self.isolate_requested)
            self._flow.addWidget(chip)
            self._chips.append(chip)

        self._empty.setVisible(not names)
        self._scroll.setVisible(bool(names))
        self._flow.invalidate()
        self.selection_changed.emit([])

    def tags(self) -> list[str]:
        return [chip.name for chip in self._chips]

    def selected_tags(self) -> list[str]:
        return [chip.name for chip in self._chips if chip.selected]

    def clear_selection(self) -> None:
        """取消所有选中。

        不信任缓存的 ``_selected``：胶囊的选中态有可能被别处直接改
        （比如代码里 set_selected），缓存过期就会漏清。以胶囊自身状态为准。
        """
        if not any(chip.selected for chip in self._chips):
            self._selected = []
            return
        for chip in self._chips:
            chip.set_selected(False)
        self._selected = []
        self.selection_changed.emit([])

    def select_all(self) -> None:
        for chip in self._chips:
            chip.set_selected(True)
        self._selected = self.tags()
        self.selection_changed.emit(list(self._selected))

    def _on_chip_clicked(self, name: str) -> None:
        self._selected = self.selected_tags()
        self.selection_changed.emit(list(self._selected))
        self.tag_clicked.emit(name)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """滚轮翻胶囊：把事件转给内部滚动区。"""
        bar = self._scroll.verticalScrollBar()
        if bar.maximum() > 0:
            bar.setValue(bar.value() - event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)
