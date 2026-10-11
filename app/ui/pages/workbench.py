"""打标台：管理配套 txt 标注。

设计要点（对着用户的工作流来）：

**只认配套 txt。** 有 txt 的图才显示标签，没有就提示去设置里生成。
不读 PNG 元数据 —— 元数据是「出图时用的词」，txt 才是「训练要用的标注」，
这里管的是后者。

**搬标签的流程：**
    点胶囊（紫→灰，可多选） → 右侧点「复制」或「移动」
    → 切到别的图 → 「粘贴」亮起 → 点一下追加到末尾（去重）
切换图片时胶囊选中自动清空，但剪贴板保留，方便连着粘很多张。

**暂存区：** 所有改动先攒着不落盘，保存时才写 txt（先备份）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import captions, thumbs, trash, wd14
from .base import BasePage
from .. import theme
from ..workers import Wd14Worker
from ..widgets import (
    TagPillBox,
    ThumbGrid,
    ThumbPool,
    elide,
    hline,
    muted,
    progress_bar,
)

ID_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class WorkbenchPage(BasePage):
    title = "打标台"
    subtitle = "管理配套 txt 标注：选中标签 → 复制/移动 → 粘贴"

    def build(self) -> None:
        self._rows: list[dict] = []
        self._current: dict | None = None
        self._clipboard: list[str] = []
        self._clipboard_mode = ""
        self._checked: set[int] = set()
        # 左栏缩略图用的异步池（256 够小图用了，省内存）
        self.thumb_pool = ThumbPool(size=256, max_threads=3, parent=self)
        # 抽离视图：看某个标签的全部图片
        self._isolated_tag: str = ""
        self._saved_rows: list[dict] | None = None

        # ---------------- 顶部控制条 ----------------
        bar = QFrame()
        bar.setObjectName("Panel")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(9)

        self.dataset_combo = QComboBox()
        self.dataset_combo.setMinimumWidth(170)
        self.dataset_combo.currentIndexChanged.connect(lambda _: self.reload_queue())
        row.addWidget(self.dataset_combo)

        self.only_pending = QCheckBox("只看未保存过的")
        self.only_pending.setToolTip("只列出还没有配套 txt 的图（这些图在这里不显示标签）")
        self.only_pending.stateChanged.connect(lambda _: self.reload_queue())
        row.addWidget(self.only_pending)

        row.addStretch(1)

        self.staged_label = muted("暂存 0 处改动")
        row.addWidget(self.staged_label)

        self.revert_btn = QPushButton("放弃改动")
        self.revert_btn.setObjectName("Segment")
        self.revert_btn.setEnabled(False)
        self.revert_btn.clicked.connect(self._revert_all)
        row.addWidget(self.revert_btn)

        self.save_btn = QPushButton("保存到 txt")
        self.save_btn.setObjectName("Primary")
        self.save_btn.setEnabled(False)
        self.save_btn.setToolTip("把暂存的改动写回配套 txt（会先备份原文件）")
        self.save_btn.clicked.connect(self._save)
        row.addWidget(self.save_btn)

        self.root.addWidget(bar)

        # ---------------- 主体：队列 | 编辑区 ----------------
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        splitter.addWidget(self._build_queue())
        splitter.addWidget(self._build_editor())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 5)
        # 左栏要能排下 3 列小图，太窄就没法「同时看多张」
        splitter.setSizes([560, 860])
        self.splitter = splitter
        self.root.addWidget(splitter, 1)

        self.ctx.datasets_changed.connect(self._reload_datasets)
        self.ctx.images_changed.connect(self.reload_queue)

    # ------------------------------------------------------------------ #
    def _build_queue(self) -> QWidget:
        left = QFrame()
        left.setObjectName("Panel")
        col = QVBoxLayout(left)
        col.setContentsMargins(10, 10, 10, 10)
        col.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(QLabel("队列"))
        head.addStretch(1)
        self.queue_count = muted("0 张")
        head.addWidget(self.queue_count)
        col.addLayout(head)

        # 抽离横幅：只在「查看某标签全部图」时出现
        self.iso_bar = QFrame()
        self.iso_bar.setObjectName("Panel2")
        iso_lay = QVBoxLayout(self.iso_bar)
        iso_lay.setContentsMargins(9, 8, 9, 8)
        iso_lay.setSpacing(6)

        iso_top = QHBoxLayout()
        iso_top.setSpacing(6)
        self.iso_label = QLabel("")
        self.iso_label.setWordWrap(True)
        iso_top.addWidget(self.iso_label, 1)
        self.iso_back_btn = QPushButton("← 返回队列")
        self.iso_back_btn.setObjectName("Segment")
        self.iso_back_btn.setToolTip("回到完整队列")
        self.iso_back_btn.clicked.connect(self._exit_isolated)
        iso_top.addWidget(self.iso_back_btn)
        iso_lay.addLayout(iso_top)

        iso_ops = QHBoxLayout()
        iso_ops.setSpacing(6)
        self.iso_all_btn = QPushButton("全选这些图")
        self.iso_all_btn.setObjectName("Segment")
        self.iso_all_btn.clicked.connect(self._isolate_select_all)
        iso_ops.addWidget(self.iso_all_btn)

        self.iso_none_btn = QPushButton("取消选择")
        self.iso_none_btn.setObjectName("Segment")
        self.iso_none_btn.clicked.connect(self._isolate_select_none)
        iso_ops.addWidget(self.iso_none_btn)

        self.iso_hint = muted("")
        iso_ops.addWidget(self.iso_hint, 1)
        iso_lay.addLayout(iso_ops)

        iso_act = QHBoxLayout()
        iso_act.setSpacing(6)
        self.iso_del_btn = QPushButton("从选中的图删除这个标签")
        self.iso_del_btn.setObjectName("GhostDanger")
        self.iso_del_btn.clicked.connect(self._isolate_delete_tag)
        iso_act.addWidget(self.iso_del_btn)

        self.iso_replace_btn = QPushButton("替换成…")
        self.iso_replace_btn.setObjectName("Primary")
        self.iso_replace_btn.setToolTip("把选中的图里这个标签换成另一个标签（自己输入）")
        self.iso_replace_btn.clicked.connect(self._isolate_replace_tag)
        iso_act.addWidget(self.iso_replace_btn)

        # 删图片（连 txt 一起）—— 只移进垃圾桶，不真删
        self.iso_trash_btn = QPushButton("删除选中的图（进垃圾桶）")
        self.iso_trash_btn.setObjectName("GhostDanger")
        self.iso_trash_btn.setToolTip(
            "把选中的图片连同它的配套 txt 一起移进垃圾桶。\n"
            "文件不会真的消失，随时能在设置里还原。"
        )
        self.iso_trash_btn.clicked.connect(self._trash_selected)
        iso_act.addWidget(self.iso_trash_btn)
        iso_act.addStretch(1)
        iso_lay.addLayout(iso_act)

        self.iso_bar.setVisible(False)
        col.addWidget(self.iso_bar)

        # 缩略图小网格：比一长串文件名直观得多
        self.grid = ThumbGrid(self.thumb_pool)
        self.grid.current_changed.connect(self._on_grid_current)
        self.grid.pick_changed.connect(self._on_grid_pick)
        col.addWidget(self.grid, 1)

        zoom_row = QHBoxLayout()
        zoom_row.setSpacing(6)
        zoom_row.addWidget(muted("大小"))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(72, 220)
        self.zoom.setValue(112)
        self.zoom.setToolTip("调整缩略图大小")
        self.zoom.valueChanged.connect(self.grid.set_zoom)
        zoom_row.addWidget(self.zoom, 1)
        col.addLayout(zoom_row)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        prev_btn = QPushButton("‹ 上一张")
        prev_btn.setObjectName("Segment")
        prev_btn.clicked.connect(lambda: self.grid.step(-1))
        next_btn = QPushButton("下一张 ›")
        next_btn.setObjectName("Segment")
        next_btn.clicked.connect(lambda: self.grid.step(1))
        for btn in (prev_btn, next_btn):
            btn.setMinimumWidth(0)
            actions.addWidget(btn)
        col.addLayout(actions)

        hint = muted("Ctrl / Shift 可多选　·　改过的图标 ●")
        col.addWidget(hint)
        return left

    def _build_editor(self) -> QWidget:
        right = QFrame()
        right.setObjectName("Panel")
        col = QVBoxLayout(right)
        col.setContentsMargins(12, 12, 12, 12)
        col.setSpacing(9)

        self.preview = QLabel("从左边选一张图")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(200)
        self.preview.setStyleSheet(
            f"background: {theme.BG}; border: 1px solid {theme.BORDER_SOFT};"
            f" border-radius: 8px; color: {theme.TEXT_FAINT};"
        )
        col.addWidget(self.preview, 2)

        self.file_label = QLabel("—")
        self.file_label.setObjectName("CardTitle")
        self.file_label.setWordWrap(True)
        col.addWidget(self.file_label)

        self.meta_label = muted("—")
        self.meta_label.setWordWrap(True)
        col.addWidget(self.meta_label)

        col.addWidget(hline())

        # 标签胶囊 + 右侧操作区
        pill_head = QHBoxLayout()
        pill_head.addWidget(QLabel("标签"))
        self.sel_label = muted("未选中")
        pill_head.addWidget(self.sel_label)
        pill_head.addStretch(1)
        self.sel_all_btn = QPushButton("全选")
        self.sel_all_btn.setObjectName("Segment")
        self.sel_all_btn.clicked.connect(lambda: self.pill_box.select_all())
        pill_head.addWidget(self.sel_all_btn)
        self.sel_none_btn = QPushButton("取消选中")
        self.sel_none_btn.setObjectName("Segment")
        self.sel_none_btn.clicked.connect(lambda: self.pill_box.clear_selection())
        pill_head.addWidget(self.sel_none_btn)
        col.addLayout(pill_head)

        body = QHBoxLayout()
        body.setSpacing(10)

        self.pill_box = TagPillBox(max_height=168,
                                   placeholder="这张图没有配套 txt（可用设置里的生成功能）")
        self.pill_box.selection_changed.connect(self._on_pill_selection)
        self.pill_box.remove_requested.connect(self._remove_tag)
        self.pill_box.ban_requested.connect(self._ban_tag)
        self.pill_box.isolate_requested.connect(self.isolate_tag)
        body.addWidget(self.pill_box, 1)

        # 右侧：复制 / 移动 / 释放 / 粘贴
        side = QVBoxLayout()
        side.setSpacing(6)
        side.addWidget(muted("操作"))
        self.copy_btn = QPushButton("复制")
        self.copy_btn.setToolTip("把选中的标签放进剪贴板，本图保留")
        self.copy_btn.clicked.connect(lambda: self._collect("copy"))
        side.addWidget(self.copy_btn)
        self.move_btn = QPushButton("移动")
        self.move_btn.setToolTip("把选中的标签放进剪贴板，并从本图移除")
        self.move_btn.clicked.connect(lambda: self._collect("move"))
        side.addWidget(self.move_btn)
        self.release_btn = QPushButton("释放选中")
        self.release_btn.setToolTip("清空剪贴板，并取消胶囊的选中状态")
        self.release_btn.clicked.connect(self._release)
        side.addWidget(self.release_btn)
        side.addWidget(hline())
        self.paste_btn = QPushButton("粘贴")
        self.paste_btn.setObjectName("Primary")
        self.paste_btn.setEnabled(False)
        self.paste_btn.setToolTip("把剪贴板里的标签追加到这张图（自动去重）")
        self.paste_btn.clicked.connect(self._paste)
        side.addWidget(self.paste_btn)
        side.addStretch(1)
        body.addLayout(side)
        col.addLayout(body)

        self.clip_label = muted("剪贴板：空")
        col.addWidget(self.clip_label)

        # 手动加标签
        add_row = QHBoxLayout()
        from PySide6.QtWidgets import QLineEdit  # noqa: PLC0415

        self.add_edit = QLineEdit()
        self.add_edit.setPlaceholderText("手动加标签，回车或逗号确认（可一次粘贴多个）")
        self.add_edit.returnPressed.connect(self._commit_add)
        add_row.addWidget(self.add_edit, 1)
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._commit_add)
        add_row.addWidget(add_btn)
        col.addLayout(add_row)

        col.addWidget(hline())

        # ---------------- 批量统计 ----------------
        stat_head = QHBoxLayout()
        stat_head.addWidget(QLabel("批量统计"))
        self.stat_scope = muted("未选取")
        stat_head.addWidget(self.stat_scope)
        stat_head.addStretch(1)
        self.pick_all_btn = QPushButton("全选整个数据集")
        self.pick_all_btn.setObjectName("Segment")
        self.pick_all_btn.clicked.connect(self._pick_all)
        stat_head.addWidget(self.pick_all_btn)
        self.pick_none_btn = QPushButton("清空选取")
        self.pick_none_btn.setObjectName("Segment")
        self.pick_none_btn.clicked.connect(self._pick_none)
        stat_head.addWidget(self.pick_none_btn)
        self.stat_btn = QPushButton("统计选中")
        self.stat_btn.clicked.connect(self._run_stats)
        stat_head.addWidget(self.stat_btn)
        col.addLayout(stat_head)

        self.stat_table = QTableWidget(0, 3)
        self.stat_table.setHorizontalHeaderLabels(["标签", "出现", "占比"])
        self.stat_table.horizontalHeader().setStretchLastSection(False)
        self.stat_table.setColumnWidth(0, 300)
        self.stat_table.setColumnWidth(1, 70)
        self.stat_table.setColumnWidth(2, 70)
        self.stat_table.verticalHeader().setVisible(False)
        self.stat_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.stat_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stat_table.setMaximumHeight(190)
        self.stat_table.itemSelectionChanged.connect(self._on_stat_selection)
        col.addWidget(self.stat_table)

        del_row = QHBoxLayout()
        self.del_btn = QPushButton("从选中的图里删除这个标签")
        self.del_btn.setObjectName("GhostDanger")
        self.del_btn.setEnabled(False)
        self.del_btn.clicked.connect(self._delete_tag_from_selection)
        del_row.addWidget(self.del_btn)

        self.isolate_btn = QPushButton("查看所有该标签图片")
        self.isolate_btn.setObjectName("Primary")
        self.isolate_btn.setEnabled(False)
        self.isolate_btn.setToolTip(
            "把这个标签的全部图片单独抽出来看。\n"
            "抽离后可以勾选其中一部分，统一删除或替换成别的标签。\n"
            "（选一行统计结果或点一个标签胶囊都能用）"
        )
        self.isolate_btn.clicked.connect(self._isolate_selected_tag)
        del_row.addWidget(self.isolate_btn)

        del_row.addStretch(1)
        self.stat_hint = muted("勾选队列里的图（Ctrl+点）后点「统计选中」")
        del_row.addWidget(self.stat_hint)
        col.addLayout(del_row)

        # 标签写法转换（作用于选中的那批图）
        conv_row = QHBoxLayout()
        conv_row.setSpacing(6)
        conv_row.addWidget(muted("写法转换："))

        # 主推：转义写法。448style 训练集和出图都用这个形态。
        self.escaped_btn = QPushButton("角色词 → 转义 \\(推荐\\)")
        self.escaped_btn.setObjectName("Primary")
        self.escaped_btn.setToolTip(
            "把角色词统一成 Danbooru 转义写法 —— 这是 448style 的目标形态：\n"
            "  musashi (the purple rabbit's marksmanship) (azur lane)\n"
            "    →  musashi \\(the purple rabbit's marksmanship\\) \\(azur lane\\)\n"
            "  swire_(arknights)      →  swire \\(arknights\\)\n"
            "  sirius\\(azur lane\\)    →  sirius \\(azur lane\\)   （顺带补空格）\n\n"
            "为什么用转义：ComfyUI 会把没转义的括号当权重语法剥掉，\n"
            "害得 (azur lane) 里的 azur 粘成 Ġaz，和训练时的 token 不一致。\n"
            "转义后括号不会被剥，是最接近训练的形态。\n\n"
            "只动「括号内容像作品名」的词 —— 判定规则见\n"
            "《448style_角色名格式与括号转义》。\n"
            "所以 alternate breast size (larger)、kaname madoka (cosplay)\n"
            "这类修饰语不会被误改；权重语法 (词:1.3) 也不动。"
        )
        self.escaped_btn.clicked.connect(self._convert_escaped_tags)
        conv_row.addWidget(self.escaped_btn)
        self.escaped_btn.setEnabled(False)

        self.role_btn = QPushButton("角色词 → 下划线")
        self.role_btn.setObjectName("Segment")
        self.role_btn.setToolTip(
            "把角色词改成 anima / Danbooru 下划线写法：\n"
            "  swire \\(arknights\\)    →  swire_(arknights)\n"
            "  swire (arknights)     →  swire_(arknights)\n"
            "  zara \\(poolside coincidence\\) \\(azur lane\\)\n"
            "                        →  zara_(poolside_coincidence)_(azur_lane)\n\n"
            "注意：448style 实际用的是转义写法，不是这个下划线写法。\n"
            "只有想改成 Danbooru 标签规范时才用。"
        )
        self.role_btn.clicked.connect(self._convert_role_tags)
        conv_row.addWidget(self.role_btn)
        self.role_btn.setEnabled(False)

        self.underscore_btn = QPushButton("下划线 → 空格")
        self.underscore_btn.setObjectName("Segment")
        self.underscore_btn.setToolTip(
            "把选中图里标签的下划线换成空格：\n"
            "  wuthering_waves  →  wuthering waves\n"
            "  male_rover_      →  male rover\n"
            "注意：角色词也会一起转（swire_(arknights) → swire (arknights)）。\n"
            "如果只想规整普通标签，先别选角色词所在的图。"
        )
        self.underscore_btn.clicked.connect(self._convert_underscores)
        conv_row.addWidget(self.underscore_btn)
        self.underscore_btn.setEnabled(False)

        self.conv_hint = muted("需要先勾选图片")
        conv_row.addWidget(self.conv_hint)
        conv_row.addStretch(1)

        # WD14 本地打标单独一行 —— 跟写法转换不是一类事，混一排会点错
        wd_row = QHBoxLayout()
        wd_row.setSpacing(6)
        wd_row.addWidget(muted("本地打标："))

        self.wd14_btn = QPushButton("WD14 打标")
        self.wd14_btn.setObjectName("Segment")
        self.wd14_btn.setToolTip(
            "让 WD14 模型看图猜标签，结果写进暂存区（不直接落盘）。\n\n"
            "什么时候用：手拍图、网图这种没有 PNG 元数据的图；\n"
            "有元数据的图别用这个 —— 元数据更准，WD14 会丢角色名。\n\n"
            "结果是追加还是替换，看右边的「打标方式」。\n"
            "跑完可以在检视面板里改，满意了再「保存到 txt」。"
        )
        self.wd14_btn.clicked.connect(self._run_wd14)
        wd_row.addWidget(self.wd14_btn)
        self.wd14_btn.setEnabled(False)

        self.wd14_mode = QComboBox()
        self.wd14_mode.addItem("替换原有", "replace")
        self.wd14_mode.addItem("追加到末尾", "append")
        self.wd14_mode.setToolTip(
            "替换原有：丢掉现在的标签，只用 WD14 的结果\n"
            "追加到末尾：保留现有标签，把 WD14 的接在后面（自动去重）"
        )
        self.wd14_mode.setMaximumWidth(120)
        wd_row.addWidget(muted("打标方式"))
        wd_row.addWidget(self.wd14_mode)

        self.wd14_status = muted("")
        wd_row.addWidget(self.wd14_status)
        wd_row.addStretch(1)

        col.addLayout(wd_row)

        # 删图片（连 txt）—— 挪到最右，跟写法转换隔开，免得误点
        self.trash_btn = QPushButton("删除选中的图（进垃圾桶）")
        self.trash_btn.setObjectName("GhostDanger")
        self.trash_btn.setToolTip(
            "把勾选的图片连同它的配套 txt 一起移进垃圾桶：\n"
            "  xxx.png  →  trash/<数据集>/<目录>/xxx.png\n"
            "  xxx.txt  →  trash/<数据集>/<目录>/xxx.txt\n\n"
            "**不会真删**，随时能在「设置 → 垃圾桶」里还原。\n"
            "桶里保留了原目录结构，同名文件也不会互相覆盖。"
        )
        self.trash_btn.clicked.connect(self._trash_selected)
        conv_row.addWidget(self.trash_btn)
        self.trash_btn.setEnabled(False)

        col.addLayout(conv_row)

        # 图上勾选由 ThumbGrid 自己处理（Ctrl / Shift 多选）

        self._stat_rows: list[tuple[str, int]] = []

        # 提示条：没有 txt 的图
        self.no_txt_hint = muted("")
        col.addWidget(self.no_txt_hint)
        return right

    # ------------------------------------------------------------------ #
    # 数据加载
    # ------------------------------------------------------------------ #
    def _reload_datasets(self) -> None:
        current = self.dataset_combo.currentData()
        self.dataset_combo.blockSignals(True)
        self.dataset_combo.clear()
        self.dataset_combo.addItem("全部数据集", None)
        for ds in self.db.list_datasets():
            total = self.db.stats(dataset_id=int(ds["id"]))["images"]
            self.dataset_combo.addItem(f"{ds['name']}（{total} 张）", int(ds["id"]))
        if current is not None:
            idx = self.dataset_combo.findData(current)
            if idx >= 0:
                self.dataset_combo.setCurrentIndex(idx)
        self.dataset_combo.blockSignals(False)

    def reload_queue(self) -> None:
        if not self._loaded:
            return
        # 抽离视图里刷新时，保持只看那个标签（也保持滚动位置）
        if self._isolated_tag:
            self._load_isolated(self._isolated_tag, keep_view=True)
            return
        dataset_id = self.dataset_combo.currentData()
        self._reload_datasets()
        rows = self.db.list_images(
            dataset_id=dataset_id, status="", order="mtime_desc", limit=4000
        )
        self._rows = rows
        changed_ids = {e.image_id for e in self.db.caption_edits()}
        with_txt = sum(1 for row in rows if row.get("has_caption"))
        self.grid.set_rows(rows, changed_ids)
        self.queue_count.setText(f"{len(rows):,} 张 · 有 txt {with_txt:,}")
        self._update_staged()
        if rows:
            first_id = int(rows[0].get("id") or 0)
            self.grid.select_image(first_id)

    def _update_staged(self) -> None:
        count = self.db.pending_caption_count()
        self.staged_label.setText(
            f"暂存 {count} 处改动" if count else "暂无改动"
        )
        self.save_btn.setEnabled(count > 0)
        self.revert_btn.setEnabled(count > 0)

    # ------------------------------------------------------------------ #
    # 当前图片
    # ------------------------------------------------------------------ #
    def _on_grid_current(self, image_id: int) -> None:
        # 切图时清掉胶囊选中，但剪贴板保留（方便连着粘很多张）
        self.pill_box.clear_selection()
        if not image_id:
            self._current = None
            self._render_current()
            return
        source = self.grid.rows()
        row = next((r for r in source if int(r.get("id") or 0) == image_id), None)
        self._current = row
        self._render_current()

    def _render_current(self) -> None:
        row = self._current
        if row is None:
            self.preview.setText("从左边选一张图")
            self.preview.setPixmap(QPixmap())
            self.file_label.setText("—")
            self.meta_label.setText("—")
            self.pill_box.set_tags([])
            return

        path = str(row.get("path") or "")
        self.file_label.setText(str(row.get("filename") or ""))
        size = f"{int(row.get('width') or 0)}×{int(row.get('height') or 0)}"
        has_txt = bool(row.get("has_caption"))
        self.meta_label.setText(
            f"{size}　·　{'有配套 txt' if has_txt else '没有配套 txt'}"
        )

        pixmap = self._preview_pixmap(path)
        if pixmap is not None and not pixmap.isNull():
            self.preview.setPixmap(
                pixmap.scaled(
                    900, 420,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            self.preview.setText("（无法预览）")

        if not has_txt:
            self.pill_box.set_tags([])
            self.no_txt_hint.setText(
                "⚠ 这张图没有配套 txt。到「设置 → 为缺 txt 的图生成标注」批量生成后再来筛选。"
            )
            return

        self.no_txt_hint.setText("")
        tags = self.db.caption_current(int(row.get("id") or 0), path)
        self.pill_box.set_tags(tags)

    # ------------------------------------------------------------------ #
    # 搬标签
    # ------------------------------------------------------------------ #
    def _on_pill_selection(self, selected: list[str]) -> None:
        if selected:
            self.sel_label.setText(f"已选中 {len(selected)} 个")
        else:
            self.sel_label.setText("未选中")
        has = bool(selected)
        self.copy_btn.setEnabled(has)
        self.move_btn.setEnabled(has)
        # 抽离按钮：恰好选中一个标签时才亮（多个的话不知道该看哪个）
        if hasattr(self, "isolate_btn"):
            self._sync_isolate_btn()

    def _collect(self, mode: str) -> None:
        selected = self.pill_box.selected_tags()
        if not selected or self._current is None:
            return
        self._clipboard = list(selected)
        self._clipboard_mode = mode
        if mode == "move":
            path = str(self._current["path"])
            image_id = int(self._current["id"])
            current = self.db.caption_current(image_id, path)
            remaining = [t for t in current if t not in selected]
            self.db.set_caption_tags(image_id, path, remaining)
            self.pill_box.set_tags(remaining)
        self.pill_box.clear_selection()
        self._refresh_clipboard(reselect=True)
        self._update_staged()
        self._refresh_queue_marks()
        action = "复制" if mode == "copy" else "移动"
        self.ctx.notify(f"已{action} {len(self._clipboard)} 个标签到剪贴板，去别的图点粘贴")

    def _release(self) -> None:
        self._clipboard = []
        self._clipboard_mode = ""
        self.pill_box.clear_selection()
        self._refresh_clipboard()
        self.ctx.notify("已清空剪贴板和选中状态")

    def _refresh_clipboard(self, reselect: bool = False) -> None:
        if self._clipboard:
            mode = "复制" if self._clipboard_mode == "copy" else "移动"
            preview = "、".join(self._clipboard[:4])
            more = f" 等 {len(self._clipboard)} 个" if len(self._clipboard) > 4 else ""
            self.clip_label.setText(f"剪贴板（{mode}）：{preview}{more}")
            self.paste_btn.setEnabled(True)
        else:
            self.clip_label.setText("剪贴板：空")
            self.paste_btn.setEnabled(False)
        _ = reselect

    def _paste(self) -> None:
        if not self._clipboard or self._current is None:
            return
        if not self._current.get("has_caption"):
            self.ctx.notify("这张图没有配套 txt，无法粘贴")
            return
        image_id = int(self._current["id"])
        path = str(self._current["path"])
        current = self.db.caption_current(image_id, path)
        merged = captions.dedupe(list(current) + list(self._clipboard))
        added = len(merged) - len(current)
        if added == 0:
            self.ctx.notify("这些标签这张图已经有了，没有新增")
            return
        self.db.set_caption_tags(image_id, path, merged)
        self.pill_box.set_tags(merged)
        self._update_staged()
        self._refresh_queue_marks()
        self.ctx.notify(f"粘贴了 {added} 个新标签（还没保存）")

    def _remove_tag(self, name: str) -> None:
        if self._current is None:
            return
        image_id = int(self._current["id"])
        path = str(self._current["path"])
        current = self.db.caption_current(image_id, path)
        remaining = [t for t in current if t != name]
        self.db.set_caption_tags(image_id, path, remaining)
        self.pill_box.set_tags(remaining)
        self._update_staged()
        self._refresh_queue_marks()
        self.ctx.notify(f"已移除「{name}」（还没保存）")

    def _ban_tag(self, name: str, _flag: bool = True) -> None:
        self.db.set_tag_banned(name, True)
        self.ctx.tags_changed.emit()
        self.ctx.notify(f"「{name}」已加入屏蔽词")

    def _commit_add(self) -> None:
        raw = self.add_edit.text().strip()
        if not raw or self._current is None:
            return
        if not self._current.get("has_caption"):
            self.ctx.notify("这张图没有配套 txt，无法添加")
            return
        image_id = int(self._current["id"])
        path = str(self._current["path"])
        new_tags = captions.split_caption(raw)
        current = self.db.caption_current(image_id, path)
        merged = captions.dedupe(list(current) + new_tags)
        self.add_edit.clear()
        self.db.set_caption_tags(image_id, path, merged)
        self.pill_box.set_tags(merged)
        self._update_staged()
        self._refresh_queue_marks()

    def _refresh_queue_marks(self) -> None:
        changed_ids = {e.image_id for e in self.db.caption_edits()}
        self.grid.refresh_marks(changed_ids)

    # ------------------------------------------------------------------ #
    # 批量统计
    # ------------------------------------------------------------------ #
    def _pick_all(self) -> None:
        self.grid.select_all()

    def _pick_none(self) -> None:
        self.grid.clear_selection()

    def _selected_image_ids(self) -> list[int]:
        return self.grid.selected_ids()

    def _on_grid_pick(self) -> None:
        """队列勾选变化：更新计数 + 统计/转换按钮可用性。"""
        count = len(self.grid.selected_ids())
        self.stat_scope.setText(f"已选取 {count} 张" if count else "未选取")
        self.stat_btn.setEnabled(count > 0)
        if hasattr(self, "role_btn"):
            self.role_btn.setEnabled(count > 0)
            self.escaped_btn.setEnabled(count > 0)
            self.underscore_btn.setEnabled(count > 0)
            # 打标跑到一半别被选中变化重新点亮
            if hasattr(self, "wd14_btn"):
                running = getattr(self, "_wd14_worker", None)
                busy = running is not None and running.isRunning()
                self.wd14_btn.setEnabled(count > 0 and not busy)
            if count:
                self.conv_hint.setText(f"将作用于选中的 {count} 张")
            else:
                self.conv_hint.setText("需要先勾选图片")
        if hasattr(self, "trash_btn"):
            self.trash_btn.setEnabled(count > 0)
        if hasattr(self, "iso_trash_btn") and self._isolated_tag:
            self.iso_trash_btn.setEnabled(count > 0)
        if hasattr(self, "iso_hint") and self._isolated_tag:
            self._update_iso_hint()

    def _run_stats(self) -> None:
        ids = self._selected_image_ids()
        if not ids:
            self.ctx.notify("先在队列里勾选图片（Ctrl+点可多选）")
            return
        with_tags = self.db.caption_applied_count(ids)
        counts = self.db.caption_tag_counts(ids)
        self._stat_rows = counts

        self.stat_table.setRowCount(0)
        denominator = len(ids)  # 分母 = 选中的图数
        for name, count in counts:
            row = self.stat_table.rowCount()
            self.stat_table.insertRow(row)
            self.stat_table.setItem(row, 0, QTableWidgetItem(name))
            self.stat_table.setItem(row, 1, QTableWidgetItem(str(count)))
            ratio = (count / denominator * 100.0) if denominator else 0.0
            self.stat_table.setItem(row, 2, QTableWidgetItem(f"{ratio:.1f}%"))
        self.stat_scope.setText(
            f"选中 {len(ids)} 张（其中 {with_tags} 张有标注）· {len(counts)} 个不同标签"
        )
        self.stat_hint.setText("选中一行后点右边按钮，可从这批图里删掉这个标签")

    def _on_stat_selection(self) -> None:
        has_row = bool(self.stat_table.selectedItems())
        self.del_btn.setEnabled(has_row)
        self._sync_isolate_btn()

    def _sync_isolate_btn(self) -> None:
        """统计表选中一行，或胶囊恰好选中一个 → 抽离按钮可用。"""
        tag = self._current_stat_tag() or self._pill_single_tag()
        self.isolate_btn.setEnabled(bool(tag))
        if tag:
            self.isolate_btn.setText(f"查看所有「{elide(tag, 12)}」图片")
        else:
            self.isolate_btn.setText("查看所有该标签图片")

    def _delete_tag_from_selection(self) -> None:
        rows = self.stat_table.selectionModel().selectedRows() if self.stat_table.selectionModel() else []
        if not rows:
            return
        tag = self.stat_table.item(rows[0].row(), 0).text()
        ids = self._selected_image_ids()
        if not ids:
            return
        affected = 0
        for image_id in ids:
            edit = self.db.caption_edit(image_id, self._path_of(image_id))
            if tag in edit.current:
                remaining = [t for t in edit.current if t != tag]
                self.db.set_caption_tags(image_id, edit.image_path, remaining)
                affected += 1
        self._update_staged()
        self._refresh_queue_marks()
        if self._current is not None:
            self._render_current()
        self._run_stats()
        self.ctx.notify(f"已从 {affected} 张图里移除「{tag}」（还没保存）")

    # ------------------------------------------------------------------ #
    # 抽离视图：看某个标签的全部图片
    # ------------------------------------------------------------------ #
    def _current_stat_tag(self) -> str:
        """统计表里选中的那一行的标签名。"""
        model = self.stat_table.selectionModel()
        if model is None:
            return ""
        rows = model.selectedRows()
        if not rows:
            return ""
        item = self.stat_table.item(rows[0].row(), 0)
        return item.text() if item else ""

    def _isolate_selected_tag(self) -> None:
        tag = self._current_stat_tag() or self._pill_single_tag()
        if not tag:
            self.ctx.notify("先选中一个标签（统计表里点一行，或点一个标签胶囊）")
            return
        self.isolate_tag(tag)

    def _pill_single_tag(self) -> str:
        """当前图里恰好选中一个胶囊时，返回它。"""
        selected = self.pill_box.selected_tags()
        return selected[0] if len(selected) == 1 else ""

    def isolate_tag(self, tag: str) -> None:
        """抽出所有带这个标签的图，切到抽离视图。"""
        tag = (tag or "").strip()
        if not tag:
            return
        dataset_id = self.dataset_combo.currentData()
        if not self.db.image_ids_with_tag(tag, dataset_id=dataset_id):
            self.ctx.notify(f"没有图片带「{tag}」这个标签")
            return
        if not self._isolated_tag:
            # 第一次抽离：记住完整队列，返回时恢复
            self._saved_rows = list(self.grid.rows())
        self._isolated_tag = tag
        self._load_isolated(tag)

    def _load_isolated(self, tag: str, *, keep_view: bool = False) -> None:
        """按标签筛选出图片并铺到网格上。

        ``keep_view=True`` 时保留滚动位置和当前图（改完标签重筛时用），
        否则定位到第一张（刚进抽离视图时用）。
        """
        dataset_id = self.dataset_combo.currentData()
        wanted = set(self.db.image_ids_with_tag(tag, dataset_id=dataset_id))
        source = self._saved_rows if self._saved_rows is not None else self.grid.rows()
        rows = [r for r in source if int(r.get("id") or 0) in wanted]
        if not rows and wanted:
            # 兜底：_saved_rows 里没有（比如刚重启），直接查库
            rows = [
                r for r in self.db.list_images(dataset_id=dataset_id, limit=4000)
                if int(r.get("id") or 0) in wanted
            ]

        changed_ids = {e.image_id for e in self.db.caption_edits()}
        self.grid.set_rows(rows, changed_ids, keep_view=keep_view)
        self.queue_count.setText(f"抽离 {len(rows):,} 张")

        self.iso_label.setText(
            f"只看带「<b>{tag}</b>」的图　·　共 {len(rows):,} 张"
        )
        self.iso_bar.setVisible(True)
        self._update_iso_hint()
        if keep_view:
            return
        if rows:
            self.grid.select_image(int(rows[0].get("id") or 0))
        else:
            self._current = None
            self._render_current()

    def _exit_isolated(self) -> None:
        """回到完整队列。"""
        tag = self._isolated_tag
        self._isolated_tag = ""
        self.iso_bar.setVisible(False)
        if self._saved_rows is not None:
            rows = self._saved_rows
            self._saved_rows = None
            changed_ids = {e.image_id for e in self.db.caption_edits()}
            with_txt = sum(1 for row in rows if row.get("has_caption"))
            self.grid.set_rows(rows, changed_ids)
            self.queue_count.setText(f"{len(rows):,} 张 · 有 txt {with_txt:,}")
            if self._current is not None:
                self.grid.select_image(int(self._current.get("id") or 0))
        else:
            self.reload_queue()
        self.ctx.notify(f"已退出「{tag}」抽离视图" if tag else "已退出抽离视图")

    def _isolate_select_all(self) -> None:
        self.grid.select_all()

    def _isolate_select_none(self) -> None:
        self.grid.clear_selection()

    def _update_iso_hint(self) -> None:
        count = len(self.grid.selected_ids())
        total = self.grid.model.rowCount()
        self.iso_hint.setText(
            f"已选 {count} / {total} 张" if count else f"未选（共 {total} 张）"
        )
        for btn in (self.iso_del_btn, self.iso_replace_btn):
            btn.setEnabled(count > 0)

    def _isolate_targets(self) -> list[int]:
        """抽离视图里要动手的图 = 勾选的那些。"""
        return self.grid.selected_ids()

    # ------------------------------------------------------------------ #
    # 删图片（连 txt 一起进垃圾桶，不真删）
    # ------------------------------------------------------------------ #
    def _confirm_delete_all(self, count: int) -> bool:
        """全队列删除的二次确认。要输入「全部删除」才放行。

        用独立的 QDialog 而不是 QMessageBox —— QMessageBox 里塞输入框，
        按钮角色和布局都不好使，还容易在离屏测试里卡住。
        """
        dialog = QDialog(self)
        dialog.setWindowTitle("等等，你要删掉整个队列")
        dialog.setMinimumWidth(420)
        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        head = QLabel(f"你选中了队列里<b>全部 {count} 张</b>图片。")
        head.setWordWrap(True)
        lay.addWidget(head)

        warn = QLabel(
            "这通常不是想要的操作。\n\n"
            "图片会进垃圾桶（还能还原），但整个队列一起消失，\n"
            "多半说明是点错了「全选」。"
        )
        warn.setWordWrap(True)
        lay.addWidget(warn)

        tip = QLabel("确实要删的话，请在下面输入 <b>全部删除</b> 四个字：")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        edit = QLineEdit()
        edit.setPlaceholderText("在这里输入：全部删除")
        lay.addWidget(edit)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("Segment")
        cancel.clicked.connect(dialog.reject)
        buttons.addWidget(cancel)

        ok = QPushButton("确定删")
        ok.setObjectName("GhostDanger")
        ok.setEnabled(False)
        buttons.addWidget(ok)
        lay.addLayout(buttons)

        def _check(text: str) -> None:
            ok.setEnabled(text.strip() == "全部删除")

        edit.textChanged.connect(_check)
        edit.returnPressed.connect(
            lambda: dialog.accept() if edit.text().strip() == "全部删除" else None
        )
        ok.clicked.connect(dialog.accept)

        edit.setFocus()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _trash_selected(self) -> None:
        """把勾选的图片连同配套 txt 移进垃圾桶。

        这里**只移文件**，磁盘上的标注也可能还没保存 —— 所以删之前先把
        对应的暂存改动丢掉，免得回头「保存」又把记录写回一个不存在的文件。
        """
        ids = self.grid.selected_ids()
        if not ids:
            self.ctx.notify("先勾选要删除的图片")
            return

        rows_by_id = {int(r.get("id") or 0): r for r in self.grid.rows()}
        targets = [(i, rows_by_id.get(i)) for i in ids]
        targets = [(i, r) for i, r in targets if r is not None]
        if not targets:
            self.ctx.notify("找不到要删除的图片记录")
            return

        dataset_name = self.dataset_combo.currentText().strip() or "未命名"
        n_txt = sum(
            1 for _i, r in targets
            if Path(str(r.get("path") or "")).with_suffix(".txt").exists()
        )

        # 一次删掉**整个队列**多半是误操作 —— 正常打标不会这么干。
        # 起因：我的截图脚本调 select_all() 然后删除，把用户 789 张图全搬走了。
        #
        # 但只有队列"够大"时才拦：队列里就一两张图的时候，全选删掉是正常操作，
        # 还弹护栏只会烦人（也会把自动化测试卡死）。
        _GUARD_MIN = 5
        total_in_queue = self.grid.model.rowCount()
        if (
            total_in_queue >= _GUARD_MIN
            and len(targets) >= total_in_queue
        ):
            if not self._confirm_delete_all(len(targets)):
                self.ctx.notify("已取消，什么都没删")
                return
        elif self.settings.confirm_before_delete:
            box = QMessageBox(self)
            box.setWindowTitle("删除图片")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(f"把选中的 {len(targets)} 张图片备份到 bin 文件夹？")
            detail = (
                f"配套的 {n_txt} 个 txt 标注会一起移走。\n\n"
                f"文件不会被彻底删除，只会移到备份目录：{trash.trash_root()}\n"
                "图片和同名 txt 会一起备份。"
            )
            if n_txt < len(targets):
                detail += f"\n\n其中 {len(targets) - n_txt} 张没有配套 txt。"
            box.setInformativeText(detail)
            yes = box.addButton("移进垃圾桶", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(yes)
            box.exec()
            if box.clickedButton() is not yes:
                return

        items = [(str(r.get("path") or ""), dataset_name) for _i, r in targets]
        items = [(p, d) for p, d in items if p]
        moved, errors = trash.trash_images(
            items,
            note="工作台删除",
            dataset_name=dataset_name,
            keep_structure=bool(getattr(self.settings, "trash_keep_structure", False)),
        )

        # 只清理成功移走的图片；失败项仍保留在队列和暂存区。
        for image_id, row in targets:
            image_path = Path(str((row or {}).get("path") or ""))
            if image_path.exists():
                continue
            try:
                self.db.discard_caption(image_id)
            except AttributeError:
                pass
            try:
                self.db.delete_image(image_id)
            except Exception:
                pass

        self._update_staged()
        self.reload_queue()
        for _ in range(2):
            QApplication.processEvents()

        if errors:
            QMessageBox.warning(
                self,
                "部分没删成",
                f"已移动 {moved} 个文件，但有 {len(errors)} 个失败：\n\n"
                + "\n".join(errors[:8]),
            )
        self.ctx.notify(
            f"已把 {len(targets)} 张图（{moved} 个文件）移进垃圾桶，"
            f"备份位置：{trash.trash_root()}"
        )

    def _after_tag_change(self, message: str) -> None:
        """删除/替换之后的统一收尾：刷标记、重筛、重渲染。

        滚动位置和当前图由 ``ThumbGrid.set_rows(keep_view=True)`` 自己保住，
        这里不要再 scrollTo，否则视野会跳。
        """
        self._update_staged()
        self._refresh_queue_marks()
        if self._isolated_tag:
            self._load_isolated(self._isolated_tag, keep_view=True)
            if not self.grid.model.rowCount():
                # 这个标签已经被处理干净了，退回队列更有用
                self._exit_isolated()
                self.ctx.notify(message + "　·　该标签已无剩余图片，已返回队列")
                return
        current_id = self.grid.current_id()
        rows = self.grid.rows()
        self._current = next(
            (r for r in rows if int(r.get("id") or 0) == current_id), None
        )
        self._render_current()
        self.ctx.notify(message)

    def _isolate_delete_tag(self) -> None:
        tag = self._isolated_tag
        ids = self._isolate_targets()
        if not tag or not ids:
            self.ctx.notify("先勾选要处理的图片")
            return
        box = QMessageBox(self)
        box.setWindowTitle("删除标签")
        box.setText(f"从选中的 {len(ids)} 张图里删掉「{tag}」？")
        box.setInformativeText("改的是暂存区，还要点「保存到 txt」才落盘。")
        box.addButton("删除", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not box.buttons()[0]:
            return
        affected = self._apply_tag_change(ids, tag, None)
        self._after_tag_change(
            f"已从 {affected} 张图里删除「{tag}」（还没保存）"
        )

    def _isolate_replace_tag(self) -> None:
        tag = self._isolated_tag
        ids = self._isolate_targets()
        if not tag or not ids:
            self.ctx.notify("先勾选要处理的图片")
            return
        new_tag = self._ask_new_tag(tag)
        if not new_tag or new_tag == tag:
            return
        affected = self._apply_tag_change(ids, tag, new_tag)
        self._after_tag_change(
            f"已把 {affected} 张图里的「{tag}」换成「{new_tag}」（还没保存）"
        )

    def _apply_tag_change(self, ids: list[int], old: str, new: str | None) -> int:
        """把 ids 里所有 old 标签删掉（new=None）或换成 new。返回改动张数。"""
        affected = 0
        for image_id in ids:
            edit = self.db.caption_edit(image_id, self._path_of(image_id))
            current = list(edit.current)
            if old not in current:
                continue
            if new is None:
                updated = [t for t in current if t != old]
            else:
                updated = []
                for t in current:
                    if t == old:
                        if new not in updated:      # 替换也要去重
                            updated.append(new)
                    elif t not in updated:
                        updated.append(t)
            if updated != current:
                self.db.set_caption_tags(image_id, edit.image_path, updated)
                affected += 1
        return affected

    def _ask_new_tag(self, old: str) -> str:
        """弹出输入框，带已有标签的下拉提示，防止拼错。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("替换标签")
        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        head = QLabel(f"把「<b>{old}</b>」替换成：")
        head.setWordWrap(True)
        lay.addWidget(head)

        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setMinimumWidth(360)
        combo.setMaxVisibleItems(18)
        # 已有标签做下拉提示（按用得多排前面），拼错也不怕
        for name, _count in self.db.all_caption_tags():
            if name != old:
                combo.addItem(name)
        combo.setCurrentText("")
        # 输入时按内容过滤，标签多的时候好用
        combo.completer().setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion
        )
        combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        combo.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        lay.addWidget(combo)

        tip = muted("可以直接输入新标签；下拉里是这批数据已有的标签。回车 = 替换。")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("Segment")
        cancel.clicked.connect(dialog.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("替换")
        ok.setObjectName("Primary")
        ok.setDefault(True)          # 让回车落到这个按钮上
        ok.setAutoDefault(True)
        ok.clicked.connect(dialog.accept)
        buttons.addWidget(ok)
        lay.addLayout(buttons)

        # 关键：可编辑 QComboBox 会把回车吃掉（它想自己选中下拉项），
        # 所以给它的输入框单独接一个回车 → 直接确认。
        # 用户报过：鼠标点「替换」好使，按回车没反应。
        def _submit() -> None:
            if combo.currentText().strip():
                dialog.accept()

        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.returnPressed.connect(_submit)

        combo.setFocus()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return ""
        return combo.currentText().strip()

    # ------------------------------------------------------------------ #
    # 写法转换（作用于选中的那批图）
    # ------------------------------------------------------------------ #
    def _convert_role_tags(self) -> None:
        self._run_conversion(
            captions.to_underscore_role_tag,
            "角色词 → 下划线",
            "角色词已统一成 anima 下划线写法",
        )

    def _convert_escaped_tags(self) -> None:
        self._run_conversion(
            captions.to_escaped_role_tag,
            "角色词 → 转义",
            "角色词已改回 Danbooru 转义写法",
        )

    def _convert_underscores(self) -> None:
        self._run_conversion(
            captions.letter_underscores_to_spaces,
            "下划线 → 空格",
            "下划线已换成空格",
        )

    def _run_wd14(self) -> None:
        """给选中的图跑 WD14，结果写暂存区。"""
        ids = self._selected_image_ids()
        if not ids:
            self.ctx.notify("先在队列里勾选要打标的图（Ctrl+点可多选）")
            return

        settings = self.ctx.settings
        if not getattr(settings, "wd14_enabled", True):
            self.ctx.notify("WD14 已关闭 —— 去设置页打开「启用」")
            return

        images: list[dict] = []
        for image_id in ids:
            path = self._path_of(image_id)
            if not path:
                continue
            if not Path(path).is_file():
                continue
            images.append({"id": image_id, "path": path})

        if not images:
            self.ctx.notify("选中的图在磁盘上找不到了")
            return

        # 一个已经在跑了就别再起一个，否则两个线程各加载一次 1.2GB 模型
        running = getattr(self, "_wd14_worker", None)
        if running is not None and running.isRunning():
            self.ctx.notify("WD14 正在跑，等这批完事")
            return

        config = wd14.TaggerConfig(
            model_path=settings.wd14_model,
            use_gpu=bool(settings.wd14_use_gpu),
            threshold=float(settings.wd14_threshold),
            character_threshold=float(settings.wd14_character_threshold),
            max_tags=int(settings.wd14_max_tags),
            batch_size=int(settings.wd14_batch_size),
            replace_underscore=bool(settings.wd14_replace_underscore),
            undesired={
                part.strip().lower()
                for part in str(settings.wd14_undesired or "").split(",")
                if part.strip()
            },
        )

        self._wd14_pending_ids = ids
        self.wd14_btn.setEnabled(False)
        self.wd14_btn.setText("打标中…")
        self.conv_hint.setText(f"WD14 准备中（{len(images)} 张）…")
        self.ctx.status.emit("WD14 打标中…")
        QApplication.processEvents()

        worker = Wd14Worker(images, config, parent=self)
        worker.loaded.connect(self._on_wd14_loaded)
        worker.progress.connect(self._on_wd14_progress)
        worker.finished_ok.connect(self._on_wd14_done)
        worker.failed.connect(self._on_wd14_failed)
        self._wd14_worker = worker
        worker.start()

    def _on_wd14_loaded(self, provider: str) -> None:
        self.conv_hint.setText(f"WD14 在跑（{provider}）…")

    def _on_wd14_progress(self, done: int, total: int, name: str) -> None:
        self.conv_hint.setText(f"WD14 {done}/{total} · {name}")

    def _on_wd14_failed(self, message: str) -> None:
        self.wd14_btn.setEnabled(True)
        self.wd14_btn.setText("WD14 打标")
        self.conv_hint.setText("WD14 出错了")
        self.ctx.status.emit("")
        QMessageBox.critical(self, "WD14 打标失败", message)

    def _on_wd14_done(self, results: dict, error: str) -> None:
        self.wd14_btn.setEnabled(True)
        self.wd14_btn.setText("WD14 打标")
        self.ctx.status.emit("")

        if error:
            self.conv_hint.setText("WD14 不可用")
            QMessageBox.warning(
                self,
                "WD14 不可用",
                f"{error}\n\n"
                "去设置页的「WD14 本地打标」看看状态，"
                "点「检测引擎」能看到具体原因。",
            )
            return

        if not results:
            self.conv_hint.setText("WD14 没认出任何标签")
            self.ctx.notify("WD14 没认出标签（图太简单或阈值太高）")
            return

        append = self.wd14_mode.currentData() == "append"
        touched = 0
        added = 0
        for image_id, tags in results.items():
            path = self._path_of(image_id)
            if not path:
                continue
            try:
                if append:
                    current = list(self.db.caption_current(image_id, path) or [])
                    seen = {t.strip().lower() for t in current}
                    fresh = [t for t in tags if t.strip().lower() not in seen]
                    if not fresh:
                        continue
                    merged = current + fresh
                    added += len(fresh)
                else:
                    merged = list(tags)
                    added += len(tags)
                self.db.set_caption_tags(image_id, path, merged)
                touched += 1
            except Exception:  # noqa: BLE001
                continue

        self._update_staged()
        self._refresh_queue_marks()
        if self._current is not None:
            self._render_current()

        if not touched:
            self.conv_hint.setText("没有需要写入的标签")
            self.ctx.notify("WD14 打标完成，没有新标签要写（追加模式下可能已存在）")
            return

        mode_note = "追加" if append else "替换"
        self.conv_hint.setText(f"已{mode_note} {touched} 张")
        self.ctx.notify(
            f"WD14 打标完成：{touched} 张图，{added} 个标签（{mode_note}），"
            "已进暂存区，确认后保存到 txt"
        )

    def _run_conversion(self, converter, action: str, done_note: str) -> None:
        """对选中的图批量跑一次标签转换。"""
        ids = self._selected_image_ids()
        if not ids:
            self.ctx.notify("先在队列里勾选要转换的图（Ctrl+点可多选）")
            return

        report = captions.ConvertReport()
        for image_id in ids:
            path = self._path_of(image_id)
            if not path:
                continue
            try:
                current = self.db.caption_current(image_id, path)
            except Exception:  # noqa: BLE001
                continue
            if not current:
                continue
            converted, changed = captions.convert_tags(
                current, converter, filename=Path(path).name, report=report
            )
            if changed:
                self.db.set_caption_tags(image_id, path, converted)
                report.images_touched += 1
                report.tags_changed += changed

        self._update_staged()
        self._refresh_queue_marks()
        if self._current is not None:
            self._render_current()

        if not report.images_touched:
            self.ctx.notify(f"{action}：选中的图里没有需要转换的标签")
            self.conv_hint.setText("没有需要转换的")
            return

        self.conv_hint.setText(report.summary(action))
        # 把前几个改动摆出来，让用户能核对
        detail = "\n".join(f"  {old}  →  {new}" for _f, old, new in report.samples[:6])
        box = QMessageBox(self)
        box.setWindowTitle(action)
        box.setText(report.summary(action))
        box.setInformativeText(
            f"{done_note}（进暂存区，点「保存到 txt」才落盘）。\n\n"
            f"例如：\n{detail}"
            + ("\n  …" if report.tags_changed > len(report.samples) else "")
        )
        box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
        box.exec()

    def _preview_pixmap(self, path: str) -> QPixmap | None:
        """加载预览图：先试缓存缩略图，再退回原图。"""
        try:
            stat = Path(path).stat()
        except OSError:
            return None
        thumb = thumbs.get_thumb(path, stat.st_mtime, 900)
        if thumb is not None:
            pixmap = QPixmap(str(thumb))
            if not pixmap.isNull():
                return pixmap
        pixmap = QPixmap(path)
        return pixmap if not pixmap.isNull() else None

    def _path_of(self, image_id: int) -> str:
        for row in self._rows:
            if int(row.get("id") or 0) == image_id:
                return str(row.get("path") or "")
        found = self.db.find_by_id(image_id)
        return str(found["path"]) if found else ""

    # ------------------------------------------------------------------ #
    # 保存 / 放弃
    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        edits = self.db.caption_edits()
        if not edits:
            self.ctx.notify("没有需要保存的改动")
            return
        preview = captions.preview_diff(edits)
        box = QMessageBox(self)
        box.setWindowTitle("保存标注")
        box.setText(f"把 {len(edits)} 张图的改动写回配套 txt？")
        box.setInformativeText(
            preview[:1500] + ("\n..." if len(preview) > 1500 else "")
            + "\n\n原文件会先备份（同名 + 时间戳 + .bak）。"
        )
        ok_btn = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not ok_btn:
            return
        saved, errors = self.db.save_captions(
            edits, make_backup=bool(self.settings.caption_backup)
        )
        self._update_staged()
        self.reload_queue()
        if errors:
            QMessageBox.warning(self, "部分保存失败", "\n".join(errors[:12]))
        self.ctx.notify(f"已保存 {saved} 个标注文件")

    def _revert_all(self) -> None:
        count = self.db.pending_caption_count()
        if count <= 0:
            return
        box = QMessageBox(self)
        box.setWindowTitle("放弃改动")
        box.setText(f"放弃 {count} 张图的未保存改动？")
        box.setInformativeText("这些改动还没写进 txt，放弃后无法恢复。")
        yes = box.addButton("放弃", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            return
        removed = self.db.discard_all_captions()
        self._update_staged()
        self.reload_queue()
        self.ctx.notify(f"已放弃 {removed} 处改动")

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self._reload_datasets()
        if not self._rows:
            self.reload_queue()
        self._update_staged()
        self._refresh_clipboard()

    def on_payload(self, payload: object) -> None:
        if isinstance(payload, dict) and payload.get("select_id"):
            self._select_image(int(payload["select_id"]))

    def _select_image(self, image_id: int) -> None:
        self.grid.select_image(image_id)
