"""ComfyUI 出图元数据解析。

ComfyUI 的 ``SaveImage`` 节点会把两份 JSON 塞进 PNG 的文本块：

    prompt     —— API 格式工作流 {node_id: {"class_type": ..., "inputs": {...}}}
    workflow   —— 前端 UI 格式工作流 {"nodes": [{"type": ..., "widgets_values": [...]}]}

本模块负责：
  1. 不依赖 Pillow 手工扫 PNG 文本块（tEXt / zTXt / iTXt）
  2. 从 API 格式里沿着连线回溯，拿到真正的正向 / 反向提示词
  3. 抽出模型、LoRA（含权重）、采样器、步数、CFG、种子、尺寸、保存前缀
  4. API 格式缺失时退回 UI 格式解析
  5. JPEG / WebP 走 Pillow 的 EXIF UserComment 兜底

返回统一结构：
    {
        "prompt": str, "negative": str, "meta": {...}, "has_meta": bool,
        "source": "png:prompt" | "png:workflow" | "exif" | ""
    }
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 节点类别表
# --------------------------------------------------------------------------- #

# 直接产出文本的节点
TEXT_NODES = {
    "CLIPTextEncode",
    "CLIPTextEncodeSDXL",
    "CLIPTextEncodeSDXLRefiner",
    "BNK_CLIPTextEncodeAdvanced",
    "Text Multiline",
    "ttN text",
    "String Literal",
    "PrimitiveNode",
    "easy string",
    "Prompt (Text)",
    "JWMText",
    "CR Prompt Text",
    "ImpactWildcardProcessor",
    "ImpactWildcardEncode",
    "WildcardProcessor",
    "ConditioningTextSDXL",
    "TextEncodeQwenImageEdit",
    # WeiLin 系：把提示词本身作为 positive/negative 字段存着
    "WeiLinComfyUIPromptAllInOneGreat",
    "WeiLinComfyUIPromptAllInOne",
    "WeiLinPromptAllInOne",
    "WeiLinComfyUIPrompt",
    "WeiLinPrompt",
}

# 只是把 conditioning 往下传的节点，回溯时要穿过
PASSTHROUGH_CONDITIONING = {
    "ConditioningCombine",
    "ConditioningConcat",
    "ConditioningSetArea",
    "ConditioningSetAreaPercentage",
    "ConditioningSetAreaStrength",
    "ConditioningSetMask",
    "ConditioningSetTimestepRange",
    "ConditioningAverage",
    "ConditioningZeroOut",
    "ConditioningSetProperties",
    "ConditioningSetPropertiesAndCombine",
    "ConditioningCombineSelect",
    "ConditioningSetTimestepRangePercent",
    "ControlNetApply",
    "ControlNetApplyAdvanced",
    "ControlNetApplySD3",
    "FluxGuidance",
    "StyleModelApply",
    "CLIPTextEncodeFlux",
}

# 模型类加载节点 -> (输入字段名, 元数据键)
MODEL_LOADERS = {
    "CheckpointLoaderSimple": ("ckpt_name", "checkpoint"),
    "CheckpointLoader": ("ckpt_name", "checkpoint"),
    "unCLIPCheckpointLoader": ("ckpt_name", "checkpoint"),
    "UNETLoader": ("unet_name", "unet"),
    "UnetLoaderGGUF": ("unet_name", "unet"),
    "VAELoader": ("vae_name", "vae"),
    "CLIPLoader": ("clip_name", "clip"),
    "DualCLIPLoader": ("clip_name1", "clip"),
    "CLIPVisionLoader": ("clip_name", "clip_vision"),
    "ControlNetLoader": ("control_net_name", "controlnet"),
    "UpscaleModelLoader": ("model_name", "upscale_model"),
    "StyleModelLoader": ("style_model_name", "style_model"),
    "IPAdapterModelLoader": ("ipadapter_file", "ipadapter"),
    "DiffusionLoader": ("model_name", "diffusion"),
}

LORA_NODES = {
    "LoraLoader",
    "LoraLoaderModelOnly",
    "LoraLoader|pysssss",
    "LoraTagLoader",
    "LoraLoaderBlockWeight",
    "WanVideoLoraSelect",
    "LoraLoaderAdvanced",
}

SAMPLER_NODES = {
    "KSampler",
    "KSamplerAdvanced",
    "KSampler (Efficient)",
    "KSamplerSelect",
    "SamplerCustom",
    "SamplerCustomAdvanced",
    "KSamplerSDXL",
    "dpmpp_2m_sde",
    "SamplerEfficient",
    "DetailerForEach",
    "FaceDetailer",
    "UltimateSDUpscale",
}

# 这些虽然名字里带 Sampler，但**不持有提示词**：
#   KSamplerSelect / KSamplerSelectCustom —— 只挑采样算法名字，没有 positive/negative
#   *Detailer / *Upscale —— 二级细化，连的是自己的正负，不是主图的正负
# 挑主提示词时必须排到后面，否则会把脸部细化器的词当成整图的词。
_SAMPLER_SELECTOR_NODES = {
    "KSamplerSelect",
    "KsamplerSelect",
    "KSamplerSelectCustom",
    "SamplerSelect",
    "dpmpp_2m_sde",
}

_DETAILER_NODES = {
    "FaceDetailer",
    "DetailerForEach",
    "DetailerForEachDebug",
    "UltimateSDUpscale",
    "FaceDetailerPipe",
    "MaskDetailer",
    "DetailerForEachPipe",
}

LATENT_NODES = {
    "EmptyLatentImage",
    "EmptySD3LatentImage",
    "EmptyLatentImagePresets",
    "EmptyLatentImageSDXL",
    "EmptyHunyuanLatentVideo",
    "EmptyLatentAudio",
    "LatentFromBatch",
    "EmptyLatentImageCustom",
}

SAVE_NODES = {"SaveImage", "SaveImageWithMetadata", "Image Save", "SaveImageWebsocket"}

SEED_KEYS = ("seed", "noise_seed", "seed_num", "rand_seed")

# --------------------------------------------------------------------------- #
# PNG 文本块
# --------------------------------------------------------------------------- #

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _decode_latin1(raw: bytes) -> str:
    return raw.decode("latin-1", errors="replace")


def read_png_text_chunks(path: str | Path) -> dict[str, str]:
    """手工扫 PNG 的 tEXt / zTXt / iTXt 块，返回 {关键字: 文本}。"""
    out: dict[str, str] = {}
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != _PNG_SIG:
                return out
            while True:
                head = fh.read(8)
                if len(head) < 8:
                    break
                length, ctype = struct.unpack(">I4s", head)
                data = fh.read(length)
                fh.read(4)  # CRC
                if ctype == b"tEXt":
                    if b"\x00" not in data:
                        continue
                    key, _, val = data.partition(b"\x00")
                    out[_decode_latin1(key)] = _decode_latin1(val)
                elif ctype == b"zTXt":
                    key, _, rest = data.partition(b"\x00")
                    if len(rest) < 1:
                        continue
                    try:
                        out[_decode_latin1(key)] = _decode_latin1(
                            zlib.decompress(rest[1:])
                        )
                    except zlib.error:
                        continue
                elif ctype == b"iTXt":
                    key, _, rest = data.partition(b"\x00")
                    if len(rest) < 2:
                        continue
                    comp_flag, comp_method = rest[0], rest[1]
                    rest = rest[2:]
                    _, _, rest = rest.partition(b"\x00")   # language tag
                    _, _, rest = rest.partition(b"\x00")   # translated keyword
                    try:
                        if comp_flag == 1:
                            text = zlib.decompress(rest).decode("utf-8", "replace")
                        else:
                            text = rest.decode("utf-8", "replace")
                    except zlib.error:
                        continue
                    out[_decode_latin1(key)] = text
                elif ctype == b"IEND":
                    break
    except (OSError, struct.error):
        return out
    return out


def read_exif_user_comment(path: str | Path) -> str:
    """JPEG / WebP 的 EXIF UserComment 兜底（需要 Pillow）。"""
    try:
        from PIL import Image  # noqa: PLC0415
    except Exception:
        return ""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return ""
            for tag_id in (0x9286, 0x010E):  # UserComment, ImageDescription
                val = exif.get(tag_id)
                if isinstance(val, bytes):
                    val = val.decode("utf-8", "replace")
                if isinstance(val, str) and val.strip():
                    return val.lstrip("ASCII\x00\x00\x00UNICODE").strip("\x00 ")
    except Exception:
        return ""
    return ""


# --------------------------------------------------------------------------- #
# API 格式工作流解析
# --------------------------------------------------------------------------- #

def _as_inputs(node: Any) -> dict:
    if isinstance(node, dict):
        ins = node.get("inputs")
        if isinstance(ins, dict):
            return ins
    return {}


def _link_target(value: Any) -> str | None:
    """ComfyUI 的连线写法是 ["12", 0] 或 [12, 0]；取出节点 id。"""
    if isinstance(value, (list, tuple)) and value:
        head = value[0]
        if isinstance(head, (str, int)):
            return str(head)
    return None


def _strip_widget_suffix(text: str) -> str:
    """去掉 wildcard / 动态提示里的 [a|b] 选择器的随机残留。"""
    return text.strip()


# 负面提示词的强特征。命中越多，这段文本越可能是负面。
_NEGATIVE_MARKERS = (
    "worst quality", "low quality", "lowres", "bad anatomy", "bad hands",
    "bad proportions", "bad perspective", "worst aesthetic", "bad quality",
    "normal quality", "average quality", "jpeg artifacts", "compression artifacts",
    "watermark", "signature", "username", "logo", "censored", "disfigured",
    "deformed", "extra digits", "fewer digits", "missing fingers", "unfinished",
    "artistic error", "adversarial noise", "blurry", "monochrome", "cropped",
    "ai-generated", "ai generated", "safe_neg", "very displeasing", "displeasing",
    "off-topic", "off topic", "what", "text, error", "out of frame", "scan",
    "oldest", "early", "concept art", "sketch", "2koma", "4koma", "speech bubble",
    "letterboxed", "unclear", "doesnotexist", "abstract",
)

# 负面常见前缀名（一些工作流用这样的开关词打头）
_NEGATIVE_LEAD = ("safe_neg", "neg", "negative", "uc", "bad_neg")


def _negative_score(text: str) -> int:
    """给一段文本打"像不像负面"的分。0 = 完全不像负面。"""
    if not text:
        return 0
    low = text.lower()
    score = 0
    for marker in _NEGATIVE_MARKERS:
        if marker in low:
            score += 1
    # 开头就是负面开关词，权重更高
    head = low.lstrip()[:40]
    for lead in _NEGATIVE_LEAD:
        if head.startswith(lead):
            score += 3
            break
    # 逗号分隔且短词密集，也偏负面；但不重罚长提示词
    return score


class _ApiGraph:
    """把 API 格式的 prompt 包一层，方便回溯。"""

    def __init__(self, graph: dict) -> None:
        self.graph = {str(k): v for k, v in graph.items() if isinstance(v, dict)}

    def node(self, node_id: str) -> dict | None:
        return self.graph.get(str(node_id))

    def class_type(self, node_id: str) -> str:
        node = self.node(node_id)
        if not isinstance(node, dict):
            return ""
        return str(node.get("class_type") or node.get("type") or "")

    def text_of(self, node_id: str, _depth: int = 0) -> str:
        """回溯一个 conditioning 输入，拼出提示词文本。"""
        if _depth > 64:
            return ""
        node = self.node(node_id)
        if not isinstance(node, dict):
            return ""
        ctype = self.class_type(node_id)
        inputs = _as_inputs(node)

        if ctype in TEXT_NODES:
            # 先找字面量；没有再看这个字段是不是连到别处
            # （BNK_CLIPTextEncodeAdvanced 之类会把 text 接到上游节点）
            for key in ("text", "prompt", "string", "value", "text_g", "text_l", "wildcard_text"):
                val = inputs.get(key)
                if isinstance(val, str) and val.strip():
                    return _strip_widget_suffix(val)
            # WeiLin 系把提示词存在 positive/negative 字段里（是字符串不是连线）
            for key in ("positive", "negative"):
                val = inputs.get(key)
                if isinstance(val, str) and val.strip():
                    return _strip_widget_suffix(val)
            for key in ("text", "prompt", "string", "value", "text_g", "text_l", "wildcard_text",
                        "positive", "negative"):
                nested = _link_target(inputs.get(key))
                if nested:
                    inner = self.text_of(nested, _depth + 1)
                    if inner:
                        return inner
            return ""

        # 穿越传递节点，优先找 conditioning 类输入
        for key in (
            "conditioning", "conditioning_1", "conditioning_2",
            "conditioning_to", "positive", "negative", "cond",
            "text", "prompt",
        ):
            target = _link_target(inputs.get(key))
            if target:
                got = self.text_of(target, _depth + 1)
                if got:
                    return got

        # 有些编码器把模型和文本混在一起，扫一遍所有链接输入
        if ctype in PASSTHROUGH_CONDITIONING or ctype.endswith("TextEncode"):
            for val in inputs.values():
                target = _link_target(val)
                if target:
                    got = self.text_of(target, _depth + 1)
                    if got:
                        return got
            for key in ("text", "text_g", "text_l"):
                if isinstance(inputs.get(key), str) and inputs[key].strip():
                    return inputs[key]
        return ""

    def sampler_nodes(self) -> list[tuple[str, dict]]:
        out = []
        for node_id in self.graph:
            if self.class_type(node_id) in SAMPLER_NODES:
                out.append((node_id, self.node(node_id) or {}))
        return out

    def prompt_samplers(self) -> list[tuple[str, dict]]:
        """真正持有提示词链接的采样器，按可信度排序。

        老图的工作流里节点很杂：``KSamplerSelect`` 只挑采样算法、没有正负输入；
        ``FaceDetailer`` 连的是脸部细化自己的词。直接取 ``sampler_nodes()[-1]``
        会挑中这些，于是读出来全错（实盘见过把负面读成正向）。

        排序规则（越靠前越可信）：
          1. 有 positive/negative 链接，且不是细化器/选择器
          2. 有 positive/negative 链接的细化器（只剩它可用时）
          3. 其余
        """
        usable: list[tuple[str, dict]] = []
        for node_id, node in self.sampler_nodes():
            ins = _as_inputs(node)
            if not (_link_target(ins.get("positive")) or _link_target(ins.get("negative"))):
                continue
            usable.append((node_id, node))

        def rank(item: tuple[str, dict]) -> tuple[int, int]:
            node_id, _ = item
            ctype = self.class_type(node_id)
            if ctype in _DETAILER_NODES:
                tier = 1
            elif ctype in _SAMPLER_SELECTOR_NODES:
                tier = 2
            else:
                tier = 0
            # 同一档里节点号大的排后面（保持工作流里的先后）
            try:
                order = int(node_id)
            except (TypeError, ValueError):
                order = 10 ** 6
            return (tier, order)

        usable.sort(key=rank)
        return usable

    def first_of_class(self, classes: set[str] | tuple[str, ...]) -> tuple[str, dict] | None:
        for node_id in self.graph:
            if self.class_type(node_id) in classes:
                return node_id, self.node(node_id) or {}
        return None

    def all_of_class(self, classes: set[str] | tuple[str, ...]) -> list[tuple[str, dict]]:
        return [
            (nid, self.node(nid) or {})
            for nid in self.graph
            if self.class_type(nid) in classes
        ]

    def all_texts(self) -> list[str]:
        texts: list[str] = []
        for node_id in self.graph:
            if self.class_type(node_id) in TEXT_NODES:
                ins = _as_inputs(self.node(node_id))
                for key in ("text", "prompt", "string", "value"):
                    val = ins.get(key)
                    if isinstance(val, str) and val.strip():
                        texts.append(val.strip())
                        break
        return texts

    def texts_by_likelihood(self) -> list[str]:
        """没有可靠采样器时，猜哪段文本最可能是正向。

        负面词的信号很强（worst quality / lowres / bad anatomy 之类密集出现），
        按这个打分排序，宁可老实说"猜的"也不要把负面当正向塞给用户。
        """
        scored: list[tuple[int, str]] = []
        for text in self.all_texts():
            scored.append((_negative_score(text), text))
        # 分低的排前面（负面特征少 = 更像正向）
        scored.sort(key=lambda item: (item[0], -len(item[1])))
        return [text for _, text in scored]

    def widest_prompt_chain(self) -> tuple[str, str]:
        """兜底：扫全图所有带 positive 的节点，挑出最像正向/负面的两条链。

        实盘的老工作流里，主采样器未必被识别出来（自研节点、二次细化插在中间），
        所以这里不认节点类名，只看「谁连出了非空文本」+「像不像负面」。
        """
        best_pos = ""
        best_neg = ""
        for node_id in self.graph:
            ins = _as_inputs(self.node(node_id))
            for slot, is_neg in (("positive", False), ("negative", True)):
                link = _link_target(ins.get(slot))
                if not link:
                    continue
                text = self.text_of(link)
                if not text.strip():
                    continue
                looks_neg = _negative_score(text) >= 3
                if is_neg or looks_neg:
                    if len(text) > len(best_neg):
                        best_neg = text
                elif len(text) > len(best_pos):
                    best_pos = text
        if not best_pos and best_neg:
            # 只找到疑似负面的：宁可正向留空也不要给错
            return "", best_neg
        return best_pos, best_neg


def parse_api_prompt(graph: dict) -> dict:
    """从 API 格式工作流里抽出提示词与生成参数。"""
    api = _ApiGraph(graph if isinstance(graph, dict) else {})
    meta: dict[str, Any] = {}

    prompt_text = ""
    negative_text = ""
    samplers = api.sampler_nodes()
    # 挑真正持有提示词的采样器；KSamplerSelect / FaceDetailer 之类会被排到后面
    pickers = api.prompt_samplers() or samplers

    # 按可信度**从后往前**试：很多老图里最后一个采样器是脸部细化器，
    # 它连的可能是空文本节点；能读出内容的那条链才是主链。
    for node_id, node in reversed(pickers):
        ins = _as_inputs(node)
        pos = _link_target(ins.get("positive"))
        neg = _link_target(ins.get("negative"))
        candidate = api.text_of(pos) if pos else ""
        if candidate.strip():
            prompt_text = candidate
            if neg and not negative_text:
                negative_text = api.text_of(neg)
            break

    # 主链没读出东西：扫全图所有 positive 链，挑最能当正向的那条
    if not prompt_text:
        prompt_text, negative_text = api.widest_prompt_chain()

    if not prompt_text:
        # 采样器没连 positive：退而求其次，第一段文本当正向
        # 但要避开负面 —— 负面通常含大量质量贬义词，先按可疑度筛一遍
        texts = api.all_texts()
        if texts:
            ordered = api.texts_by_likelihood()
            if ordered:
                prompt_text = ordered[0]
                if len(ordered) > 1 and not negative_text:
                    negative_text = ordered[1]
            else:
                prompt_text = texts[0]
                if len(texts) > 1 and not negative_text:
                    negative_text = texts[1]

    # ---- 采样参数 ----
    if samplers:
        node_id, node = samplers[-1]
        ins = _as_inputs(node)
        meta["sampler_node"] = api.class_type(node_id)
        for key, out in (
            ("steps", "steps"), ("cfg", "cfg"), ("sampler_name", "sampler"),
            ("scheduler", "scheduler"), ("denoise", "denoise"),
            ("start_at_step", "start_step"), ("end_at_step", "end_step"),
            ("add_noise", "add_noise"), ("guidance", "guidance"),
        ):
            if key in ins and not isinstance(ins[key], (list, tuple)):
                meta[out] = ins[key]
        for key in SEED_KEYS:
            if key in ins and not isinstance(ins[key], (list, tuple)):
                meta["seed"] = ins[key]
                break

    # ---- 模型 ----
    models: dict[str, list[str]] = {}
    for ctype, (field, bucket) in MODEL_LOADERS.items():
        for _, node in api.all_of_class({ctype}):
            val = _as_inputs(node).get(field)
            if isinstance(val, str) and val.strip():
                models.setdefault(bucket, [])
                if val not in models[bucket]:
                    models[bucket].append(val)
    if models:
        meta["models"] = models
        meta["checkpoint"] = (models.get("checkpoint") or [""])[0]

    # ---- LoRA（后面做 LoRA tag 择取就吃这份数据）----
    loras: list[dict] = []
    for _, node in api.all_of_class(LORA_NODES):
        ins = _as_inputs(node)
        name = ins.get("lora_name") or ins.get("lora") or ins.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        entry = {"name": name.strip()}
        for key in ("strength_model", "strength", "strength_clip", "weight"):
            val = ins.get(key)
            if isinstance(val, (int, float)):
                entry[key] = float(val)
        for key in ("trigger_word", "trigger", "prompt"):
            val = ins.get(key)
            if isinstance(val, str) and val.strip():
                entry["trigger"] = val.strip()
                break
        loras.append(entry)
    # 去重（同一 LoRA 被多层串接时）
    seen_lora: set[str] = set()
    uniq_loras: list[dict] = []
    for item in loras:
        key = item["name"].lower()
        if key in seen_lora:
            continue
        seen_lora.add(key)
        uniq_loras.append(item)
    if uniq_loras:
        meta["loras"] = uniq_loras

    # ---- 尺寸 ----
    latent = api.first_of_class(tuple(LATENT_NODES))
    if latent:
        ins = _as_inputs(latent[1])
        for key, out in (("width", "width"), ("height", "height"), ("batch_size", "batch_size")):
            if isinstance(ins.get(key), (int, float)):
                meta[out] = int(ins[key])

    # ---- 保存前缀 ----
    save = api.first_of_class(tuple(SAVE_NODES))
    if save:
        ins = _as_inputs(save[1])
        if isinstance(ins.get("filename_prefix"), str):
            meta["save_prefix"] = ins["filename_prefix"]

    # ---- 用到的全部节点类型（排查工作流很方便）----
    meta["node_types"] = sorted({api.class_type(n) for n in api.graph})

    meta["node_count"] = len(api.graph)
    return {"prompt": prompt_text.strip(), "negative": negative_text.strip(), "meta": meta}


# --------------------------------------------------------------------------- #
# UI 格式工作流解析（兜底）
# --------------------------------------------------------------------------- #

_WIDGET_HINTS = ("text", "clip_name", "ckpt_name", "lora_name", "filename_prefix")


def parse_ui_workflow(workflow: dict) -> dict:
    """前端格式：nodes[].type + widgets_values。信息比 API 格式少，但比没有强。"""
    meta: dict[str, Any] = {}
    texts: list[str] = []
    loras: list[dict] = []
    models: dict[str, list[str]] = {}
    params: dict[str, Any] = {}

    nodes = workflow.get("nodes") if isinstance(workflow, dict) else None
    if not isinstance(nodes, list):
        return {"prompt": "", "negative": "", "meta": meta}

    group_titles: dict[int, str] = {}
    for group in workflow.get("groups") or []:
        if isinstance(group, dict):
            try:
                group_titles[int(group.get("id", -1))] = str(group.get("title", ""))
            except (TypeError, ValueError):
                continue

    def title_of(node: dict) -> str:
        return str(node.get("title") or "").lower()

    for node in nodes:
        if not isinstance(node, dict):
            continue
        ntype = str(node.get("type") or "")
        widgets = node.get("widgets_values")
        widgets = widgets if isinstance(widgets, list) else []
        title = title_of(node)

        if ntype in TEXT_NODES or ntype.endswith("TextEncode"):
            best = ""
            for val in widgets:
                if isinstance(val, str) and len(val.strip()) > len(best):
                    best = val.strip()
            if best:
                texts.append(best)

        if ntype in LORA_NODES:
            for val in widgets:
                if isinstance(val, str) and val.strip():
                    entry = {"name": val.strip()}
                    nums = [x for x in widgets if isinstance(x, (int, float)) and not isinstance(x, bool)]
                    if nums:
                        entry["strength_model"] = float(nums[0])
                    if len(nums) > 1:
                        entry["strength_clip"] = float(nums[1])
                    loras.append(entry)
                    break

        for ctype, (field, bucket) in MODEL_LOADERS.items():
            if ntype == ctype:
                for val in widgets:
                    if isinstance(val, str) and val.strip():
                        models.setdefault(bucket, [])
                        if val not in models[bucket]:
                            models[bucket].append(val)
                        break

        if ntype in SAMPLER_NODES:
            nums = [x for x in widgets if isinstance(x, (int, float)) and not isinstance(x, bool)]
            # 常见顺序：seed, control, steps, cfg, sampler_name, scheduler, denoise
            for idx, key in ((0, "seed"), (2, "steps"), (3, "cfg")):
                if len(nums) > idx:
                    params[key] = nums[idx]
            for val in widgets:
                if isinstance(val, str) and val in (
                    "euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde",
                    "dpmpp_sde", "ddim", "uni_pc", "heun", "lms", "ddpm",
                ):
                    params["sampler"] = val
                if isinstance(val, str) and val in ("normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform", "beta"):
                    params["scheduler"] = val

        if ntype in LATENT_NODES:
            nums = [x for x in widgets if isinstance(x, (int, float)) and not isinstance(x, bool)]
            if len(nums) >= 2:
                params["width"], params["height"] = int(nums[0]), int(nums[1])

        if ntype in SAVE_NODES:
            for val in widgets:
                if isinstance(val, str) and val.strip():
                    meta["save_prefix"] = val.strip()
                    break

        if "negative" in title or "负" in title:
            best = max(
                (v.strip() for v in widgets if isinstance(v, str)),
                key=len,
                default="",
            )
            if best:
                meta["_negative_hint"] = best

    if group_titles:
        meta["groups"] = list(group_titles.values())

    negative_hint = meta.pop("_negative_hint", "")
    prompt_text = max(texts, key=len) if texts else ""

    meta.update(params)
    if models:
        meta["models"] = models
        meta["checkpoint"] = (models.get("checkpoint") or [""])[0]
    if loras:
        seen, uniq = set(), []
        for item in loras:
            k = item["name"].lower()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(item)
        meta["loras"] = uniq
    meta["node_count"] = len(nodes)
    return {"prompt": prompt_text, "negative": negative_hint, "meta": meta}


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #

def extract(path: str | Path, parser: str = "auto") -> dict:
    """解析一张图的元数据。永远返回结构化结果，不抛异常。

    ``parser``：
      - ``"auto"``   —— 先认 NAI，认不出再按 ComfyUI 走（默认）
      - ``"nai"``    —— 强制按 NovelAI 规则读
      - ``"comfyui"``—— 强制按 ComfyUI 规则读

    NAI 和 ComfyUI 的元数据格式完全不同（NAI 是 tEXt/Comment 里的
    扁平 JSON，ComfyUI 是节点图），所以能可靠自动区分。
    """
    if parser in ("nai", "auto"):
        nai_result = _try_nai(path)
        if nai_result is not None:
            return nai_result
        if parser == "nai":
            return _empty_result()
    return _extract_comfyui(path)


def _empty_result() -> dict:
    return {"prompt": "", "negative": "", "meta": {}, "has_meta": False, "source": ""}


def _loads_dict(text: str) -> dict | None:
    """尽量把一段文本解析成 dict，失败返回 None。"""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_foreign_meta(data: dict) -> bool:
    """这段 JSON 是不是别家的元数据（目前主要是 NAI）。

    这类数据不该被 ComfyUI 解析器当成提示词文本 —— 那会生成一堆
    带引号和花括号的垃圾标签。
    """
    from . import nai_meta  # 延迟导入

    try:
        if nai_meta.looks_like_nai(data):
            return True
    except Exception:  # noqa: BLE001
        pass
    # NAI 常见的旁证字段
    foreign_keys = ("v4_prompt", "v4_negative_prompt", "noise_schedule",
                    "qualityToggle", "uc", "steps", "sampler", "seed")
    hits = sum(1 for key in foreign_keys if key in data)
    return hits >= 3 and "nodes" not in data and "last_node_id" not in data


def _try_nai(path: str | Path) -> dict | None:
    """按 NAI 解析；不是 NAI 图返回 None。"""
    from . import nai_meta  # 延迟导入，避免循环

    path = Path(path)
    if path.suffix.lower() != ".png":
        return None
    data = nai_meta.read_png_comment(path)
    if not data or not nai_meta.looks_like_nai(data):
        return None
    parsed = nai_meta.parse(path)
    meta = dict(parsed.get("nai_params") or {})
    meta["loras"] = parsed.get("loras") or []
    return {
        "prompt": parsed.get("positive", ""),
        "negative": parsed.get("negative", ""),
        "meta": meta,
        "has_meta": bool(parsed.get("has_metadata")),
        "source": "nai",
        "char_groups": parsed.get("char_groups") or [],
    }


def _extract_comfyui(path: str | Path) -> dict:
    path = Path(path)
    result = {
        "prompt": "",
        "negative": "",
        "meta": {},
        "has_meta": False,
        "source": "",
    }
    suffix = path.suffix.lower()
    raw_prompt_text = ""
    raw_workflow_text = ""

    if suffix == ".png":
        chunks = read_png_text_chunks(path)
        raw_prompt_text = chunks.get("prompt") or chunks.get("Prompt") or ""
        raw_workflow_text = (
            chunks.get("workflow")
            or chunks.get("Workflow")
            or chunks.get("workflow_api")
            or ""
        )
        if not raw_prompt_text and not raw_workflow_text:
            # 有些前端只写 parameters（A1111 / WebUI 风格）
            parameters = chunks.get("parameters") or chunks.get("Comment") or ""
            if parameters:
                stripped = parameters.strip()
                # Comment 里可能是别的格式的 JSON —— 尤其是 NAI。
                # 强制 ComfyUI 时就该老实说「这不是 ComfyUI 元数据」，
                # 而不是把 NAI 的 JSON 当成提示词文本吐出去。
                as_json = _loads_dict(stripped)
                if as_json is not None:
                    if _is_foreign_meta(as_json):
                        return result          # 认不出，保持 has_meta=False
                    graph = as_json.get("prompt") if "prompt" in as_json else as_json
                    if isinstance(graph, dict):
                        parsed = parse_api_prompt(graph)
                        if parsed["prompt"] or parsed["meta"]:
                            result.update(parsed)
                            result["has_meta"] = True
                            result["source"] = "png:comment-graph"
                            return result
                    return result
                result["prompt"] = stripped
                result["has_meta"] = True
                result["source"] = "png:parameters"
                return result
    else:
        comment = read_exif_user_comment(path)
        if comment:
            raw_prompt_text = comment

    # ---- API 格式优先 ----
    if raw_prompt_text:
        try:
            graph = json.loads(raw_prompt_text)
        except (json.JSONDecodeError, TypeError):
            graph = None
        if isinstance(graph, dict):
            # 有些实现把 prompt/workflow 包了一层
            if "prompt" in graph and isinstance(graph["prompt"], dict) and "nodes" not in graph:
                graph = graph["prompt"]
            parsed = parse_api_prompt(graph)
            if parsed["prompt"] or parsed["meta"]:
                result.update(parsed)
                result["has_meta"] = True
                result["source"] = "png:prompt" if suffix == ".png" else "exif:prompt"
                _finalize(result)
                return result

    # ---- UI 格式兜底 ----
    if raw_workflow_text:
        try:
            workflow = json.loads(raw_workflow_text)
        except (json.JSONDecodeError, TypeError):
            workflow = None
        if isinstance(workflow, dict):
            parsed = parse_ui_workflow(workflow)
            if parsed["prompt"] or parsed["meta"]:
                result.update(parsed)
                result["has_meta"] = True
                result["source"] = "png:workflow"
                _finalize(result)
                return result

    return result


def _finalize(result: dict) -> None:
    """合并模型名进 LoRA 触发词候选，并补上 lora 名称的干净形式。"""
    meta = result.get("meta") or {}
    for item in meta.get("loras") or []:
        name = str(item.get("name", ""))
        # 去掉路径与扩展名，得到可直接当触发词用的短名
        stem = re.sub(r"\.(safetensors|ckpt|pt|bin|gguf)$", "", Path(name).name, flags=re.I)
        item["short"] = stem
    if meta.get("checkpoint"):
        meta["checkpoint_short"] = re.sub(
            r"\.(safetensors|ckpt|pt|bin|gguf)$", "", Path(str(meta["checkpoint"])).name, flags=re.I
        )


def summarize(result: dict) -> str:
    """一行摘要，用于列表/日志。"""
    meta = result.get("meta") or {}
    bits = []
    if meta.get("checkpoint_short"):
        bits.append(meta["checkpoint_short"])
    if meta.get("loras"):
        bits.append("+".join(l.get("short", "?") for l in meta["loras"][:3]))
    if meta.get("steps"):
        bits.append(f"steps={meta['steps']}")
    if meta.get("seed") is not None:
        bits.append(f"seed={meta['seed']}")
    return " | ".join(str(b) for b in bits)


# 只剥掉「列表序号」和「项目符号」，绝不能吃掉 1girl / 2boys / 4k 这类标签
_LEADING_NOISE = re.compile(r"^\s*(?:\d{1,3}[\.\)、]\s*|\(\d{1,3}\)\s*|[-*•·]\s*)")


def quick_tags(path: str | Path, max_tags: int = 24, parser: str = "auto") -> list[str]:
    """给图库卡片用的轻量取标签：只读元数据，不碰磁盘以外的东西。

    返回去重后的标签列表；没有元数据就返回空列表。
    NAI 的多角色分组会摊平合并。
    """
    try:
        parsed = extract(path, parser=parser)
    except Exception:  # noqa: BLE001
        return []
    if not parsed.get("has_meta"):
        return []
    groups = parsed.get("char_groups") or []
    if groups:
        text = "\n".join(
            ", ".join(g) if isinstance(g, (list, tuple)) else str(g) for g in groups
        )
    else:
        text = str(parsed.get("prompt") or "")
    return split_prompt_to_tags(text, max_tags=max_tags)


def split_prompt_to_tags(text: str, max_tags: int = 0) -> list[str]:
    """把提示词拆成标签候选（英文逗号 / 换行 / 分号）。"""
    if not text:
        return []
    parts = re.split(r"[,\n;、]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        item = " ".join(part.strip().split())
        item = _LEADING_NOISE.sub("", item).strip()
        if not item or len(item) > 80:
            continue
        low = item.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(item)
        if max_tags and len(out) >= max_tags:
            break
    return out
