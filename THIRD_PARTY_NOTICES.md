# 第三方组件

MIT 许可证仅适用于本项目自有源码，不替代依赖组件或模型的许可证。

Windows 完整发布包使用下列第三方组件，具体版本与许可文件保存在包内 `licenses/`：

- Python：PSF License；https://www.python.org/psf/license/
- Qt / PySide6 / Shiboken：开源许可包含 LGPLv3 / GPL 选项；https://doc.qt.io/qtforpython-6/licenses.html 。本程序通过独立 DLL 动态加载 Qt，用户可以替换兼容的库。对应源代码：https://code.qt.io/cgit/qt/ 与 https://code.qt.io/cgit/pyside/pyside-setup.git/ 。版本以发布包记录为准。
- Pillow：HPND 及所用图像库的许可；https://github.com/python-pillow/Pillow
- NumPy：BSD 许可及其第三方组件许可；https://github.com/numpy/numpy
- ONNX Runtime：MIT 与其第三方通知；https://github.com/microsoft/onnxruntime
- NVIDIA CUDA / cuDNN 运行库：NVIDIA 各组件分发许可证，见包内原始 `License.txt`。安装显卡驱动与使用这些库仍受相应条款约束。
- PyInstaller：GPL 与打包程序分发例外；https://pyinstaller.org/en/stable/license.html

其他被收集的 Python 依赖许可也保存在发布包内。WD14 模型与词表不随软件分发，下载者应查看模型作者提供的许可与使用说明。
