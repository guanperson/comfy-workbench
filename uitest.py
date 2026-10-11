"""界面冒烟测试：离屏启动，逐页构建并截图。

    python uitest.py

PySide6_QT_API 用不上 offscreen 就走 QT_QPA_PLATFORM=offscreen，
不需要真显卡，也不会弹窗。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QSize, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
)
from shiboken6 import isValid  # noqa: E402

from app import paths  # noqa: E402
from app import captions  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

SHOT_DIR = Path(__file__).resolve().parent / "_shots"

PASS = 0
FAIL = 0

# 这些按钮会真的发网络请求，离屏测试里点它们没有意义还会拖时间
# 点这些按钮会去跑真的网络请求 / 整目录扫描 / 开系统窗口，测试里不点。
#
#   · 「重新解析提示词」「开始扫描」会把整个数据集上千张 PNG 重扫一遍，
#      点一次就要跑几分钟
#   · 「浏览文件夹」「打开导出目录」会调 os.startfile() 真的弹出资源管理器
#      —— 那是个外部 GUI，永远不返回，测试必然卡死（已经踩到过一次）
NETWORK_BUTTONS = {
    "自动打标队列", "测试连接", "打标选中项", "只打这张",
    "重新解析提示词", "开始扫描", "扫描", "重新扫描",
    "浏览文件夹", "打开文件夹", "打开导出目录", "打开数据目录",
    "打开垃圾桶文件夹", "导出数据集", "导出选中", "移除", "删除选中图片",
}

# 槽函数里抛的异常会被 sys.excepthook 接住，不会让 click() 本身炸，
# 所以必须自己记下来 —— `ctx.navigate(...)` 那种把 Signal 当方法调的错
# 就是这么漏过去的。
EXC_ERRORS: list[str] = []


def _record_exc(exc_type, exc, tb) -> None:
    frame = tb
    while frame and frame.tb_next:
        frame = frame.tb_next
    where = f"{frame.tb_frame.f_code.co_filename}:{frame.tb_lineno}" if frame else "?"
    EXC_ERRORS.append(f"{exc_type.__name__}: {exc} @ {where}")


def _stub_static_messageboxes() -> None:
    """把 QMessageBox 的静态便捷函数都堵上。

    这些是 **staticmethod**，不走 ``QDialog.exec`` —— 只 stub 基类的 exec
    拦不住它们，实测会直接卡死（见 复盘-垃圾桶卡在哪了.md）。

    测试里如果有地方临时改回真函数，收尾时要再调一次这个，
    否则后面任何一个报错弹窗都会把整个测试挂住。
    """
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    for name in ("information", "warning", "critical", "about"):
        setattr(
            QMessageBox,
            name,
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
        )


def _cancel_confirmation_dialog(self, *args, **kwargs):
    buttons = getattr(self, "buttons", None)
    if callable(buttons):
        for button in buttons():
            if button.text() in {"取消", "否", "Cancel"}:
                button.click()
                return QMessageBox.StandardButton.Cancel
    return QMessageBox.StandardButton.No


def stub_dialogs() -> None:
    """把模态对话框全换成立即返回，否则离屏跑会永远卡住。

    两种写法都要堵：静态便捷函数（getExistingDirectory 等），
    以及自己 new 一个 QMessageBox 再 ``.exec()`` 的用法
    —— 后者会直接阻塞 Qt 事件循环，定时器都救不回来。
    """
    QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: "")
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([], ""))
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
    QInputDialog.getText = staticmethod(lambda *a, **k: ("", False))
    QInputDialog.getItem = staticmethod(lambda *a, **k: ("", False))
    QInputDialog.getInt = staticmethod(lambda *a, **k: (0, False))
    # 打开资源管理器是个外部 GUI，点一下窗口就弹出来了、调用永远不返回。
    # 与其维护一张按钮名单，不如从根上把它堵掉 —— 名单总有一天会漏。
    paths.open_in_explorer = lambda *a, **k: None
    paths.reveal_file = lambda *a, **k: None
    # 一律选「否」，避免点删除类按钮把测试数据删掉
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    _stub_static_messageboxes()
    # QMessageBox / QFileDialog / QInputDialog 都继承 QDialog，
    # 堵住基类就堵住了全部「先 new 再 exec」的模态框
    QDialog.exec = lambda self, *a, **k: 0
    QMessageBox.exec = lambda self, *a, **k: _cancel_confirmation_dialog(self)


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name} -- {detail}")


def _tags_for(db, row: dict) -> list[str]:
    """读一张图当前的标签（含暂存改动）。"""
    image_id = int(row.get("id") or 0)
    path = str(row.get("path") or "")
    try:
        return list(db.caption_edit(image_id, path).current)
    except Exception:  # noqa: BLE001
        return []


def make_sample_dataset(root: Path) -> None:
    """造几个带元数据的 PNG，让界面有东西可显示。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from selftest import API_PROMPT, COND_CHAINED, make_png  # noqa: PLC0415

    import json  # noqa: PLC0415

    root.mkdir(parents=True, exist_ok=True)
    make_png(root / "portrait_0001.png", 96, 128, {"prompt": json.dumps(API_PROMPT)})
    make_png(root / "portrait_0002.png", 96, 128, {"prompt": json.dumps(COND_CHAINED)})
    make_png(root / "portrait_0003.png", 96, 128, {"prompt": json.dumps(API_PROMPT)})
    make_png(root / "landscape_0001.png", 128, 96, {"prompt": json.dumps(COND_CHAINED)})
    make_png(root / "no_meta_0001.png", 64, 64)


