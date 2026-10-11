"""主页：一眼看清库存、打标进度、待办。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ... import thumbs
from .base import BasePage
from .. import theme
from ..widgets import (
    HoverFrame,
    StatCard,
    elide,
    hline,
    icon_badge,
    muted,
    progress_bar,
)


class _DatasetRow(QFrame):
    def __init__(self, data: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.data = data
        self.setObjectName("Card")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 11)
        outer.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(icon_badge("▦", theme.ACCENT, 30))

        name_box = QVBoxLayout()
        name_box.setSpacing(1)
        self.name = QLabel(data.get("name", ""))
        self.name.setObjectName("CardTitle")
        self.sub = muted(
            f"{data.get('total', 0)} 张图 · 已打标 {data.get('tagged', 0)}"
        )
        name_box.addWidget(self.name)
        name_box.addWidget(self.sub)
        top.addLayout(name_box)
        top.addStretch(1)

        self.count = QLabel(
            f"{int(round((data.get('progress') or 0) * 100))}%"
        )
        self.count.setStyleSheet(f"color: {theme.GREEN}; font-weight: 700;")
        top.addWidget(self.count, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(top)

        bar = progress_bar(data.get("progress") or 0.0)
        bar.setFixedHeight(5)
        outer.addWidget(bar)


class _RecentRow(QFrame):
    """最近改动：缩略图 + 文件名 + 标签数。"""

    open_requested = Signal(int)

    def __init__(self, data: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.data = data
        self.path = str(data.get("path", ""))
        self.setObjectName("Card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(11)

        self.thumb = QLabel()
        self.thumb.setFixedSize(44, 44)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet(
            f"background: {theme.BG}; border: 1px solid {theme.BORDER_SOFT};"
            f" border-radius: 6px; color: {theme.TEXT_FAINT}; font-size: 10px;"
        )
        self.thumb.setText("···")
        row.addWidget(self.thumb)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        self.title = QLabel(elide(str(data.get("filename", "")), 44))
        self.title.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px;")
        self.title.setToolTip(str(data.get("path", "")))
        self.sub = muted(f"{int(data.get('tag_count') or 0)} 个标签")
        text_box.addWidget(self.title)
        text_box.addWidget(self.sub)
        row.addLayout(text_box, 1)

        row.addWidget(muted(self._rel_time(data.get("tagged_at") or data.get("added_at"))))

    def set_thumb(self, image) -> None:
        if image is None or image.isNull():
            return
        self.thumb.setText("")
        self.thumb.setPixmap(
            QPixmap.fromImage(image).scaled(
                44, 44,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    @staticmethod
    def _rel_time(ts) -> str:
        if not ts:
            return "——"
        import time as _t  # noqa: PLC0415

        delta = max(0, int(_t.time() - float(ts)))
        if delta < 60:
            return "刚刚"
        if delta < 3600:
            return f"{delta // 60} 分钟前"
        if delta < 86400:
            return f"{delta // 3600} 小时前"
        return f"{delta // 86400} 天前"

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_requested.emit(int(self.data.get("id") or 0))
        super().mouseReleaseEvent(event)



class _CheckItem(QFrame):
    def __init__(self, glyph: str, title: str, desc: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 11, 12, 11)
        row.setSpacing(11)
        row.addWidget(icon_badge(glyph, color, 26), 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px; font-weight: 600;")
        d = QLabel(desc)
        d.setObjectName("Muted")
        d.setWordWrap(True)
        col.addWidget(t)
        col.addWidget(d)
        row.addLayout(col, 1)


class DashboardPage(BasePage):
    title = "主页"
    subtitle = "工作台概览：数据规模、打标进度与待办"

    def build(self) -> None:
        body = QVBoxLayout()
        body.setSpacing(14)

        # ---- 顶部品牌条 ----
        brand = QHBoxLayout()
        brand.setSpacing(12)
        brand.addWidget(icon_badge("◉", theme.ACCENT, 42))
        col = QVBoxLayout()
        col.setSpacing(1)
        self.title_label = QLabel("ComfyUI 工作台")
        self.title_label.setStyleSheet("font-size: 19px; font-weight: 700;")
        self.sub_label = muted("尚未扫描任何数据集")
        col.addWidget(self.title_label)
        col.addWidget(self.sub_label)
        brand.addLayout(col)
        brand.addStretch(1)
        self.scan_btn = QPushButton("添加数据集")
        self.scan_btn.setObjectName("Primary")
        self.scan_btn.clicked.connect(lambda: self.ctx.go("datasets", "add"))
        brand.addWidget(self.scan_btn)
        body.addLayout(brand)

        # ---- 统计卡 ----
        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)
        self.card_images = StatCard("图片总数", "0", "▦", theme.ACCENT, "2 个数据集")
        self.card_images.clicked.connect(lambda: self.ctx.go("gallery"))
        self.card_tagged = StatCard("已有标签", "0%", "✓", theme.GREEN, "0 张待处理")
        self.card_tagged.clicked.connect(lambda: self.ctx.go("workbench"))
        self.card_terms = StatCard("标签词条", "0", "❖", theme.PURPLE, "0 个已屏蔽")
        self.card_terms.clicked.connect(lambda: self.ctx.go("taglib"))
        self.card_pending = StatCard("待打标", "0", "✦", theme.ORANGE, "可开始")
        self.card_pending.clicked.connect(lambda: self.ctx.go("workbench"))
        for card in (self.card_images, self.card_tagged, self.card_terms, self.card_pending):
            stats_row.addWidget(card)
        body.addLayout(stats_row)

        # ---- 两栏 ----
        cols = QHBoxLayout()
        cols.setSpacing(12)

        # 数据集
        left = QFrame()
        left.setObjectName("Panel")
        left.setMinimumWidth(340)
        left_col = QVBoxLayout(left)
        left_col.setContentsMargins(14, 12, 14, 12)
        left_col.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(QLabel("数据集"))
        head.addStretch(1)
        all_btn = QPushButton("全部  →")
        all_btn.setObjectName("Segment")
        all_btn.clicked.connect(lambda: self.ctx.go("datasets"))
        head.addWidget(all_btn)
        left_col.addLayout(head)
        self.dataset_box = QVBoxLayout()
        self.dataset_box.setSpacing(8)
        left_col.addLayout(self.dataset_box)
        left_col.addStretch(1)
        cols.addWidget(left, 1)

        # 最近改动
        right = QFrame()
        right.setObjectName("Panel")
        right_col = QVBoxLayout(right)
        right_col.setContentsMargins(14, 12, 14, 12)
        right_col.setSpacing(10)
        rhead = QHBoxLayout()
        rhead.addWidget(QLabel("最近改动"))
        rhead.addStretch(1)
        gallery_btn = QPushButton("去图库  →")
        gallery_btn.setObjectName("Segment")
        gallery_btn.clicked.connect(lambda: self.ctx.go("gallery"))
        rhead.addWidget(gallery_btn)
        right_col.addLayout(rhead)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(6)
        right_col.addLayout(self.recent_box)
        right_col.addStretch(1)
        cols.addWidget(right, 1)

        body.addLayout(cols, 1)

        # ---- 开始使用 ----
        start = QFrame()
        start.setObjectName("Panel")
        s_col = QVBoxLayout(start)
        s_col.setContentsMargins(14, 12, 14, 12)
        s_col.setSpacing(10)
        shead = QHBoxLayout()
        shead.addWidget(QLabel("开始使用"))
        shead.addStretch(1)
        go_btn = QPushButton("▷  打标工作台")
        go_btn.clicked.connect(lambda: self.ctx.go("workbench"))
        shead.addWidget(go_btn)
        s_col.addLayout(shead)
        self.checks = QVBoxLayout()
        self.checks.setSpacing(8)
        s_col.addLayout(self.checks)
        body.addWidget(start)

        # 套进滚动区，整页可滚
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setLayout(body)
        scroll.setWidget(holder)
        self.root.addWidget(scroll)

        # 缩略图回来时统一刷新（只连一次，避免每次刷新堆积连接）
        self._recent_rows: dict[str, _RecentRow] = {}
        self.ctx.thumb_pool.loaded.connect(self._on_thumb_loaded)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        stats = self.db.stats()
        ratio = int(round(stats["tagged_ratio"] * 100))
        self.card_images.set_value(
            f"{stats['images']:,}", f"{stats['datasets']} 个数据集"
        )
        self.card_tagged.set_value(f"{ratio}%", f"{stats['tagged']:,} 张已处理")
        self.card_terms.set_value(
            f"{stats['tag_terms']:,}", f"{stats['banned']} 个已屏蔽"
        )
        self.card_pending.set_value(
            f"{stats['pending']:,}", "可开始" if stats["pending"] else "已清空"
        )
        self.sub_label.setText(
            f"{stats['datasets']} 个数据集 · {stats['images']:,} 张图片 · "
            f"{stats['tag_terms']:,} 个标签词条，全部保存在本机"
        )

        self._fill_datasets()
        self._fill_recent()
        self._fill_checks(stats)

    def _clear(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _fill_datasets(self) -> None:
        self._clear(self.dataset_box)
        rows = self.db.list_datasets()
        if not rows:
            empty = QLabel("还没有数据集。点右上角「添加数据集」，选 ComfyUI 的 output 目录。")
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            self.dataset_box.addWidget(empty)
            return
        for data in rows[:5]:
            self.dataset_box.addWidget(_DatasetRow(data))

    def _fill_recent(self) -> None:
        self._clear(self.recent_box)
        self._recent_rows = {}
        rows = self.db.recent_images(limit=7)
        if not rows:
            empty = QLabel("扫描之后，最近改动的图片会出现在这里。")
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            self.recent_box.addWidget(empty)
            return
        for data in rows:
            row = _RecentRow(data)
            row.open_requested.connect(self._open_image)
            self.recent_box.addWidget(row)
            self._recent_rows[row.path] = row
            cached = self.ctx.thumb_pool.cached(row.path)
            if cached is not None:
                row.set_thumb(cached)
            else:
                self.ctx.thumb_pool.request(row.path, float(data.get("mtime") or 0))

    def _on_thumb_loaded(self, path: str, image) -> None:
        row = getattr(self, "_recent_rows", {}).get(path)
        if row is not None:
            row.set_thumb(image)

    def _fill_checks(self, stats: dict) -> None:
        self._clear(self.checks)
        self.checks.addWidget(
            _CheckItem(
                "✓", "添加数据集文件夹",
                "把 ComfyUI 的出图目录纳入进来，自动解析提示词与模型",
                theme.GREEN,
            )
        )
        self.checks.addWidget(
            _CheckItem(
                "✓", "配置打标用的视觉模型",
                "填 OpenAI 兼容的 Base URL 与 API Key（也支持环境变量）",
                theme.ACCENT,
            )
        )
        if stats["images"] and not stats["with_meta"]:
            self.checks.addWidget(
                _CheckItem(
                    "!", "有图没有元数据",
                    f"{stats['images'] - stats['with_meta']} 张图没读到提示词，"
                    "可能是被转存或二次编辑过，可以手动补标签",
                    theme.ORANGE,
                )
            )

    def _open_image(self, image_id: int) -> None:
        self.ctx.go("gallery", {"select_id": image_id})
