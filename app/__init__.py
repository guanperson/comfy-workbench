"""ComfyUI Workbench - ComfyUI 出图资产管理 / 打标工作台.

一个纯 Python + PySide6 的桌面程序，用于：
  1. 扫描本地 ComfyUI 出图目录，解析 PNG/EXIF 内嵌的工作流与提示词
  2. 手动 / 自动（视觉大模型）为每张图打标签
  3. 标签库管理与屏蔽词
  4. 导出为 LoRA / 训练可用的数据集

入口见 main.py
"""

__version__ = "0.2.0"
__all__ = ["__version__"]
