"""配置：模型接口、并发、打标提示词模板、导出选项。

设计原则：
- 所有字段都有默认值，不改配置也能跑
- 密钥优先从用户指定的环境变量读取
- 任何 OpenAI 兼容的 /chat/completions 端点都能用（OpenAI / Gemini 代理 / 聚合站 / 本地 vLLM、Ollama…）
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import paths

# 默认打标提示词模板。
# 占位符：{banned} 屏蔽词列表、{max} 期望标签数量、{language} 标签语言
DEFAULT_TAG_PROMPT = """你是一个专业的图片打标助手。请分析这张 AI 生成的插画，返回 Danbooru / 品质塔 风格的英文标签。

规则：
1. 只输出 JSON 数组，不要任何解释文字、不要 markdown 代码块。
2. 标签用小写，多个单词用下划线连接，例如 "1girl, solo, long_hair, smile"。
3. 重点覆盖：人物数量与特征、发型发色、瞳色、服装配饰、表情、姿势、镜头、背景、画风、氛围。
4. 不要臆测画面里没有的内容，不要输出句子，不要输出同义词重复。
5. 最多输出 {max} 个标签，按重要性从高到低排列。
6. {language}
7. 以下词已被屏蔽，绝对不要输出：{banned}
"""

DEFAULT_PROMPT_EN = "标签使用英文。"

PROMPT_PRESETS: dict[str, str] = {
    "通用 Danbooru": DEFAULT_PROMPT_EN,
    "角色 LoRA": "第 1 个标签尽量是角色/画师名称；其余为外观与场景标签。",
    "画风 LoRA": "第 1 个标签尽量是画风/作者名称；其余为画面内容标签。",
    "概念 LoRA": "第 1 个标签尽量是概念物名称；其余为画面内容标签。",
    "场景 LoRA": "第 1 个标签尽量是场景/地点名称；其余为画面内容标签。",
}


@dataclass
class Settings:
    # ---- 模型接口 ----
    api_base: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"          # 环境变量名，留空则用下面的明文
    api_key: str = ""                            # 明文 key（仅本机 settings.json）
    model: str = "gpt-4o-mini"
    timeout: int = 60                            # 单次请求超时（秒）
    concurrency: int = 3                         # 并发线程数
    max_retries: int = 2
    temperature: float = 0.2

    # ---- 打标行为 ----
    max_tags: int = 30
    language: str = "英文"                       # 英文 / 中文
    tag_prompt: str = DEFAULT_TAG_PROMPT
    preset: str = "通用 Danbooru"
    auto_tag_on_scan: bool = False               # 扫描完立刻自动打标
    image_max_side: int = 768                    # 送模型前的最长边，控制 token
    tag_source_suffix: bool = True               # 自动标签带 (auto) 来源标记

    # ---- 界面 / 存储 ----
    thumb_size: int = 512
    gallery_zoom: int = 190
    start_page: str = "dashboard"
    confirm_before_delete: bool = True
    trash_dir: str = ""                          # 删的图片丢到哪；空则用 data/trash
    trash_keep_structure: bool = False           # 丢进去时是否保留原目录层级
    dataset_dir: str = ""                        # 记住上次添加数据集的目录
    export_dir: str = ""                         # 导出根目录，空则用 data/exports
    preprocess_export_dir: str = ""              # 预处理上次导出位置
    ui_theme: str = "light"                      # light / dark
    ui_background: str = ""                     # 数据目录内的背景副本
    ui_background_blur: int = 18
    ui_background_shade: int = 48
    ui_panel_opacity: int = 86

    # ---- 元数据解析 ----
    # auto / comfyui / nai
    meta_parser: str = "auto"                    # 自动识别优先，NAI 与 ComfyUI 可互斥区分
    gallery_show_caption: bool = False           # 图库是否并列显示配套 txt
    gallery_show_meta: bool = True               # 图库显示 PNG 元数据标签
    caption_backup: bool = True                  # 保存标注前备份原文件

    # ---- WD14 打标 ----
    wd14_enabled: bool = True                    # 总开关：没元数据时允许 WD14 兜底
    wd14_model: str = ""                         # 模型路径，空则自动挑
    wd14_use_gpu: bool = True                    # GPU 优先，失败自动回退 CPU
    wd14_threshold: float = 0.35                 # 一般词阈值
    wd14_character_threshold: float = 0.60       # 角色词阈值（宁缺毋滥）
    wd14_max_tags: int = 0                       # 0 = 不限
    wd14_batch_size: int = 8
    wd14_replace_underscore: bool = True
    wd14_undesired: str = ""                     # 额外屏蔽词，逗号分隔
    # 生成标注时对已有 txt 的处理：skip 不覆盖 / overwrite 覆盖
    caption_generate_policy: str = "skip"

    # ---- 导出 ----
    export_write_txt: bool = True                # 每张图同名 .txt
    export_write_jsonl: bool = True
    export_copy_images: bool = False             # 把图也复制到导出目录
    export_join: str = ", "                      # 标签拼接分隔符
    export_trigger: str = ""                     # 固定触发词（前置）
    export_use_ckpt: bool = False                # 用出图模型名做触发词
    export_filename_pattern: str = "{stem}.txt"

    _extra: dict = field(default_factory=dict, repr=False, compare=False)

    # ------------------------------------------------------------------ #
    def resolve_api_key(self) -> str:
        """实际使用的 key：环境变量优先，其次明文配置。"""
        name = (self.api_key_env or "").strip()
        if name:
            val = os.environ.get(name, "").strip()
            if val:
                return val
        return (self.api_key or "").strip()

    def key_source(self) -> str:
        """给右侧状态栏显示“密钥来源”。"""
        name = (self.api_key_env or "").strip()
        if name and os.environ.get(name, "").strip():
            return f"环境变量 {name}"
        if (self.api_key or "").strip():
            return "本地明文"
        return "未配置"

    def has_api_key(self) -> bool:
        return bool(self.resolve_api_key())

    def endpoint(self) -> str:
        base = (self.api_base or "").strip().rstrip("/")
        return f"{base}/chat/completions"

    def export_root(self) -> Path:
        if self.export_dir.strip():
            p = Path(self.export_dir.strip()).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p
        return paths.export_dir()

    def to_dict(self) -> dict:
        return asdict(self)

    def banner(self) -> str:
        """拼出带屏蔽词的最终打标提示词。"""
        banned = self._banned_cache or "(无)"
        txt = (self.tag_prompt or DEFAULT_TAG_PROMPT).format(
            banned=banned,
            max=int(self.max_tags),
            language=self.language_instruction(),
        )
        if self.preset and self.preset in PROMPT_PRESETS and self.preset != "通用 Danbooru":
            txt = txt + "\n附加要求：" + PROMPT_PRESETS[self.preset]
        return txt

    def language_instruction(self) -> str:
        return "标签使用中文。" if self.language.startswith("中") else "标签使用英文。"

    # 屏蔽词缓存，由 TagWorker 启动前注入，避免每次请求都查库
    _banned_cache: str = ""

    def set_banned(self, words: list[str]) -> None:
        self._banned_cache = ", ".join(w for w in words if w) or "(无)"


def _coerce(raw: dict, cls: type[Settings]) -> Settings:
    """把 dict 安全地塞回 dataclass，忽略未知键、做类型兜底。"""
    known = {f.name: f for f in fields(cls)}
    kwargs: dict = {}
    for key, val in raw.items():
        if key not in known or key.startswith("_"):
            continue
        spec = known[key]
        default = cls.__dataclass_fields__[key].default
        try:
            if isinstance(default, bool):
                kwargs[key] = bool(val)
            elif isinstance(default, int) and not isinstance(val, bool):
                kwargs[key] = int(val)
            elif isinstance(default, float):
                kwargs[key] = float(val)
            else:
                kwargs[key] = str(val)
        except (TypeError, ValueError):
            kwargs[key] = default
    return cls(**kwargs)


def load_settings(path: Path | None = None) -> Settings:
    path = path or paths.settings_path()
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return Settings()
        return _coerce(raw, Settings)
    except (OSError, json.JSONDecodeError):
        return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> bool:
    path = path or paths.settings_path()
    data = settings.to_dict()
    data.pop("_extra", None)
    data.pop("_banned_cache", None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
        return True
    except OSError:
        return False


# 便于 UI 侧引用
SETTINGS_FIELD_HELP = {
    "api_base": "OpenAI 兼容接口根地址，例如 https://api.openai.com/v1",
    "api_key_env": "存放 key 的环境变量名；留空则使用下方明文",
    "model": "视觉模型名，需支持看图（image_url）",
    "max_tags": "每张图最多打多少个标签",
    "image_max_side": "发模型前图片缩到多少像素（越大越贵）",
}
