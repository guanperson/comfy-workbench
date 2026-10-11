"""自动打标：把图片发给 OpenAI 兼容的视觉模型，拿回标签列表。

只依赖标准库（urllib）发请求，缩图用 Pillow（装了就用，没装就直传原图）。
端点、模型、并发、超时全部来自 Settings，方便对接任意聚合站或本地 vLLM / Ollama。
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .config import Settings

# 单个图片最多压到多少字节再传（兜底，防止超长边参数被忽略）
MAX_UPLOAD_BYTES = 4 * 1024 * 1024

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class TaggerError(RuntimeError):
    """打标失败，带出可读原因。"""


@dataclass
class TagRequest:
    image_path: str
    extra_context: str = ""      # 提示词里的信息，可作为额外线索喂给模型


@dataclass
class TagResponse:
    image_path: str
    tags: list[str] = field(default_factory=list)
    raw: str = ""
    error: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error


# --------------------------------------------------------------------------- #
# 图片编码
# --------------------------------------------------------------------------- #

def encode_image(path: str | Path, max_side: int = 768) -> tuple[str, str]:
    """返回 (data_uri, mime)。优先用 Pillow 缩放并用 JPEG 压缩。"""
    path = Path(path)
    raw: bytes
    mime = mimetypes.guess_type(path.name)[0] or "image/png"

    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as im:
            im = im.convert("RGB")
            if max_side and max(im.size) > max_side:
                ratio = max_side / float(max(im.size))
                new_size = (max(1, int(im.width * ratio)), max(1, int(im.height * ratio)))
                im = im.resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=88, optimize=True)
            raw = buf.getvalue()
            mime = "image/jpeg"
    except Exception:
        raw = path.read_bytes()

    if len(raw) > MAX_UPLOAD_BYTES:
        raw = _hard_shrink(raw, path)

    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}", mime


def _hard_shrink(raw: bytes, path: Path) -> bytes:
    """极端情况下继续压，最多再试两轮。"""
    try:
        from PIL import Image  # noqa: PLC0415

        for side in (512, 384):
            with Image.open(io.BytesIO(raw)) as im:
                im = im.convert("RGB")
                ratio = side / float(max(im.size))
                if ratio < 1:
                    im = im.resize(
                        (max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                        Image.LANCZOS,
                    )
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=80, optimize=True)
                out = buf.getvalue()
                if len(out) <= MAX_UPLOAD_BYTES:
                    return out
        return out
    except Exception:
        return raw


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #

def parse_tag_payload(content: str, max_tags: int = 0) -> list[str]:
    """把模型返回的一段文本尽力解析成标签列表。"""
    text = (content or "").strip()
    if not text:
        return []

    candidates: list[str] = []

    # 1) 直接是 JSON
    def _try_json(blob: str) -> list[str] | None:
        blob = blob.strip()
        if not blob or blob[0] not in "[{":
            return None
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(data, list):
            out = []
            for item in data:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    for key in ("tag", "name", "text", "label"):
                        if isinstance(item.get(key), str):
                            out.append(item[key])
                            break
            return out
        if isinstance(data, dict):
            for key in ("tags", "labels", "result", "data", "items"):
                val = data.get(key)
                if isinstance(val, list):
                    return [v if isinstance(v, str) else str(v) for v in val]
                if isinstance(val, str):
                    return re.split(r"[,\n;、]+", val)
        return None

    got = _try_json(text)
    if got is None:
        fence = _JSON_FENCE.search(text)
        if fence:
            got = _try_json(fence.group(1))
    if got is None:
        # 2) 段落里第一个 JSON 数组
        start, end = text.find("["), text.rfind("]")
        if 0 <= start < end:
            got = _try_json(text[start : end + 1])
    if got is None:
        # 3) 兜底：按分隔符切
        got = re.split(r"[,\n;、]+", text)

    for item in got:
        clean = _clean_tag(item)
        if clean:
            candidates.append(clean)

    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for name in candidates:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if max_tags and len(out) >= max_tags:
            break
    return out


_NOISE = re.compile(r"^(标签|tags?|json|输出|结果)[:：\s]*$", re.I)


def _clean_tag(raw: object) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    text = text.strip("\"'`“”‘’[]{}()*-•· \t")
    text = re.sub(r"^\d+[\.\)、]\s*", "", text)
    text = " ".join(text.split())
    if not text or len(text) > 64:
        return ""
    if _NOISE.match(text):
        return ""
    # 去掉模型偶尔加的解释尾巴
    if text.lower().startswith(("i cannot", "sorry", "抱歉", "无法")):
        return ""
    return text


def filter_banned(tags: Iterable[str], banned: Sequence[str]) -> list[str]:
    """屏蔽词过滤：整词匹配 + 子串匹配双保险。"""
    if not banned:
        return list(tags)
    ban_low = [b.strip().lower() for b in banned if b and b.strip()]
    ban_set = set(ban_low)
    out = []
    for tag in tags:
        low = tag.strip().lower()
        if not low or low in ban_set:
            continue
        if any(b in low for b in ban_low if len(b) > 2):
            continue
        out.append(tag)
    return out


# --------------------------------------------------------------------------- #
# 模型客户端
# --------------------------------------------------------------------------- #

class VisionTagger:
    """OpenAI 兼容 /chat/completions 的最小客户端。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._prompt_cache: str = ""
        self.banned: list[str] = []

    # ------------------------------------------------------------------ #
    def set_banned(self, banned: Sequence[str]) -> None:
        """注入屏蔽词：既进提示词，也用于返回结果的二次过滤。"""
        self.banned = [b for b in banned if b and b.strip()]
        self.settings.set_banned(self.banned)
        self._prompt_cache = ""

    def system_prompt(self) -> str:
        if not self._prompt_cache:
            self._prompt_cache = self.settings.banner()
        return self._prompt_cache

    def invalidate_prompt(self) -> None:
        self._prompt_cache = ""

    # ------------------------------------------------------------------ #
    def tag_image(self, req: TagRequest) -> TagResponse:
        started = time.time()
        result = TagResponse(image_path=req.image_path)

        if not self.settings.has_api_key():
            result.error = "未配置 API 密钥（设置页填写，或设置对应的环境变量）"
            return result

        try:
            data_uri, _ = encode_image(req.image_path, self.settings.image_max_side)
        except OSError as exc:
            result.error = f"读取图片失败：{exc}"
            return result

        user_text = self.system_prompt()
        if req.extra_context.strip():
            user_text += (
                "\n\n这张图的生成提示词（供参考，可从中提取准确的角色/画师名，"
                "但不要照抄全部内容）：\n" + req.extra_context.strip()[:1500]
            )

        payload = {
            "model": self.settings.model,
            "temperature": float(self.settings.temperature),
            "max_tokens": 1024,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
        }

        last_error = ""
        for attempt in range(max(1, int(self.settings.max_retries) + 1)):
            try:
                body = self._post(payload)
                content = self._extract_content(body)
                result.raw = content
                tags = parse_tag_payload(content, self.settings.max_tags)
                tags = filter_banned(tags, self.banned)
                if not tags:
                    last_error = "模型没有返回可用标签"
                    continue
                result.tags = tags
                result.error = ""
                result.elapsed = time.time() - started
                return result
            except TaggerError as exc:
                last_error = str(exc)
                # 认证 / 参数类错误重试没意义
                if "401" in last_error or "403" in last_error or "404" in last_error:
                    break
                time.sleep(1.2 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001 - 网络层各种意外统一兜住
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(1.2 * (attempt + 1))

        result.error = last_error or "未知错误"
        result.elapsed = time.time() - started
        return result

    # ------------------------------------------------------------------ #
    def _post(self, payload: dict) -> dict:
        settings = self.settings
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            settings.endpoint(),
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {settings.resolve_api_key()}",
                "User-Agent": "ComfyUI-Workbench/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            raise TaggerError(f"HTTP {exc.code} {exc.reason} {detail}".strip()) from exc
        except urllib.error.URLError as exc:
            raise TaggerError(f"连接失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise TaggerError(f"请求超时（{settings.timeout}s）") from exc

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TaggerError(f"返回不是 JSON：{raw[:200]}") from exc

        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise TaggerError(f"接口报错：{msg}")
        return body

    @staticmethod
    def _extract_content(body: dict) -> str:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")

        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif isinstance(item.get("content"), str):
                        parts.append(item["content"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return ""

    # ------------------------------------------------------------------ #
    def ping(self) -> tuple[bool, str]:
        """设置页“测试连接”：拉一次 /models，失败就报原因。"""
        settings = self.settings
        if not settings.has_api_key():
            return False, "未配置 API 密钥"
        url = settings.api_base.strip().rstrip("/") + "/models"
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {settings.resolve_api_key()}",
                "Accept": "application/json",
                "User-Agent": "ComfyUI-Workbench/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=min(settings.timeout, 20)) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            count = len(body.get("data") or []) if isinstance(body, dict) else 0
            return True, f"连接正常，可用模型 {count} 个"
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return False, f"密钥被拒绝（HTTP {exc.code}）"
            if exc.code == 404:
                return True, "根地址可达，但该服务未提供 /models 列表"
            return False, f"HTTP {exc.code} {exc.reason}"
        except Exception as exc:  # noqa: BLE001
            return False, f"连接失败：{exc}"
