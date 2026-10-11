"""打包成单文件 / 单目录 exe。

用法：
    python build.py            # 默认 onedir（启动快，推荐）
    python build.py --onefile  # 单文件（启动慢，体积一样但要多解压一次）

产物在 dist/ComfyUI-Workbench/ 里。

⚠ 数据目录保护
``--noconfirm`` 会先删掉整个输出目录再重写，而输出目录里可能正好躺着
用户数据（``%APPDATA%`` 不可写时程序会退到 ``dist/ComfyUI-Workbench/data/``）。
本脚本打包前会先把 data/ 挪到安全位置，打完再原样放回去。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "ComfyUI-Workbench"

# 明确排除用不到的重型模块，能砍掉不少体积。
# 注意：千万别排除 shiboken6 / shiboken6.Shiboken，
# PySide6 靠它加载 Qt 绑定，排除后 exe 一启动就 ImportError。
EXCLUDES = [
    # Runtime tagging does not use setuptools. Old host pkg_resources versions
    # can crash a frozen Python 3.12+ app before main.py is entered.
    "pkg_resources", "setuptools",
    "tkinter", "unittest", "pydoc", "doctest", "test",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtSql", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtTest",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSvgWidgets",
    # 打标/训练用的重型库，Workbench 自己用不到
    "matplotlib", "scipy", "pandas", "cv2", "torch", "torchvision",
    "transformers", "cupy", "tensorflow", "jax", "sklearn",
]

# 瘦身版额外排除：WD14 全靠 onnxruntime，砍掉它功能自动降级
SLIM_EXCLUDES = [
    "onnxruntime", "onnxruntime_gpu", "onnxruntime.capi",
    "nvidia", "cuda",
]

# WD14 需要的 CUDA 运行库（DLL 放在 nvidia/<pkg>/bin 下）。
# 只收这些包，别用 --collect-all nvidia，那会把 2.4 GB 全塞进来。
CUDA_PACKAGES = [
    "nvidia.cublas", "nvidia.cuda_runtime", "nvidia.cudnn", "nvidia.cufft",
    "nvidia.cuda_nvrtc", "nvidia.nvjitlink",
]


def _has_onnxruntime() -> bool:
    try:
        import onnxruntime  # noqa: F401,PLC0415
    except ImportError:
        return False
    return True


def _pyside_runtime_dlls() -> list[Path]:
    """补齐 PyInstaller 6.22 偶尔漏掉的 Qt C++ 运行库。

    Qt6Core.dll 直接依赖 ``msvcp140_1.dll``。部分机器的系统目录里碰巧有它，
    会把漏包问题掩盖掉；但 PyInstaller 的隔离 DLL 搜索不会可靠地使用系统
    副本，最终表现为 ``DLL load failed while importing QtCore``。
    """
    try:
        import PySide6  # noqa: PLC0415
    except ImportError:
        return []
    base = Path(PySide6.__file__).resolve().parent
    return [dll for name in ("msvcp140_1.dll",) if (dll := base / name).is_file()]


def _remove_foreign_icu(dist: Path) -> None:
    """移除从外部 PATH 误收进来的 ICU。

    Windows 版 Qt6Core 使用系统 ICU。若当前 PATH 恰好含 Poppler/Conda 的
    ICU 78，PyInstaller 会把那套同名 DLL 捡进 ``_internal``，运行时反而
    覆盖系统 ICU，报 ``无法定位程序输入点 ucnv_open``。
    PySide6 自己不携带这些文件，因此这里删掉的只可能是外来冲突副本。
    """
    internal = dist / "_internal"
    removed: list[str] = []
    for name in ("icuuc.dll", "icudt78.dll"):
        dll = internal / name
        if dll.is_file():
            dll.unlink()
            removed.append(name)
    if removed:
        print("移除冲突的外来 ICU：", ", ".join(removed))


def _cuda_bin_dirs() -> list[Path]:
    """找出 nvidia-*-cu12 里真正放 DLL 的目录。

    onnxruntime 加载 CUDA EP 时会去这些目录找 cublas64_12.dll / cudnn*.dll，
    所以必须把 ``nvidia/<pkg>/bin`` 原样放进 exe，并让启动时 prepend 到
    PATH（app/wd14.py 的 ``_nvidia_dll_dirs()`` 干的就是这件事）。

    注意：``nvidia`` 与 ``nvidia.cublas`` 都是**命名空间包**，``__file__`` 是
    None，所以不能走 ``mod.__file__``，得从 ``__path__`` 拿目录。
    """
    dirs: list[Path] = []
    for pkg in CUDA_PACKAGES:
        try:
            mod = __import__(pkg, fromlist=["__path__"])
        except ImportError:
            continue
        for entry in getattr(mod, "__path__", []) or []:
            base = Path(entry)
            for sub in ("bin", "lib"):
                cand = base / sub
                if cand.is_dir() and any(cand.glob("*.dll")):
                    if cand not in dirs:
                        dirs.append(cand)
    return dirs


def _stash_user_data() -> Path | None:
    """把 dist/<NAME>/data 挪出来，避免被 PyInstaller 的 --noconfirm 删掉。

    返回暂存目录（没有数据时返回 None）。
    """
    data = ROOT / "dist" / NAME / "data"
    if not data.is_dir():
        return None
    stash = ROOT / ".build-data-stash"
    if stash.exists():
        raise RuntimeError(
            f"发现上次打包遗留的数据暂存目录：{stash}。请先核对并恢复，不能覆盖它。"
        )
    shutil.move(str(data), str(stash))
    files = sum(1 for p in stash.rglob("*") if p.is_file())
    size = sum(p.stat().st_size for p in stash.rglob("*") if p.is_file())
    print(f"已暂存用户数据：{stash}（{files} 个文件，{size / 1024 / 1024:.1f} MB）")
    return stash


def _prune_user_data(data: Path) -> None:
    """清掉 data/ 里可再生或过期的文件，免得 dist 越滚越大。

    - ``thumbs/``：缩略图缓存，程序会自己重建（几十 MB 起）。
    - ``*.db-wal`` / ``*.db-shm``：SQLite 临时文件，正常退出会自己收掉。
    - ``*.backup-before-migrate``：一次性迁移备份，迁移完就没用了。
      （只在主库行数不少于备份时才删，避免误删唯一的数据副本。）
    """
    if not data.is_dir():
        return
    freed = 0.0

    thumbs = data / "thumbs"
    if thumbs.is_dir():
        freed += sum(f.stat().st_size for f in thumbs.rglob("*") if f.is_file())
        shutil.rmtree(thumbs, ignore_errors=True)
        print(f"  清理缩略图缓存：{thumbs}")

    for pattern in ("*.db-wal", "*.db-shm"):
        for p in data.glob(pattern):
            freed += p.stat().st_size
            p.unlink(missing_ok=True)
            print(f"  清理 SQLite 临时文件：{p.name}")

    for bak in data.glob("*.backup-before-migrate"):
        main = Path(str(bak).replace(".backup-before-migrate", ""))
        if not main.exists():
            continue
        try:
            import sqlite3  # noqa: PLC0415

            with sqlite3.connect(f"file:{main}?mode=ro", uri=True) as con:
                main_rows = con.execute(
                    "SELECT COUNT(*) FROM images"
                ).fetchone()[0]
            with sqlite3.connect(f"file:{bak}?mode=ro", uri=True) as con:
                bak_rows = con.execute(
                    "SELECT COUNT(*) FROM images"
                ).fetchone()[0]
            if main_rows < bak_rows:
                print(f"  保留迁移备份（主库更小：{main_rows} < {bak_rows}）")
                continue
        except Exception:
            continue
        freed += bak.stat().st_size
        bak.unlink(missing_ok=True)
        print(f"  清理过期迁移备份：{bak.name}")

    if freed:
        print(f"  共释放 {freed / 1024 / 1024:.1f} MB")


def _restore_user_data(stash: Path | None) -> None:
    """把暂存的数据放回 dist/<NAME>/data。"""
    if stash is None:
        return
    data = ROOT / "dist" / NAME / "data"
    data.parent.mkdir(parents=True, exist_ok=True)
    if data.exists():
        unexpected = ROOT / f"unexpected-build-data-{__import__('time').strftime('%Y%m%d-%H%M%S')}"
        shutil.move(str(data), str(unexpected))
        print(f"警告：打包期间出现新 data 目录，已保留在 {unexpected}")
    shutil.move(str(stash), str(data))
    print(f"已恢复用户数据：{data}")


def build(onefile: bool = False, slim: bool = False) -> int:
    try:
        import PyInstaller  # noqa: F401,PLC0415
    except ImportError:
        print("没装 PyInstaller。先跑：pip install pyinstaller", file=sys.stderr)
        return 2

    excludes = list(EXCLUDES)
    if slim:
        excludes += SLIM_EXCLUDES
        print("瘦身模式：不带 WD14 本地打标")
    elif not _has_onnxruntime():
        print("提醒：本机没装 onnxruntime，打出来的 exe 不能本地打标。")
        print("      要带 WD14 先 pip install onnxruntime-gpu，或改用 --slim 明确瘦身。")
        excludes += SLIM_EXCLUDES

    stash = _stash_user_data()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",                     # 不弹黑框
        "--name", NAME,
        "--onefile" if onefile else "--onedir",
        "--collect-submodules", "PySide6.QtCore",
    ]
    for mod in excludes:
        cmd += ["--exclude-module", mod]

    qt_runtime_dlls = _pyside_runtime_dlls()
    for dll in qt_runtime_dlls:
        cmd += ["--add-binary", f"{dll};PySide6"]
    if qt_runtime_dlls:
        print("补齐 Qt 运行库：", ", ".join(dll.name for dll in qt_runtime_dlls))

    # WD14：把 onnxruntime 的 CUDA provider 与 nvidia 运行库一起收进来。
    # numpy 之前被整个排除，但 onnxruntime 依赖它（还依赖 PIL，PIL 本来就在用）。
    if not slim and _has_onnxruntime():
        cmd += ["--collect-binaries", "onnxruntime"]
        cmd += ["--hidden-import", "onnxruntime.capi._pybind_state"]
        cmd += ["--hidden-import", "onnxruntime.capi.onnxruntime_pybind11_state"]
        for pkg in CUDA_PACKAGES:
            cmd += ["--collect-binaries", pkg]
        bins = _cuda_bin_dirs()
        if bins:
            print(f"收集到 {len(bins)} 个 CUDA DLL 目录：")
            for d in bins:
                n = len(list(d.glob("*.dll")))
                print(f"  {d}  ({n} 个 DLL)")
        else:
            print("提醒：没找到 nvidia-*-cu12 的 DLL，exe 里只能跑 CPU 打标（慢但能用）")
    cmd.append(str(ROOT / "main.py"))

    print("运行：", " ".join(cmd))
    try:
        code = subprocess.call(cmd, cwd=str(ROOT))
    except BaseException:
        _restore_user_data(stash)
        raise
    _restore_user_data(stash)
    if code != 0:
        return code

    dist = ROOT / "dist" / (NAME + (".exe" if onefile else ""))
    if dist.is_dir():
        _remove_foreign_icu(dist)
    print()
    print("=" * 60)
    print("打包完成：", dist)
    if dist.is_dir():
        total = sum(f.stat().st_size for f in dist.rglob("*") if f.is_file())
        print(f"目录体积：{total / 1024 / 1024:.1f} MB")
    print("双击 exe 即可运行。数据存在 %APPDATA%\\ComfyUI-Workbench\\")
    print("（%APPDATA% 不可写时退到 exe 同级的 data\\，那里不会被本脚本删掉）")
    print("=" * 60)
    return 0


def clean() -> None:
    if (ROOT / "dist" / NAME / "data").exists() or (ROOT / ".build-data-stash").exists():
        raise RuntimeError("发现用户数据目录，拒绝清理 dist。请先关闭程序并将数据移到独立安全位置。")
    for name in ("build", "dist", f"{NAME}.spec"):
        target = ROOT / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean()
        print("已清理 build/dist")
        raise SystemExit(0)
    raise SystemExit(build(
        onefile="--onefile" in sys.argv,
        slim="--slim" in sys.argv,
    ))
