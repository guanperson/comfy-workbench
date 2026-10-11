"""主窗口：侧边栏 + 顶栏 + 页面栈 + 右侧检查器 + 状态栏。"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, paths
from ..config import Settings, save_settings
from ..db import Database
from . import theme
from .inspector import Inspector
from .pages.base import AppContext, BasePage
from .pages.dashboard import DashboardPage
from .pages.datasets import DatasetsPage
from .pages.gallery import GalleryPage
from .pages.preprocess_page import PreprocessPage
from .pages.settings_page import SettingsPage
from .pages.taglib import TagLibPage
from .pages.workbench import WorkbenchPage
from .widgets import muted


def _draw_icon(name: str, size: int = 20) -> QIcon:
    """Theme-independent line icons for consistent rendering on Windows."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    color = QColor(theme.ACCENT if name == "dashboard" else theme.TEXT_DIM)
    pen = QPen(color)
    pen.setWidthF(max(2.0, size * 0.09))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    r = pixmap.rect().adjusted(3, 3, -3, -3)
    if name in {"dashboard", "gallery"}:
        painter.drawRoundedRect(r, 2, 2)
        if name == "dashboard":
            painter.drawLine(size // 2, 3, size // 2, size - 3)
            painter.drawLine(3, size // 2, size - 3, size // 2)
        else:
            painter.drawLine(3, size // 3, size - 3, size // 3)
            painter.drawLine(3, size * 2 // 3, size - 3, size * 2 // 3)
    elif name == "datasets":
        painter.drawLine(3, 7, 9, 7)
        painter.drawLine(9, 7, 11, 9)
        painter.drawLine(11, 9, size - 3, 9)
        painter.drawRoundedRect(3, 7, size - 6, size - 10, 2, 2)
    elif name == "workbench":
        painter.drawRoundedRect(r, 2, 2)
        painter.drawLine(7, 7, size - 5, 7)
        painter.drawLine(7, 11, size - 5, 11)
        painter.drawLine(7, 15, 12, 15)
    elif name == "taglib":
        painter.drawPolygon([
            QPoint(3, 5), QPoint(11, 5), QPoint(17, 11),
            QPoint(11, 17), QPoint(3, 17),
        ])
        painter.drawPoint(7, 9)
    else:
        painter.drawEllipse(r)
        painter.drawLine(size // 2, 6, size // 2, size - 6)
        painter.drawLine(6, size // 2, size - 6, size // 2)
    painter.end()
    return QIcon(pixmap)


def _draw_brand_mark(size: int = 38) -> QPixmap:
    """绘制品牌标记；纯代码生成，打包时不依赖额外图片资源。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.ACCENT))
    painter.drawRoundedRect(0, 0, size, size, 11, 11)
    painter.setBrush(QColor("#0b1716"))
    unit = max(4, size // 7)
    gap = max(3, size // 10)
    start = (size - unit * 2 - gap) // 2
    painter.drawRoundedRect(start, start, unit, unit, 2, 2)
    painter.drawRoundedRect(start + unit + gap, start, unit, unit, 2, 2)
    painter.drawRoundedRect(start, start + unit + gap, unit, unit, 2, 2)
    painter.drawRoundedRect(start + unit + gap, start + unit + gap, unit, unit, 2, 2)
    painter.end()
    return pixmap


class BackgroundRoot(QWidget):
    """One shared, pre-blurred background for all workbench pages."""

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        wallpaper = theme.background_pixmap()
        painter.fillRect(self.rect(), QColor(theme.BG))
        if wallpaper is not None and not wallpaper.isNull():
            scaled = wallpaper.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation)
            x = (scaled.width() - self.width()) // 2
            y = (scaled.height() - self.height()) // 2
            painter.drawPixmap(self.rect(), scaled, scaled.rect().adjusted(x, y, -x, -y))
            shade = QColor("#ffffff" if self.window().property("ui_theme") != "dark" else "#0a0f14")
            shade.setAlpha(round(theme.background_shade() * 2.55))
            painter.fillRect(self.rect(), shade)


class Sidebar(QFrame):
    navigate = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(196)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 18, 0, 12)
        col.setSpacing(0)

        # 品牌
        brand = QHBoxLayout()
        brand.setContentsMargins(14, 0, 12, 18)
        brand.setSpacing(11)
        logo = QLabel()
        logo.setFixedSize(38, 38)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setPixmap(_draw_brand_mark(38))
        brand.addWidget(logo)
        text = QVBoxLayout()
        text.setSpacing(0)
        title = QLabel("COMFY")
        title.setObjectName("BrandTitle")
        sub = QLabel("DATA WORKBENCH")
        sub.setObjectName("BrandSub")
        text.addWidget(title)
        text.addWidget(sub)
        brand.addLayout(text)
        brand.addStretch(1)
        col.addLayout(brand)

        self.buttons: dict[str, QPushButton] = {}
        for key, label, _icon_name in theme.PAGE_ORDER:
            btn = QPushButton(label)
            btn.setIcon(_draw_icon(key))
            btn.setIconSize(QSize(18, 18))
            btn.setObjectName("NavButton")
            btn.setToolTip(label)
            btn.setCheckable(True)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _=False, k=key: self.navigate.emit(k))
            col.addWidget(btn)
            self.buttons[key] = btn

        col.addStretch(1)

        self.help_btn = QPushButton("?   帮助与快捷键")
        self.help_btn.setObjectName("NavButton")
        col.addWidget(self.help_btn)

        version = QLabel(f"v{__version__}")
        version.setObjectName("Version")
        version.setContentsMargins(16, 8, 0, 0)
        col.addWidget(version)

    def set_active(self, key: str) -> None:
        for name, btn in self.buttons.items():
            btn.setChecked(name == key)


class MainWindow(QMainWindow):
    def __init__(self, db: Database, settings: Settings) -> None:
        super().__init__()
        self.db = db
        self.ctx = AppContext(db, settings, self)
        self.setWindowTitle("ComfyUI 工作台")
        app_icon = _draw_icon("dashboard", 64)
        self.setWindowIcon(app_icon)
        if QApplication.instance():
            QApplication.instance().setWindowIcon(app_icon)
        self.resize(1460, 940)
        self.setMinimumSize(1080, 700)

        self.setProperty("ui_theme", settings.ui_theme)
        root = BackgroundRoot()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---------------- 侧边栏 ----------------
        self.sidebar = Sidebar()
        self.sidebar.navigate.connect(self.goto)
        self.sidebar.help_btn.clicked.connect(self._show_help)
        outer.addWidget(self.sidebar)

        # ---------------- 中间列 ----------------
        center = QWidget()
        center_col = QVBoxLayout(center)
        center_col.setContentsMargins(0, 0, 0, 0)
        center_col.setSpacing(0)

        center_col.addWidget(self._build_topbar())

        # 数据库出问题时的横幅（默认隐藏，不用模态框打断人）
        self._db_banner = QLabel("")
        self._db_banner.setObjectName("WarnBanner")
        self._db_banner.setWordWrap(True)
        self._db_banner.hide()
        center_col.addWidget(self._db_banner)

        # 页面栈 + 检查器
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)

        self.pages = QStackedWidget()
        self._page_index: dict[str, int] = {}
        self._page_objects: dict[str, BasePage] = {}
        self._register_pages()

        self.inspector = Inspector(self.ctx)
        self._inspector_before_preprocess = True
        self.inspector.setMinimumWidth(280)
        # 设置页改了「解析方式」后，检视面板的下拉跟着同步
        self.ctx.inspector_changed.connect(self.inspector.sync_parser)

        self.splitter.addWidget(self.pages)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1050, 330])
        center_col.addWidget(self.splitter, 1)
        outer.addWidget(center, 1)

        # ---------------- 状态栏 ----------------
        self._build_statusbar()

        # ---------------- 信号 ----------------
        self.ctx.navigate.connect(self.goto)
        self.ctx.status.connect(self.show_status)
        self.ctx.datasets_changed.connect(self._refresh_all)
        self.ctx.tags_changed.connect(self._refresh_all)
        self._install_shortcuts()
        self._after_pages_built()

        self.goto(settings.start_page or "dashboard")

    def closeEvent(self, event) -> None:  # noqa: N802
        preprocess_page = self._page_objects.get("preprocess")
        if preprocess_page is not None and hasattr(preprocess_page, "shutdown"):
            preprocess_page.shutdown()
        super().closeEvent(event)

    # ------------------------------------------------------------------ #
    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 12, 18, 12)
        row.setSpacing(10)

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        self.page_title = QLabel("主页")
        self.page_title.setStyleSheet("font-size: 18px; font-weight: 700;")
        self.page_sub = muted("")
        title_col.addWidget(self.page_title)
        title_col.addWidget(self.page_sub)
        row.addLayout(title_col)

        row.addStretch(1)

        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("搜索图片、标签、提示词…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(360)
        self.search.returnPressed.connect(self._on_search)
        row.addWidget(self.search)

        kbd = QLabel("Ctrl K")
        kbd.setStyleSheet(
            f"background: {theme.BG_PANEL}; border: 1px solid {theme.BORDER};"
            f" border-radius: 6px; padding: 4px 8px; color: {theme.TEXT_DIM};"
            f" font-size: 11px;"
        )
        row.addWidget(kbd)

        row.addSpacing(6)

        gallery_btn = QPushButton("浏览图库")
        gallery_btn.setIcon(_draw_icon("gallery"))
        gallery_btn.clicked.connect(lambda: self.goto("gallery"))
        row.addWidget(gallery_btn)

        wb_btn = QPushButton("打标工作台")
        wb_btn.setIcon(_draw_icon("workbench"))
        wb_btn.setObjectName("Primary")
        wb_btn.clicked.connect(lambda: self.goto("workbench"))
        row.addWidget(wb_btn)

        return bar

    def _build_statusbar(self) -> None:
        bar = QStatusBar()
        bar.setSizeGripEnabled(False)
        self.setStatusBar(bar)

        self.status_left = QLabel("就绪")
        bar.addWidget(self.status_left)

        self.status_images = QLabel("0 张图片")
        self.status_datasets = QLabel("0 个数据集")
        self.status_selected = QLabel("未选择")
        bar.addPermanentWidget(self.status_images)
        bar.addPermanentWidget(self._sep())
        bar.addPermanentWidget(self.status_datasets)
        bar.addPermanentWidget(self._sep())
        bar.addPermanentWidget(self.status_selected)
        bar.addPermanentWidget(self._sep())

        self.status_progress = QLabel("已打标 0/0")
        bar.addPermanentWidget(self.status_progress)
        bar.addPermanentWidget(self._sep())

        self.status_storage = QLabel("本地存储")
        self.status_storage.setStyleSheet(f"color: {theme.GREEN};")
        bar.addPermanentWidget(self.status_storage)
        bar.addPermanentWidget(self._sep())

        self.status_api = QLabel("API 未配置")
        bar.addPermanentWidget(self.status_api)

    @staticmethod
    def _sep() -> QLabel:
        sep = QLabel("│")
        sep.setStyleSheet(f"color: {theme.BORDER};")
        return sep

    # ------------------------------------------------------------------ #
    def _register_pages(self) -> None:
        mapping = {
            "dashboard": DashboardPage,
            "gallery": GalleryPage,
            "preprocess": PreprocessPage,
            "datasets": DatasetsPage,
            "workbench": WorkbenchPage,
            "taglib": TagLibPage,
            "settings": SettingsPage,
        }
        for key, cls in mapping.items():
            page = cls(self.ctx)
            self._page_index[key] = self.pages.addWidget(page)
            self._page_objects[key] = page
            self.ctx.pages[key] = page

    # ------------------------------------------------------------------ #
    def goto(self, key: str, payload: object = None) -> None:
        page = self._page_objects.get(key)
        if page is None:
            return
        was_preprocess = self.pages.currentWidget() is self._page_objects.get("preprocess")
        if key == "preprocess" and not was_preprocess:
            self._inspector_before_preprocess = not self.inspector.isHidden()
            self.inspector.hide()
        elif key != "preprocess" and was_preprocess:
            self.inspector.setVisible(self._inspector_before_preprocess)
        self.pages.setCurrentWidget(page)
        self.sidebar.set_active(key)
        page.on_show()
        if payload is not None and hasattr(page, "on_payload"):
            page.on_payload(payload)
        self.page_title.setText(page.title)
        self.page_sub.setText(page.subtitle)
        self.show_status(f"{page.title} · {page.subtitle}")
        # 停在状态面板时顺便把统计刷一下，免得右侧数字是旧的
        if self.inspector.isVisible() and self.inspector.stack.currentIndex() == 0:
            self.inspector.refresh_status()

    def show_status(self, message: str) -> None:
        self.status_left.setText(f"{message}   ·   {time.strftime('%H:%M')}")

    def _refresh_all(self) -> None:
        page = self.pages.currentWidget()
        if isinstance(page, BasePage):
            page.safe_refresh()
        self._refresh_statusbar()
        if self.inspector.isVisible():
            self.inspector.refresh_status()

    def _refresh_statusbar(self) -> None:
        stats = self.db.stats()
        self.status_images.setText(f"{stats['images']:,} 张图片")
        self.status_datasets.setText(f"{stats['datasets']} 个数据集")
        self.status_progress.setText(
            f"已打标 {stats['tagged']:,}/{stats['images']:,}"
        )
        s = self.ctx.settings
        if s.has_api_key():
            self.status_api.setText(f"API 就绪 · {s.model}")
            self.status_api.setStyleSheet(f"color: {theme.GREEN};")
        else:
            self.status_api.setText("API 未配置")
            self.status_api.setStyleSheet(f"color: {theme.ORANGE};")
        self.status_storage.setText("本地存储")
        self.status_storage.setStyleSheet(f"color: {theme.GREEN};")

    # ------------------------------------------------------------------ #
    def _install_shortcuts(self) -> None:
        def bind(sequence: str, callback) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(callback)

        for index, (key, _label, _glyph) in enumerate(theme.PAGE_ORDER, start=1):
            bind(f"Ctrl+{index}", lambda k=key: self.goto(k))

        bind("Ctrl+K", self._focus_search)
        bind("Ctrl+B", self.inspector.toggle)
        bind("Ctrl+A", self._select_all)
        bind("Escape", self._clear_selection)
        bind("F5", self._retry_after_db_problem)
        bind("Ctrl+R", self._refresh_all)
        bind("Ctrl+T", lambda: self.goto("workbench"))

        space = QShortcut(QKeySequence("Space"), self)
        space.setContext(Qt.ShortcutContext.ApplicationShortcut)
        space.activated.connect(self._toggle_current_selection)

        enter = QShortcut(QKeySequence("Return"), self)
        enter.setContext(Qt.ShortcutContext.ApplicationShortcut)
        enter.activated.connect(self._open_details)

        self.search.textChanged.connect(self._quick_filter)

    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _current_page_key(self) -> str:
        current = self.pages.currentWidget()
        for key, page in self._page_objects.items():
            if page is current:
                return key
        return ""

    def _after_pages_built(self) -> None:
        gallery = self._page_objects["gallery"]
        gallery.selection_count_changed.connect(self._update_selected_label)
        self.ctx.db_problem.connect(self._on_db_problem)
        self._refresh_statusbar()
        # 右侧状态面板默认是空白的，开窗时先填一次
        self.inspector.refresh_status()

    def _on_db_problem(self, message: str) -> None:
        """数据库出问题：给一条能撑住、又不打断人的提示。

        不弹「出错了」模态框——数据库类的错误通常重连/重试就能恢复，
        真需要用户处理时，状态栏 + 横幅 + 日志已经够了。
        """
        self.show_status(f"⚠ {message}")
        self._db_banner.setText(f"⚠ {message}　（详情见数据目录里的 workbench.log）")
        self._db_banner.show()
        QTimer.singleShot(30_000, self._db_banner.hide)

    def _retry_after_db_problem(self) -> None:
        """F5 / Ctrl+R：重开数据库连接并重刷当前页。"""
        try:
            self.db.reopen()
        except Exception as exc:  # noqa: BLE001
            self.ctx.report_db_problem(f"重新连接数据库失败：{exc}", exc)
            return
        self.ctx._db_problem_shown = ""
        self._db_banner.hide()
        page = self.pages.currentWidget()
        if isinstance(page, BasePage):
            page.safe_refresh()
        self._refresh_statusbar()
        self.show_status("已重新连接数据库")

    def _select_all(self) -> None:
        if self._current_page_key() == "gallery":
            self._page_objects["gallery"].select_all()

    def _clear_selection(self) -> None:
        if self._current_page_key() == "gallery":
            self._page_objects["gallery"].clear_selection()
        self.status_selected.setText("未选择")

    def _toggle_current_selection(self) -> None:
        if self._current_page_key() != "gallery":
            return
        gallery = self._page_objects["gallery"]
        view = gallery.view
        index = view.currentIndex()
        if index.isValid() and view.selectionModel() is not None:
            view.selectionModel().select(
                index,
                QItemSelectionModel.SelectionFlag.Toggle
                | QItemSelectionModel.SelectionFlag.Rows,
            )
        self._update_selected_label()

    def _open_details(self) -> None:
        if self._current_page_key() != "gallery":
            return
        self._page_objects["gallery"].open_details()

    def _update_selected_label(self, count: int | None = None) -> None:
        if count is None:
            if self._current_page_key() != "gallery":
                return
            count = len(self._page_objects["gallery"].selected_rows())
        self.status_selected.setText("未选择" if not count else f"已选 {count} 张")

    def _on_search(self) -> None:
        text = self.search.text().strip()
        if not text:
            return
        self.goto("gallery")
        self._page_objects["gallery"].set_filter(text)

    def _quick_filter(self, text: str) -> None:
        """顶栏搜索框直接过滤图库（切到图库页才有意义）。"""
        if self._current_page_key() != "gallery":
            return
        self._page_objects["gallery"].set_filter(text)

    # ------------------------------------------------------------------ #
    def _show_help(self) -> None:
        from PySide6.QtWidgets import QMessageBox  # noqa: PLC0415

        box = QMessageBox(self)
        box.setWindowTitle("帮助")
        box.setText(f"ComfyUI 工作台 v{__version__}")
        box.setInformativeText(
            "工作流：\n"
            "1. 数据集页 →「浏览…」选 ComfyUI 的 output 目录 →「添加并扫描」\n"
            "2. 设置页 → 填 OpenAI 兼容的 Base URL / 密钥 / 模型 →「测试连接」\n"
            "3. 打标工作台 →「自动打标队列」，或图库选中几张后「打标选中项」\n"
            "4. 标签库 → 把误判的词加进屏蔽词，下次打标就不会再出现\n"
            "5. 数据集页 →「导出数据集」，得到每图一个 .txt 的训练集\n\n"
            f"数据目录：{paths.data_dir()}"
        )
        box.exec()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.ctx.settings.start_page = self._current_page_key() or "dashboard"
        save_settings(self.ctx.settings)
        self.db.close()
        super().closeEvent(event)
