"""页面基类与共享上下文。"""

from __future__ import annotations

import sqlite3
import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...config import Settings
from ...db import Database
from ..widgets import ThumbPool


class AppContext(QObject):
    """全局状态 + 页面间广播。

    页面之间不互相 import，一律通过这里的信号通信。
    """

    status = Signal(str)                      # 状态栏文本
    datasets_changed = Signal()               # 数据集增删改
    images_changed = Signal()                 # 图片/标签变化
    tags_changed = Signal()                   # 标签库变化
    selection_changed = Signal(object)        # 选中的图片 dict（None 表示清空）
    navigate = Signal(str, object)            # 请求切页，第二参为附加数据
    db_problem = Signal(str)                  # 数据库出问题了（主窗口统一提示）
    inspector_changed = Signal(str)           # 检视面板「读取方式」变了（带 parser 值）

    def __init__(self, db: Database, settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.settings = settings
        self.pages: dict[str, QWidget] = {}
        self.thumb_pool = ThumbPool(settings.thumb_size, max_threads=4, parent=self)
        self.selected_image: dict | None = None
        self._db_problem_shown = ""

    # ------------------------------------------------------------------ #
    def go(self, page: str, payload: object = None) -> None:
        self.navigate.emit(page, payload)

    def notify(self, message: str) -> None:
        self.status.emit(message)

    def report_db_problem(self, message: str, exc: BaseException | None = None) -> None:
        """数据库出问题时统一走这里：记日志 + 状态栏提示 + 只弹一次窗。"""
        import traceback as _tb  # noqa: PLC0415

        text = f"{message}"
        if exc is not None:
            import sqlite3 as _sq  # noqa: PLC0415

            health = ""
            if isinstance(exc, _sq.Error):
                try:
                    info = self.db.health()
                    health = f"（{info['path']}，自动重连 {info['reconnects']} 次）"
                except Exception:  # noqa: BLE001
                    health = ""
            text = f"{text}{health}"
            print(_tb.format_exc(), file=sys.stderr)

        self.status.emit(text)
        # 同一个错别连着弹窗骚扰用户
        if self._db_problem_shown == message:
            return
        self._db_problem_shown = message
        self.db_problem.emit(text)


class BasePage(QWidget):
    """所有页面的父类，负责懒加载。"""

    title = "页面"
    subtitle = ""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.db = ctx.db
        self.settings = ctx.settings
        self._loaded = False
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(20, 16, 20, 16)
        self.root.setSpacing(14)

    # 子类实现：真正构建界面
    def build(self) -> None:
        raise NotImplementedError

    def refresh(self) -> None:
        """数据变化时被主窗口调用。"""

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._loaded = True
            self.build()

    def on_show(self) -> None:
        """切到本页时触发。"""
        self.ensure_loaded()
        self.safe_refresh()

    # ------------------------------------------------------------------ #
    def safe_refresh(self) -> bool:
        """刷新，但不让数据库异常冒成「出错了」弹窗。

        数据库偶尔会临时不可用（目录被挪走、文件被别的程序占住、
        磁盘掉线）。这类问题重试或重连就能恢复，用弹窗打断用户
        没意义；真正恢复不了再提示，而且要说清楚是哪一层坏了。
        """
        try:
            self.refresh()
            return True
        except sqlite3.Error as exc:
            # 数据层已经自动重连重试过一次了，到这儿还是不行才算真坏
            self.ctx.report_db_problem(
                f"读取 {self.title} 的数据失败：{exc}", exc
            )
            return False
