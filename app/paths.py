"""运行期路径解析：兼容源码运行与 PyInstaller 打包后的 exe。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

APP_NAME = "ComfyWorkbench"
APP_DIR_NAME = "ComfyUI-Workbench"


def app_root() -> Path:
    """程序所在目录：打包后是 exe 所在目录，源码模式是项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_trash_dir() -> Path:
    """默认的图片垃圾桶目录：放在数据目录下的 ``trash/``。

    跟着 :func:`data_dir` 走，数据目录在哪桶就在哪；需要固定位置时
    用「设置 → 垃圾桶」显式指定（会经过安全检查），或设环境变量
    ``COMFY_WORKBENCH_TRASH_DIR``。
    """
    return data_dir() / "trash"


def strip_device_prefix(text: str) -> str:
    r"""剥掉 Windows 设备前缀（``\\?\`` / ``\\.\``）。

    **这是真实踩过的坑，别删。**

    ``\\?\C:\a\b`` 和 ``C:\a\b`` 是**同一个目录**，
    但加了前缀之后 Windows 会**跳过路径规范化**，
    ``Path.resolve()`` 也不会把它映射回 ``C:\a\b``。
    后果分两种：

    1. **删除类**：垃圾桶护栏认不出系统目录 —— 实测删掉过
       ``C:\Windows\System32\drivers\etc`` 里的 5 个文件
       （详见 ``app/trash.py`` 里的同名函数说明）。
    2. **记录类**：同一个数据集被存成**两条记录**（一条正常、一条
       ``\\?\`` 形式且 0 张图），用户看到"数据集重复了、图不见了"。

    所以凡是「拿路径当身份」的地方都要先剥前缀。
    """
    s = (text or "").strip()
    for prefix in ("\\\\?\\", "\\\\.\\"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    # \\?\UNC\server\share -> \\server\share
    if s.upper().startswith("UNC\\"):
        s = "\\\\" + s[4:]
    return s


def norm_path(path: Path | str) -> Path:
    r"""规范化路径：先剥设备前缀（见 :func:`strip_device_prefix`），再 resolve。

    不要求路径存在（``strict=False``），所以能给"还没建的目录"用。

    给「拿路径当身份」的地方用（数据集路径、缩略图 key 等）：
    这样 ``\\?\D:\imgs`` 和 ``D:\imgs`` 会得到**同一个结果**，
    不会变成两条数据集。
    """
    text = strip_device_prefix(str(path))
    try:
        return Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(text))


def reveal_file(target: Path | str) -> None:
    """打开文件所在目录并选中它；失败交给界面显示，避免静默失效。"""
    path = Path(target).expanduser().resolve(strict=True)
    if not path.is_file():
        raise FileNotFoundError(f"不是文件：{path}")
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes

        shell = ctypes.WinDLL("shell32", use_last_error=True)
        ole = ctypes.WinDLL("ole32", use_last_error=True)
        ole.CoInitialize.argtypes = [ctypes.c_void_p]
        ole.CoInitialize.restype = ctypes.c_long
        ole.CoUninitialize.argtypes = []
        ole.CoUninitialize.restype = None
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoTaskMemFree.restype = None
        shell.ILFree.argtypes = [ctypes.c_void_p]
        shell.ILFree.restype = None
        shell.SHParseDisplayName.argtypes = [
            wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        shell.SHParseDisplayName.restype = ctypes.c_long
        shell.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
        ]
        shell.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        shell.ILFindLastID.argtypes = [ctypes.c_void_p]
        shell.ILFindLastID.restype = ctypes.c_void_p
        initialized = ole.CoInitialize(None)
        folder_pidl = ctypes.c_void_p()
        file_pidl = ctypes.c_void_p()
        try:
            result = shell.SHParseDisplayName(
                str(path.parent), None, ctypes.byref(folder_pidl), 0, None
            )
            if result < 0:
                raise OSError(f"无法解析文件夹路径（HRESULT 0x{result & 0xffffffff:08X}）：{path.parent}")
            result = shell.SHParseDisplayName(str(path), None, ctypes.byref(file_pidl), 0, None)
            if result < 0:
                raise OSError(f"无法解析文件路径（HRESULT 0x{result & 0xffffffff:08X}）：{path}")
            child_pidl = shell.ILFindLastID(file_pidl)
            children = (ctypes.c_void_p * 1)(child_pidl)
            result = shell.SHOpenFolderAndSelectItems(folder_pidl, 1, children, 0)
            if result < 0:
                raise OSError(f"无法在资源管理器中定位文件（HRESULT 0x{result & 0xffffffff:08X}）")
        finally:
            if file_pidl:
                shell.ILFree(file_pidl)
            if folder_pidl:
                shell.ILFree(folder_pidl)
            if initialized >= 0:
                ole.CoUninitialize()
    else:
        import subprocess
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])


