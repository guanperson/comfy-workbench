"""设置：模型接口、打标行为、存储与导出默认值。"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import caption_gen, paths, thumbs, trash, wd14
from ...config import PROMPT_PRESETS, SETTINGS_FIELD_HELP, save_settings
from .base import BasePage
from .. import theme
from ..widgets import muted, progress_bar
from ..workers import PingWorker


class SettingsPage(BasePage):
    title = "设置"
    subtitle = "模型接口、打标行为与存储"

    saved = Signal()

    def build(self) -> None:
        outer = QVBoxLayout()
        outer.setSpacing(12)
        s = self.settings

        appearance = QGroupBox("外观 · 浅深主题与模糊背景")
        visual = QFormLayout(appearance)
        self.ui_theme = QComboBox()
        self.ui_theme.addItem("浅色（默认）", "light")
        self.ui_theme.addItem("深色", "dark")
        self.ui_theme.setCurrentIndex(1 if s.ui_theme == "dark" else 0)
        visual.addRow("主题", self.ui_theme)
        bg_row = QHBoxLayout()
        self.ui_background = QLineEdit(s.ui_background)
        self.ui_background.setReadOnly(True)
        self.ui_background.setPlaceholderText("无背景图片")
        bg_row.addWidget(self.ui_background, 1)
        choose_bg = QPushButton("选择图片")
        choose_bg.clicked.connect(self._pick_background)
        bg_row.addWidget(choose_bg)
        clear_bg = QPushButton("移除")
        clear_bg.clicked.connect(lambda: self.ui_background.clear())
        bg_row.addWidget(clear_bg)
        visual.addRow("背景图", bg_row)
        self.ui_blur = QSlider(Qt.Orientation.Horizontal)
        self.ui_blur.setRange(0, 80); self.ui_blur.setValue(s.ui_background_blur)
        visual.addRow("模糊强度", self.ui_blur)
        self.ui_shade = QSlider(Qt.Orientation.Horizontal)
        self.ui_shade.setRange(0, 100); self.ui_shade.setValue(s.ui_background_shade)
        visual.addRow("背景遮罩", self.ui_shade)
        self.ui_opacity = QSlider(Qt.Orientation.Horizontal)
        self.ui_opacity.setRange(45, 100); self.ui_opacity.setValue(s.ui_panel_opacity)
        visual.addRow("面板不透明度", self.ui_opacity)
        visual.addRow(QLabel("背景保存为应用数据中的压缩副本。调整后点底部“保存设置”。"))
        outer.addWidget(appearance)

        # ---------------- 模型接口 ----------------
        api_box = QGroupBox("打标模型接口（OpenAI 兼容）")
        form = QFormLayout(api_box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setSpacing(9)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.api_base = QLineEdit(s.api_base)
        self.api_base.setPlaceholderText("https://api.openai.com/v1")
        self.api_base.setToolTip(SETTINGS_FIELD_HELP["api_base"])
        form.addRow("Base URL", self.api_base)

        self.key_env = QLineEdit(s.api_key_env)
        self.key_env.setPlaceholderText("例如 OPENAI_API_KEY / FALO_API_KEY")
        self.key_env.setToolTip(SETTINGS_FIELD_HELP["api_key_env"])
        self.key_env.textChanged.connect(self._update_key_state)
        form.addRow("密钥环境变量", self.key_env)

        self.api_key = QLineEdit(s.api_key)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("留空则只用环境变量")
        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        key_row.addWidget(self.api_key, 1)
        self.show_key_btn = QPushButton("显示")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setObjectName("Segment")
        self.show_key_btn.setToolTip("仅切换屏幕显示方式，不会修改密钥")
        self.show_key_btn.toggled.connect(self._toggle_api_key_visibility)
        key_row.addWidget(self.show_key_btn)
        form.addRow("API 密钥", key_row)

        self.model = QLineEdit(s.model)
        self.model.setToolTip(SETTINGS_FIELD_HELP["model"])
        form.addRow("模型", self.model)

        row = QHBoxLayout()
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 600)
        self.timeout.setValue(s.timeout)
        self.timeout.setSuffix(" 秒")
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 16)
        self.concurrency.setValue(s.concurrency)
        self.concurrency.setSuffix(" 线程")
        self.retries = QSpinBox()
        self.retries.setRange(0, 5)
        self.retries.setValue(s.max_retries)
        row.addWidget(muted("超时"))
        row.addWidget(self.timeout)
        row.addSpacing(8)
        row.addWidget(muted("并发"))
        row.addWidget(self.concurrency)
        row.addSpacing(8)
        row.addWidget(muted("重试"))
        row.addWidget(self.retries)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        form.addRow("请求策略", holder)

        self.temp = QDoubleSpinBox()
        self.temp.setRange(0.0, 2.0)
        self.temp.setSingleStep(0.1)
        self.temp.setValue(s.temperature)
        self.temp.setMaximumWidth(140)
        self.temp.setToolTip("数值越低，标签结果越稳定；通常保持 0.2 即可")
        form.addRow("随机度", self.temp)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._test_connection)
        test_row.addWidget(self.test_btn)
        self.test_result = muted("还没测过")
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.test_result, 1)
        test_holder = QWidget()
        test_holder.setLayout(test_row)
        form.addRow("", test_holder)

        outer.addWidget(api_box)

        # ---------------- 打标行为 ----------------
        tag_box = QGroupBox("打标行为")
        tag_form = QFormLayout(tag_box)
        tag_form.setSpacing(9)

        self.preset = QComboBox()
        for name in PROMPT_PRESETS:
            self.preset.addItem(name, name)
        idx = self.preset.findData(s.preset)
        if idx >= 0:
            self.preset.setCurrentIndex(idx)
        self.preset.currentIndexChanged.connect(self._on_preset_change)
        tag_form.addRow("预设", self.preset)

        self.language = QComboBox()
        self.language.addItems(["英文", "中文"])
        self.language.setCurrentIndex(0 if not s.language.startswith("中") else 1)
        tag_form.addRow("标签语言", self.language)

        self.max_tags = QSpinBox()
        self.max_tags.setRange(5, 120)
        self.max_tags.setValue(s.max_tags)
        self.max_tags.setSuffix(" 个")
        self.max_tags.setMaximumWidth(140)
        tag_form.addRow("每图最多标签", self.max_tags)

        self.max_side = QSpinBox()
        self.max_side.setRange(256, 2048)
        self.max_side.setSingleStep(64)
        self.max_side.setValue(s.image_max_side)
        self.max_side.setSuffix(" px")
        self.max_side.setMaximumWidth(140)
        self.max_side.setToolTip(SETTINGS_FIELD_HELP["image_max_side"])
        tag_form.addRow("上传前缩放", self.max_side)

        self.auto_tag = QCheckBox("扫描完成后自动进入打标队列")
        self.auto_tag.setChecked(s.auto_tag_on_scan)
        tag_form.addRow("", self.auto_tag)

        outer.addWidget(tag_box)

        # ---------------- WD14 本地打标 ----------------
        wd_box = QGroupBox("WD14 本地打标（看图猜标签）")
        wd_col = QVBoxLayout(wd_box)
        wd_col.setSpacing(9)

        # 引擎状态条
        status_row = QHBoxLayout()
        self.wd_status = QLabel("还没检测")
        self.wd_status.setObjectName("Mono")
        self.wd_status.setWordWrap(True)
        status_row.addWidget(self.wd_status, 1)

        self.wd_probe_btn = QPushButton("检测引擎")
        self.wd_probe_btn.setToolTip("加载一次模型，看能不能用、走的是 GPU 还是 CPU")
        self.wd_probe_btn.clicked.connect(self._probe_wd14)
        status_row.addWidget(self.wd_probe_btn)
        wd_col.addLayout(status_row)

        wd_form = QFormLayout()
        wd_form.setSpacing(9)

        self.wd_enabled = QCheckBox("启用（没元数据的图用它兜底）")
        self.wd_enabled.setChecked(s.wd14_enabled)
        wd_form.addRow("", self.wd_enabled)

        # 模型选择 + 找模型
        model_row = QHBoxLayout()
        self.wd_model = QComboBox()
        self.wd_model.setMinimumWidth(220)
        self.wd_model.setToolTip("模型来自 ComfyUI 的 WD14 插件目录，程序不重复下载")
        model_row.addWidget(self.wd_model, 1)

        self.wd_browse_btn = QPushButton("找模型…")
        self.wd_browse_btn.setToolTip("指定一个 .onnx（同目录要有同名 .csv 词表）")
        self.wd_browse_btn.clicked.connect(self._pick_wd_model)
        model_row.addWidget(self.wd_browse_btn)

        self.wd_open_btn = QPushButton("打开目录")
        self.wd_open_btn.setToolTip("打开程序找模型的默认目录")
        self.wd_open_btn.clicked.connect(self._open_wd_dir)
        model_row.addWidget(self.wd_open_btn)
        wd_form.addRow("模型", model_row)

        self.wd_models_hint = muted("")
        wd_form.addRow("", self.wd_models_hint)

        # 控件都建好了再填内容
        self._reload_wd_models(s.wd14_model)

        # 阈值
        thr_row = QHBoxLayout()
        self.wd_threshold = QDoubleSpinBox()
        self.wd_threshold.setRange(0.05, 0.95)
        self.wd_threshold.setSingleStep(0.05)
        self.wd_threshold.setDecimals(2)
        self.wd_threshold.setValue(s.wd14_threshold)
        self.wd_threshold.setMaximumWidth(100)
        self.wd_threshold.setToolTip("普通词的分数线，越低出词越多也越吵")
        thr_row.addWidget(self.wd_threshold)

        self.wd_char_threshold = QDoubleSpinBox()
        self.wd_char_threshold.setRange(0.05, 0.99)
        self.wd_char_threshold.setSingleStep(0.05)
        self.wd_char_threshold.setDecimals(2)
        self.wd_char_threshold.setValue(s.wd14_character_threshold)
        self.wd_char_threshold.setMaximumWidth(100)
        self.wd_char_threshold.setToolTip(
            "角色名的分数线，要比普通词高——WD14 容易认错角色，宁缺毋滥"
        )
        thr_row.addWidget(muted("普通词"))
        thr_row.addWidget(self.wd_threshold)
        thr_row.addSpacing(10)
        thr_row.addWidget(muted("角色词"))
        thr_row.addWidget(self.wd_char_threshold)
        thr_row.addStretch(1)
        wd_form.addRow("阈值", thr_row)

        # 输出
        out_row = QHBoxLayout()
        self.wd_max_tags = QSpinBox()
        self.wd_max_tags.setRange(0, 200)
        self.wd_max_tags.setSpecialValueText("不限")
        self.wd_max_tags.setValue(s.wd14_max_tags)
        self.wd_max_tags.setMaximumWidth(100)
        self.wd_max_tags.setToolTip("0 = 不限")
        out_row.addWidget(self.wd_max_tags)
        out_row.addWidget(muted("个（0 = 不限）"))
        out_row.addSpacing(12)

        self.wd_batch = QSpinBox()
        self.wd_batch.setRange(1, 64)
        self.wd_batch.setValue(s.wd14_batch_size)
        self.wd_batch.setMaximumWidth(90)
        out_row.addWidget(muted("批"))
        out_row.addWidget(self.wd_batch)

        self.wd_underscore = QCheckBox("下划线转空格")
        self.wd_underscore.setChecked(s.wd14_replace_underscore)
        self.wd_underscore.setToolTip("long_hair → long hair")
        out_row.addSpacing(12)
        out_row.addWidget(self.wd_underscore)
        out_row.addStretch(1)
        wd_form.addRow("每图最多", out_row)

        self.wd_gpu = QCheckBox("优先用 GPU（失败自动回退 CPU）")
        self.wd_gpu.setChecked(s.wd14_use_gpu)
        wd_form.addRow("", self.wd_gpu)

        self.wd_undesired = QLineEdit(s.wd14_undesired)
        self.wd_undesired.setPlaceholderText("逗号分隔，例如：watermark, signature, artist name")
        self.wd_undesired.setToolTip("WD14 认出来的这些词直接丢掉，两种写法都认")
        wd_form.addRow("屏蔽词", self.wd_undesired)

        wd_col.addLayout(wd_form)

        wd_hint = muted(
            "原则：元数据优先，WD14 兜底。\n"
            "有 PNG 元数据的图直接用元数据（更准、不丢角色名）；\n"
            "手拍图、网图这类没元数据的，才让 WD14 看图猜。"
        )
        wd_col.addWidget(wd_hint)

        outer.addWidget(wd_box)

        # ---------------- 元数据解析 + 生成标注 ----------------
        meta_box = QGroupBox("元数据解析 / 生成标注")
        meta_col = QVBoxLayout(meta_box)
        meta_col.setSpacing(9)

        meta_form = QFormLayout()
        meta_form.setSpacing(9)

        self.meta_parser = QComboBox()
        self.meta_parser.addItem("自动识别（先认 NAI，再按 ComfyUI）", "auto")
        self.meta_parser.addItem("强制 ComfyUI", "comfyui")
        self.meta_parser.addItem("强制 NAI（NovelAI）", "nai")
        idx = self.meta_parser.findData(s.meta_parser)
        if idx >= 0:
            self.meta_parser.setCurrentIndex(idx)
        self.meta_parser.setToolTip(
            "NAI 把参数放在 PNG 的 Comment 字段（扁平 JSON，多角色分组），\n"
            "ComfyUI 放的是节点图。两者格式完全不同，可以可靠自动区分。"
        )
        meta_form.addRow("解析方式", self.meta_parser)

        self.show_meta_chk = QCheckBox("图库卡片显示元数据标签")
        self.show_meta_chk.setChecked(s.gallery_show_meta)
        meta_form.addRow("", self.show_meta_chk)

        self.show_caption_chk = QCheckBox("图库卡片显示配套 txt 标签")
        self.show_caption_chk.setChecked(s.gallery_show_caption)
        meta_form.addRow("", self.show_caption_chk)

        self.caption_backup_chk = QCheckBox("保存标注前备份原文件（.bak）")
        self.caption_backup_chk.setChecked(s.caption_backup)
        meta_form.addRow("", self.caption_backup_chk)

        self.gen_policy = QComboBox()
        self.gen_policy.addItem("跳过已有 txt 的图（不覆盖）", "skip")
        self.gen_policy.addItem("只补内容为空的 txt", "fill")
        self.gen_policy.addItem("覆盖已有 txt（会先备份）", "overwrite")
        idx = self.gen_policy.findData(s.caption_generate_policy)
        if idx >= 0:
            self.gen_policy.setCurrentIndex(idx)
        self.gen_policy.setToolTip("生成标注时怎么对待已经存在的 txt")
        meta_form.addRow("已有 txt 时", self.gen_policy)
        meta_col.addLayout(meta_form)

        gen_hint = muted(
            "生成来源：PNG 元数据优先（ComfyUI 节点图 / NAI 角色分组）。\n"
            "没有元数据的图（手拍、网图）由下面的 WD14 本地打标兜底；\n"
            "WD14 关掉的话这些图会跳过。"
        )
        meta_col.addWidget(gen_hint)

        gen_row = QHBoxLayout()
        self.gen_btn = QPushButton("为缺 txt 的图生成标注")
        self.gen_btn.setObjectName("Primary")
        self.gen_btn.clicked.connect(self._generate_captions)
        gen_row.addWidget(self.gen_btn)

        self.gen_scope = QLabel("全部数据集")
        gen_row.addWidget(self.gen_scope)
        gen_row.addStretch(1)
        self.gen_count = muted("")
        gen_row.addWidget(self.gen_count)
        meta_col.addLayout(gen_row)

        gen_progress_row = QHBoxLayout()
        self.gen_progress = progress_bar(0.0)
        self.gen_progress.setFixedHeight(6)
        self.gen_progress.setVisible(False)
        gen_progress_row.addWidget(self.gen_progress, 1)
        meta_col.addLayout(gen_progress_row)

        outer.addWidget(meta_box)

        # ---------------- 提示词模板 ----------------
        prompt_box = QGroupBox("提示词模板")
        pcol = QVBoxLayout(prompt_box)
        pcol.setSpacing(6)
        hint = muted(
            "占位符：{max} 标签数量上限、{language} 语言要求、{banned} 屏蔽词列表"
        )
        pcol.addWidget(hint)
        self.prompt_edit = QPlainTextEdit(s.tag_prompt)
        self.prompt_edit.setMinimumHeight(150)
        pcol.addWidget(self.prompt_edit)
        reset_row = QHBoxLayout()
        reset = QPushButton("恢复默认模板")
        reset.setObjectName("Segment")
        reset.clicked.connect(self._reset_prompt)
        reset_row.addWidget(reset)
        reset_row.addStretch(1)
        pcol.addLayout(reset_row)
        outer.addWidget(prompt_box)

        # ---------------- 导出 ----------------
        exp_box = QGroupBox("导出默认值")
        eform = QFormLayout(exp_box)
        eform.setSpacing(9)

        self.export_dir = QLineEdit(s.export_dir)
        self.export_dir.setPlaceholderText("留空则用程序数据目录下的 exports")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._pick_export_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.export_dir, 1)
        dir_row.addWidget(browse)
        dir_holder = QWidget()
        dir_holder.setLayout(dir_row)
        eform.addRow("导出目录", dir_holder)

        self.export_write_txt = QCheckBox("每张图写一个同名 .txt")
        self.export_write_txt.setChecked(s.export_write_txt)
        eform.addRow("", self.export_write_txt)

        self.export_copy_images = QCheckBox("把图片也复制到导出目录")
        self.export_copy_images.setChecked(s.export_copy_images)
        eform.addRow("", self.export_copy_images)

        self.export_trigger = QLineEdit(s.export_trigger)
        self.export_trigger.setPlaceholderText("例如：my_char_v1")
        eform.addRow("固定触发词", self.export_trigger)

        self.export_use_ckpt = QCheckBox("把出图模型名当触发词")
        self.export_use_ckpt.setChecked(s.export_use_ckpt)
        self.export_use_ckpt.setToolTip(
            "打开后，每张图的 caption 最前面会自动加上它用的 checkpoint 短名"
        )
        eform.addRow("", self.export_use_ckpt)

        self.export_join = QLineEdit(s.export_join)
        self.export_join.setMaximumWidth(120)
        eform.addRow("标签连接符", self.export_join)

        outer.addWidget(exp_box)

        # ---------------- 存储 ----------------
        store_box = QGroupBox("本地存储")
        scol = QVBoxLayout(store_box)
        scol.setSpacing(8)
        self.store_label = muted("")
        self.store_label.setWordWrap(True)
        scol.addWidget(self.store_label)

        store_row = QHBoxLayout()
        open_data = QPushButton("打开数据目录")
        open_data.clicked.connect(lambda: paths.open_in_explorer(paths.data_dir()))
        store_row.addWidget(open_data)
        reconnect = QPushButton("重新连接数据库")
        reconnect.setToolTip(
            "数据库读写出错时用来恢复：重开连接并刷新当前页。\n"
            "也可以直接按 F5。"
        )
        reconnect.clicked.connect(self._reconnect_db)
        store_row.addWidget(reconnect)
        rebuild = QPushButton("重建标签索引")
        rebuild.clicked.connect(self._rebuild_index)
        store_row.addWidget(rebuild)
        resync = QPushButton("核对 txt 标记")
        resync.setToolTip(
            "重新核对每张图旁边的 .txt 是否真的存在，刷新数据库里的标记。\n\n"
            "什么时候用：手动在资源管理器里删过 / 加过 txt，\n"
            "或者数据集是很早扫描的（那时还没记录 txt 情况），\n"
            "打标台会误报「这张图没有配套 txt」。\n\n"
            "只读磁盘、只改标记，不动任何图片或标注文件。"
        )
        resync.clicked.connect(self._resync_captions)
        store_row.addWidget(resync)
        clear = QPushButton("清理缩略图缓存")
        clear.setObjectName("GhostDanger")
        clear.clicked.connect(self._clear_thumbs)
        store_row.addWidget(clear)
        store_row.addStretch(1)
        scol.addLayout(store_row)
        outer.addWidget(store_box)

        # ---------------- 垃圾桶 ----------------
        trash_box = QGroupBox("删除的图片丢到哪")
        tcol = QVBoxLayout(trash_box)
        tcol.setSpacing(8)

        # 目标目录：用户自己指定
        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        dir_row.addWidget(QLabel("目标文件夹"))
        self.trash_dir_edit = QLineEdit()
        self.trash_dir_edit.setPlaceholderText(
            "留空则使用应用数据目录下的 trash 文件夹"
        )
        self.trash_dir_edit.setToolTip(
            "在工作台删除图片时，图片和同名 txt 会一起备份到这里。\n\n"
            "留空 → 默认备份到应用数据目录下的 trash 文件夹，平铺保存"
            "\n填了 → 备份到指定文件夹；同名文件自动编号，不会覆盖。"
        )
        dir_row.addWidget(self.trash_dir_edit, 1)

        browse_trash = QPushButton("浏览…")
        browse_trash.clicked.connect(self._pick_trash_dir)
        dir_row.addWidget(browse_trash)

        apply_trash = QPushButton("应用")
        apply_trash.setObjectName("Primary")
        apply_trash.setToolTip("保存这个路径并立即生效")
        apply_trash.clicked.connect(self._apply_trash_dir)
        dir_row.addWidget(apply_trash)
        tcol.addLayout(dir_row)

        keep_row = QHBoxLayout()
        self.trash_keep_struct = QCheckBox("保留原来的目录层级")
        self.trash_keep_struct.setToolTip(
            "勾上：废图\\2_dsh\\xxx.png（能看出原来在哪）\n"
            "不勾：废图\\xxx.png（全平铺，好翻）\n\n"
            "自定义目录默认不勾（平铺）；同名文件都会自动加序号，不会覆盖。"
        )
        self.trash_keep_struct.toggled.connect(self._on_keep_struct_toggled)
        keep_row.addWidget(self.trash_keep_struct)
        keep_row.addStretch(1)
        tcol.addLayout(keep_row)

        self.trash_label = muted("")
        self.trash_label.setWordWrap(True)
        tcol.addWidget(self.trash_label)

        self.trash_table = QTableWidget(0, 4)
        self.trash_table.setHorizontalHeaderLabels(["文件名", "原目录", "删除时间", "原因"])
        self.trash_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.trash_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.trash_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.trash_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.trash_table.verticalHeader().setVisible(False)
        self.trash_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.trash_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.trash_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.trash_table.setMaximumHeight(200)
        tcol.addWidget(self.trash_table)

        trow = QHBoxLayout()
        self.trash_restore_btn = QPushButton("还原选中的")
        self.trash_restore_btn.setObjectName("Primary")
        self.trash_restore_btn.setToolTip(
            "把选中的图片（连同它的 txt）搬回原来的目录。\n"
            "原位置已经有同名文件的话，会加 _restored 后缀，不会覆盖。"
        )
        self.trash_restore_btn.clicked.connect(self._restore_trash)
        trow.addWidget(self.trash_restore_btn)

        self.trash_restore_all_btn = QPushButton("全部还原")
        self.trash_restore_all_btn.clicked.connect(self._restore_all_trash)
        trow.addWidget(self.trash_restore_all_btn)

        open_trash = QPushButton("打开垃圾桶文件夹")
        open_trash.clicked.connect(lambda: paths.open_in_explorer(trash.trash_root()))
        trow.addWidget(open_trash)

        refresh_trash = QPushButton("刷新")
        refresh_trash.clicked.connect(self._refresh_trash)
        trow.addWidget(refresh_trash)

        trow.addStretch(1)
        self.trash_empty_btn = QPushButton("清空垃圾桶（真删）")
        self.trash_empty_btn.setObjectName("GhostDanger")
        self.trash_empty_btn.setToolTip("把垃圾桶里的文件彻底删掉。这个**没法撤销**。")
        self.trash_empty_btn.clicked.connect(self._empty_trash)
        trow.addWidget(self.trash_empty_btn)
        tcol.addLayout(trow)

        outer.addWidget(trash_box)

        # ---------------- 保存栏 ----------------
        save_row = QHBoxLayout()
        self.dirty_label = muted("")
        save_row.addWidget(self.dirty_label)
        save_row.addStretch(1)
        reset_all = QPushButton("放弃改动")
        reset_all.setObjectName("Segment")
        reset_all.clicked.connect(self._discard)
        save_row.addWidget(reset_all)
        save_btn = QPushButton("保存设置")
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self._save)
        save_row.addWidget(save_btn)
        outer.addLayout(save_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setLayout(outer)
        scroll.setWidget(holder)
        self.root.addWidget(scroll)

        self._update_key_state()
        self._update_store_label()
        self._refresh_trash()

    # ------------------------------------------------------------------ #
    def _pick_background(self) -> None:
        import os
        import tempfile
        from PIL import Image

        chosen, _ = QFileDialog.getOpenFileName(
            self, "选择工作台背景", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if not chosen:
            return
        folder = paths.data_dir() / "appearance"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / "background.jpg"
        fd, temp_name = tempfile.mkstemp(prefix=".background-", suffix=".jpg", dir=folder)
        os.close(fd)
        try:
            with Image.open(chosen) as image:
                image = image.convert("RGB")
                image.thumbnail((2400, 1600))
                image.save(temp_name, format="JPEG", quality=88)
            os.replace(temp_name, destination)
            self.ui_background.setText(str(destination))
        except Exception as exc:  # noqa: BLE001
            Path(temp_name).unlink(missing_ok=True)
            QMessageBox.warning(self, "背景设置失败", str(exc))

    # ------------------------------------------------------------------ #
    def _toggle_api_key_visibility(self, visible: bool) -> None:
        self.api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.show_key_btn.setText("隐藏" if visible else "显示")

    def _update_key_state(self) -> None:
        env = self.key_env.text().strip()
        import os  # noqa: PLC0415

        if env:
            has = bool(os.environ.get(env, "").strip())
            hint = f"{env}：{'已设置' if has else '未设置'}"
        elif self.api_key.text().strip():
            hint = "使用本地明文密钥"
        else:
            hint = "还没配密钥"
        self.api_key.setPlaceholderText(hint)

    def _update_store_label(self) -> None:
        count = thumbs.cache_count()
        size = thumbs.human_size(thumbs.cache_size_bytes())
        try:
            health = self.db.health()
        except Exception as exc:  # noqa: BLE001
            health = {"ok": False, "path": str(paths.db_path()), "error": str(exc),
                      "size": 0, "journal_mode": "?", "temp_store": "?", "reconnects": 0}
        db_size = thumbs.human_size(int(health.get("size") or 0))
        state = "正常" if health.get("ok") else f"异常：{health.get('error')}"

        # 别的候选目录里也有数据 —— 目录漂移之后留下的"孤岛"。
        # 这是用户"数据集变少了"时最需要看到的一条信息，所以放在这里。
        extra = ""
        try:
            others = paths.other_data_dirs()
        except Exception:  # noqa: BLE001
            others = []
        if others:
            joined = "\n".join(f"　　　　　　{p}" for p in others)
            extra = (
                "\n[注意] 以下目录里也有数据，但当前没有使用：\n"
                f"{joined}\n"
                "　　　　　　如果发现数据集变少，可能是数据分散在这些目录里"
            )

        self.store_label.setText(
            f"数据库：{health.get('path')}\n"
            f"　　　　{db_size}，journal={health.get('journal_mode')}，"
            f"temp_store={health.get('temp_store')}，状态 {state}"
            f"（自动重连 {health.get('reconnects', 0)} 次）\n"
            f"配置：{paths.settings_path()}\n"
            f"缩略图缓存：{count:,} 个文件，{size}"
            f"{extra}"
        )

    # ------------------------------------------------------------------ #
    def _collect(self) -> None:
        s = self.settings
        s.ui_theme = str(self.ui_theme.currentData() or "light")
        s.ui_background = self.ui_background.text().strip()
        s.ui_background_blur = self.ui_blur.value()
        s.ui_background_shade = self.ui_shade.value()
        s.ui_panel_opacity = self.ui_opacity.value()
        s.api_base = self.api_base.text().strip()
        s.api_key_env = self.key_env.text().strip()
        s.api_key = self.api_key.text()
        s.model = self.model.text().strip()
        s.timeout = self.timeout.value()
        s.concurrency = self.concurrency.value()
        s.max_retries = self.retries.value()
        s.temperature = self.temp.value()
        s.preset = str(self.preset.currentData() or "通用 Danbooru")
        s.language = self.language.currentText()
        s.max_tags = self.max_tags.value()
        s.image_max_side = self.max_side.value()
        s.auto_tag_on_scan = self.auto_tag.isChecked()

        # WD14
        s.wd14_enabled = self.wd_enabled.isChecked()
        s.wd14_model = str(self.wd_model.currentData() or "")
        s.wd14_use_gpu = self.wd_gpu.isChecked()
        s.wd14_threshold = float(self.wd_threshold.value())
        s.wd14_character_threshold = float(self.wd_char_threshold.value())
        s.wd14_max_tags = int(self.wd_max_tags.value())
        s.wd14_batch_size = int(self.wd_batch.value())
        s.wd14_replace_underscore = self.wd_underscore.isChecked()
        s.wd14_undesired = self.wd_undesired.text().strip()

        s.tag_prompt = self.prompt_edit.toPlainText()
        s.export_dir = self.export_dir.text().strip()
        s.export_write_txt = self.export_write_txt.isChecked()
        s.export_copy_images = self.export_copy_images.isChecked()
        s.export_trigger = self.export_trigger.text().strip()
        s.export_use_ckpt = self.export_use_ckpt.isChecked()
        s.export_join = self.export_join.text() or ", "
        s.meta_parser = str(self.meta_parser.currentData() or "auto")
        s.gallery_show_meta = self.show_meta_chk.isChecked()
        s.gallery_show_caption = self.show_caption_chk.isChecked()
        s.caption_backup = self.caption_backup_chk.isChecked()
        s.caption_generate_policy = str(self.gen_policy.currentData() or "skip")

    def _save(self) -> None:
        self._collect()
        if save_settings(self.settings):
            theme.apply_theme(QApplication.instance(), self.settings)
            self.window().setProperty("ui_theme", self.settings.ui_theme)
            self.window().update()
            self.ctx.notify("设置已保存")
            self.dirty_label.setText("已保存")
            self._update_key_state()
            self.ctx.status.emit(f"设置已保存（{self.settings.model}）")
            # 检视面板的「读取方式」下拉跟着走，免得两处显示不一致
            try:
                self.ctx.inspector_changed.emit(self.settings.meta_parser)
            except Exception:  # noqa: BLE001
                pass
        else:
            QMessageBox.critical(self, "保存失败", f"无法写入 {paths.settings_path()}")

    def _discard(self) -> None:
        from ...config import load_settings  # noqa: PLC0415

        fresh = load_settings()
        self.settings.__dict__.update(
            {k: v for k, v in fresh.__dict__.items() if not k.startswith("_")}
        )
        self.ensure_loaded()
        self._rebuild()
        theme.apply_theme(QApplication.instance(), self.settings)
        self.window().setProperty("ui_theme", self.settings.ui_theme)
        self.window().update()
        self.ctx.notify("已放弃未保存的改动")

    def _rebuild(self) -> None:
        """把控件重新填一遍当前 settings。"""
        s = self.settings
        self.ui_theme.setCurrentIndex(1 if s.ui_theme == "dark" else 0)
        self.ui_background.setText(s.ui_background)
        self.ui_blur.setValue(s.ui_background_blur)
        self.ui_shade.setValue(s.ui_background_shade)
        self.ui_opacity.setValue(s.ui_panel_opacity)
        self.api_base.setText(s.api_base)
        self.key_env.setText(s.api_key_env)
        self.api_key.setText(s.api_key)
        self.model.setText(s.model)
        self.timeout.setValue(s.timeout)
        self.concurrency.setValue(s.concurrency)
        self.retries.setValue(s.max_retries)
        self.temp.setValue(s.temperature)
        self.max_tags.setValue(s.max_tags)
        self.max_side.setValue(s.image_max_side)
        self.auto_tag.setChecked(s.auto_tag_on_scan)
        self.prompt_edit.setPlainText(s.tag_prompt)
        self.export_dir.setText(s.export_dir)
        self.export_write_txt.setChecked(s.export_write_txt)
        self.export_copy_images.setChecked(s.export_copy_images)
        self.export_trigger.setText(s.export_trigger)
        self.export_use_ckpt.setChecked(s.export_use_ckpt)
        self.export_join.setText(s.export_join)
        self.meta_parser.setCurrentIndex(max(0, self.meta_parser.findData(s.meta_parser)))
        self.show_meta_chk.setChecked(s.gallery_show_meta)
        self.show_caption_chk.setChecked(s.gallery_show_caption)
        self.caption_backup_chk.setChecked(s.caption_backup)
        self.gen_policy.setCurrentIndex(
            max(0, self.gen_policy.findData(s.caption_generate_policy))
        )
        self._update_key_state()

    # ------------------------------------------------------------------ #
    def _generate_captions(self) -> None:
        """给缺 txt 的图批量生成标注（目前只走 PNG 元数据）。"""
        missing = self.db.images_missing_caption(None)
        if not missing:
            QMessageBox.information(self, "无需生成", "所有图片都已有配套 txt。")
            return

        policy = str(self.gen_policy.currentData() or "skip")
        box = QMessageBox(self)
        box.setWindowTitle("生成标注")
        box.setText(f"为 {missing} 张缺 txt 的图生成标注？")
        box.setInformativeText(
            "来源：PNG 元数据优先（ComfyUI 节点图 / NAI 角色分组）；\n"
            "没有元数据的图由 WD14 本地打标兜底"
            + ("。" if self.settings.wd14_enabled else "（当前已关闭，这些图会跳过）。")
            + "\n\n已有 txt 的图按当前策略处理，任何覆盖都会先备份。"
        )
        ok_btn = box.addButton("开始生成", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not ok_btn:
            return

        self._collect()
        self.gen_btn.setEnabled(False)
        self.gen_progress.setVisible(True)
        self.gen_progress.setRange(0, 0)  # 忙碌指示
        self.gen_count.setText("生成中…")
        QApplication.processEvents()

        try:
            images = list(caption_gen.iter_missing_caption_images(self.db, None))
            report = caption_gen.generate_for_images(
                self.db,
                images,
                parser=self.settings.meta_parser,
                use_wd14=bool(self.settings.wd14_enabled),
                wd14_config=self._wd_config_from_ui() if self.settings.wd14_enabled else None,
                policy=policy,
                make_backup=bool(self.settings.caption_backup),
            )
        except Exception as exc:  # noqa: BLE001
            self.gen_progress.setVisible(False)
            self.gen_btn.setEnabled(True)
            self.gen_count.setText("")
            QMessageBox.critical(self, "生成失败", f"{type(exc).__name__}: {exc}")
            return

        self.gen_progress.setRange(0, 100)
        self.gen_progress.setValue(100)
        self.gen_count.setText(report.summary())
        self.gen_btn.setEnabled(True)
        self.ctx.images_changed.emit()

        detail = caption_gen.preview_report(report, limit=15)
        QMessageBox.information(self, "生成完成", report.summary() + "\n\n" + detail)

    # ------------------------------------------------------------------ #
    # WD14 本地打标
    # ------------------------------------------------------------------ #
    def _reload_wd_models(self, current: str = "") -> None:
        """列模型。找不到就保持可编辑，让用户能自己指定路径。"""
        self.wd_model.clear()
        try:
            found = wd14.list_models()
        except Exception:  # noqa: BLE001
            found = []

        if found:
            for model in found:
                self.wd_model.addItem(model.stem, str(model))
            # 设置里存的是路径，尽量选回它
            if current:
                idx = self.wd_model.findData(current)
                if idx < 0:
                    # 存的是别的写法（比如只存了文件名），按名字找
                    idx = self.wd_model.findText(Path(current).stem)
                if idx >= 0:
                    self.wd_model.setCurrentIndex(idx)
            self.wd_models_hint.setText(
                f"在 {wd14.models_root()} 找到 {len(found)} 个模型"
            )
        else:
            self.wd_model.addItem("（没找到模型）", "")
            self.wd_models_hint.setText(
                f"这里没找到模型：{wd14.models_root()} —— 点「找模型…」指一个 .onnx"
            )

    def _pick_wd_model(self) -> None:
        start = str(wd14.models_root())
        picked, _ = QFileDialog.getOpenFileName(
            self, "选 WD14 模型", start, "ONNX 模型 (*.onnx)"
        )
        if not picked:
            return
        model = Path(picked)
        if not model.with_suffix(".csv").is_file():
            QMessageBox.warning(
                self,
                "缺词表",
                f"同目录下没找到 {model.with_suffix('.csv').name}。\n\n"
                "WD14 的 .onnx 和 .csv 必须成对，csv 是标签词表，"
                "行序对应模型输出列序，缺了没法用。",
            )
            return
        # 塞进下拉里并选中
        idx = self.wd_model.findData(str(model))
        if idx < 0:
            self.wd_model.addItem(model.stem, str(model))
            idx = self.wd_model.count() - 1
        self.wd_model.setCurrentIndex(idx)
        self.wd_models_hint.setText(f"已指定：{model}")
        self.ctx.notify(f"已选模型 {model.stem}，记得保存")

    def _open_wd_dir(self) -> None:
        root = wd14.models_root()
        root.mkdir(parents=True, exist_ok=True)
        try:
            paths.open_in_explorer(str(root))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(self, "模型目录", f"{root}\n\n（打不开资源管理器：{exc}）")

    def _wd_config_from_ui(self) -> wd14.TaggerConfig:
        return wd14.TaggerConfig(
            model_path=str(self.wd_model.currentData() or ""),
            use_gpu=self.wd_gpu.isChecked(),
            threshold=float(self.wd_threshold.value()),
            character_threshold=float(self.wd_char_threshold.value()),
            max_tags=int(self.wd_max_tags.value()),
            batch_size=int(self.wd_batch.value()),
            replace_underscore=self.wd_underscore.isChecked(),
            undesired={
                part.strip().lower()
                for part in self.wd_undesired.text().split(",")
                if part.strip()
            },
        )

    def _probe_wd14(self) -> None:
        """同步加载模型探一次。会卡几秒，给个忙碌提示。"""
        self.wd_probe_btn.setEnabled(False)
        self.wd_status.setText("检测中…（第一次要加载 1.2GB 模型，慢一点）")
        QApplication.processEvents()
        try:
            status = wd14.Wd14Tagger(self._wd_config_from_ui()).load()
        except Exception as exc:  # noqa: BLE001
            status = wd14.TaggerStatus(False, error=f"{type(exc).__name__}: {exc}")

        if status.available:
            self.wd_status.setText(f"✓ 可用 · {status.provider} · {status.model}")
            self.wd_status.setStyleSheet(f"color: {theme.GREEN};")
            self.ctx.notify(f"WD14 可用（{status.provider}）")
        else:
            self.wd_status.setText(f"✗ 不可用 · {status.error}")
            self.wd_status.setStyleSheet(f"color: {theme.RED};")
        self.wd_probe_btn.setEnabled(True)

    def _auto_probe_wd14(self) -> None:
        """进设置页时探一次（只探不加载模型时很轻）。

        load() 在第一次会真的加载模型（约 2-3 秒），所以这里只做
        轻量检查：模型文件在不在 + onnxruntime 装没装。
        """
        try:
            model = wd14.pick_default_model()
        except Exception:  # noqa: BLE001
            model = None
        if model is None:
            self.wd_status.setText("✗ 没找到模型文件")
            self.wd_status.setStyleSheet(f"color: {theme.RED};")
            return
        try:
            import onnxruntime  # noqa: F401, PLC0415
            has_ort = True
        except ImportError:
            has_ort = False
        if not has_ort:
            self.wd_status.setText("✗ 没装 onnxruntime（源码环境里 pip install onnxruntime-gpu）")
            self.wd_status.setStyleSheet(f"color: {theme.RED};")
            return
        self.wd_status.setText(f"模型就绪：{model.stem}（点「检测引擎」确认 GPU）")
        self.wd_status.setStyleSheet(f"color: {theme.TEXT_DIM};")

    def _test_connection(self) -> None:
        self._collect()
        worker = PingWorker(self.settings, parent=self)
        worker.finished_ok.connect(self._on_ping)
        self.test_btn.setEnabled(False)
        self.test_result.setText("测试中…")
        self._ping_worker = worker
        worker.start()

    def _on_ping(self, ok: bool, message: str) -> None:
        self.test_btn.setEnabled(True)
        color = theme.GREEN if ok else theme.RED
        self.test_result.setText(message)
        self.test_result.setStyleSheet(f"color: {color};")

    # ------------------------------------------------------------------ #
    def _on_preset_change(self) -> None:
        name = str(self.preset.currentData() or "")
        extra = PROMPT_PRESETS.get(name, "")
        self.prompt_edit.setToolTip(extra or "通用预设，无附加要求")
        self.dirty_label.setText("预设已切换，记得保存")

    def _reset_prompt(self) -> None:
        from ...config import DEFAULT_TAG_PROMPT  # noqa: PLC0415

        self.prompt_edit.setPlainText(DEFAULT_TAG_PROMPT)
        self.ctx.notify("已恢复默认模板，记得保存")

    def _pick_export_dir(self) -> None:
        start = self.export_dir.text().strip() or str(paths.export_dir())
        folder = QFileDialog.getExistingDirectory(self, "选择导出目录", start)
        if folder:
            self.export_dir.setText(folder)

    def _rebuild_index(self) -> None:
        total = self.db.rebuild_all_tags_text()
        self.ctx.notify(f"已重建 {total:,} 张图的标签索引")
        self.ctx.images_changed.emit()

    def _resync_captions(self) -> None:
        """核对磁盘上的 .txt，刷新 has_caption 标记。"""
        before = self.db.stats()["with_caption"]
        changed = self.db.refresh_caption_flags()
        after = self.db.stats()["with_caption"]
        self.ctx.images_changed.emit()
        self._update_store_label()
        if changed:
            self.ctx.notify(
                f"核对完成：有 txt 的图从 {before:,} 张更新为 {after:,} 张"
                f"（修正 {changed:,} 行）"
            )
        else:
            self.ctx.notify(f"核对完成：{after:,} 张有 txt，标记本来就是对的")

    def _clear_thumbs(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("清理缓存")
        box.setText("清空缩略图缓存？")
        box.setInformativeText(
            f"当前缓存 {thumbs.cache_count():,} 个文件，"
            f"{thumbs.human_size(thumbs.cache_size_bytes())}。\n清空后下次浏览会重新生成。"
        )
        yes = box.addButton("清理", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            return
        removed = thumbs.clear_cache()
        self.ctx.thumb_pool.clear_cache()
        self._update_store_label()
        self.ctx.notify(f"已清理 {removed:,} 个缓存文件")

    # ------------------------------------------------------------------ #
    # 垃圾桶
    # ------------------------------------------------------------------ #
    def _on_keep_struct_toggled(self, checked: bool) -> None:
        self.settings.trash_keep_structure = bool(checked)
        save_settings(self.settings)

    def _pick_trash_dir(self) -> None:
        start = self.trash_dir_edit.text().strip() or str(paths.default_trash_dir())
        picked = QFileDialog.getExistingDirectory(self, "选一个文件夹存废图", start)
        if picked:
            self.trash_dir_edit.setText(picked)

    def _apply_trash_dir(self) -> None:
        folder = self.trash_dir_edit.text().strip()
        if folder:
            # 先做安全检查：盘根、系统目录、用户目录一律不接受。
            # 垃圾桶是唯一会真删的地方，这里必须拦死。
            reason = trash.unsafe_reason(folder)
            if reason:
                QMessageBox.warning(
                    self,
                    "这个位置不能用",
                    f"{reason}\n\n"
                    "请选一个专用的空文件夹，例如 D:\\废图。",
                )
                return
            target = Path(folder).expanduser()
            try:
                target.mkdir(parents=True, exist_ok=True)
                probe = target / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    "这个文件夹不能用",
                    f"{target}\n\n{exc}\n\n换一个位置试试。",
                )
                return
        try:
            trash.set_trash_dir(folder)
        except trash.UnsafeTrashRoot as exc:
            QMessageBox.warning(self, "这个位置不能用", str(exc))
            return
        self.settings.trash_dir = folder
        save_settings(self.settings)
        self._refresh_trash()
        self.ctx.notify(
            f"以后删的图会丢到：{trash.trash_root()}" if folder
            else "已改回默认位置（数据目录下的 trash）"
        )

    def _refresh_trash(self) -> None:
        """刷新垃圾桶列表和统计。"""
        # 输入框跟着当前设置走（但别打断正在输入的人）
        if not self.trash_dir_edit.hasFocus():
            self.trash_dir_edit.setText(getattr(self.settings, "trash_dir", ""))

        # set_trash_dir / stats / trash_root 都可能抛 UnsafeTrashRoot
        # （设置里的旧值或 COMFY_WORKBENCH_TRASH_DIR 指向了危险目录）。
        # 这是刷新界面，**绝不能因为一个坏设置把整页搞崩**，
        # 所以整段兜住，把原因显示给用户。
        unsafe = ""
        try:
            trash.set_trash_dir(getattr(self.settings, "trash_dir", ""))
        except trash.UnsafeTrashRoot as exc:
            unsafe = str(exc)
            try:
                trash.set_trash_dir("")
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            unsafe = f"{type(exc).__name__}: {exc}"

        self.trash_keep_struct.setChecked(
            bool(getattr(self.settings, "trash_keep_structure", False))
        )

        try:
            stats = trash.stats()
        except trash.UnsafeTrashRoot as exc:
            unsafe = str(exc)
            stats = {"files": 0, "bytes": 0, "images": 0}
        except Exception as exc:  # noqa: BLE001
            unsafe = f"{type(exc).__name__}: {exc}"
            stats = {"files": 0, "bytes": 0, "images": 0}

        if unsafe:
            self.trash_label.setText(
                f"<b>[安全] 当前删除目录不可用，已退回默认桶。</b><br>"
                f"原因：{unsafe}<br>"
                "请在上面重新选一个专用目录（例如 D:\\废图）。"
            )
            return

        if stats["files"]:
            self.trash_label.setText(
                f"桶里现在有 <b>{stats['images']:,}</b> 张图片"
                f"（共 {stats['files']:,} 个文件，"
                f"{trash.format_size(stats['bytes'])}）。\n"
                f"位置：{trash.trash_root()}\n"
                "选中一条可以还原；「清空垃圾桶」才会真正删除文件。"
            )
        else:
            self.trash_label.setText(
                "垃圾桶是空的。\n"
                "在工作台勾选图片后点「删除选中的图（进垃圾桶）」，"
                "图片和它的 txt 就会移到这里，随时能还原。"
            )

        entries = trash.list_trashed()
        self.trash_table.setRowCount(0)
        for entry in entries:
            row = self.trash_table.rowCount()
            self.trash_table.insertRow(row)
            original = Path(entry.original)
            when = ""
            if entry.deleted_at:
                when = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(entry.deleted_at)
                )
            cells = [
                original.name,
                str(original.parent),
                when,
                entry.note or "",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if col == 0:
                    # 记录原路径，还原时要用
                    item.setData(Qt.ItemDataRole.UserRole, entry.original)
                self.trash_table.setItem(row, col, item)

        has_rows = bool(entries)
        self.trash_restore_btn.setEnabled(has_rows)
        self.trash_restore_all_btn.setEnabled(has_rows)
        self.trash_empty_btn.setEnabled(stats["files"] > 0)

    def _selected_trash_entries(self) -> list:
        rows = sorted(
            {i.row() for i in self.trash_table.selectedIndexes()}
        )
        picked: list[str] = []
        for r in rows:
            item = self.trash_table.item(r, 0)
            if item is not None:
                picked.append(str(item.data(Qt.ItemDataRole.UserRole) or ""))
        by_original = {e.original: e for e in trash.list_trashed()}
        return [by_original[p] for p in picked if p in by_original]

    def _restore_trash(self) -> None:
        entries = self._selected_trash_entries()
        if not entries:
            self.ctx.notify("先选中要还原的图片")
            return
        done, errors = trash.restore(entries)
        self._rescan_after_restore(entries)
        self._refresh_trash()
        self.ctx.notify(f"已还原 {len(entries)} 张（{done} 个文件）")
        if errors:
            QMessageBox.warning(
                self, "部分没还原成",
                "\n".join(errors[:8]),
            )

    def _restore_all_trash(self) -> None:
        entries = trash.list_trashed()
        if not entries:
            self.ctx.notify("垃圾桶是空的")
            return
        box = QMessageBox(self)
        box.setWindowTitle("全部还原")
        box.setText(f"还原垃圾桶里的全部 {len(entries)} 张图片？")
        box.setInformativeText("会连同它们的 txt 一起搬回原目录。")
        yes = box.addButton("还原", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not yes:
            return
        done, errors = trash.restore(entries)
        self._rescan_after_restore(entries)
        self._refresh_trash()
        self.ctx.notify(f"已还原 {len(entries)} 张（{done} 个文件）")
        if errors:
            QMessageBox.warning(self, "部分没还原成", "\n".join(errors[:8]))

    def _rescan_after_restore(self, entries: list) -> None:
        """还原之后要让图片重新出现在库里。

        删的时候连数据库记录一起删了，光把文件搬回去**不会**自动入库 ——
        得重新扫一遍那个目录。这里只扫涉及到的数据集，不整库重扫。
        """
        restored = {str(Path(e.original).parent).lower() for e in entries}
        if not restored:
            return
        from ...scanner import scan_folder  # noqa: PLC0415

        for ds in self.db.list_datasets():
            root = str(ds.get("path") or "")
            if not root:
                continue
            low = root.lower()
            if not any(
                p == low or p.startswith(low + "\\") or p.startswith(low + "/")
                for p in restored
            ):
                continue
            try:
                scan_folder(
                    self.db,
                    int(ds["id"]),
                    root,
                    parse_meta=False,
                    prune_missing=False,
                )
            except Exception:  # noqa: BLE001
                # 扫描失败不该让还原流程报错 —— 文件已经在原位了，
                # 用户手动「扫描」一下也能回来。
                continue

    def _empty_trash(self) -> None:
        stats = trash.stats()
        if not stats["files"]:
            self.ctx.notify("垃圾桶已经空了")
            return
        box = QMessageBox(self)
        box.setWindowTitle("清空垃圾桶")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"彻底删除垃圾桶里的 {stats['files']:,} 个文件？")
        box.setInformativeText(
            f"共 {trash.format_size(stats['bytes'])}。\n\n"
            "**这一步没法撤销** —— 文件会真的从硬盘上消失。\n"
            "如果只是想收拾一下，建议先「打开垃圾桶文件夹」自己看看。"
        )
        yes = box.addButton("彻底删除", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(box.buttons()[-1])
        box.exec()
        if box.clickedButton() is not yes:
            return
        removed, errors = trash.empty_trash()
        self._refresh_trash()
        self.ctx.notify(f"已彻底删除 {removed:,} 个文件")
        if errors:
            QMessageBox.warning(self, "部分没删掉", "\n".join(errors[:8]))

    # ------------------------------------------------------------------ #
    def _reconnect_db(self) -> None:
        try:
            self.db.reopen()
        except Exception as exc:  # noqa: BLE001
            self.ctx.report_db_problem(f"重新连接数据库失败：{exc}", exc)
            return
        self.ctx._db_problem_shown = ""
        self.ctx.notify("数据库连接已重建")
        self._update_store_label()

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        if self._loaded:
            self._update_store_label()
            self._refresh_trash()
            self._auto_probe_wd14()
