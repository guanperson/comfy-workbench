"""图片垃圾桶：删除图片和它的配套 txt，但只是**移走**，不真删。

设计要求（用户明确提的）：
- 删图片时把同名 ``.txt`` 配套标注一起处理
- **别真删**，丢进一个垃圾桶文件夹，随时能捞回来

于是这里的做法是「移动」而不是「删除」：

- 垃圾桶使用固定默认 bin 目录，或用户设置的自定义目录
- 桶内按图片与 txt 成对分配同一文件名，避免覆盖及配对错乱
- 每次删除写一条 ``trash/index.jsonl`` 记录，记下原路径、时间、原因，
  还原时照着搬回去
- 还原时发现原位置已有同名文件，就加 ``_restored`` 后缀，**不覆盖**

万一移动失败（跨盘、权限、文件被占用），抛 :class:`TrashError`，
调用方原样报错 —— 宁可删不掉，也不能半个成功。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# 图片的配套标注文件：同目录同名的 .txt
CAPTION_SUFFIX = ".txt"

# 桶里放一个说明文件，免得用户翻到 trash/ 以为是垃圾可以随手清掉
README_NAME = "说明.txt"
README_TEXT = """这是 ComfyUI 工作台的垃圾桶。

「删除图片」不会真的删掉文件，只是把它们移到这里。想找回来：
  在程序里打开设置 → 垃圾桶 → 还原，或者直接把文件拖回原目录。

桶里保留了原来的目录结构，所以同名文件不会互相覆盖。
确认不要了的话可以手动清空这个文件夹（程序里也有「清空垃圾桶」）。
"""


class TrashError(RuntimeError):
    """移动进垃圾桶失败。调用方应原样报给用户，不要吞掉。"""


class UnsafeTrashRoot(TrashError):
    """垃圾桶根目录指向了系统/用户目录等不该碰的位置。"""


# 系统目录环境变量。刻意不含 TEMP/TMP：临时目录下自建的子目录是测试沙箱的
# 正当用法，而 %TEMP% 本身由下面 AppData 规则里的 "Temp" 判定拦下。
_UNSAFE_ENV_KEYS = (
    "SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE",
)

# 用户目录下这些子目录，即使是空目录也绝不接受。
_UNSAFE_PROFILE_NAMES = (
    "desktop", "documents", "downloads", "pictures", "videos", "music",
    "favorites", "links", "contacts", "searches", "saved games", "onedrive",
    "appdata", "3d objects",
)


def _strip_device_prefix(text: str) -> str:
    r"""剥掉 Windows 设备前缀（``\\?\`` / ``\\.\``）。

    **真实事故点，别删。** 详见
    :func:`app.paths.strip_device_prefix` —— 实测 `\\?\` 前缀让
    `unsafe_reason()` 认不出系统目录，把
    ``C:\Windows\System32\drivers\etc`` 里 5 个文件删了。

    实现直接复用 :mod:`app.paths`，**不要在这里另写一份** ——
    两份实现迟早会不一致，而这里不一致的代价是真删文件。
    """
    return paths.strip_device_prefix(text)


def _norm(path: Path | str) -> Path:
    r"""规范化成绝对路径（不要求存在，避免 strict 抛错）。

    先剥 ``\\?\`` 设备前缀（这一步**必须**有，否则护栏可被绕过），
    再 resolve。没有直接用 `paths.norm_path()` 是为了保留这里
    "任何异常都退回 abspath" 的兜底行为。
    """
    text = _strip_device_prefix(str(path))
    try:
        return Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(os.path.abspath(text))



def _is_within(child: Path, parent: Path) -> bool:
    """child 是否等于 parent 或位于 parent 之下（按路径分量比较，不靠字符串前缀）。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def unsafe_reason(folder: Path | str) -> str | None:
    """检查某个目录能不能当垃圾桶根。可以则返回 None，否则返回中文原因。

    垃圾桶是唯一会「真删」的地方，它删的是根目录下的每一个子项，
    而且 ``shutil.rmtree`` 连隐藏和系统文件一起删。所以这里必须拦死：
    盘根、系统目录、用户目录本身及其标准子目录，一律不接受。
    """
    if folder is None:
        return None
    raw = str(folder).strip().strip('"')
    if not raw:
        return None

    target = _norm(raw)

    # 1) 盘根，例如 C:\ 或 D:\
    if target.parent == target:
        return f"这是盘根目录，不能当垃圾桶：{target}"

    # 2) 用户目录下的标准子目录 / AppData：即使 %USERPROFILE% 变量缺失也要拦住。
    #    注意这里**不能**禁止整个用户目录 —— 那样会把临时目录沙箱也一起毙掉
    #    （%TEMP% 就在用户目录下），而自建的临时目录是安全的。
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        home = None
    if home is not None:
        home = _norm(home)
        if target == home:
            return f"这是用户主目录本身，不能当垃圾桶：{target}"
        if _is_within(target, home):
            rel = target.relative_to(home)
            first = rel.parts[0].casefold() if rel.parts else ""
            # AppData 只拦它自己和它下面**一层**（Roaming / Local / LocalLow），
            # 不再往下拦 —— 否则 %TEMP%\\xxx 这种自建沙箱会被误伤，
            # 而 %TEMP% 本身已经由上面这层拦住了。
            if first == "appdata":
                if len(rel.parts) <= 2:
                    return (
                        f"这是 {target}，属于系统与程序数据目录，"
                        f"不能当垃圾桶：{target}"
                    )
                # AppData\Local\Temp 本身也拒绝（系统临时目录，会被系统清理，
                # 而且里面全是别人的临时文件）；但它下面自建的沙箱子目录放行。
                if len(rel.parts) == 3 and rel.parts[2].casefold() == "temp":
                    return (
                        f"这是系统临时目录，不能当垃圾桶：{target}"
                    )
            elif first in _UNSAFE_PROFILE_NAMES:
                return (
                    f"这是用户目录下的 {rel.parts[0]}，"
                    f"里面是你的个人文件，不能当垃圾桶：{target}"
                )

    # 3) 系统目录（Windows / Program Files / ProgramData 等）本身或其后代。
    #    放在用户目录规则之后，这样 %TEMP% 这类「既在用户目录下、又是系统变量」
    #    的位置由上面的规则判定，而独立的临时沙箱不会被误伤。
    for key in _UNSAFE_ENV_KEYS:
        base = os.environ.get(key)
        if not base:
            continue
        base_path = _norm(base)
        # 对 HOMEDRIVE 这种只到 "C:" 的，跳过（已被盘根规则覆盖）
        if base_path.parent == base_path:
            continue
        # 只拦系统目录本身和它的祖先链上的位置，不拦临时目录下的自建子目录
        if target == base_path:
            return f"这是系统目录本身，不能当垃圾桶：{target}"
        if key in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)",
                   "ProgramData", "HOMEDRIVE") and _is_within(target, base_path):
            return f"这个位置在系统目录 {base_path} 里，不能当垃圾桶：{target}"

    return None