def _env_override(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def data_dir_candidates() -> list[Path]:
    """按优先级列出数据目录候选，顺序即尝试顺序。

    最后一项刻意放在程序目录下的 ``data``：受限环境（例如 %APPDATA% 不可写、
    又没有权限写用户目录）下有个**固定**的落点。

    这里绝不用临时目录——临时目录会被系统清理，而且 Windows 上
    ``tempfile.gettempdir()`` 在 %TEMP% 不可写时会退化成「当前工作目录」，
    于是数据会跟着启动位置乱跑，用户看起来就像「数据丢了」。
    """
    candidates: list[Path] = []

    override = _env_override("COMFY_WORKBENCH_HOME")
    if override is not None:
        candidates.append(override)

    base = os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        candidates.append(Path(base) / APP_DIR_NAME)
    candidates.append(Path.home() / ".comfy-workbench")
    candidates.append(app_root() / "data")

    return candidates


# data_dir() 上一次是怎么挑的，给启动日志用
LAST_RESOLUTION: dict[str, str] = {}


class DataDirError(RuntimeError):
    """所有候选目录都不可写。"""


# 解析结果只算一次。之前每次调用都重新探测，候选目录的可写状态一变
# （受控文件夹访问、杀软临时锁、沙箱策略），同一次运行里就会返回不同目录，
# 于是 thumbs/、数据库散落在好几个地方，看起来像「数据丢了」。
_RESOLVED: Path | None = None


def reset_data_dir_cache() -> None:
    """清掉缓存（测试用，或者环境变量变了之后）。"""
    global _RESOLVED
    _RESOLVED = None


def _db_has_rows(db: Path) -> bool:
    """``workbench.db`` 里是不是**真的有用户数据**。

    为什么不能只看文件大小：一个刚建出来、schema 建好但一条记录都没有的库，
    也有 8~60 KB。光看 ``size > 0`` 会把它误判成"有数据"，
    于是又选错目录（这个坑我踩过）。

    用只读方式打开，数一下 ``datasets`` / ``images`` 行数。
    任何异常（库损坏、被独占锁住、表不存在）都当作"没有数据" ——
    宁可回落到别的候选，也不要因为读库失败就崩掉。
    """
    try:
        if not db.is_file() or db.stat().st_size == 0:
            return False
    except OSError:
        return False

    try:
        # 只读 + 不建文件：绝不因为探测而创建一个新库
        uri = f"file:{db.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            for table in ("datasets", "images", "tags"):
                try:
                    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                except sqlite3.Error:
                    continue
                if n:
                    return True
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return False
    return False


def _settings_looks_real(path: Path) -> bool:
    """``settings.json`` 看起来是不是**用户真实用过的**配置。

    为什么不能只看 ``size > 0``：一个被写坏/截断的 settings
    （哪怕只有几个字节的空白）也会让 ``size > 0`` 成立，
    于是这个目录被当成"有数据"，把真正带数据库的目录顶掉。
    实测过：``"   "``（6 字节空白）就能抢走真数据目录。

    判定标准：是**合法 JSON 对象且至少有 1 个键**。

    注意：**不 import app.config**（那会绕出循环依赖），
    这里只做最朴素的 json 解析，够用就行。
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and len(raw) > 0


def _has_user_data(folder: Path) -> bool:
    """这个候选目录里是不是**已经有用户数据**。

    用来避免「数据目录漂移」：``%APPDATA%`` 在受控文件夹访问 / 杀软的影响下
    可写状态会变。它一旦变成可写，就会把 ``<程序目录>/data`` 顶掉，
    而那边才是用户真实的库 —— 用户看到「数据集全没了」（其实没删）。

    判定依据（**都是"有内容"才算，不是"文件存在"就算**）：

    - ``workbench.db`` 里 ``datasets``/``images``/``tags`` **有行**（见
      :func:`_db_has_rows`）—— 空库不算
    - 或者 ``settings.json`` 是**合法的非空 JSON 对象**（见
      :func:`_settings_looks_real`）—— 截断/空白/数组都不算

    特意不看 ``thumbs/``：程序给任何候选建目录时都会顺手建 thumbs，
    光有 thumbs 不代表这里有用户数据。
    """
    try:
        if _db_has_rows(folder / "workbench.db"):
            return True
        if _settings_looks_real(folder / "settings.json"):
            return True
    except OSError:
        return False
    return False


def data_dir() -> Path:
    """用户数据目录（数据库 / 配置 / 缩略图缓存）。

    顺序：``COMFY_WORKBENCH_HOME`` -> %APPDATA%（或 XDG）->
    ``~/.comfy-workbench`` -> ``<程序目录>/data``。

    但**不是**简单地取第一个可写的：如果某个候选里已经有用户数据
    （见 :func:`_has_user_data`），就优先用它。否则 ``%APPDATA%``
    一旦从「被拒」变成「可写」，程序就会换用一个空库，
    用户会以为数据被删了。

    结果会缓存，**一次运行内绝不换目录**。全部不可写时抛
    :class:`DataDirError` —— 静默换地方比报错更糟。
    """
    global _RESOLVED
    if _RESOLVED is not None:
        return _RESOLVED

    candidates = data_dir_candidates()
    LAST_RESOLUTION.clear()
    rejected: list[str] = []

    # 第一遍：可写 **且已经有数据** 的，优先
    for candidate in candidates:
        if not _has_user_data(candidate):
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            rejected.append(f"{candidate} ({exc.__class__.__name__})")
            continue
        LAST_RESOLUTION["chosen"] = str(candidate)
        LAST_RESOLUTION["reason"] = "已有用户数据"
        LAST_RESOLUTION["tried"] = " -> ".join(str(c) for c in candidates)
        if rejected:
            LAST_RESOLUTION["rejected"] = "; ".join(rejected)
        _RESOLVED = candidate
        return candidate

    # 第二遍：没有带数据的，就按原顺序取第一个可写的（全新安装的情形）
    for index, candidate in enumerate(candidates):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            rejected.append(f"{candidate} ({exc.__class__.__name__})")
            continue
        LAST_RESOLUTION["chosen"] = str(candidate)
        LAST_RESOLUTION["reason"] = "新建（所有候选都没有数据）"
        LAST_RESOLUTION["tried"] = " -> ".join(str(c) for c in candidates[: index + 1])
        if rejected:
            LAST_RESOLUTION["rejected"] = "; ".join(rejected)
        _RESOLVED = candidate
        return candidate

    LAST_RESOLUTION["rejected"] = "; ".join(rejected)
    raise DataDirError(
        "没有可写的数据目录。试过："
        + "、".join(str(c) for c in candidates)
        + "。可以用环境变量 COMFY_WORKBENCH_HOME 指定一个可写目录。"
    )



def db_path() -> Path:
    return data_dir() / "workbench.db"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def thumb_dir() -> Path:
    d = data_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return data_dir() / "workbench.log"


def other_data_dirs() -> list[Path]:
    """列出**别的候选目录**里也有用户数据的那些（不含当前正在用的）。

    这是目录漂移真的发生之后的补救信息。举例：

    - 程序以前在 ``%APPDATA%`` 里建过库（那时它可写）
    - 后来 ``%APPDATA%`` 被拒，程序改用 ``<程序目录>/data``
    - 用户在 ``<程序目录>/data`` 里又加了一堆数据集

    这时 ``%APPDATA%`` 里那份旧库就成了"孤岛"：用户看不到它，
    也不知道它还在。这个函数就是把它找出来，让启动日志（以及将来
    可能的提示 UI）能告诉用户「还有另一个地方存着你的数据」。

    判定用 :func:`_has_user_data`，所以空库 / 坏 settings 不会被误报。
    读库失败一律跳过，绝不因为探测而让程序起不来。
    """
    try:
        current = data_dir()
    except DataDirError:
        return []

    found: list[Path] = []
    for candidate in data_dir_candidates():
        try:
            if candidate.resolve() == current.resolve():
                continue
        except OSError:
            if str(candidate) == str(current):
                continue
        if _has_user_data(candidate):
            found.append(candidate)
    return found


def describe_other_data_dirs() -> str:
    """给日志/提示用的一句话说明（没有别的数据目录时返回空串）。"""
    others = other_data_dirs()
    if not others:
        return ""
    return "；".join(str(p) for p in others)



def export_dir() -> Path:
    d = data_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def open_in_explorer(target: Path | str) -> None:
    """在系统文件管理器中打开一个目录（Windows / macOS / Linux）。"""
    target = str(target)
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess

            subprocess.Popen(["open", target])
        else:
            import subprocess

            subprocess.Popen(["xdg-open", target])
    except Exception:
        pass