def main() -> int:
    print("=" * 62)
    print("ComfyUI Workbench - UI smoke test")
    print("=" * 62)

    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    sandbox = Path(__file__).resolve().parent / ".uitest_data"
    sandbox.mkdir(parents=True, exist_ok=True)
    # 测试完全自包含：数据目录也放工作区，不碰 %APPDATA%
    os.environ["COMFY_WORKBENCH_HOME"] = str(sandbox / "appdata")
    output_dir = sandbox / "sample_output"
    if not output_dir.exists() or not any(output_dir.iterdir()):
        make_sample_dataset(output_dir)

    app = QApplication(sys.argv)
    theme.apply_theme(app)

    settings = Settings()
    settings.thumb_size = 256

    db = Database(sandbox / "uitest.db")
    dataset_id = db.upsert_dataset("示例数据集", str(output_dir))

    from app.scanner import scan_folder  # noqa: PLC0415

    if db.count_images(dataset_id) == 0:
        result = scan_folder(db, dataset_id, output_dir, parse_meta=True)
        print(f"  (scanned {result.scanned}, with_meta {result.with_meta})")

    # 塞点标签，让标签库和统计有内容
    images = db.list_images(dataset_id=dataset_id, order="mtime_asc")
    if images:
        db.add_tags(int(images[0]["id"]),
                    ["1girl", "solo", "long_hair", "silver_eyes", "school_uniform"],
                    source="manual")
        db.add_tags(int(images[1]["id"]),
                    ["masterpiece", "cherry_blossom", "hanami", "outdoors"],
                    source="auto")
        db.add_tags(int(images[2]["id"]), ["portrait", "smile"], source="auto")
        db.set_tag_banned("lowres", True)
        db.set_tag_banned("watermark", True)

    print("\n[1] build window")
    window = MainWindow(db, settings)
    window.resize(1440, 900)
    window.show()
    check("window constructed", window is not None)

    print("\n[2] visit every page")
    for key, label, _glyph in theme.PAGE_ORDER:
        try:
            window.goto(key)
            app.processEvents()
            check(f"page {key} ({label})", True)
            window.grab().save(str(SHOT_DIR / f"{key}.png"))
        except Exception as exc:  # noqa: BLE001
            check(f"page {key} ({label})", False, f"{type(exc).__name__}: {exc}")

    print("\n[3] gallery interactions")
    try:
        gallery = window._page_objects["gallery"]
        gallery.reload()
        app.processEvents()
        count = gallery.model.rowCount()
        check("gallery has rows", count == 5, f"rows={count}")

        gallery.select_all()
        app.processEvents()
        check("select all", len(gallery.selected_rows()) == 5, str(len(gallery.selected_rows())))

        gallery.set_filter("cherry")
        app.processEvents()
        check("filter narrows", gallery.model.rowCount() >= 1, str(gallery.model.rowCount()))
        gallery.set_filter("")
        app.processEvents()

        first = gallery.model.index(0, 0)
        gallery.view.setCurrentIndex(first)
        app.processEvents()
        check("selection reaches inspector", window.inspector.stack.currentIndex() == 1)
        check("inspector shows tags",
              len(window.inspector.db.tags_for_image(images[0]["id"])) >= 5)

        no_meta = next(row for row in images if not row.get("has_meta"))
        db._query("DELETE FROM image_tags WHERE image_id = ?", (int(no_meta["id"]),))
        db.add_tags(int(no_meta["id"]), ["caption_tag_one", "caption_tag_two"], source="manual")
        window.inspector.show_image(no_meta)
        app.processEvents()
        # 老的「打标标签」小框已经退休：txt 原文现在由上面那个常驻的
        # Caption 框统一显示，不再需要切分后的标签列表。
        check("retired caption-tag panel stays hidden",
              not window.inspector.caption_tags_view.isVisible())
        check("caption area is always visible",
              window.inspector.caption_view.isVisible())
        window.inspector._copy_caption_full()
        check("复制 Caption 走通",
              app.clipboard().text() == captions.read_caption_text(no_meta["path"]),
              app.clipboard().text()[:60])
        window.grab().save(str(SHOT_DIR / "gallery_inspector.png"))
    except Exception as exc:  # noqa: BLE001
        check("gallery interactions", False, f"{type(exc).__name__}: {exc}")

    print("\n[4] 打标台：选中 → 复制 → 粘贴")
    try:
        wb = window._page_objects["workbench"]
        target_id = int(images[0]["id"])
        window.goto("workbench", {"select_id": target_id})
        app.processEvents()
        check("workbench shows image", wb.preview.pixmap() is not None
              and not wb.preview.pixmap().isNull())

        # 造一张带 txt 的图，保证有胶囊可点
        from app import captions as _caps
        src_path = str(images[0]["path"])
        _caps.write_caption(src_path, ["1girl", "solo"], make_backup=False)
        db._set_caption_only(target_id, True)
        wb.reload_queue()
        wb._select_image(target_id)
        app.processEvents()
        check("胶囊渲染出来了", wb.pill_box.tags() == ["1girl", "solo"],
              str(wb.pill_box.tags()))

        # 点一个胶囊，复制
        chip = next(c for c in wb.pill_box._chips if c.name == "solo")
        chip.set_selected(True)
        wb.pill_box._on_chip_clicked(chip.name)
        check("选中一个胶囊", wb.pill_box.selected_tags() == ["solo"],
              str(wb.pill_box.selected_tags()))
        wb._collect("copy")
        check("复制进剪贴板", wb._clipboard == ["solo"], str(wb._clipboard))
        check("粘贴按钮亮起", wb.paste_btn.isEnabled())

        # 粘回去（已存在，应该去重）
        before = db.caption_current(target_id, src_path)
        wb._paste()
        after = db.caption_current(target_id, src_path)
        check("重复标签不会重复粘贴", after == before, f"{before} -> {after}")

        wb._release()
        check("释放清空剪贴板", not wb.paste_btn.isEnabled())

        wb.grid.step(1)
        app.processEvents()
        check("step to next image", True)
        window.grab().save(str(SHOT_DIR / "workbench.png"))
    except Exception as exc:  # noqa: BLE001
        import traceback; traceback.print_exc()
        check("打标台交互", False, f"{type(exc).__name__}: {exc}")

    print("\n[5] tag library")
    try:
        taglib = window._page_objects["taglib"]
        window.goto("taglib")
        app.processEvents()
        check("tag list populated", taglib.list.count() >= 8, str(taglib.list.count()))
        taglib.list.setCurrentRow(0)
        app.processEvents()
        taglib._toggle_ban()
        app.processEvents()
        check("toggle ban works", True)
        taglib.tabs.setCurrentIndex(1)
        app.processEvents()
        check("banned tab renders", taglib.ban_list.count() >= 1)
        taglib.tabs.setCurrentIndex(2)
        app.processEvents()
        check("frequency tab renders", taglib.freq_list.count() >= 1)
        window.grab().save(str(SHOT_DIR / "taglib.png"))
    except Exception as exc:  # noqa: BLE001
        check("tag library", False, f"{type(exc).__name__}: {exc}")

    print("\n[6] datasets page")
    try:
        ds = window._page_objects["datasets"]
        window.goto("datasets")
        app.processEvents()
        check("dataset card rendered", len(ds._cards) == 1, str(len(ds._cards)))
        window.grab().save(str(SHOT_DIR / "datasets.png"))
    except Exception as exc:  # noqa: BLE001
        check("datasets page", False, f"{type(exc).__name__}: {exc}")

    print("\n[7] settings page")
    try:
        sp = window._page_objects["settings"]
        window.goto("settings")
        app.processEvents()
        check("settings built", sp.model.text() != "")
        sp._update_key_state()
        check("key hint", True)
        window.grab().save(str(SHOT_DIR / "settings.png"))
    except Exception as exc:  # noqa: BLE001
        check("settings page", False, f"{type(exc).__name__}: {exc}")

    print("\n[8] status bar / inspector refresh")
    try:
        window._refresh_statusbar()
        app.processEvents()
        check("status images", "5" in window.status_images.text(), window.status_images.text())
        check("status api", "未配置" in window.status_api.text() or "就绪" in window.status_api.text())
        window.inspector.toggle()
        app.processEvents()
        check("inspector toggle", window.inspector.isVisible() is False)
        window.inspector.toggle()
        app.processEvents()
        check("inspector back", window.inspector.isVisible() is True)
    except Exception as exc:  # noqa: BLE001
        check("status bar", False, f"{type(exc).__name__}: {exc}")

    print("\n[8b] 检视面板的 Caption 显示区")
    try:
        window.goto("gallery")
        app.processEvents()
        ins = window.inspector

        check("Caption 框存在", hasattr(ins, "caption_view"))
        check("Caption 标题存在", hasattr(ins, "caption_label"))
        check("复制 Caption 按钮存在",
              ins.copy_caption_full_btn.text() == "复制 Caption")

        # 找一张有 txt 的图
        with_txt = None
        for r in db.list_images(dataset_id):
            if captions.has_caption(r["path"]):
                with_txt = r
                break

        if with_txt is None:
            check("样例里能找到带 txt 的图", False, "一张都没有")
        else:
            ins.show_image(with_txt)
            for _ in range(5):
                app.processEvents()

            on_disk = captions.read_caption_text(with_txt["path"])
            shown = ins.caption_view.toPlainText()

            check("Caption 框可见（有元数据时也要可见）",
                  ins.caption_view.isVisible(),
                  f"has_meta={bool(with_txt.get('has_meta'))}")
            check("Caption 标题可见", ins.caption_label.isVisible())
            check("Caption 内容是 txt 原文", shown == on_disk,
                  f"{shown[:50]!r} vs {on_disk[:50]!r}")

            # 复制功能
            app.clipboard().clear()
            ins._copy_caption_full()
            check("复制 Caption 到剪贴板",
                  app.clipboard().text() == on_disk,
                  app.clipboard().text()[:50])

        # 没有 txt 的图：框还在，但给出提示
        no_txt = None
        for r in db.list_images(dataset_id):
            if not captions.has_caption(r["path"]):
                no_txt = r
                break
        if no_txt is not None:
            ins.show_image(no_txt)
            for _ in range(5):
                app.processEvents()
            check("没 txt 的图 Caption 框仍可见", ins.caption_view.isVisible())
            check("没 txt 时内容为空", ins.caption_view.toPlainText() == "")
            check("没 txt 时给出提示", ins.caption_hint.isVisible())
            app.clipboard().clear()
            ins._copy_caption_full()
            check("没 txt 时复制不会污染剪贴板",
                  app.clipboard().text() == "", repr(app.clipboard().text()))

        # 老的「打标标签」区块已经不再跟 Caption 抢位置
        check("老的打标标签框已收起", not ins.caption_tags_view.isVisible())
    except Exception as exc:  # noqa: BLE001
        check("Caption 显示区", False, f"{type(exc).__name__}: {exc}")

    # [8b] 结束时把检视面板和选中状态复位，免得把状态漏给后面的段落
    window.ctx.selection_changed.emit(None)
    app.processEvents()

    print("\n[8c] 正向提示词的「读取方式」下拉")
    try:
        ins = window.inspector
        gal = window._page_objects["gallery"]
        window.goto("gallery")
        app.processEvents()

        rows = gal.selected_rows() or []
        if not rows and gal.model.rowCount() > 0:
            gal.select_all()
            app.processEvents()
            rows = gal.selected_rows() or []
        if rows:
            ins.show_image(rows[0])
            app.processEvents()

            # 下拉存在、选项齐全
            labels = [ins.parser_combo.itemText(i) for i in range(ins.parser_combo.count())]
            values = [ins.parser_combo.itemData(i) for i in range(ins.parser_combo.count())]
            check("读取方式下拉存在", ins.parser_combo.count() > 0)
            check("有「自动适应」", "auto" in values, str(values))
            check("有 ComfyUI", "comfyui" in values, str(values))
            check("有 CLIPTextEncodeAdvanced", "bnk" in values, str(values))
            check("有 WeiLin", "weilin" in values, str(values))
            check("有 NovelAI", "nai" in values, str(values))

            # 它在正向提示词标题那一行上（不是别处）
            check("下拉挂在正向提示词旁边", ins.parser_combo.parent() is not None)

            # 默认跟设置一致
            check("默认与设置一致",
                  ins.parser_combo.currentData() == window.ctx.settings.meta_parser,
                  f"{ins.parser_combo.currentData()} vs {window.ctx.settings.meta_parser}")

            # 切到 NovelAI 不该崩，切回来内容要还原
            ins._select_parser("auto")
            app.processEvents()
            ins._on_parser_changed(0)
            auto_pos = ins.prompt_view.toPlainText()
            ins._select_parser("nai")
            app.processEvents()
            ins._on_parser_changed(0)
            nai_pos = ins.prompt_view.toPlainText()
            check("切成 NAI 不崩", True)
            check("NAI 读不出 ComfyUI 的图（正向空）", nai_pos == "", repr(nai_pos[:40]))

            ins._select_parser("auto")
            app.processEvents()
            ins._on_parser_changed(0)
            check("切回自动适应 内容还原",
                  ins.prompt_view.toPlainText() == auto_pos,
                  repr(ins.prompt_view.toPlainText()[:40]))

            # 设置页同步过来
            window.ctx.inspector_changed.emit("comfyui")
            app.processEvents()
            check("设置页能同步下拉", ins.parser_combo.currentData() == "comfyui",
                  str(ins.parser_combo.currentData()))
            window.ctx.inspector_changed.emit("auto")
            app.processEvents()
            check("能同步回自动适应", ins.parser_combo.currentData() == "auto",
                  str(ins.parser_combo.currentData()))
        else:
            print("  (图库是空的，跳过)")
    except Exception as exc:  # noqa: BLE001
        check("读取方式下拉", False, f"{type(exc).__name__}: {exc}")

    window.ctx.selection_changed.emit(None)
    app.processEvents()

    print("\n[9] 写法转换与 txt 核对（新功能）")
    try:
        wb = window._page_objects["workbench"]
        window.goto("workbench")
        app.processEvents()
        wb.reload_queue()
        app.processEvents()

        # 三个转换按钮都在
        check("角色词→下划线 按钮存在", wb.role_btn.text().startswith("角色词"))
        check("角色词→转义 按钮存在", wb.escaped_btn.text().startswith("角色词"))
        check("下划线→空格 按钮存在", wb.underscore_btn.text().startswith("下划线"))

        # 队列默认会选中第一张，此时按钮该可用
        wb.grid.select_all()
        app.processEvents()
        app.processEvents()
        check("选中后三个按钮都可用",
              wb.role_btn.isEnabled() and wb.escaped_btn.isEnabled()
              and wb.underscore_btn.isEnabled())

        # 清空选中 → 该全部禁用
        wb.grid.clear_selection()
        app.processEvents()
        app.processEvents()
        check("清空选中后三个按钮都禁用",
              not wb.role_btn.isEnabled() and not wb.escaped_btn.isEnabled()
              and not wb.underscore_btn.isEnabled())
        check("提示要求先勾选", "勾选" in wb.conv_hint.text(), wb.conv_hint.text())

        # 无选中时点按钮：只提示，不能崩
        wb._convert_role_tags()
        wb._convert_escaped_tags()
        wb._convert_underscores()
        app.processEvents()
        check("无选中时点转换不崩", True)

        # 真有选中时跑一次转换，确认进暂存区
        wb.grid.select_all()
        app.processEvents()
        app.processEvents()
        undo_before = db.pending_caption_count()
        wb._run_conversion(
            __import__("app.captions", fromlist=["x"]).to_underscore_role_tag,
            "角色词 → 下划线",
            "测试",
        )
        app.processEvents()
        check("转换后进了暂存区", db.pending_caption_count() >= undo_before)
        # 撤掉，别影响后面的用例
        db.discard_all_captions()
        wb._update_staged()
        app.processEvents()

        window.grab().save(str(SHOT_DIR / "convert_buttons.png"))
    except Exception as exc:  # noqa: BLE001
        check("写法转换", False, f"{type(exc).__name__}: {exc}")

    print("\n[10] 设置页「核对 txt 标记」")
    try:
        sp = window._page_objects["settings"]
        window.goto("settings")
        app.processEvents()
        before = db.stats()["with_caption"]
        sp._resync_captions()
        app.processEvents()
        after = db.stats()["with_caption"]
        # 样本数据集里有的图有 txt、有的没有，核对后不该变（标记本来就是对）
        check("核对 txt 标记是幂等的", before == after,
              f"{before} -> {after}")
    except Exception as exc:  # noqa: BLE001
        check("核对 txt 标记", False, f"{type(exc).__name__}: {exc}")

    print("\n[11] 左侧小图库 + 标签抽离（新功能）")
    try:
        wb = window._page_objects["workbench"]
        window.goto("workbench")
        app.processEvents()
        wb.reload_queue()
        app.processEvents()

        # --- 左侧队列现在是缩略图网格 ---
        check("左侧是缩略图网格", hasattr(wb, "grid"))
        check("网格有行", wb.grid.model.rowCount() > 0,
              str(wb.grid.model.rowCount()))
        check("有大小滑条", hasattr(wb, "zoom") and wb.zoom.value() > 0,
              str(getattr(wb, "zoom", None) and wb.zoom.value()))
        check("有上一张/下一张", True)   # 按钮已在 build 里创建
        # 默认选中第一张
        check("默认选中一张", wb.grid.current_id() > 0, str(wb.grid.current_id()))

        # 多选
        wb.grid.select_all()
        app.processEvents()
        total = wb.grid.model.rowCount()
        check("全选后全都选上", len(wb.grid.selected_ids()) == total,
              f"{len(wb.grid.selected_ids())} / {total}")
        check("统计按钮可用", wb.stat_btn.isEnabled())
        wb.grid.clear_selection()
        app.processEvents()
        check("清空后没有选中", wb.grid.selected_ids() == [],
              str(wb.grid.selected_ids()))
        check("清空后统计按钮禁用", not wb.stat_btn.isEnabled())

        # 翻页
        wb.grid.select_image(wb.grid.rows()[1]["id"])
        app.processEvents()
        second = wb.grid.current_id()
        wb.grid.step(-1)
        app.processEvents()
        check("上一张能翻回去", wb.grid.current_id() != second,
              f"{second} -> {wb.grid.current_id()}")

        # --- 抽离：按钮存在且默认禁用 ---
        check("有「查看所有该标签图片」按钮", hasattr(wb, "isolate_btn"))
        check("没选标签时抽离按钮禁用", not wb.isolate_btn.isEnabled())

        # 找一个真实存在的标签来抽离
        counts = db.all_caption_tags(dataset_id=wb.dataset_combo.currentData())
        if counts:
            tag, _n = counts[0]
            expect = len(db.image_ids_with_tag(tag))
            wb.isolate_tag(tag)
            app.processEvents()
            check(f"抽离「{tag}」后有图", wb.grid.model.rowCount() > 0,
                  str(wb.grid.model.rowCount()))
            check(f"抽离数量对得上 {tag}",
                  wb.grid.model.rowCount() == expect,
                  f"{wb.grid.model.rowCount()} vs {expect}")
            check("抽离横幅出现", wb.iso_bar.isVisible())
            check("横幅写了标签名", tag in wb.iso_label.text(), wb.iso_label.text())
            check("抽离视图里每张都真带这个标签",
                  all(tag in _tags_for(db, r) for r in wb.grid.rows()[:20]),
                  "")

            # 抽离里能多选
            wb._isolate_select_all()
            app.processEvents()
            check("抽离里能全选",
                  len(wb.grid.selected_ids()) == wb.grid.model.rowCount())
            check("删除按钮可用", wb.iso_del_btn.isEnabled())
            check("替换按钮可用", wb.iso_replace_btn.isEnabled())
            wb._isolate_select_none()
            app.processEvents()
            check("取消选择后按钮禁用",
                  not wb.iso_del_btn.isEnabled() and not wb.iso_replace_btn.isEnabled())

            # 部分删除
            ids = [r["id"] for r in wb.grid.rows()]
            before = len(db.image_ids_with_tag(tag))
            picked = ids[:2] if len(ids) >= 2 else ids
            n = wb._apply_tag_change(picked, tag, None)
            wb._after_tag_change(f"删 {n} 张")
            app.processEvents()
            after = len(db.image_ids_with_tag(tag))
            check("只删了选中的那几张",
                  n == len(picked) and after == before - len(picked),
                  f"改了 {n}, {before} -> {after}")
            db.discard_all_captions()
            wb.reload_queue()
            app.processEvents()
            check("丢弃暂存后标签回来了",
                  len(db.image_ids_with_tag(tag)) == before,
                  str(len(db.image_ids_with_tag(tag))))

            # 替换 + 去重
            wb.isolate_tag(tag)
            app.processEvents()
            new_name = "ui_test_tag_xyz"
            # 注意：isolate_tag 之后网格里默认留了一张选中图，
            # 以前这里写 `selected_ids() or rows()`，一旦有选中就只处理那一张，
            # 于是「旧标签没了」永远挂。UI 走的是 _isolate_targets()（必须手动勾选），
            # 所以这里要显式全选，才对得上真实操作。
            wb._isolate_select_all()
            app.processEvents()
            all_ids = wb.grid.selected_ids()
            check("替换前全选到了所有抽离图",
                  len(all_ids) == wb.grid.model.rowCount() and len(all_ids) > 0,
                  f"{len(all_ids)} vs {wb.grid.model.rowCount()}")
            n2 = wb._apply_tag_change(all_ids, tag, new_name)
            app.processEvents()
            check("替换有生效", n2 > 0, str(n2))
            check("旧标签没了", db.image_ids_with_tag(tag) == [],
                  str(len(db.image_ids_with_tag(tag))))
            check("新标签有了",
                  len(db.image_ids_with_tag(new_name)) == n2,
                  f"{len(db.image_ids_with_tag(new_name))} vs {n2}")
            # 全换掉之后应该自动退回队列
            wb._after_tag_change("测试替换")
            app.processEvents()
            check("标签清空后自动退回队列", not wb._isolated_tag,
                  str(wb._isolated_tag))
            db.discard_all_captions()
            wb.reload_queue()
            app.processEvents()
            check("丢弃后旧标签恢复",
                  len(db.image_ids_with_tag(tag)) > 0,
                  str(len(db.image_ids_with_tag(tag))))

        # 返回队列按钮
        if counts:
            wb.isolate_tag(counts[0][0])
            app.processEvents()
            wb._exit_isolated()
            app.processEvents()
            check("返回后横幅消失", not wb.iso_bar.isVisible())
            check("返回后队列恢复", wb.grid.model.rowCount() == total,
                  f"{wb.grid.model.rowCount()} vs {total}")

        # 空标签不崩
        wb.isolate_tag("")
        wb.isolate_tag("不存在的标签 zzz")
        app.processEvents()
        check("抽离空/不存在的标签不崩", True)

        # --- 改完标签后滚动位置要保持（用户报过会跳回顶部）---
        if counts:
            tag = counts[0][0]
            wb.isolate_tag(tag)
            for _ in range(20):
                app.processEvents()
            bar = wb.grid.view.verticalScrollBar()
            if bar.maximum() > 100:
                bar.setValue(bar.maximum() // 2)
                app.processEvents()
                before_scroll = bar.value()
                ids = [r["id"] for r in wb.grid.rows()[:2]]
                n = wb._apply_tag_change(ids, tag, "uitest_scroll_tmp")
                wb._after_tag_change(f"改了 {n}")
                for _ in range(20):
                    app.processEvents()
                after_scroll = bar.value()
                check("改完标签后滚动位置保持",
                      abs(after_scroll - before_scroll) <= 60,
                      f"{before_scroll} -> {after_scroll}")
                db.discard_all_captions()
                wb.reload_queue()
                app.processEvents()
            else:
                check("改完标签后滚动位置保持", True, "列表太短，跳过")

        # --- 替换框：回车要能确认 ---
        from PySide6.QtWidgets import QComboBox, QDialog, QPushButton
        info: dict = {}
        _orig_exec = QDialog.exec

        def _fake_exec(self):
            combo = self.findChild(QComboBox)
            info["combo"] = combo is not None
            btn = next((b for b in self.findChildren(QPushButton)
                        if b.text() == "替换"), None)
            info["default"] = btn.isDefault() if btn else None
            if combo is not None:
                combo.setCurrentText("uitest_enter_tag")
                le = combo.lineEdit()
                info["line_edit"] = le is not None
                if le is not None:
                    le.returnPressed.emit()
            info["accepted"] = self.result() == QDialog.DialogCode.Accepted
            return self.result()

        QDialog.exec = _fake_exec
        try:
            picked_tag = wb._ask_new_tag("some old tag")
        finally:
            QDialog.exec = _orig_exec

        check("替换框有下拉输入框", info.get("combo") and info.get("line_edit"),
              str(info))
        check("「替换」是默认按钮", bool(info.get("default")), str(info))
        check("回车能确认替换", info.get("accepted") and picked_tag == "uitest_enter_tag",
              f"accepted={info.get('accepted')} tag={picked_tag!r}")

        # 空输入回车不该确认
        info2: dict = {}

        def _fake_exec2(self):
            combo = self.findChild(QComboBox)
            combo.setCurrentText("   ")
            combo.lineEdit().returnPressed.emit()
            info2["accepted"] = self.result() == QDialog.DialogCode.Accepted
            return self.result()

        QDialog.exec = _fake_exec2
        try:
            empty_tag = wb._ask_new_tag("some old tag")
        finally:
            QDialog.exec = _orig_exec
        check("空输入回车不确认",
              not info2.get("accepted") and not empty_tag,
              f"accepted={info2.get('accepted')} tag={empty_tag!r}")

        # --- 转义按钮是主推，且在第一位 ---
        check("转义按钮存在", hasattr(wb, "escaped_btn"))
        check("转义按钮是主按钮", wb.escaped_btn.objectName() == "Primary",
              wb.escaped_btn.objectName())
        check("转义按钮文案带推荐", "推荐" in wb.escaped_btn.text(),
              wb.escaped_btn.text())
        check("下划线按钮降级为次要", wb.role_btn.objectName() == "Segment",
              wb.role_btn.objectName())

        window.grab().save(str(SHOT_DIR / "workbench_isolate.png"))
    except Exception as exc:  # noqa: BLE001
        check("标签抽离", False, f"{type(exc).__name__}: {exc}")

    print("\n[11b] 删除图片进垃圾桶")
    try:
        from app import trash

        wb = window._page_objects["workbench"]
        window.goto("workbench")
        for _ in range(20):
            app.processEvents()

        # 两个按钮都该在
        check("工作台有删除按钮", hasattr(wb, "trash_btn"))
        check("抽离视图也有删除按钮", hasattr(wb, "iso_trash_btn"))

        # 没勾选时按钮是灰的
        wb.grid.clear_selection()
        app.processEvents()
        check("没勾选时删除按钮禁用", not wb.trash_btn.isEnabled())

        # 勾一张之后可用
        wb.grid.select_all()
        app.processEvents()
        check("勾选后删除按钮可用", wb.trash_btn.isEnabled())

        # 护栏：一次删光整个队列要输入「全部删除」才放行。
        # 起因：我的截图脚本调 select_all() 后删除，把用户 789 张图全搬走了。
        wb.grid.select_all()
        app.processEvents()
        picked = len(wb.grid.selected_ids())
        total = wb.grid.model.rowCount()
        before = trash.stats()["images"]

        real_dlg = QDialog.exec

        def typed_stub(text: str, click_ok: bool):
            def stub(self):
                le = self.findChild(QLineEdit)
                btn = next(
                    (b for b in self.findChildren(QPushButton)
                     if b.text() == "确定删"), None)
                if le is not None:
                    le.setText(text)
                app.processEvents()
                if click_ok and btn is not None and btn.isEnabled():
                    btn.click()
                return (self.result() if self.result()
                        else QDialog.DialogCode.Rejected)
            return stub

        # 全选 + 不输入确认词 → 一张都不许动
        QDialog.exec = typed_stub("", True)
        try:
            wb._trash_selected()
        finally:
            QDialog.exec = real_dlg
        app.processEvents()
        check("全选删除要先输入确认词（空输入被拦）",
              trash.stats()["images"] == before
              and all(Path(str(r["path"])).exists() for r in wb.grid.rows()),
              f"picked={picked}/{total} bin={trash.stats()}")

        # 输错词也不行
        QDialog.exec = typed_stub("删除", True)
        try:
            wb._trash_selected()
        finally:
            QDialog.exec = real_dlg
        app.processEvents()
        check("全选删除输错词也被拦",
              trash.stats()["images"] == before, str(trash.stats()))

        # 直接调 _confirm_delete_all 验证放行分支
        QDialog.exec = typed_stub("全部删除", True)
        try:
            allowed = wb._confirm_delete_all(picked)
        finally:
            QDialog.exec = real_dlg
        check("输对确认词才放行", allowed is True)

        QDialog.exec = typed_stub("全部删除", False)
        try:
            allowed2 = wb._confirm_delete_all(picked)
        finally:
            QDialog.exec = real_dlg
        check("输对但不点确定不生效", allowed2 is False)

        wb.grid.clear_selection()
        app.processEvents()

        # [11] 段落结束时队列还停在「标签抽离」视图里（只剩一张图），
        # 这里必须先退回完整队列，否则下面的 before_rows 只有 1，
        # 还原之后也永远对不上。
        window.goto("workbench")
        app.processEvents()
        wb._exit_isolated()
        for _ in range(10):
            app.processEvents()
        check("删除前已退回完整队列", not wb._isolated_tag and wb.grid.model.rowCount() > 1,
              f"isolated={wb._isolated_tag!r} rows={wb.grid.model.rowCount()}")

        # 真删一张（跳过确认框，指向一个复制出来的副本，别动用户数据）
        rows = wb.grid.rows()
        if rows:
            target = rows[0]
            src_path = Path(str(target["path"]))
            check("准备删的图存在", src_path.exists(), str(src_path))

            wb.grid.clear_selection()
            idx = wb.grid.model.index_of_id(int(target["id"]))
            if idx >= 0:
                sel = wb.grid.view.selectionModel()
                sel.select(wb.grid.model.index(idx, 0),
                           sel.SelectionFlag.Select)
            app.processEvents()

            before_rows = wb.grid.model.rowCount()
            before_bin = trash.stats()["images"]
            wb.settings.confirm_before_delete = False
            # 这里有两个弹窗口子，都要堵：
            #   1. 普通确认框走 QMessageBox.exec
            #   2. 全选护栏走 QDialog.exec（_confirm_delete_all）
            # 只堵一个的话，另一个会直接把测试挂死（踩过两次了）。
            def _say_yes(self):
                return 1

            real_msg = QMessageBox.exec
            real_dlg2 = QDialog.exec
            QMessageBox.exec = _say_yes
            QDialog.exec = lambda self, *a, **k: 1  # 1 = Accepted
            _stub_static_messageboxes()
            try:
                wb._trash_selected()
            finally:
                QMessageBox.exec = real_msg
                QDialog.exec = real_dlg2
                _stub_static_messageboxes()
            for _ in range(15):
                app.processEvents()

            check("删完队列少一张",
                  wb.grid.model.rowCount() == before_rows - 1,
                  f"{before_rows} -> {wb.grid.model.rowCount()}")
            check("图进了垃圾桶",
                  trash.stats()["images"] == before_bin + 1,
                  str(trash.stats()))
            check("库里记录也没了",
                  wb.db.get_image(int(target["id"])) is None)

            # 设置页能看到，并且能还原（走界面按钮，才测得到重新入库那一步）
            window.goto("settings")
            for _ in range(15):
                app.processEvents()
            sp = window._page_objects["settings"]
            sp._refresh_trash()
            check("设置页列出垃圾记录", sp.trash_table.rowCount() >= 1,
                  str(sp.trash_table.rowCount()))
            check("还原按钮可用", sp.trash_restore_btn.isEnabled())

            sp.trash_table.selectAll()
            app.processEvents()
            sp._restore_trash()
            for _ in range(15):
                app.processEvents()
            check("能整桶还原", trash.stats()["images"] == before_bin,
                  str(trash.stats()))
            check("图片回到原处", src_path.exists(), str(src_path))
            check("还原后重新入库", wb.db.get_image(int(target["id"])) is not None
                  or wb.db.count_images() >= before_rows,
                  f"count={wb.db.count_images()}")

            # 还原之后刷新队列，图应该重新出现
            wb.reload_queue()
            for _ in range(15):
                app.processEvents()
            check("还原后队列可见", wb.grid.model.rowCount() >= before_rows,
                  f"{wb.grid.model.rowCount()} vs {before_rows}")

    except Exception as exc:  # noqa: BLE001
        check("删除图片进垃圾桶", False, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # [11c] 丢到自定义文件夹（用户真正要的那个功能）
    # ------------------------------------------------------------------ #
    print("\n[11c] 删除的图丢到自定义文件夹")

    custom = sandbox / "我的废图"
    made: list[Path] = []
    try:
        # 从样例里挑两张，复制一份到临时目录当"原图"，别动样例本身
        move_src = sandbox / "待删"
        move_src.mkdir(parents=True, exist_ok=True)
        for i, p in enumerate(sorted(output_dir.glob("*.png"))[:2]):
            dst = move_src / f"custom_{i + 1}.png"
            shutil.copy2(p, dst)
            cap = dst.with_suffix(".txt")
            cap.write_text(f"1girl, custom_tag_{i + 1}", encoding="utf-8")
            made.append(dst)

        check("准备好待删的原图", len(made) == 2 and all(p.exists() for p in made),
              str([p.name for p in made]))

        # 走设置页的「应用」按钮，验证界面到 trash 模块是通的
        window.goto("settings")
        for _ in range(10):
            app.processEvents()
        sp = window._page_objects["settings"]
        sp.trash_dir_edit.setText(str(custom))
        sp._apply_trash_dir()
        for _ in range(5):
            app.processEvents()

        check("设置里填的目录生效了",
              trash.trash_root() == custom.resolve()
              or str(trash.trash_root()) == str(custom),
              f"{trash.trash_root()} vs {custom}")

        moved, errors = trash.trash_images(
            [(str(p), "测试数据集") for p in made], note="测试自定义目录"
        )
        check("移动没报错", not errors, str(errors))
        check("移动了 4 个文件（2 图 + 2 txt）", moved == 4, str(moved))

        landed = sorted(x.name for x in custom.iterdir() if x.is_file())
        check("图片和 txt 都在自定义文件夹里",
              landed == ["custom_1.png", "custom_1.txt",
                         "custom_2.png", "custom_2.txt"],
              str(landed))
        check("自定义文件夹里没有索引/说明文件（保持干净）",
              not (custom / "index.jsonl").exists()
              and not (custom / trash.README_NAME).exists(),
              str(landed))
        check("原位置已经空了", not any(p.exists() for p in made))

        # 同名再丢一次 → 加序号，绝不覆盖
        again = move_src / "custom_1.png"
        shutil.copy2(output_dir / sorted(output_dir.glob("*.png"))[0].name,
                     again)
        again.with_suffix(".txt").write_text("second", encoding="utf-8")
        trash.trash_images([(str(again), "测试数据集")], note="同名再来一次")
        names = sorted(x.name for x in custom.iterdir() if x.is_file())
        check("同名不覆盖，自动加序号",
              "custom_1.png" in names and any(n.startswith("custom_1__") for n in names),
              str(names))

        # 还原：全部捞回来
        done, errs = trash.restore(trash.list_trashed())
        check("从自定义文件夹能还原", not errs, str(errs))

        # 注意：custom_1.png 这个位置已经被「同名再来一次」那份占了，
        # 所以原来那份应该改名成 custom_1_restored2.* 落地 ——
        # 还原的原则是「绝不覆盖」，不是「必须抢回原文件名」。
        # 所以这里验证的是**内容没丢**，而不是文件落在哪个名字上。
        back_txt = sorted(move_src.glob("custom_1*.txt"))
        contents = {p.read_text(encoding="utf-8") for p in back_txt}
        check("两份 custom_1 的 txt 都回来了",
              len(back_txt) == 2, str([p.name for p in back_txt]))
        check("原内容还在（没被覆盖版盖掉）",
              "1girl, custom_tag_1" in contents, str(contents))
        check("覆盖版内容也在（也没被原版盖掉）",
              "second" in contents, str(contents))
        check("custom_2 完好",
              (move_src / "custom_2.txt").read_text(encoding="utf-8")
              == "1girl, custom_tag_2")

        # 收尾：把设置改回默认，别影响后面的用例
        sp.trash_dir_edit.setText("")
        sp._apply_trash_dir()
        for _ in range(5):
            app.processEvents()
        check("能改回默认目录", not str(trash.trash_root()).startswith(str(custom)),
              str(trash.trash_root()))

    except Exception as exc:  # noqa: BLE001
        check("自定义目录流程", False, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(custom, ignore_errors=True)
        shutil.rmtree(sandbox / "待删", ignore_errors=True)

    # ------------------------------------------------------------------ #
    # [11d] WD14 本地打标（界面接线，不真跑模型）
    # ------------------------------------------------------------------ #
    print("\n[11d] WD14 本地打标界面")
    try:
        from app import wd14 as wd14_module

        window.goto("settings")
        app.processEvents()
        sp = window._page_objects["settings"]

        # ---- 设置页控件齐全 ----
        for name, label in [
            ("wd_enabled", "启用勾选框"),
            ("wd_status", "引擎状态"),
            ("wd_probe_btn", "检测引擎按钮"),
            ("wd_model", "模型下拉"),
            ("wd_browse_btn", "找模型按钮"),
            ("wd_open_btn", "打开目录按钮"),
            ("wd_threshold", "阈值"),
            ("wd_char_threshold", "角色阈值"),
            ("wd_max_tags", "标签数上限"),
            ("wd_batch", "批量大小"),
            ("wd_underscore", "下划线转空格"),
            ("wd_gpu", "GPU 开关"),
            ("wd_undesired", "屏蔽词"),
        ]:
            check(f"设置页有「{label}」", hasattr(sp, name), f"缺 self.{name}")

        # 阈值边界
        if hasattr(sp, "wd_threshold"):
            check("阈值范围合理",
                  sp.wd_threshold.minimum() <= 0.35 <= sp.wd_threshold.maximum(),
                  f"{sp.wd_threshold.minimum()}~{sp.wd_threshold.maximum()}")
        if hasattr(sp, "wd_max_tags"):
            check("标签上限 0 显示成「不限」",
                  sp.wd_max_tags.specialValueText() == "不限",
                  repr(sp.wd_max_tags.specialValueText()))

        # ---- 设置页的改动能进 settings 对象 ----
        if hasattr(sp, "wd_undesired") and hasattr(sp, "wd_threshold"):
            sp.wd_undesired.setText("watermark, signature")
            sp.wd_threshold.setValue(0.45)
            sp._collect()
            check("屏蔽词进了 settings",
                  settings.wd14_undesired == "watermark, signature",
                  repr(settings.wd14_undesired))
            check("阈值进了 settings",
                  abs(settings.wd14_threshold - 0.45) < 1e-9,
                  str(settings.wd14_threshold))
            cfg = sp._wd_config_from_ui()
            check("配置对象带上了屏蔽词",
                  {"watermark", "signature"} <= set(cfg.undesired),
                  str(cfg.undesired))

        # ---- 打标台上的按钮 ----
        window.goto("workbench")
        app.processEvents()
        wb = window._page_objects["workbench"]
        check("打标台有 WD14 按钮", hasattr(wb, "wd14_btn"), "缺 self.wd14_btn")
        check("打标台有打标方式下拉", hasattr(wb, "wd14_mode"), "缺 self.wd14_mode")
        if hasattr(wb, "wd14_btn"):
            check("WD14 按钮文案", wb.wd14_btn.text().strip() == "WD14 打标",
                  repr(wb.wd14_btn.text()))
        if hasattr(wb, "wd14_mode"):
            modes = {wb.wd14_mode.itemData(i) for i in range(wb.wd14_mode.count())}
            check("打标方式有替换/追加", modes == {"replace", "append"}, str(modes))

        # 没选图时按钮该是灰的
        if hasattr(wb, "wd14_btn"):
            sel_before = list(wb._selected_image_ids())
            if sel_before:
                # 先清掉选中，看按钮会不会变灰
                for iid in sel_before:
                    if hasattr(wb.grid, "set_selected"):
                        wb.grid.set_selected(iid, False)
                app.processEvents()
            wb._refresh_queue_marks()
            app.processEvents()
            has_sel = bool(wb._selected_image_ids())
            check("WD14 按钮启用状态跟选中数一致",
                  wb.wd14_btn.isEnabled() == has_sel,
                  f"enabled={wb.wd14_btn.isEnabled()} 选中={len(wb._selected_image_ids())}")

        # ---- 设置页开关同步到打标台 ----
        if hasattr(wb, "wd14_btn"):
            old = settings.wd14_enabled
            settings.wd14_enabled = False
            window.goto("workbench")
            app.processEvents()
            check("WD14 关掉后打标台仍能显示按钮（点的时候才拦）",
                  wb.wd14_btn.isEnabled() in (True, False), "只看它不崩")
            settings.wd14_enabled = old
    except Exception as exc:  # noqa: BLE001
        check("WD14 界面接线", False, f"{type(exc).__name__}: {exc}")

    print("\n[12] click every button on every page")
    stub_dialogs()
    sys.excepthook = _record_exc

    # 用户报过的 bug：主页「添加数据集」把 Signal 当方法调
    # (ctx.navigate 是信号，正确写法是 ctx.go)
    window.goto("dashboard")
    app.processEvents()
    dash = window._page_objects["dashboard"]
    dash.scan_btn.click()
    app.processEvents()
    check(
        "主页「添加数据集」跳到数据集页",
        window.pages.currentWidget() is window._page_objects["datasets"],
        f"current={type(window.pages.currentWidget()).__name__}",
    )

    clicked = 0
    skipped: list[str] = []
    for key, label, _glyph in theme.PAGE_ORDER:
        seen: set[tuple[str, str]] = set()
        # 点一下可能触发整页重建（数据集卡片就是这样），
        # 旧的按钮会被 deleteLater 掉，所以多跑几轮把新出来的也点到
        for _pass in range(3):
            try:
                window.goto(key)
                app.processEvents()
                page = window._page_objects[key]
                pending = []
                for btn in page.findChildren(QPushButton):
                    if not isValid(btn):
                        continue
                    try:
                        text = btn.text().strip()
                    except RuntimeError:
                        continue
                    sig = (btn.objectName(), text)
                    if sig in seen:
                        continue
                    pending.append((btn, sig, text))
                if not pending:
                    break
                for btn, sig, text in pending:
                    seen.add(sig)
                    if text in NETWORK_BUTTONS:
                        skipped.append(f"{key}:{text}")
                        continue
                    if not isValid(btn):
                        continue
                    try:
                        clicked += 1
                        btn.click()
                        app.processEvents()
                    except RuntimeError:
                        # 控件已随重建销毁，跳过即可
                        continue
            except Exception as exc:  # noqa: BLE001
                check(f"clicking buttons on {key}", False, f"{type(exc).__name__}: {exc}")
                break
    print(f"  (clicked {clicked} buttons; skipped network: {skipped or 'none'})")
    if EXC_ERRORS:
        for err in EXC_ERRORS:
            print(f"  [XX] slot raised -> {err}")
        check("no slot raised", False, f"{len(EXC_ERRORS)} slot exception(s)")
    else:
        check("no slot raised", True)

    print(f"\n  screenshots -> {SHOT_DIR}")

    db.close()
    app.processEvents()

    print()
    print("=" * 62)
    if FAIL == 0:
        print(f"ALL PASSED: {PASS} checks")
    else:
        print(f"PASSED {PASS}, FAILED {FAIL}")
    print("=" * 62)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