def _assert_safe_trash_root(folder: Path | str) -> None:
    """不安全就抛 UnsafeTrashRoot；调用方负责把消息显示给用户。"""
    reason = unsafe_reason(folder)
    if reason:
        raise UnsafeTrashRoot(reason)


@dataclass
class TrashEntry:
    """一条删除记录。"""

    original: str                      # 原始绝对路径
    trashed: str                       # 在桶里的相对路径（相对 trash/）
    kind: str                          # "image" / "caption"
    dataset: str = ""                  # 数据集名（桶内一级目录）
    deleted_at: float = field(default_factory=time.time)
    note: str = ""                     # 删除原因，给人看

    @property
    def name(self) -> str:
        return Path(self.original).name

    def to_json(self) -> str:
        return json.dumps(
            {
                "original": self.original,
                "trashed": self.trashed,
                "kind": self.kind,
                "dataset": self.dataset,
                "deleted_at": self.deleted_at,
                "note": self.note,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_dict(cls, data: dict) -> TrashEntry:
        return cls(
            original=str(data.get("original", "")),
            trashed=str(data.get("trashed", "")),
            kind=str(data.get("kind", "image")),
            dataset=str(data.get("dataset", "")),
            deleted_at=float(data.get("deleted_at") or 0.0),
            note=str(data.get("note", "")),
        )


# 用户自定义的目标目录（设置里填的）；未设置时使用固定的默认 bin 目录。
_CUSTOM_ROOT: Path | None = None

# 测试/沙箱用的环境变量：设了它就覆盖设置里的 trash_dir。
# 以前这里只认 settings.json，于是测试脚本虽然改了 COMFY_WORKBENCH_HOME
# 把自己隔离到沙箱，删除目标却仍然读真实设置 —— 这就是「789 张图被搬走」的根因。
TRASH_DIR_ENV = "COMFY_WORKBENCH_TRASH_DIR"


def set_trash_dir(folder: str) -> None:
    """设置「删除的图片丢到哪」。空字符串 = 用固定默认 bin。

    不安全的目录（盘根、系统目录、用户目录）会被拒绝并抛
    :class:`UnsafeTrashRoot`，绝不静默接受。
    """
    global _CUSTOM_ROOT
    text = (folder or "").strip().strip('"')
    if text:
        _assert_safe_trash_root(text)
        # 存**规范化后**的路径，保证「检查的路径」和「删除的路径」是同一个。
        # 以前存的是 Path(text).expanduser()（没规范化），
        # 于是 `D:\a\..\b` 这种写法会出现"检查 A、删 B"的错位。
        _CUSTOM_ROOT = _norm(text)
    else:
        _CUSTOM_ROOT = None


def _env_trash_override() -> Path | None:
    """环境变量指定的垃圾桶目录（沙箱测试用），优先级最高。

    **这里也要过护栏。** 环境变量优先级最高，如果不查，
    就等于留了个后门：`set COMFY_WORKBENCH_TRASH_DIR=C:\\Windows\\System32\\drivers\\etc`
    就能让 `empty_trash()` 去删系统目录。

    不安全时**不回退到默认桶，而是抛 UnsafeTrashRoot** ——
    静默回退更危险：调用方以为删的是自定义目录，实际删了默认桶，
    而且用户完全不知道自己的设置被忽略了。抛出去让上层明确处理。

    ⚠️ 因为这个函数会被 `trash_root()` 调用，而 `trash_root()` 在
    `main.py` 启动路径上（打印"删除目录"那行），所以调用方必须
    自己 try/except，别让程序起不来。
    """
    raw = os.environ.get(TRASH_DIR_ENV, "").strip().strip('"')
    if not raw:
        return None
    folder = _norm(raw)
    _assert_safe_trash_root(folder)
    return folder


def trash_root() -> Path:
    """垃圾桶根目录：环境变量 > 设置里的自定义目录 > 固定默认 bin。"""
    override = _env_trash_override()
    if override is not None:
        return override
    if _CUSTOM_ROOT is not None:
        return _CUSTOM_ROOT
    return paths.default_trash_dir()


def is_custom_root() -> bool:
    """当前用的是不是用户自己设的目录。

    自定义目录里**不写说明文件** —— 那是人家自己的文件夹，
    别往里塞东西。
    """
    return _CUSTOM_ROOT is not None or _env_trash_override() is not None


def ensure_trash() -> Path:
    """确保垃圾桶目录存在，并在默认桶里放一份说明。

    自定义目录（用户自己指定的文件夹）**不写任何额外文件** ——
    那是人家自己的地盘，别往里塞东西。
    """
    root = trash_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not is_custom_root():
            _write_readme(root)
    except OSError as exc:
        raise TrashError(f"建不了目标文件夹：{root}（{exc}）") from exc
    return root


def _write_readme(root: Path) -> None:
    """写说明文件；已经有就不动（用户可能自己改过）。"""
    readme = root / README_NAME
    if readme.exists():
        return
    try:
        readme.write_text(README_TEXT, encoding="utf-8")
    except OSError:
        # 说明文件写不进去不影响删除功能，静默跳过
        pass


def index_path() -> Path:
    """删除记录写在哪。

    自定义目录里**不写** —— 那是用户自己的文件夹，别往里塞文件。
    记录统一放程序数据目录，还原时照样能查。
    """
    return paths.data_dir() / "trash-index.jsonl"


def _safe_dataset_name(name: str) -> str:
    """数据集名要当目录名用，去掉路径分隔符和非法字符。"""
    cleaned = "".join(
        "_" if ch in '<>:"/\\|?*' else ch for ch in (name or "").strip()
    ).strip(". ")
    return cleaned or "_未命名"


def _unique_target(base: Path) -> Path:
    """目标已存在时加序号，绝不覆盖桶里已有的东西。"""
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    for i in range(2, 1000):
        candidate = base.with_name(f"{stem}__{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise TrashError(f"垃圾桶里同名文件太多了：{base.name}")


def plan_for(
    image_path: str,
    dataset_name: str = "",
    *,
    keep_structure: bool | None = None,
) -> tuple[Path, Path | None]:
    """算出 (图片落点, txt 落点)。txt 不存在时第二个是 None。

    **自定义目录**（设置里指定了「丢到哪」）默认**平铺**放进去：

        D:\\废图\\xxx.png
        D:\\废图\\xxx.txt

    因为用户要的就是「丢到我设好的文件夹里」，再套一层子目录反而找不着。

    **默认目录**（data/trash）保留原目录结构：

        trash/<数据集>/<图片父目录名>/xxx.png

    同名文件一律靠 :func:`_unique_target` 加序号兜底，绝不覆盖。
    """
    src = Path(image_path)
    if not src.is_absolute():
        src = src.resolve()

    root = ensure_trash()
    if keep_structure is None:
        keep_structure = False

    if keep_structure:
        ds = _safe_dataset_name(dataset_name)
        parent = src.parent.name or "_根目录"
        img_target = root / ds / parent / src.name
    else:
        img_target = root / src.name

    caption = src.with_suffix(CAPTION_SUFFIX)
    txt_target = caption if caption.exists() else None
    return img_target, txt_target


def _pair_target(base: Path, *, restoring: bool = False) -> Path:
    """为图片和 txt 一起保留 stem，也避开其他扩展名的同 stem 图片。"""
    try:
        occupied = {
            child.stem.casefold() for child in base.parent.iterdir()
        } if base.parent.exists() else set()
    except OSError as exc:
        raise TrashError(f"读取目标目录失败：{base.parent}（{exc}）") from exc
    for i in range(1, 1000):
        stem = base.stem if i == 1 else (
            f"{base.stem}_restored{i}" if restoring else f"{base.stem}__{i}"
        )
        if stem.casefold() not in occupied:
            return base.with_name(stem + base.suffix)
    raise TrashError(f"目标目录里同名文件太多了：{base.name}")


def _move(src: Path, dest: Path) -> Path:
    """移动到已分配的准确位置，不能自行改名破坏图片/txt 配对。"""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            raise TrashError(f"目标已存在，未覆盖：{dest}")
        shutil.move(str(src), str(dest))
    except (OSError, shutil.Error) as exc:
        raise TrashError(f"移动失败：{src} -> {dest}（{exc}）") from exc
    return dest


def _move_pair(src: Path, target: Path, caption: Path | None) -> list[tuple[Path, Path]]:
    """先移图片，再移标注；标注失败时将图片回滚到准确的原路径。"""
    landed = _move(src, target)
    result = [(src, landed)]
    if caption is not None:
        try:
            landed_txt = _move(caption, target.with_suffix(CAPTION_SUFFIX))
        except TrashError as exc:
            try:
                _move(landed, src)
            except TrashError as rollback_exc:
                raise TrashError(
                    f"配套 txt 移动失败：{exc}；图片回滚失败：{rollback_exc}；"
                    f"图片保留在：{landed}"
                ) from exc
            raise TrashError(f"配套 txt 移动失败，图片已回滚：{exc}") from exc
        result.append((caption, landed_txt))
    return result


def _append_index(entries: list[TrashEntry]) -> None:
    if not entries:
        return
    try:
        with index_path().open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(entry.to_json() + "\n")
    except OSError:
        # 索引写不进去不该让删除失败 —— 文件已经安全移进桶里了，
        # 大不了还原的时候手动找。
        pass


def trash_images(
    items: list[tuple[str, str]],
    note: str = "",
    dataset_name: str = "",
    *,
    keep_structure: bool | None = None,
) -> tuple[int, list[str]]:
    """把一批图片（连同配套 txt）移进垃圾桶。

    :param items: ``[(image_path, dataset_name), ...]``
    :param note: 删除原因，记进索引方便日后回看
    :return: ``(成功移动的文件数, 错误信息列表)``

    逐条处理，单条失败不影响其他条 —— 但失败的那条**不会**被静默跳过，
    错误会原样带回去给用户看。
    """
    ensure_trash()
    moved = 0
    errors: list[str] = []
    batch: list[TrashEntry] = []

    for image_path, ds_name in items:
        src = Path(image_path)
        if not src.exists():
            errors.append(f"文件不在了：{src}")
            continue

        ds = ds_name or dataset_name
        try:
            img_target, caption = plan_for(str(src), ds, keep_structure=keep_structure)
            img_target = _pair_target(img_target)
            landed_files = _move_pair(src, img_target, caption)
            for original, landed in landed_files:
                batch.append(
                    TrashEntry(
                        original=str(original),
                        trashed=str(landed.relative_to(trash_root())),
                        kind="image" if original == src else "caption",
                        dataset=_safe_dataset_name(ds),
                        note=note,
                    )
                )
            moved += len(landed_files)
        except TrashError as exc:
            errors.append(str(exc))

    _append_index(batch)
    return moved, errors


def read_index() -> list[TrashEntry]:
    """读删除记录，最新的在前。索引坏了就返回空表，不崩。"""
    path = index_path()
    if not path.exists():
        return []
    entries: list[TrashEntry] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(TrashEntry.from_dict(json.loads(line)))
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return []
    # 只留文件还在桶里的记录
    root = trash_root()
    alive = [e for e in entries if (root / e.trashed).exists()]
    alive.sort(key=lambda e: e.deleted_at, reverse=True)
    return alive


def list_trashed() -> list[TrashEntry]:
    """桶里当前的图片记录（caption 记录不单独列，跟着图片走）。"""
    return [e for e in read_index() if e.kind == "image"]


def restore(entries: list[TrashEntry]) -> tuple[int, list[str]]:
    """把记录对应的文件搬回原处。原位置已有同名文件就加后缀，不覆盖。

    还原图片时会把它的**配套 txt 一起带回去** —— 删的时候一起走的，
    还原当然也要一起回来，否则图回来了标注却留在桶里。

    :return: ``(还原成功的文件数, 错误信息列表)``
    """
    root = trash_root()
    done = 0
    errors: list[str] = []

    by_trashed = {e.trashed: e for e in read_index()}
    seen: set[str] = set()
    restored: list[TrashEntry] = []
    # 先处理图片，避免同时选中的 caption 先被独立还原。
    for entry in sorted(entries, key=lambda e: e.kind != "image"):
        if entry.trashed in seen:
            continue
        seen.add(entry.trashed)
        mate = None
        if entry.kind == "image":
            txt_key = str(Path(entry.trashed).with_suffix(CAPTION_SUFFIX))
            candidate = by_trashed.get(txt_key)
            if (candidate is not None and candidate.kind == "caption"
                    and Path(candidate.original) == Path(entry.original).with_suffix(CAPTION_SUFFIX)):
                mate = candidate
                seen.add(mate.trashed)
        src = root / entry.trashed
        if not src.exists():
            errors.append(f"桶里找不到：{entry.name}")
            continue
        try:
            target = _pair_target(Path(entry.original), restoring=True)
            landed = _move_pair(src, target, root / mate.trashed if mate else None)
            done += len(landed)
            restored.append(entry)
            if mate is not None:
                restored.append(mate)
        except TrashError as exc:
            errors.append(f"还原失败：{entry.name}（{exc}）")

    # 失败的条目继续留在索引中，用户可以重试还原。
    _rewrite_index_without(restored)
    return done, errors


def _rewrite_index_without(removed: list[TrashEntry]) -> None:
    """把已还原的记录从索引里去掉。"""
    gone = {(e.trashed, e.original) for e in removed}
    path = index_path()
    if not path.exists():
        return
    keep: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (str(data.get("trashed", "")), str(data.get("original", "")))
                if key not in gone:
                    keep.append(line)
        with path.open("w", encoding="utf-8") as fh:
            for line in keep:
                fh.write(line + "\n")
    except OSError:
        pass


def empty_trash() -> tuple[int, list[str]]:
    """真删掉桶里的文件。这是唯一会「真删」的地方，界面要二次确认。

    删除前会**重新校验**根目录是否安全。即使设置是旧版本存的、
    或者有人手改了 settings.json，只要指向系统/用户目录就拒绝执行 ——
    这里返回错误而不是抛异常，免得界面卡在异常上。
    """
    root = trash_root()

    reason = unsafe_reason(root)
    if reason:
        return 0, [
            f"拒绝清空：{reason}",
            "为防止误删系统文件，垃圾桶不会在这个位置执行删除。"
            "请到「设置 → 垃圾桶」改成一个专用目录（例如 D:\\废图）。",
        ]

    if not root.exists():
        return 0, []
    removed = 0
    errors: list[str] = []
    for child in list(root.iterdir()):
        if child.name in (README_NAME, "index.jsonl"):
            continue
        try:
            if child.is_dir():
                removed += sum(
                    1 for f in child.rglob("*") if f.is_file()
                )
                shutil.rmtree(child, ignore_errors=True)
            else:
                removed += 1
                child.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{child.name}: {exc}")
    try:
        index_path().unlink(missing_ok=True)
    except OSError:
        pass
    return removed, errors


def stats() -> dict:
    """桶的大小和文件数，给设置页显示。"""
    root = trash_root()
    if not root.exists():
        return {"files": 0, "bytes": 0, "images": 0}
    files = 0
    total = 0
    for p in root.rglob("*"):
        if p.is_file() and p.name not in (README_NAME, "index.jsonl"):
            files += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return {"files": files, "bytes": total, "images": len(list_trashed())}


def format_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _unused_os_guard() -> None:
    """保留 os 引用，方便以后加跨盘判断（shutil.move 会退化成复制）。"""
    _ = os.sep
