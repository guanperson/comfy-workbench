"""WD14 打标（ONNX，GPU 优先）。

模型来自 ComfyUI 的 WD14 插件目录，不重复下载：

    <ComfyUI>/custom_nodes/ComfyUI-WD14-Tagger/models/
        wd-eva02-large-tagger-v3.onnx
        wd-eva02-large-tagger-v3.csv      ← 标签词表，行序 = 输出列序

模型输入 ``[N, 448, 448, 3]``（NHWC, float32, 0-255 区间），
输出 ``[N, 10961]`` 的 multi-label 概率。标签名严格按 CSV 行序对应，
**不能自己猜顺序**。

GPU 说明
--------
``onnxruntime-gpu`` 的 CUDA provider 依赖 cublas 12 / cudnn 9 / cufft 11。
Python 3.13 的 Windows wheel 不带这些，需要 pip 装 nvidia-*-cu12 包，
并在建 Session **之前**把它们的 bin 目录挂到 DLL 搜索路径上。
缺任何一个都会静默回退到 CPU（不报错，只是慢 30 倍），所以这里会
主动报告实际用的是哪个 provider。
"""

from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

MODEL_SUFFIX = ".onnx"
DEFAULT_MODEL = "wd-eva02-large-tagger-v3"

# WD14 固定输入 448x448
INPUT_SIZE = 448

# 这些词留在训练集里是纯噪音，默认过滤
DEFAULT_UNDESIRED = {
    "blurry", "jpeg artifacts", "watermark", "signature", "username",
    "artist name", "web address", "commentary", "english text", "letterboxed",
    "lowres", "bad anatomy", "bad hands", "censored", "bar censor",
    "twitter username", "patreon username", "text", "logo", "speech bubble",
    "translation request", "dated", "checkered background", "simple background",
}

# WD14 词表里角色词带系列名后缀，训练时通常要单独对待
CHARACTER_HINT = ("(", ")")


@dataclass
class TaggerConfig:
    model_path: str = ""
    threshold: float = 0.35
    character_threshold: float = 0.60
    undesired: set[str] = field(default_factory=lambda: set(DEFAULT_UNDESIRED))
    max_tags: int = 0                    # 0 = 不限
    use_gpu: bool = True
    batch_size: int = 8
    replace_underscore: bool = True


@dataclass
class TaggerStatus:
    available: bool
    provider: str = ""
    model: str = ""
    error: str = ""


