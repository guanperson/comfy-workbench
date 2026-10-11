"""右侧检查器：没选图时显示运行状态与快捷键，选了图就变成详情面板。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import captions, paths, thumbs
from . import theme
from .pages.base import AppContext
from .widgets import (
    InfoRow,
    TagChipBar,
    elide,
    hline,
    muted,
)

SHORTCUTS = [
    ("命令面板", "Ctrl K"),
    ("切换视图", "Ctrl 1-6"),
    ("检视面板", "Ctrl B"),
    ("全选", "Ctrl A"),
    ("选择图片", "Space"),
    ("查看大图", "Enter / 双击"),
    ("取消选择", "Esc"),
    ("自动打标", "Ctrl T"),
    ("刷新", "F5"),
]


class Inspector(QFrame):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.db = ctx.db
        self.setObjectName("Panel")
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        self.head_title = QLabel("状态与快捷键")
        self.head_title.setObjectName("CardTitle")
        head.addWidget(self.head_title)
        head.addStretch(1)
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("Segment")
        self.close_btn.setFixedWidth(30)
        self.close_btn.clicked.connect(self.hide_panel)
        head.addWidget(self.close_btn)
        root.addLayout(head)

        self.stack = QStackedWidget()
        self.status_page = self._build_status_page()
        self.detail_page = self._build_detail_page()
        self.stack.addWidget(self.status_page)
        self.stack.addWidget(self.detail_page)
        root.addWidget(self.stack, 1)

        self.ctx.selection_changed.connect(self.show_image)
        self.ctx.images_changed.connect(self._on_images_changed)

    # ------------------------------------------------------------------ #
    # 状态页
    # ------------------------------------------------------------------ #
    def _build_status_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)

        # 运行状态
        run = QFrame()
        run.setObjectName("Card")
        rcol = QVBoxLayout(run)
        rcol.setContentsMargins(12, 11, 12, 11)
        rcol.setSpacing(7)
        head = QHBoxLayout()
        head.addWidget(QLabel("运行状态"))
        head.addStretch(1)
        self.api_badge = QLabel("已配置")
        self.api_badge.setStyleSheet(
            f"background: {theme.GREEN}22; color: {theme.GREEN};"
            f" border: 1px solid {theme.GREEN}55; border-radius: 9px;"
            f" padding: 1px 9px; font-size: 11px;"
        )
        head.addWidget(self.api_badge)
        rcol.addLayout(head)
        rcol.addWidget(muted("打标服务"))

        self.row_key = InfoRow("密钥", "—")
        self.row_endpoint = InfoRow("端点", "—")
        self.row_model = InfoRow("模型", "—")
        self.row_conc = InfoRow("并发 / 超时", "—")
        for row in (self.row_key, self.row_endpoint, self.row_model, self.row_conc):
            rcol.addWidget(row)
        col.addWidget(run)

        # 本地数据
        data = QFrame()
        data.setObjectName("Card")
        dcol = QVBoxLayout(data)
        dcol.setContentsMargins(12, 11, 12, 11)
        dcol.setSpacing(6)
        dcol.addWidget(QLabel("本地数据"))
        self.row_images = InfoRow("图片", "0 张")
        self.row_datasets = InfoRow("数据集", "0 个")
        self.row_terms = InfoRow("标签词条", "0 个")
        self.row_tagged = InfoRow("已有标签", "0 张")
        self.row_meta = InfoRow("含提示词", "0 张")
        for row in (self.row_images, self.row_datasets, self.row_terms,
                    self.row_tagged, self.row_meta):
            dcol.addWidget(row)

        self.path_label = QLabel("—")
        self.path_label.setObjectName("Mono")
        self.path_label.setWordWrap(True)
        self.path_label.setStyleSheet(
            f"background: {theme.BG}; border: 1px solid {theme.BORDER_SOFT};"
            f" border-radius: 6px; padding: 7px 8px; color: {theme.TEXT_DIM};"
        )
        dcol.addWidget(self.path_label)

        open_btn = QPushButton("📂  打开数据目录")
        open_btn.clicked.connect(lambda: paths.open_in_explorer(paths.data_dir()))
        dcol.addWidget(open_btn)
        col.addWidget(data)

        # 快捷键
        keys = QFrame()
        keys.setObjectName("Card")
        kcol = QVBoxLayout(keys)
        kcol.setContentsMargins(12, 11, 12, 11)
        kcol.setSpacing(6)
        kcol.addWidget(QLabel("快捷键"))
        self._shortcut_rows: list[InfoRow] = []
        for name, combo in SHORTCUTS:
            row = InfoRow(name, combo)
            kcol.addWidget(row)
            self._shortcut_rows.append(row)
        col.addWidget(keys)

        # 存储
        store = QFrame()
        store.setObjectName("Card")
        scol = QVBoxLayout(store)
        scol.setContentsMargins(12, 11, 12, 11)
        scol.setSpacing(5)
        scol.addWidget(QLabel("存储"))
        self.cache_label = muted("缩略图缓存 —")
        self.cache_label.setWordWrap(True)
        scol.addWidget(self.cache_label)
        col.addWidget(store)

        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    # ------------------------------------------------------------------ #
    # 详情页
    # ------------------------------------------------------------------ #
    def _build_detail_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(9)

        self.preview = QLabel("—")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(170)
        self.preview.setStyleSheet(
            f"background: {theme.BG}; border: 1px solid {theme.BORDER_SOFT};"
            f" border-radius: 8px; color: {theme.TEXT_FAINT};"
        )
        col.addWidget(self.preview, 2)

        self.file_title = QLabel("—")
        self.file_title.setObjectName("CardTitle")
        self.file_title.setWordWrap(True)
        col.addWidget(self.file_title)

        self.file_meta = muted("—")
        self.file_meta.setWordWrap(True)
        col.addWidget(self.file_meta)

        status_row = QHBoxLayout()
        status_row.addWidget(muted("状态"))
        self.status_label = QLabel("—")
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        self.source_label = muted("")
        status_row.addWidget(self.source_label)
        col.addLayout(status_row)

        col.addWidget(hline())

        tag_head = QHBoxLayout()
        tag_head.addWidget(QLabel("标签"))
        tag_head.addStretch(1)
        self.tag_count = muted("0")
        tag_head.addWidget(self.tag_count)
        col.addLayout(tag_head)

        self.tag_bar = TagChipBar()
        self.tag_bar.tag_added.connect(self._add_tag)
        self.tag_bar.tag_removed.connect(self._remove_tag)
        self.tag_bar.tag_banned.connect(self._ban_tag)
        col.addWidget(self.tag_bar)

        col.addWidget(hline())

        # 正向提示词标题右边挂一个「读取方式」下拉。
        # 老图的工作流千奇百怪（自研节点、多层细化），自动认有时会认反；
        # 放到这里就能当场切换、立刻看结果，不用跑去设置页改了再回来。
        prompt_head = QHBoxLayout()
        prompt_head.setContentsMargins(0, 0, 0, 0)
        prompt_head.setSpacing(6)
        prompt_head.addWidget(muted("正向提示词"))
        prompt_head.addStretch(1)
        self.parser_combo = QComboBox()
        for text, value in (
            ("自动适应", "auto"),
            ("ComfyUI", "comfyui"),
            ("CLIPTextEncodeAdv", "bnk"),
            ("WeiLinComfyUIPrompt", "weilin"),
            ("NovelAI", "nai"),
        ):
            self.parser_combo.addItem(text, value)
            self.parser_combo.setItemData(
                self.parser_combo.count() - 1,
                {
                    "自动适应": "顺着工作流连线找真正的正向/反向（默认）",
                    "ComfyUI": "强制按 ComfyUI 元数据读",
                    "CLIPTextEncodeAdv": "优先取 BNK_CLIPTextEncodeAdvanced 节点的词",
                    "WeiLinComfyUIPrompt": "优先取 WeiLin 系节点的词",
                    "NovelAI": "强制按 NAI 的 Comment / Description 读",
                }[text],
                Qt.ItemDataRole.ToolTipRole,
            )
        self.parser_combo.setMinimumWidth(120)
        self.parser_combo.setMaximumWidth(168)
        self.parser_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.parser_combo.setToolTip(
            "这张图的提示词按哪套规则读。默认「自动适应」会顺着工作流连线\n"
            "找真正的正向/反向；万一认反了，在这里手动指定。"
        )
        self.parser_combo.currentIndexChanged.connect(self._on_parser_changed)
        prompt_head.addWidget(self.parser_combo)
        col.addLayout(prompt_head)

        self.prompt_view = QPlainTextEdit()
        self.prompt_view.setReadOnly(True)
        self.prompt_view.setFixedHeight(90)
        col.addWidget(self.prompt_view)

        col.addWidget(muted("反向提示词"))
        self.negative_view = QPlainTextEdit()
        self.negative_view.setReadOnly(True)
        self.negative_view.setFixedHeight(56)
        col.addWidget(self.negative_view)

        # ---- Caption（配套 txt 的原文）----
        # 正向/反向是 PNG 里内嵌的元数据，caption 是训练真正用的那个 txt。
        # 两者经常不一样（元数据是出图时的，caption 可能是手改或打标生成的），
        # 所以这里单独列出来，并且**始终显示** —— 之前只在没有元数据时才显示，
        # 导图库时想复制 caption 还得先去打标台，很别扭。
        self.caption_label = muted("Caption（配套 txt）")
        col.addWidget(self.caption_label)

        self.caption_view = QPlainTextEdit()
        self.caption_view.setReadOnly(True)
        self.caption_view.setPlaceholderText("这张图没有配套的 txt")
        self.caption_view.setFixedHeight(96)
        self.caption_view.setObjectName("Mono")
        self.caption_view.setToolTip(
            "这里显示 <图片名>.txt 的原文，可以直接选中复制（Ctrl+A / Ctrl+C）。\n"
            "下面「复制 Caption」是一键整段复制。"
        )
        col.addWidget(self.caption_view)

        self.caption_hint = muted("")
        self.caption_hint.setWordWrap(True)
        self.caption_hint.hide()
        col.addWidget(self.caption_hint)

        # 老的「打标标签」视图保留（暂存区里的未保存标签），
        # 但不再抢 caption 的位置，放在下面单独提示。
        self.caption_tags_label = muted("打标标签（未保存）")
        self.caption_tags_label.hide()
        col.addWidget(self.caption_tags_label)
        self.caption_tags_view = QPlainTextEdit()
        self.caption_tags_view.setReadOnly(True)
        self.caption_tags_view.setPlaceholderText("暂无打标标签")
        self.caption_tags_view.setFixedHeight(72)
        self.caption_tags_view.hide()
        self.caption_tags_view.setToolTip("这里显示打标台标注，可点击下方复制标签")
        col.addWidget(self.caption_tags_view)

        copy_prompt = QPushButton("复制提示词")
        copy_prompt.setObjectName("Segment")
        copy_prompt.clicked.connect(self._copy_prompt)
        # 检视面板窄（默认 280px），一行塞 5 个按钮会把字压没，
        # 所以拆成两行：第一行是「读」相关的复制，第二行是定位/打开。
        row1 = QHBoxLayout()
        row1.addWidget(copy_prompt)
        self.copy_caption_full_btn = QPushButton("复制 Caption")
        self.copy_caption_full_btn.setObjectName("Primary")
        self.copy_caption_full_btn.setToolTip("把上面 Caption 那一整段原文复制到剪贴板")
        self.copy_caption_full_btn.clicked.connect(self._copy_caption_full)
        row1.addWidget(self.copy_caption_full_btn)
        self.copy_tags_btn = QPushButton("复制标签")
        self.copy_tags_btn.setObjectName("Segment")
        self.copy_tags_btn.clicked.connect(self._copy_tags)
        row1.addWidget(self.copy_tags_btn)
        col.addLayout(row1)

        row2 = QHBoxLayout()
        self.copy_caption_btn = QPushButton("复制打标标签")
        self.copy_caption_btn.setObjectName("Segment")
        self.copy_caption_btn.clicked.connect(self._copy_caption_tags)
        self.copy_caption_btn.hide()
        row2.addWidget(self.copy_caption_btn)
        reveal = QPushButton("定位文件")
        reveal.setObjectName("Segment")
        reveal.clicked.connect(self._reveal)
        row2.addWidget(reveal)
        col.addLayout(row2)

        outside = QHBoxLayout()
        open_wb = QPushButton("在打标台打开")
        open_wb.setObjectName("Primary")
        open_wb.clicked.connect(self._open_workbench)
        outside.addWidget(open_wb)
        back = QPushButton("返回状态")
        back.setObjectName("Segment")
        back.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        outside.addWidget(back)
        col.addLayout(outside)

        col.addStretch(1)
        return page

    # ------------------------------------------------------------------ #
    def hide_panel(self) -> None:
        self.setVisible(False)
        self.ctx.notify("检视面板已隐藏（Ctrl B 再打开）")

    def toggle(self) -> None:
        self.setVisible(not self.isVisible())
        if self.isVisible():
            self.refresh_status()

    # ------------------------------------------------------------------ #
    def refresh_status(self) -> None:
        s = self.ctx.settings
        stats = self.db.stats()

        if s.has_api_key():
            self.api_badge.setText("已配置")
            self.api_badge.setStyleSheet(
                f"background: {theme.GREEN}22; color: {theme.GREEN};"
                f" border: 1px solid {theme.GREEN}55; border-radius: 9px;"
                f" padding: 1px 9px; font-size: 11px;"
            )
        else:
            self.api_badge.setText("未配置")
            self.api_badge.setStyleSheet(
                f"background: {theme.RED}22; color: {theme.RED};"
                f" border: 1px solid {theme.RED}55; border-radius: 9px;"
                f" padding: 1px 9px; font-size: 11px;"
            )

        self.row_key.set_value(s.key_source())
        self.row_endpoint.set_value(elide(s.endpoint(), 38))
        self.row_model.set_value(s.model or "—")
        self.row_conc.set_value(f"{s.concurrency} · {s.timeout}s")

        self.row_images.set_value(f"{stats['images']:,} 张")
        self.row_datasets.set_value(f"{stats['datasets']:,} 个")
        self.row_terms.set_value(f"{stats['tag_terms']:,} 个")
        self.row_tagged.set_value(
            f"{stats['tagged']:,} 张（{int(stats['tagged_ratio'] * 100)}%）"
        )
        self.row_meta.set_value(f"{stats['with_meta']:,} 张")
        self.path_label.setText(str(paths.data_dir()))
        self._refresh_cache_label()

    def _refresh_cache_label(self) -> None:
        """数缓存要扫目录，别每次切页都扫。"""
        import time as _t  # noqa: PLC0415

        now = _t.time()
        if getattr(self, "_cache_checked_at", 0.0) and now - self._cache_checked_at < 5.0:
            return
        self._cache_checked_at = now
        self.cache_label.setText(
            f"缩略图缓存：{thumbs.cache_count():,} 个文件，"
            f"{thumbs.human_size(thumbs.cache_size_bytes())}"
        )

    def _on_images_changed(self) -> None:
        if self.stack.currentIndex() == 0 and self.isVisible():
            self.refresh_status()

    # ------------------------------------------------------------------ #
    def show_image(self, row: dict | None) -> None:
        if not row:
            self.stack.setCurrentIndex(0)
            self.head_title.setText("状态与快捷键")
            self.refresh_status()
            return

        self.head_title.setText("图片详情")
        self.stack.setCurrentIndex(1)

        self.file_title.setText(elide(str(row.get("filename", "")), 40))
        self.file_title.setToolTip(str(row.get("path", "")))

        meta = row.get("meta") or {}
        bits = []
        if row.get("width"):
            bits.append(f"{row['width']}×{row['height']}")
        if row.get("size"):
            bits.append(thumbs.human_size(int(row["size"])))
        if meta.get("checkpoint_short"):
            bits.append(str(meta["checkpoint_short"]))
        if meta.get("sampler"):
            bits.append(str(meta["sampler"]))
        if meta.get("steps"):
            bits.append(f"steps {meta['steps']}")
        if meta.get("cfg"):
            bits.append(f"cfg {meta['cfg']}")
        if meta.get("seed") is not None:
            bits.append(f"seed {meta['seed']}")
        for lora in (meta.get("loras") or [])[:4]:
            bits.append(f"LoRA {lora.get('short', '?')}")
        self.file_meta.setText(" · ".join(str(b) for b in bits) or "无生成参数")

        status = str(row.get("status") or "pending")
        color = theme.STATUS_COLORS.get(status, theme.TEXT_DIM)
        label = {"tagged": "已打标", "pending": "待打标", "skip": "已跳过"}.get(status, status)
        self.status_label.setText(label)
        self.status_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.source_label.setText(
            "" if row.get("has_meta") else "未读到内嵌元数据"
        )

        path = Path(str(row.get("path", "")))
        pixmap = QPixmap(str(path)) if path.exists() else QPixmap()
        if pixmap.isNull():
            self.preview.setText("图片读不出来")
            self.preview.setPixmap(QPixmap())
        else:
            self.preview.setPixmap(
                pixmap.scaled(
                    max(self.preview.width(), 240),
                    max(self.preview.height(), 170),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

        self._image_id = int(row.get("id") or 0)
        self._image_path = str(row.get("path") or "")
        self._row = row

        # 下拉框跟着设置走，但用户在这里改过之后就以这里的为准
        # （改设置页会重新同步，见 sync_parser）
        if not getattr(self, "_parser_touched", False):
            self._select_parser(getattr(self.ctx.settings, "meta_parser", "auto"))
            self._parser_touched = False

        self._load_prompt_text(row)
        self._load_caption()
        # 老的「打标标签」区块已经不再需要：
        # 上面那个 Caption 框已经把 txt 原文完整显示出来了，
        # 再摆一个切分后的标签列表只是同一份内容显示两遍。
        self.caption_tags_label.hide()
        self.caption_tags_view.hide()
        self.copy_caption_btn.hide()
        self._reload_tags()

    def _load_prompt_text(self, row: dict | None = None) -> None:
        """按当前「读取方式」重新读正向/反向。

        ``parser`` 是 ``auto`` 时用图库里已经解析好的结果（扫描时算过，
        省一次磁盘读）；选成具体引擎时才现读，因为要换规则。
        """
        row = row if row is not None else getattr(self, "_row", None)
        if not row:
            self.prompt_view.setPlainText("")
            self.negative_view.setPlainText("")
            return

        parser = self._current_parser()
        path = str(row.get("path") or "")

        if parser == "auto":
            prompt = str(row.get("prompt") or "")
            negative = str(row.get("negative") or "")
            has_meta = bool(row.get("has_meta"))
            hint = "" if has_meta else "未读到内嵌元数据"
        else:
            prompt, negative, has_meta = self._reread_with(path, parser)
            if has_meta:
                hint = ""
            elif prompt or negative:
                hint = ""
            else:
                # 用户手动指定了引擎却没读出东西：八成是这张图不是那套格式
                hint = f"按「{self.parser_combo.currentText()}」没读到提示词"

        self.prompt_view.setPlainText(prompt)
        self.negative_view.setPlainText(negative)
        self.source_label.setText(hint)

    def _reread_with(self, path: str, parser: str) -> tuple[str, str, bool]:
        """用指定规则重新读一张图的提示词。读不出来就返回空。"""
        if not path or not Path(path).exists():
            return "", "", False
        try:
            from .. import comfy_meta  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return "", "", False

        engine = {"comfyui": "comfyui", "nai": "nai"}.get(parser, "comfyui")
        try:
            parsed = comfy_meta.extract(path, parser=engine)
        except Exception:  # noqa: BLE001
            return "", "", False

        prompt = str(parsed.get("prompt") or "")
        negative = str(parsed.get("negative") or "")

        # bnk / weilin 是「在这张图里优先用哪个节点的词」：
        # 只换正向的来源，反向仍用整图解析的结果 —— 否则选了这两个
        # 反向就空掉，用户会以为图没元数据。
        if parser in ("bnk", "weilin"):
            narrowed = self._reread_by_node_class(path, parser)
            if narrowed is not None:
                narrowed_pos, narrowed_neg = narrowed
                if narrowed_pos:
                    prompt = narrowed_pos
                if narrowed_neg:
                    negative = narrowed_neg

        return prompt, negative, bool(parsed.get("has_meta"))

    def _reread_by_node_class(self, path: str, parser: str) -> tuple[str, str] | None:
        """按指定节点类名挑提示词（CLIPTextEncodeAdvanced / WeiLin 系）。"""
        try:
            from .. import comfy_meta  # noqa: PLC0415
            import json  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None

        wanted = (
            {"BNK_CLIPTextEncodeAdvanced"}
            if parser == "bnk"
            else {n for n in comfy_meta.TEXT_NODES if n.lower().startswith("weilin")}
        )
        if not wanted:
            return None

        try:
            chunks = comfy_meta.read_png_text_chunks(path)
        except Exception:  # noqa: BLE001
            return None
        raw = chunks.get("prompt") or chunks.get("Prompt") or ""
        if not raw:
            return None
        try:
            graph = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(graph, dict):
            return None

        api = comfy_meta._ApiGraph(graph)
        prompt = ""
        negative = ""
        for node_id in api.graph:
            if api.class_type(node_id) not in wanted:
                continue
            ins = comfy_meta._as_inputs(api.node(node_id))
            for slot, is_neg in (("text", False), ("positive", False), ("negative", True)):
                val = ins.get(slot)
                if isinstance(val, str) and val.strip():
                    text = val.strip()
                else:
                    link = comfy_meta._link_target(val)
                    text = api.text_of(link) if link else ""
                if not text.strip():
                    continue
                if is_neg:
                    if not negative:
                        negative = text
                elif not prompt:
                    prompt = text
        if not prompt and not negative:
            return None
        return prompt, negative

    def _current_parser(self) -> str:
        combo = getattr(self, "parser_combo", None)
        if combo is None:
            return "auto"
        return str(combo.currentData() or "auto")

    def _select_parser(self, value: str) -> None:
        combo = getattr(self, "parser_combo", None)
        if combo is None:
            return
        idx = combo.findData(str(value))
        if idx >= 0 and idx != combo.currentIndex():
            blocked = combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(blocked)

    def sync_parser(self, value: str = "") -> None:
        """设置页改了「解析方式」后，把检视面板跟着对齐。"""
        value = value or str(getattr(self.ctx.settings, "meta_parser", "auto"))
        self._select_parser(value)
        self._parser_touched = False
        self._load_prompt_text()

    def _on_parser_changed(self, _index: int = 0) -> None:
        self._parser_touched = True
        self._load_prompt_text()

    def _load_caption(self) -> None:
        """把配套 txt 的原文填进 Caption 框。

        故意显示**文件原文**而不是切分后的标签列表 —— 用户要的就是能直接
        复制走的那段字，切完再加逗号空格反而跟文件里不一样了。

        没有 txt 但有暂存的改动（比如刚跑完 WD14 还没保存）时，
        显示**暂存的那份**，否则用户看不到自己刚打出来的标签。
        """
        image_path = getattr(self, "_image_path", "")
        if not image_path:
            self.caption_view.setPlainText("")
            self.caption_hint.hide()
            return

        image_id = getattr(self, "_image_id", 0)
        has_txt = captions.has_caption(image_path)
        text = captions.read_caption_text(image_path) if has_txt else ""

        # 暂存区里那份（可能没有）
        staged = None
        if image_id:
            try:
                staged = self.db.caption_current(image_id, image_path)
            except Exception:  # noqa: BLE001
                staged = None

        on_disk = captions.read_caption(image_path) if has_txt else []
        pending = False
        hint = ""
        if staged and staged != on_disk:
            # 有未保存的改动 —— 显示改过的那份，用户才看得见刚打出来的标签
            text = captions.join_caption(staged)
            pending = True
            hint = (
                "（还没保存到 txt，这里显示的是打过标的版本）"
                if not has_txt
                else "（有未保存的改动，这里显示的是改过的版本）"
            )
        elif not has_txt:
            hint = "没有配套 txt —— 可以去打标台打一次标"

        self.caption_view.setPlainText(text)

        if hint:
            self.caption_hint.setText(hint)
            self.caption_hint.show()
        else:
            self.caption_hint.hide()

    def _reload_tags(self) -> None:
        tags = self.db.tags_for_image(getattr(self, "_image_id", 0))
        self.tag_bar.set_tags(tags)
        self.tag_count.setText(f"{len(tags)} 个")
        if hasattr(self, "caption_tags_view"):
            image_id = getattr(self, "_image_id", 0)
            image_path = getattr(self, "_image_path", "")
            caption_tags = self.db.caption_current(image_id, image_path) if image_id and image_path else []
            self.caption_tags_view.setPlainText(", ".join(caption_tags))

    def _add_tag(self, name: str) -> None:
        image_id = getattr(self, "_image_id", 0)
        if not image_id:
            return
        if self.db.add_tags(image_id, [name], source="manual"):
            self._reload_tags()
            self.ctx.tags_changed.emit()
            self.ctx.notify(f"已添加标签「{name}」")

    def _remove_tag(self, name: str) -> None:
        image_id = getattr(self, "_image_id", 0)
        if not image_id:
            return
        self.db.remove_tag(image_id, name)
        self._reload_tags()
        self.ctx.notify(f"已移除标签「{name}」")

    def _ban_tag(self, name: str) -> None:
        self.db.set_tag_banned(name, True)
        self._reload_tags()
        self.ctx.tags_changed.emit()
        self.ctx.notify(f"「{name}」已加入屏蔽词")

    # ------------------------------------------------------------------ #
    def _copy_prompt(self) -> None:
        QApplication.clipboard().setText(self.prompt_view.toPlainText())
        self.ctx.notify("提示词已复制")

    def _copy_tags(self) -> None:
        QApplication.clipboard().setText(
            ", ".join(t["name"] for t in self.db.tags_for_image(getattr(self, "_image_id", 0)))
        )
        self.ctx.notify("标签已复制")

    def _copy_caption_tags(self) -> None:
        QApplication.clipboard().setText(self.caption_tags_view.toPlainText())
        self.ctx.notify("打标标签已复制")

    def _copy_caption_full(self) -> None:
        """整段复制 caption 原文。"""
        text = self.caption_view.toPlainText()
        if not text.strip():
            self.ctx.notify("这张图没有 caption 可以复制")
            return
        QApplication.clipboard().setText(text)
        self.ctx.notify("Caption 已复制")

    def _reveal(self) -> None:
        if not getattr(self, "_image_id", 0):
            return
        row = self.db.get_image(self._image_id)
        if row is None:
            return
        try:
            paths.reveal_file(str(row["path"]))
        except (OSError, RuntimeError) as exc:
            self.ctx.notify(f"定位文件失败：{exc}")

    def _open_workbench(self) -> None:
        if getattr(self, "_image_id", 0):
            self.ctx.go("workbench", {"select_id": self._image_id})
