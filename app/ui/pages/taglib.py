"""标签库：词条管理、屏蔽词、频次查看。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .base import BasePage
from .. import theme
from ..widgets import muted

TAG_ROLE = int(Qt.ItemDataRole.UserRole) + 1
BAN_ROLE = int(Qt.ItemDataRole.UserRole) + 2
CNT_ROLE = int(Qt.ItemDataRole.UserRole) + 3

ROW_H = 28


class _TagDelegate(QStyledItemDelegate):
    """一行一个标签：状态点 + 名称 + 右侧次数。

    用 delegate 而不是 item widget：一千多个词条时后者会明显卡。
    """

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_H)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        count = int(index.data(CNT_ROLE) or 0)
        banned = bool(index.data(BAN_ROLE))

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected:
            painter.setBrush(QColor(75, 155, 255, 46))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect.adjusted(2, 1, -2, -1), 5, 5)
        elif hovered:
            painter.setBrush(QColor(theme.BG_HOVER))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect.adjusted(2, 1, -2, -1), 5, 5)

        # 状态点
        dot_color = QColor(theme.RED if banned else theme.ACCENT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot_color)
        painter.drawEllipse(rect.x() + 10, rect.center().y() - 3, 6, 6)

        # 名称
        count_text = f"{count:,}"
        font = QFont(option.font)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        count_w = metrics.horizontalAdvance(count_text) + 14
        text_rect = rect.adjusted(24, 0, -count_w - 8, 0)

        if banned:
            font.setStrikeOut(True)
            painter.setFont(font)
            metrics = QFontMetrics(font)
        painter.setPen(QColor(theme.RED if banned else theme.TEXT))
        elided = metrics.elidedText(str(name), Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided,
        )

        # 次数
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT_FAINT))
        count_rect = rect.adjusted(rect.width() - count_w - 8, 0, -8, 0)
        painter.drawText(
            count_rect,
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            count_text,
        )

        painter.restore()


class TagLibPage(BasePage):
    title = "标签库"
    subtitle = "词条统计、屏蔽词、重命名与合并"

    def build(self) -> None:
        # ---- 顶部工具 ----
        bar = QFrame()
        bar.setObjectName("Panel")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(9)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索标签…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        row.addWidget(self.search, 1)

        self.filter_combo = QComboBox()
        self.filter_combo.addItem("全部标签", "all")
        self.filter_combo.addItem("仅屏蔽词", "banned")
        self.filter_combo.addItem("仅已使用", "used")
        self.filter_combo.addItem("仅未使用", "unused")
        self.filter_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.filter_combo)

        self.order_combo = QComboBox()
        for key, text in (
            ("count_desc", "出现次数 ▾"),
            ("count_asc", "出现次数 ▴"),
            ("name", "按名称"),
            ("recent", "最近添加"),
        ):
            self.order_combo.addItem(text, key)
        self.order_combo.currentIndexChanged.connect(self.refresh)
        row.addWidget(self.order_combo)

        ban_btn = QPushButton("屏蔽选中")
        ban_btn.clicked.connect(self._toggle_ban)
        row.addWidget(ban_btn)

        rename_btn = QPushButton("重命名")
        rename_btn.clicked.connect(self._rename)
        row.addWidget(rename_btn)

        del_btn = QPushButton("删除")
        del_btn.setObjectName("GhostDanger")
        del_btn.clicked.connect(self._delete)
        row.addWidget(del_btn)
        self.root.addWidget(bar)

        # ---- 主体：列表 | 侧栏 ----
        self.tabs = QTabWidget()

        # 全部标签
        self.list = QListWidget()
        self.list.setSpacing(0)
        self.list.setUniformItemSizes(True)
        self.list.setItemDelegate(_TagDelegate(self.list))
        self.list.itemSelectionChanged.connect(self._on_selection)
        self.list.itemDoubleClicked.connect(lambda _item: self._show_images_with_tag())
        list_page = QWidget()
        lcol = QVBoxLayout(list_page)
        lcol.setContentsMargins(0, 0, 0, 0)
        lcol.setSpacing(6)
        self.count_label = muted("0 个词条")
        lcol.addWidget(self.count_label)
        lcol.addWidget(self.list, 1)
        self.tabs.addTab(list_page, "全部标签")

        # 屏蔽词
        ban_page = QWidget()
        bcol = QVBoxLayout(ban_page)
        bcol.setContentsMargins(0, 0, 0, 0)
        bcol.setSpacing(8)
        tip = QLabel(
            "屏蔽词会做两件事：\n"
            "1. 塞进打标提示词，明确要求模型不要输出这些词\n"
            "2. 模型返回后再过滤一遍，兜住不听话的情况"
        )
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        bcol.addWidget(tip)

        self.ban_input = QPlainTextEdit()
        self.ban_input.setPlaceholderText(
            "一行一个，或用逗号分隔，例如：\n"
            "worst_quality, low_quality, jpeg_artifacts, watermark, signature, artist_name"
        )
        bcol.addWidget(self.ban_input)

        actions = QHBoxLayout()
        add_ban = QPushButton("加入屏蔽词")
        add_ban.setObjectName("Primary")
        add_ban.clicked.connect(self._add_bans)
        actions.addWidget(add_ban)
        clear_ban = QPushButton("清空屏蔽词")
        clear_ban.setObjectName("Segment")
        clear_ban.clicked.connect(self._clear_bans)
        actions.addWidget(clear_ban)
        actions.addStretch(1)
        bcol.addLayout(actions)

        self.ban_list = QListWidget()
        self.ban_list.setSpacing(1)
        bcol.addWidget(self.ban_list, 1)
        self.tabs.addTab(ban_page, "屏蔽词")

        # 频次表
        freq_page = QWidget()
        fcol = QVBoxLayout(freq_page)
        fcol.setContentsMargins(0, 0, 0, 0)
        fcol.setSpacing(8)
        freq_tip = QLabel("整库标签频次，用来判断哪些是噪声标签（一般砍掉出现次数 < 5 的）")
        freq_tip.setObjectName("Muted")
        fcol.addWidget(freq_tip)
        self.freq_list = QListWidget()
        self.freq_list.setSpacing(1)
        mono_font = QFont("Consolas", 10)
        self.freq_list.setFont(mono_font)
        fcol.addWidget(self.freq_list, 1)
        freq_actions = QHBoxLayout()
        export_freq = QPushButton("复制频次表")
        export_freq.clicked.connect(self._copy_freq)
        freq_actions.addWidget(export_freq)
        copy_ban = QPushButton("复制屏蔽词")
        copy_ban.clicked.connect(self._copy_ban)
        freq_actions.addWidget(copy_ban)
        freq_actions.addStretch(1)
        fcol.addLayout(freq_actions)
        self.tabs.addTab(freq_page, "频次表")

        self.root.addWidget(self.tabs, 1)

        # ---- 底部提示 ----
        self.hint = muted("双击标签可查看含该标签的图片")
        self.root.addWidget(self.hint)

        # 最后再连信号：build() 过程中切标签会调用 refresh()，
        # 那时底部标签还没建好
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.ctx.tags_changed.connect(self.refresh)

    def _on_tab_changed(self) -> None:
        self.refresh()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        if not self._loaded:
            return
        if self.tabs.currentIndex() == 1:
            self._refresh_bans()
            return
        if self.tabs.currentIndex() == 2:
            self._refresh_freq()
            return

        keyword = self.search.text().strip()
        rows = self.db.list_tags(
            search=keyword,
            order=self.order_combo.currentData() or "count_desc",
            limit=4000,
        )
        mode = self.filter_combo.currentData()
        if mode == "banned":
            rows = [r for r in rows if r["banned"]]
        elif mode == "used":
            rows = [r for r in rows if int(r["cnt"] or 0) > 0]
        elif mode == "unused":
            rows = [r for r in rows if int(r["cnt"] or 0) == 0]

        self.list.clear()
        selected_ids = {
            int(i.data(TAG_ROLE) or 0) for i in self.list.selectedItems()
        }
        for row in rows:
            item = QListWidgetItem(str(row["name"]))
            item.setData(TAG_ROLE, int(row["id"]))
            item.setData(BAN_ROLE, int(row["banned"] or 0))
            item.setData(CNT_ROLE, int(row["cnt"] or 0))
            self.list.addItem(item)
            if int(row["id"]) in selected_ids:
                item.setSelected(True)

        self.count_label.setText(f"{len(rows):,} 个词条")
        self._recompute_hint()

    def _refresh_bans(self) -> None:
        names = self.db.banned_tag_names()
        self.ban_list.clear()
        self.ban_list.setItemDelegate(_TagDelegate(self.ban_list))
        for name in names:
            item = QListWidgetItem(name)
            item.setData(BAN_ROLE, 1)
            item.setData(CNT_ROLE, 0)
            self.ban_list.addItem(item)
        if not names:
            self.ban_list.addItem("还没有屏蔽词（在「全部标签」里右键或点「屏蔽选中」）")
        self._recompute_hint()

    def _refresh_freq(self) -> None:
        self.freq_list.clear()
        for name, count in self.db.tag_frequency()[:3000]:
            self.freq_list.addItem(f"{count:>6}  {name}")
        self._recompute_hint()

    def _recompute_hint(self) -> None:
        stats = self.db.stats()
        self.hint.setText(
            f"共 {stats['tag_terms']:,} 个词条 · {stats['banned']} 个屏蔽词 · "
            f"覆盖 {stats['images']:,} 张图 · 双击标签查看相关图片"
        )

    # ------------------------------------------------------------------ #
    def _selected_tag(self) -> dict | None:
        item = self.list.currentItem()
        if item is None:
            return None
        tag_id = int(item.data(TAG_ROLE) or 0)
        return self.db.get_tag(tag_id)

    def _on_selection(self) -> None:
        row = self._selected_tag()
        if row is None:
            return
        count = int(row["cnt"] or 0)
        if count:
            self.ctx.notify(f"「{row['name']}」用于 {count} 张图")
        else:
            self.ctx.notify(f"「{row['name']}」暂未用于任何图片")

    def _toggle_ban(self) -> None:
        row = self._selected_tag()
        if row is None:
            return
        banned = not bool(row["banned"])
        self.db.set_tag_banned_by_id(int(row["id"]), banned)
        self.ctx.tags_changed.emit()
        self.ctx.notify(
            f"「{row['name']}」{'已加入屏蔽词' if banned else '已取消屏蔽'}"
        )

    def _rename(self) -> None:
        row = self._selected_tag()
        if row is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "重命名标签", "新名称：", text=str(row["name"])
        )
        if not ok or not new_name.strip():
            return
        old = str(row["name"])
        self.db.rename_tag(int(row["id"]), new_name.strip())
        self.ctx.tags_changed.emit()
        self.ctx.images_changed.emit()
        self.ctx.notify(
            f"「{old}」已改名为「{new_name.strip()}」"
            + ("（原词条已存在，已合并）" if new_name.strip() != old else "")
        )

    def _delete(self) -> None:
        row = self._selected_tag()
        if row is None:
            return
        count = int(row["cnt"] or 0)
        if count:
            box = QMessageBox(self)
            box.setWindowTitle("删除标签")
            box.setText(f"「{row['name']}」已用于 {count} 张图。")
            box.setInformativeText(f"删除后这 {count} 张图上对应的标签都会被移除。")
            yes = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not yes:
                return
        self.db.delete_tag(int(row["id"]))
        self.ctx.tags_changed.emit()
        self.ctx.images_changed.emit()
        self.ctx.notify(f"已删除「{row['name']}」")

    def _show_images_with_tag(self) -> None:
        row = self._selected_tag()
        if row is None:
            return
        self.ctx.go("gallery", {"tag": str(row["name"])})

    def _add_bans(self) -> None:
        text = self.ban_input.toPlainText()
        names = [
            n.strip()
            for n in text.replace("，", ",").replace("\n", ",").split(",")
            if n.strip()
        ]
        if not names:
            return
        for name in names:
            self.db.set_tag_banned(name, True)
        self.ban_input.clear()
        self.ctx.tags_changed.emit()
        self.ctx.notify(f"已加入 {len(names)} 个屏蔽词")

    def _clear_bans(self) -> None:
        names = self.db.banned_tag_names()
        if not names:
            return
        box = QMessageBox(self)
        box.setWindowTitle("清空屏蔽词")
        box.setText(f"要取消全部 {len(names)} 个屏蔽词吗？")
        yes = box.addButton("清空", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            return
        for name in names:
            self.db.set_tag_banned(name, False)
        self.db.prune_orphan_tags()
        self.ctx.tags_changed.emit()

    def _copy_freq(self) -> None:
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415

        text = "\n".join(f"{c}\t{n}" for n, c in self.db.tag_frequency())
        QApplication.clipboard().setText(text)
        self.ctx.notify("频次表已复制到剪贴板")

    def _copy_ban(self) -> None:
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415

        QApplication.clipboard().setText(", ".join(self.db.banned_tag_names()))
        self.ctx.notify("屏蔽词已复制到剪贴板")

    # ------------------------------------------------------------------ #
    def on_payload(self, payload: object) -> None:
        if isinstance(payload, dict) and payload.get("tag"):
            self.search.setText(str(payload["tag"]))

