# ComfyUI Workbench

用于整理 AI 图片与 LoRA 训练素材的桌面工作台：扫描图片、解析生成信息、审核标注、预处理和导出。使用 Python / PySide6，界面以中文为主；Windows 是目前验证过的运行与打包平台。

本项目可以独立运行，不要求启动 ComfyUI 服务。项目源码采用 [MIT 许可证](LICENSE)；依赖库和自行下载的模型分别遵循其原有许可证。

## 功能

- **图库与数据集**：扫描本地目录，读取 ComfyUI / NovelAI PNG 信息，按标签、状态和文件名筛选；双击图片放大，滚轮缩放、拖动平移，通过按钮进入打标台。
- **标注与审核**：编辑同名 `.txt`，复制/粘贴标签，管理标签库、屏蔽词与触发词；支持 OpenAI 兼容的视觉接口，也可选用本地 WD14 打标。
- **重命名**：拖动排序、批量移动、星标分组、日期/编号模板、目录分流、冲突预览；原地改名、复制导出或移动，图片与同名 `.txt` 成对处理。
- **马赛克**：画笔和矩形，像素化、模糊、黑/白遮挡、擦除、逐步撤销、切图草稿及可选自动导出、批量导出。
- **裁剪**：单图拖动比例裁剪框、统一比例居中批量裁剪，先预览后生成。
- **外观**：浅色/深色主题、自选背景，调整模糊、遮罩与面板透明度。
- **导出与恢复**：导出图片和标注、JSONL 等；删除图片进入应用垃圾桶，图片和 `.txt` 可成对恢复。重命名操作记录可跨重启撤销，撤销前检查文件是否被再次改动。

## Windows EXE

在 GitHub Releases 下载 Windows 发布包，解压到可写目录，双击 `ComfyUI-Workbench.exe`。必须保留整个文件夹及 `_internal`，不能只复制 EXE。

完整 GPU 版包含 ONNX Runtime 与 CUDA 运行库，体积较大；**不包含 WD14 模型、个人数据库、接口密钥或图片**。未配置模型也可使用图库、标注编辑、预处理和导出。

## 从源码运行

建议使用 Python 3.11–3.13。以下命令在项目目录的 PowerShell 中运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

首次使用：添加一个图片目录为数据集，然后扫描。图库选图后点「预处理选中项」，也可在预处理页打开未入库的外部目录。

## 可选打标方式

### 视觉接口

在设置中填写接口根地址、支持图片输入的模型名和密钥来源。建议将密钥存入环境变量（默认读取 `OPENAI_API_KEY`），再启动程序。图片会发送到你配置的服务；使用前自行确认服务和模型支持视觉输入。

本地图库、裁剪和马赛克无需网络。软件不自动下载模型。

### 本地 WD14

CPU 环境安装 `requirements-wd14-cpu.txt`；NVIDIA GPU 环境安装 `requirements-wd14-gpu.txt`。使用独立虚拟环境，CPU / GPU 两种 ONNX Runtime 不要同时安装。GPU 版需要兼容的 NVIDIA 驱动，设置页显示实际使用的 GPU / CPU 状态。

准备相互对应的 `.onnx` 和同名 `.csv` 词表，在设置中点「找模型…」选择 ONNX 文件。也可以：

- 放到程序旁的 `models/wd14/`。
- 用环境变量 `WD14_MODELS_DIR` 指定模型目录。
- 用 `COMFYUI_HOME` 指定 ComfyUI 根目录，复用 `custom_nodes/ComfyUI-WD14-Tagger/models/`。

程序不会依赖作者电脑的盘符。词表必须与模型输出顺序一致；模型及其许可证由使用者自行获取。

## 数据与文件操作

数据目录优先候选为 `COMFY_WORKBENCH_HOME`、`%APPDATA%/ComfyUI-Workbench`、用户目录下的 `.comfy-workbench`、程序旁的 `data/`。为了避免切换到空库，程序会优先采用可写且已有用户数据的候选。设置页可查看实际位置；已有安装的同一台电脑可能继续使用原有配置。

数据库、个人设置、缩略图、背景副本、操作记录与默认导出都保存在数据目录中。备份时先关闭程序，再复制整个数据目录。更新程序时保留数据目录。若将密钥直接填入设置，它会保存在本机配置中，不应上传到 GitHub。

- 删除图片会移动到专用垃圾桶；在图库即可删除，设置页可恢复或清空。
- 原地改名/移动会更新图库路径，保留标签、状态与暂存标注；移动后的已入库原图归入目标数据集。
- 裁剪、马赛克与复制导出的新图默认不自动入库，需自行审核再扫描。
- 像素编辑生成的新图会复制同名 `.txt`，但清除图片内嵌生成信息。
- 马赛克默认另存副本；覆盖原图须勾选、确认并先备份，按源格式写入。
- 像素编辑支持 PNG、JPEG、WebP、BMP；重命名范围以程序识别的图片类型为准。

## 打包与验证

```powershell
python -m pip install -r requirements-dev.txt
python build.py           # 独立目录版；已安装 GPU 依赖时收集运行库
python build.py --slim    # 不包含本地 WD14 的轻量版
```

输出为 `dist/ComfyUI-Workbench/`。打包前关闭程序；脚本保护输出目录里的 `data/`。不要把个人数据、模型、备份或生成的 `.spec` 上传到源码仓库。

运行 `python tools/package_release.py --output ../comfy-workbench-release`，可生成纯净源码 ZIP、独立程序 ZIP 和 SHA256 校验文件。该步骤会下载公开许可证文本，需要网络；输出目录应独立于源码，且不覆盖现有发布文件。

```powershell
python -B selftest.py
python -B uitest.py
python -B -m unittest test_preprocess test_preprocess_ui test_trash_pairs
python -B verify_packed_ui.py  # 打包后，用临时隔离数据验证 EXE 启动
# 配置 WD14_MODELS_DIR 后，可追加 --wd14 验证打包版 GPU 实际加载
```

界面测试生成 `_shots/` 和 `.uitest_data/`，它们不是发布文件。核心测试中的实际 WD14 加载检查依赖本机模型配置。

## 后续方向

当前版本尚未提供近似图检测、训练集质检报告或训练/验证集切分。建议优先顺序与界面改进见 [ROADMAP.md](ROADMAP.md)。欢迎通过 Issue 报告问题；请附操作步骤与必要日志，先移除密钥和个人目录信息。

## 许可证与第三方依赖

应用源码采用 MIT，允许使用、修改与分发，需保留版权和许可声明。Qt/PySide6、ONNX Runtime、CUDA 等不是 MIT 应用代码的一部分；详情见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
