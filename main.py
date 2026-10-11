"""ComfyUI 工作台 —— 启动入口。

用法：
    python main.py
    或者打包成 exe 后直接双击 dist/ComfyUI-Workbench/ComfyUI-Workbench.exe

排错：日志写在数据目录的 workbench.log 里（默认 %APPDATA%\\ComfyUI-Workbench\\）。
打包成 --windowed 之后看不到任何控制台输出，出问题先看这个文件。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import traceback
from pathlib import Path

# 让 `python main.py` 和 `python -m app` 都能跑
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

APP_NAME = "ComfyUI 工作台"
ORG_NAME = "ComfyWorkbench"

LOG_PATH: Path | None = None


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def _resolve_log_path() -> Path:
    """日志优先放数据目录；数据目录本身出问题时退回临时目录。"""
    try:
        from app.paths import data_dir  # noqa: PLC0415

        return data_dir() / "workbench.log"
    except Exception:  # noqa: BLE001
        import tempfile  # noqa: PLC0415

        return Path(tempfile.gettempdir()) / "ComfyUI-Workbench.log"


def _log(message: str) -> None:
    global LOG_PATH
    if LOG_PATH is None:
        LOG_PATH = _resolve_log_path()
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass
    # 有控制台的时候（python main.py / --console 打包）也打一份
    try:
        print(line, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


def _headless() -> bool:
    """离屏或显式禁用弹窗时不弹模态框，否则自动化测试会永远卡住。"""
    if os.environ.get("COMFY_WORKBENCH_NO_DIALOG"):
        return True
    return os.environ.get("QT_QPA_PLATFORM", "").lower() in {"offscreen", "minimal"}


def _show_error(title: str, text: str, detail: str = "") -> None:
    """把错误告诉用户：写日志 + 尽量弹窗。"""
    _log(f"[错误] {title}: {text}")
    if detail:
        _log(detail.rstrip())
    if _headless():
        return
    try:
        from PySide6.QtWidgets import QMessageBox  # noqa: PLC0415

        box = QMessageBox()
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText(text)
        if detail:
            box.setDetailedText(detail)
        box.exec()
    except Exception:  # noqa: BLE001
        pass


def _install_excepthook() -> None:
    """未捕获异常写日志 + 弹窗，不要一闪而过。"""

    def hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        # 先原样打到 stderr：--console 打包时这是唯一能看到真相的地方
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        _show_error("出错了", f"{exc_type.__name__}: {exc_value}", detail)

    sys.excepthook = hook


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def _enable_dpi_awareness() -> None:
    """高分屏下别糊；Qt6 默认已处理，这里只是保险。"""
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass


def _check_wd14() -> int:
    """打包后验证 WD14 能不能在 exe 里跑起来。

    用法：ComfyUI-Workbench.exe --check-wd14 [图片路径...]
    把结果写进 workbench.log 再退出，方便 --windowed 下排查。
    不启动主窗口，所以不会碰用户数据。
    """
    _log("=" * 50)
    _log(f"[自检] frozen={bool(getattr(sys, 'frozen', False))} "
         f"python={sys.version.split()[0]}")
    try:
        from app import wd14  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        _log(f"[自检] 导入 app.wd14 失败: {type(exc).__name__}: {exc}")
        return 2

    dirs = wd14._nvidia_dll_dirs()  # noqa: SLF001
    _log(f"[自检] 找到 {len(dirs)} 个 CUDA DLL 目录")
    for d in dirs:
        _log(f"[自检]   {d}")

    model = wd14.pick_default_model()
    _log(f"[自检] 默认模型: {model or '(没找到)'}")
    if not model:
        return 3

    tagger = wd14.Wd14Tagger(wd14.default_config(use_gpu=True))
    status = tagger.load()
    _log(f"[自检] 加载: available={status.available} provider={status.provider!r} "
         f"model={status.model!r} error={status.error!r}")
    if not status.available:
        return 4

    # 有图就真打一张
    targets = [a for a in sys.argv[1:] if not a.startswith("-")]
    targets = [t for t in targets if Path(t).is_file()][:3]
    if not targets:
        _log("[自检] 没给图片路径，只验证到加载")
        return 0
    for path in targets:
        tags = tagger.tag_image(path)
        _log(f"[自检] {Path(path).name} -> {len(tags)} 个标签: {', '.join(tags[:12])}")
    return 0


def main() -> int:
    global LOG_PATH
    LOG_PATH = _resolve_log_path()
    _log("=" * 50)
    _log(f"启动  frozen={bool(getattr(sys, 'frozen', False))} python={sys.version.split()[0]}")
    _log(f"日志文件: {LOG_PATH}")

    # 隐藏自检入口：打包后验证 WD14（不弹窗，写完日志就退）
    if "--check-wd14" in sys.argv:
        return _check_wd14()

    _install_excepthook()
    _enable_dpi_awareness()
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    try:
        from PySide6.QtCore import Qt  # noqa: PLC0415
        from PySide6.QtGui import QIcon  # noqa: PLC0415
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415
    except ImportError as exc:
        _log(f"PySide6 导入失败: {exc}")
        print("缺少 PySide6。先安装依赖：", file=sys.stderr)
        print("    pip install -r requirements.txt", file=sys.stderr)
        return 2

    _log("PySide6 导入成功")

    from app.config import load_settings  # noqa: PLC0415
    from app.db import Database  # noqa: PLC0415
    from app.paths import DataDirError, data_dir, data_dir_candidates  # noqa: PLC0415
    from app.ui import theme  # noqa: PLC0415
    from app.ui.main_window import MainWindow  # noqa: PLC0415
    from app.ui.main_window import _draw_icon  # noqa: PLC0415
    from app import paths as paths  # noqa: PLC0415

    try:
        data_path = data_dir()
    except DataDirError as exc:
        _show_error("无法创建数据目录", str(exc), traceback.format_exc())
        return 1

    _log(f"数据目录: {data_path}")
    # reason 是排查「数据看起来不见了」的关键：
    # 如果这里显示「新建（所有候选都没有数据）」，而用户明明有数据，
    # 说明真正的库在别的候选目录里 —— 别急着下"被删了"的结论。
    if paths.LAST_RESOLUTION.get("reason"):
        _log(f"  理由: {paths.LAST_RESOLUTION['reason']}")
    _log("  候选: " + " | ".join(str(p) for p in data_dir_candidates()))
    if paths.LAST_RESOLUTION.get("rejected"):
        _log(f"  被拒: {paths.LAST_RESOLUTION['rejected']}")

    # 别的候选目录里也有数据 —— 典型是目录漂移之后留下的"孤岛"。
    # 用户看不到那边的内容，日志里必须留痕，否则又变成"数据莫名少了"。
    try:
        others = paths.other_data_dirs()
    except Exception:  # noqa: BLE001
        others = []
    if others:
        _log("  注意: 以下目录里也有数据，但本次没有使用：")
        for other in others:
            _log(f"        {other}")
        _log("        （如果发现数据集变少，可能是数据分散在这些目录里）")

    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName(ORG_NAME)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)

    app = QApplication(sys.argv)
    _log(f"QApplication 就绪 platform={app.platformName()}")
    settings = load_settings()
    _log("配置已加载")
    theme.apply_theme(app, settings)

    # 删除图片的目标目录（设置里可改；空则用 data/trash）
    from app import trash as _trash  # noqa: PLC0415

    _saved_trash = getattr(settings, "trash_dir", "")
    try:
        _trash.set_trash_dir(_saved_trash)
    except _trash.UnsafeTrashRoot as exc:
        # 旧版本可能存进了盘根/系统目录。不回退成删除，直接退回默认桶，
        # 并把设置里的危险值清掉 —— 宁可位置变了，也不能删错地方。
        _trash.set_trash_dir("")
        settings.trash_dir = ""
        _log(f"[安全] 已忽略不安全的删除目录：{exc}")
    if getattr(settings, "trash_dir", "").strip():
        # trash_root() 现在会把「环境变量指向危险目录」也拦下来并抛错，
        # 这里必须兜住 —— 否则一个过期的 COMFY_WORKBENCH_TRASH_DIR
        # 就能让程序在启动时崩掉（这行以前是不会抛的）。
        try:
            _log(f"删除目录: {_trash.trash_root()}")
        except _trash.UnsafeTrashRoot as exc:
            _log(f"[安全] 删除目录不可用，将退回默认桶：{exc}")
        except Exception as exc:  # noqa: BLE001
            _log(f"[安全] 读取删除目录失败：{type(exc).__name__}: {exc}")

    try:
        db = Database()
        _log(f"数据库已打开: {db.path}")
    except Exception as exc:  # noqa: BLE001
        _show_error(
            "无法打开数据库",
            f"打开工作台数据库失败：\n{exc}\n\n数据目录权限或文件被占用都会导致这种情况。",
            traceback.format_exc(),
        )
        return 1

    app.setWindowIcon(_draw_icon("dashboard", 64))

    window = MainWindow(db, settings)
    _log("主窗口构建完成")
    window.show()
    _log("进入事件循环")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