def models_root() -> Path:
    """环境变量优先，否则寻找程序旁的模型目录。模型不随软件分发。"""
    from .paths import app_root

    env = os.environ.get("WD14_MODELS_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    candidates = [app_root() / "models" / "wd14"]
    comfy_home = os.environ.get("COMFYUI_HOME", "").strip()
    if comfy_home:
        candidates.insert(0, Path(comfy_home).expanduser() / "custom_nodes" / "ComfyUI-WD14-Tagger" / "models")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def list_models() -> list[Path]:
    """可用的 WD14 模型（必须同时有 .onnx 和 .csv）。"""
    root = models_root()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for onnx_file in sorted(root.glob(f"*{MODEL_SUFFIX}")):
        csv_file = onnx_file.with_suffix(".csv")
        if csv_file.is_file():
            found.append(onnx_file)
    return found


def pick_default_model() -> Path | None:
    models = list_models()
    if not models:
        return None
    for model in models:
        if DEFAULT_MODEL in model.stem:
            return model
    return models[0]


def _load_tags(csv_path: Path) -> list[str]:
    """读词表。行序就是输出列序，必须原样保留。"""
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    if "name" in header:
        index = header.index("name")
        tags = [row[index].strip() for row in rows[1:] if len(row) > index]
    else:
        tags = [row[0].strip() for row in rows if row]
    return tags


def _frozen_root() -> Path | None:
    """打包成 exe 后的解包目录（源码运行返回 None）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def _nvidia_dll_dirs() -> list[str]:
    """找 nvidia-*-cu12 装进来的 DLL 目录（含 exe 打包后的位置）。"""
    dirs: list[str] = []

    candidates: list[Path] = []
    frozen = _frozen_root()
    if frozen is not None:
        # PyInstaller 把 --collect-binaries nvidia.xxx 的 DLL 放这儿
        candidates.append(frozen / "nvidia")
        # onedir 模式下也可能平铺在 _internal/nvidia
        candidates.append(Path(sys.executable).resolve().parent / "_internal" / "nvidia")
    else:
        candidates.append(Path(sys.prefix) / "Lib" / "site-packages" / "nvidia")

    for base in candidates:
        if not base.is_dir():
            continue
        for package in sorted(base.iterdir()):
            for sub in ("bin", "lib"):
                bin_dir = package / sub
                if bin_dir.is_dir() and any(bin_dir.glob("*.dll")):
                    dirs.append(str(bin_dir))

    # 兜底：exe 根目录、_internal 根目录平铺的 DLL
    if frozen is not None:
        for extra in (frozen, Path(sys.executable).resolve().parent / "_internal"):
            if extra.is_dir() and any(extra.glob("cublas*.dll")):
                dirs.append(str(extra))

    # 源码环境可复用同一 Python 环境中已有的 torch DLL，无需导入 torch。
    for extra in (Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib",):
        if extra.is_dir():
            dirs.append(str(extra))
    return dirs


def prepare_onnxruntime(use_gpu: bool = True):
    """导入 onnxruntime 并把 CUDA 依赖准备好。返回 (ort, provider 列表)。"""
    import onnxruntime as ort  # noqa: PLC0415

    if not use_gpu:
        return ort, ["CPUExecutionProvider"]

    for directory in _nvidia_dll_dirs():
        try:
            os.add_dll_directory(directory)
        except OSError:
            pass
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")

    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls(cuda=True, cudnn=True, msvc=True)
        except Exception:  # noqa: BLE001
            pass

    available = ort.get_available_providers()
    if "CUDAExecutionProvider" in available:
        return ort, ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ort, ["CPUExecutionProvider"]


class Wd14Tagger:
    """一次加载，反复打标。"""

    def __init__(self, config: TaggerConfig | None = None) -> None:
        self.config = config or TaggerConfig()
        self.session = None
        self.tags: list[str] = []
        self.status = TaggerStatus(available=False)
        self._input_name = "input"
        self._np = None

    # ------------------------------------------------------------------ #
    def load(self) -> TaggerStatus:
        """加载模型。失败时 status.error 说明原因，不抛异常。"""
        if self.session is not None:
            return self.status

        try:
            import numpy as np  # noqa: PLC0415
        except ImportError as exc:
            self.status = TaggerStatus(False, error=f"缺 numpy：{exc}")
            return self.status

        self._np = np

        model_path = Path(self.config.model_path) if self.config.model_path else None
        if model_path is None or not model_path.is_file():
            model_path = pick_default_model()
        if model_path is None or not model_path.is_file():
            self.status = TaggerStatus(
                False,
                error=(
                    "没找到 WD14 模型。把 .onnx 和同名 .csv 放进 "
                    f"{models_root()}，或用 WD14_MODELS_DIR 指定目录。"
                ),
            )
            return self.status

        csv_path = model_path.with_suffix(".csv")
        if not csv_path.is_file():
            self.status = TaggerStatus(
                False, model=model_path.name, error=f"缺词表：{csv_path.name}"
            )
            return self.status

        try:
            self.tags = _load_tags(csv_path)
        except OSError as exc:
            self.status = TaggerStatus(False, model=model_path.name, error=f"读词表失败：{exc}")
            return self.status

        try:
            ort, providers = prepare_onnxruntime(self.config.use_gpu)
        except ImportError as exc:
            self.status = TaggerStatus(
                False, model=model_path.name,
                error=f"没装 onnxruntime：{exc}。pip install onnxruntime-gpu",
            )
            return self.status

        options = ort.SessionOptions()
        options.log_severity_level = 3
        try:
            self.session = ort.InferenceSession(
                str(model_path), options, providers=providers
            )
        except Exception as exc:  # noqa: BLE001
            self.status = TaggerStatus(
                False, model=model_path.name, error=f"加载模型失败：{exc}"
            )
            return self.status

        active = self.session.get_providers()
        self._input_name = self.session.get_inputs()[0].name
        provider = "GPU (CUDA)" if "CUDAExecutionProvider" in active else "CPU"
        self.status = TaggerStatus(True, provider=provider, model=model_path.stem)
        return self.status

    # ------------------------------------------------------------------ #
    def tag_image(self, path: str | Path) -> list[str]:
        """给一张图打标，返回标签列表（已按阈值过滤 + 排序）。"""
        results = self.tag_batch([path])
        return results[0] if results else []

    def tag_batch(self, paths: Sequence[str | Path]) -> list[list[str]]:
        """批量打标。返回和输入等长的列表。"""
        status = self.load()
        if not status.available or self.session is None:
            return [[] for _ in paths]

        np = self._np
        out: list[list[str]] = []
        batch_size = max(1, int(self.config.batch_size))

        for start in range(0, len(paths), batch_size):
            chunk = paths[start : start + batch_size]
            arrays = []
            valid: list[int] = []
            for offset, path in enumerate(chunk):
                array = self._preprocess(path)
                if array is None:
                    out.append([])
                    continue
                arrays.append(array)
                valid.append(offset)

            if not arrays:
                continue

            batch = np.stack(arrays).astype(np.float32)
            try:
                raw = self.session.run(None, {self._input_name: batch})[0]
            except Exception:  # noqa: BLE001
                # 整批失败就逐张重试，别让一张坏图拖垮一批
                for path in chunk:
                    single = self._preprocess(path)
                    if single is None:
                        out.append([])
                        continue
                    try:
                        one = self.session.run(
                            None, {self._input_name: single[None, ...].astype(np.float32)}
                        )[0]
                        out.append(self._postprocess(one[0]))
                    except Exception:  # noqa: BLE001
                        out.append([])
                continue

            for row_index in range(raw.shape[0]):
                out.append(self._postprocess(raw[row_index]))

        # 对齐长度（失败的图补空）
        while len(out) < len(paths):
            out.append([])
        return out[: len(paths)]

    # ------------------------------------------------------------------ #
    def _preprocess(self, path: str | Path):
        """读图 → letterbox 到 448 → NHWC float32（0-255）。"""
        try:
            from PIL import Image  # noqa: PLC0415
        except ImportError:
            return None
        try:
            with Image.open(path) as image:
                image = image.convert("RGB")
                width, height = image.size
                if width <= 0 or height <= 0:
                    return None
                # 长边缩放到 448，短边按比例补白（保持长宽比）
                scale = INPUT_SIZE / max(width, height)
                new_w = max(1, int(round(width * scale)))
                new_h = max(1, int(round(height * scale)))
                resized = image.resize((new_w, new_h), Image.Resampling.BICUBIC)
                canvas = Image.new("RGB", (INPUT_SIZE, INPUT_SIZE), (255, 255, 255))
                canvas.paste(resized, ((INPUT_SIZE - new_w) // 2, (INPUT_SIZE - new_h) // 2))
                array = self._np.asarray(canvas, dtype=self._np.float32)
        except Exception:  # noqa: BLE001
            return None
        return array  # HWC, 已经是 0-255

    def _postprocess(self, scores) -> list[str]:
        """概率 → 标签名。按阈值过滤并去掉噪音词。

        词表里的名字带下划线（``simple_background``），显示时才转空格。
        屏蔽词两种写法都认，免得用户按显示名填了却过滤不掉。
        """
        threshold = float(self.config.threshold)
        char_threshold = float(self.config.character_threshold)
        undesired: set[str] = set()
        for word in self.config.undesired:
            normalized = word.strip().lower()
            if not normalized:
                continue
            undesired.add(normalized)
            undesired.add(normalized.replace(" ", "_"))

        picked: list[tuple[float, str]] = []
        count = min(len(self.tags), scores.shape[0])
        for index in range(count):
            score = float(scores[index])
            if score < threshold:
                continue
            raw_name = self.tags[index]
            if not raw_name:
                continue
            if raw_name.lower() in undesired:
                continue
            is_character = any(hint in raw_name for hint in CHARACTER_HINT)
            if is_character and score < char_threshold:
                continue
            display = (
                raw_name.replace("_", " ") if self.config.replace_underscore else raw_name
            )
            picked.append((score, display))

        picked.sort(key=lambda item: (-item[0], item[1]))
        tags = [name for _score, name in picked]
        if self.config.max_tags and len(tags) > self.config.max_tags:
            tags = tags[: self.config.max_tags]
        return tags


def probe() -> TaggerStatus:
    """给设置页用的可用性探测。"""
    tagger = Wd14Tagger()
    return tagger.load()


def tag_paths(
    paths: Sequence[str | Path],
    config: TaggerConfig | None = None,
    progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[list[str]]:
    """一批图打标的便捷入口。"""
    tagger = Wd14Tagger(config)
    status = tagger.load()
    if not status.available:
        return [[] for _ in paths]

    results: list[list[str]] = []
    total = len(paths)
    for offset, path in enumerate(paths):
        if should_stop and should_stop():
            results.extend([[]] * (total - offset))
            break
        results.append(tagger.tag_image(path))
        if progress:
            progress(offset + 1, total)
    return results


def available_models_text() -> str:
    models = list_models()
    if not models:
        return f"（没找到模型，放在 {models_root()}）"
    return "、".join(m.stem for m in models)


def _collect_undesired_from_env() -> set[str]:
    raw = os.environ.get("WD14_UNDESIRED", "").strip()
    if not raw:
        return set(DEFAULT_UNDESIRED)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def default_config(**overrides) -> TaggerConfig:
    """构造一份配置，方便测试注入。"""
    config = TaggerConfig()
    config.undesired = _collect_undesired_from_env()
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def iter_with_progress(
    paths: Iterable[str | Path], chunk: int = 16
) -> Iterable[list[str | Path]]:
    batch: list[str | Path] = []
    for path in paths:
        batch.append(path)
        if len(batch) >= chunk:
            yield batch
            batch = []
    if batch:
        yield batch
