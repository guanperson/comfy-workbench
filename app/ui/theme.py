"""ComfyUI Workbench 的统一视觉系统。

深色界面以冷灰为底、薄荷青为主色，避免传统工具软件常见的纯黑底和
高饱和蓝色。所有页面只消费这里的令牌，保证卡片、输入框和状态色一致。
"""

from __future__ import annotations

# ---- 颜色 ----
BG          = "#0a0f14"
BG_SIDEBAR  = "#0d141b"
BG_PANEL    = "#111922"
BG_PANEL_2  = "#17222d"
BG_HOVER    = "#1b2a36"
BORDER      = "#263746"
BORDER_SOFT = "#1b2a35"

TEXT        = "#edf5f7"
TEXT_DIM    = "#9eb0b8"
TEXT_FAINT  = "#667983"

ACCENT      = "#58e1c1"
ACCENT_DEEP = "#21b99a"
GREEN       = "#50d890"
ORANGE      = "#f5b85a"
RED         = "#ff737d"
PURPLE      = "#9f8cff"
CYAN        = "#54d6e8"

# ---- 标签胶囊：外框一个颜色，内壁另一个颜色 ----
# 未选中：紫内壁；点一下转灰表示选中，可以批量选
PILL_BORDER     = "#756bd8"      # 外框（未选中）
PILL_FILL       = "rgba(117,107,216,0.20)"   # 内壁（未选中，淡紫）
PILL_SEL_BORDER = "#58e1c1"      # 外框（已选中）
PILL_SEL_FILL   = "rgba(88,225,193,0.16)"    # 内壁（已选中，薄荷青）

PAGE_ORDER = [
    ("dashboard", "主页", "grid"),
    ("gallery",   "图库", "gallery"),
    ("preprocess", "预处理", "workbench"),
    ("datasets",  "数据集", "folder"),
    ("workbench", "打标", "workbench"),
    ("taglib",    "标签库", "tag"),
    ("settings",  "设置", "settings"),
]

APP_ICON = "workbench"

STATUS_COLORS = {
    "tagged":  GREEN,
    "pending": ORANGE,
    "skip":    TEXT_FAINT,
}

QSS = f"""
* {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Noto Sans CJK SC", "Noto Sans SC",
                 "Source Han Sans SC", "WenQuanYi Micro Hei", "SimHei",
                 "Malgun Gothic", "Yu Gothic UI", "Meiryo", sans-serif;
    font-size: 13px;
    color: {TEXT};
    outline: none;
}}

QWidget#Root, QMainWindow {{ background: {BG}; }}

QToolTip {{
    background: {BG_PANEL_2};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 5px 8px;
    border-radius: 7px;
}}

/* ---------------- 侧边栏 ---------------- */
QFrame#Sidebar {{
    background: {BG_SIDEBAR};
    border-right: 1px solid {BORDER_SOFT};
}}
QLabel#BrandTitle {{ font-size: 16px; font-weight: 700; color: {TEXT}; letter-spacing: 0.3px; }}
QLabel#BrandSub   {{ font-size: 10px; color: {ACCENT}; font-weight: 600; }}
QLabel#Version    {{ font-size: 11px; color: {TEXT_FAINT}; }}

QPushButton#NavButton {{
    background: transparent;
    border: 1px solid transparent;
    padding: 10px 12px;
    text-align: left;
    font-size: 13.5px;
    color: {TEXT_DIM};
    border-radius: 8px;
    margin: 2px 10px;
}}
QPushButton#NavButton:hover {{
    background: {BG_HOVER};
    color: {TEXT};
    border-color: {BORDER_SOFT};
}}
QPushButton#NavButton:checked {{
    background: rgba(88, 225, 193, 0.11);
    border: 1px solid rgba(88, 225, 193, 0.22);
    color: {ACCENT};
    font-weight: 600;
}}

/* ---------------- 顶栏 ---------------- */
QFrame#TopBar {{
    background: {BG};
    border-bottom: 1px solid {BORDER_SOFT};
}}
QLineEdit#Search {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 8px 13px;
    color: {TEXT};
    font-size: 13px;
}}
QLineEdit#Search:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit#Search::placeholder {{ color: {TEXT_FAINT}; }}

/* ---------------- 通用按钮 ---------------- */
QPushButton {{
    background: {BG_PANEL_2};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 14px;
    color: {TEXT};
}}
QPushButton:hover  {{ background: {BG_HOVER}; border-color: #3a5262; }}
QPushButton:pressed {{ background: #10201f; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: #131920; border-color: {BORDER_SOFT}; }}

QPushButton#Primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #071310;
    font-weight: 600;
}}
QPushButton#Primary:hover {{ background: #72ead0; border-color: #72ead0; }}
QPushButton#Primary:disabled {{ background: #1d2b3d; border-color: #24384f; color: #5b6b7d; }}

QPushButton#GhostDanger {{ background: transparent; border: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QPushButton#GhostDanger:hover {{ color: {RED}; border-color: {RED}; background: rgba(255,96,96,0.08); }}

QPushButton#Chip {{
    background: rgba(88,225,193,0.10);
    border: 1px solid rgba(88,225,193,0.30);
    border-radius: 11px;
    padding: 3px 11px;
    color: #a8f3e2;
    font-size: 12px;
}}
QPushButton#Chip:hover {{ background: rgba(75,155,255,0.24); }}
QPushButton#Chip:checked {{
    background: {ACCENT_DEEP}; border-color: {ACCENT_DEEP}; color: #ffffff; font-weight: 600;
}}

QPushButton#Segment {{
    background: transparent; border: 1px solid transparent;
    border-radius: 6px; padding: 6px 13px; color: {TEXT_DIM};
}}
QPushButton#Segment:hover {{ color: {TEXT}; background: {BG_HOVER}; }}
QPushButton#Segment:checked {{
    background: {BG_PANEL_2}; border-color: {BORDER}; color: {TEXT}; font-weight: 600;
}}

/* ---------------- 卡片 / 面板 ---------------- */
QFrame#Card, QFrame#Panel {{
    background: {BG_PANEL};
    border: 1px solid {BORDER_SOFT};
    border-radius: 12px;
}}
QFrame#CardHover {{ background: {BG_PANEL}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#StatCard {{
    background: {BG_PANEL_2};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QFrame#StatAccent {{ background: transparent; border: none; }}
QFrame#HLine {{ background: {BORDER_SOFT}; border: none; max-height: 1px; min-height: 1px; }}

QLabel#CardTitle  {{ font-size: 14px; font-weight: 650; color: {TEXT}; }}
QLabel#SectionTitle {{ font-size: 15px; font-weight: 650; color: {TEXT}; }}
QLabel#StatValue {{ font-size: 26px; font-weight: 700; color: {TEXT}; }}
QLabel#StatLabel {{ font-size: 12px; color: {TEXT_DIM}; }}
QLabel#Muted     {{ color: {TEXT_DIM}; font-size: 12px; }}
QLabel#Faint     {{ color: {TEXT_FAINT}; font-size: 11px; }}
QLabel#Mono      {{ font-family: "Cascadia Mono", "Consolas", "JetBrains Mono", monospace;
                    font-size: 12px; color: {TEXT_DIM}; }}
/* Caption 框用等宽字体：方便看标签之间的逗号空格，也便于逐行对比 */
QPlainTextEdit#Mono {{ font-family: "Cascadia Mono", "Consolas", "JetBrains Mono", monospace;
                    font-size: 12px; }}
QLabel#WarnBanner {{
    background: rgba(255, 149, 0, 0.13);
    border: 1px solid rgba(255, 149, 0, 0.45);
    border-radius: 8px;
    color: {ORANGE};
    font-size: 12px;
    padding: 8px 12px;
    margin: 8px 12px 0 12px;
}}
QWidget#PillCanvas, QScrollArea {{ background: transparent; border: none; }}

/* ---------------- 进度条 ---------------- */
QProgressBar {{
    background: #091016;
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {GREEN}; border-radius: 3px; }}

/* ---------------- 输入控件 ---------------- */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: #0b1219;
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 10px;
    color: {TEXT};
    selection-background-color: {ACCENT_DEEP};
    selection-color: #ffffff;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {TEXT_FAINT}; }}

QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {BG_PANEL_2};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DEEP};
    color: {TEXT};
    padding: 4px;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 16px; background: {BG_PANEL_2}; border: none; }}

QCheckBox {{ color: {TEXT_DIM}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {BORDER}; border-radius: 4px; background: {BG};
}}
QCheckBox::indicator:checked {{ background: {ACCENT_DEEP}; border-color: {ACCENT}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}

QSlider::groove:horizontal {{ background: {BG}; height: 4px; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT_DEEP}; height: 4px; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT}; width: 13px; height: 13px;
    margin: -5px 0; border-radius: 6px;
}}

/* ---------------- 列表 / 表格 ---------------- */
QListWidget, QTreeWidget, QTableWidget, QListView {{
    background: transparent;
    border: none;
    color: {TEXT};
}}
QListWidget::item, QTreeWidget::item {{
    padding: 7px 8px;
    border-radius: 6px;
    margin: 1px 2px;
}}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {BG_HOVER}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: rgba(88,225,193,0.14);
    color: {TEXT};
}}
QHeaderView::section {{
    background: {BG_PANEL};
    color: {TEXT_DIM};
    border: none;
    border-bottom: 1px solid {BORDER_SOFT};
    padding: 7px 8px;
    font-size: 12px;
    font-weight: 600;
}}

QTreeWidget {{ show-decoration-selected: 1; }}
QTreeWidget::item {{ padding: 5px 4px; }}

/* ---------------- 滚动条 ---------------- */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #2b3746; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: #3a4a5d; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: #2b3746; border-radius: 5px; min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: #3a4a5d; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------------- 分割 / 标签页 ---------------- */
QSplitter::handle {{ background: {BORDER_SOFT}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{
    background: transparent; color: {TEXT_DIM};
    padding: 7px 14px; border: none; border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; font-weight: 600; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

/* ---------------- 状态栏 ---------------- */
QStatusBar {{
    background: {BG_SIDEBAR};
    border-top: 1px solid {BORDER_SOFT};
    color: {TEXT_DIM};
    font-size: 12px;
}}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ padding: 0 4px; }}

/* ---------------- 对话框 ---------------- */
QDialog {{ background: {BG_PANEL}; }}
QGroupBox {{
    border: 1px solid {BORDER_SOFT};
    border-radius: 11px;
    margin-top: 12px;
    padding-top: 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px; padding: 0 6px; color: {ACCENT};
}}
QMessageBox {{ background: {BG_PANEL}; }}
QMenu {{
    background: {BG_PANEL_2};
    border: 1px solid {BORDER};
    border-radius: 6px; padding: 4px;
}}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_DEEP}; color: #ffffff; }}
QMenu::separator {{ height: 1px; background: {BORDER_SOFT}; margin: 4px 6px; }}
"""


_DARK_TOKENS = {name: globals()[name] for name in (
    "BG", "BG_SIDEBAR", "BG_PANEL", "BG_PANEL_2", "BG_HOVER", "BORDER", "BORDER_SOFT",
    "TEXT", "TEXT_DIM", "TEXT_FAINT", "ACCENT", "ACCENT_DEEP", "GREEN", "ORANGE",
    "RED", "PURPLE", "CYAN", "PILL_BORDER", "PILL_SEL_BORDER",
)}
_LIGHT_TOKENS = {
    "BG": "#e9eff2", "BG_SIDEBAR": "#ecf2f4", "BG_PANEL": "#ffffff",
    "BG_PANEL_2": "#f4f8fa", "BG_HOVER": "#dcebef", "BORDER": "#b8c8cf",
    "BORDER_SOFT": "#ccd9de", "TEXT": "#172a34", "TEXT_DIM": "#4e6570",
    "TEXT_FAINT": "#70858d", "ACCENT": "#137e73", "ACCENT_DEEP": "#0c7065",
    "GREEN": "#258459", "ORANGE": "#a46821", "RED": "#c6465d",
    "PURPLE": "#6352ac", "CYAN": "#23839b", "PILL_BORDER": "#7b68bb",
    "PILL_SEL_BORDER": "#19867a",
}
_wallpaper = None
_background_shade = 48


def background_pixmap():
    return _wallpaper


def background_shade() -> int:
    return _background_shade


def apply_theme(app, settings=None) -> None:
    """把样式表装到 QApplication 上。"""
    import io
    import re
    from pathlib import Path
    from PIL import Image, ImageFilter
    from PySide6.QtGui import QFont, QFontDatabase, QPalette, QPixmap  # noqa: PLC0415

    global _wallpaper, _background_shade
    light = getattr(settings, "ui_theme", "light") != "dark"
    selected = _LIGHT_TOKENS if light else _DARK_TOKENS
    for name, value in selected.items():
        globals()[name] = value
    replacements = {_DARK_TOKENS[name].lower(): selected[name] for name in selected}
    if light:
        replacements.update({
            "#0b1219": "#ffffff", "#091016": "#e2eaed",
            "#2b3746": "#a9bac2", "#3a4a5d": "#829aa5",
            "#131920": "#e8eff1", "#1d2b3d": "#b9d9d3",
            "#24384f": "#a7c9c1", "#5b6b7d": "#5e777e",
            "#10201f": "#d0e9e4", "#071310": "#ffffff",
            "#72ead0": "#0c7065", "#a8f3e2": "#0c7065",
            "#3a5262": "#8fa9b2",
        })
    qss = re.sub(r"#[0-9a-fA-F]{6}", lambda m: replacements.get(m.group(0).lower(), m.group(0)), QSS)
    # Custom-painted cards consume the live token globals above. Native QSS widgets
    # get translucent surfaces so the blurred image can show through.
    opacity = max(45, min(100, int(getattr(settings, "ui_panel_opacity", 86))))
    alpha = round(opacity * 2.55)
    surface = f"rgba(255,255,255,{alpha})" if light else f"rgba(17,25,34,{alpha})"
    shell = f"rgba(236,242,244,{alpha})" if light else f"rgba(13,20,27,{alpha})"
    qss += f"""
    QWidget#Root {{ background: transparent; }}
    QFrame#Panel, QFrame#Card, QFrame#CardHover, QFrame#StatCard,
    QGroupBox {{ background: {surface}; }}
    QFrame#Sidebar, QFrame#TopBar, QStatusBar {{ background: {shell}; }}
    """

    app.setStyle("Fusion")
    app.setStyleSheet(qss)
    palette = app.palette()
    # 让原生控件的默认色也跟着暗下来
    palette.setColor(QPalette.ColorRole.Window, BG)
    palette.setColor(QPalette.ColorRole.Base, BG_PANEL)
    palette.setColor(QPalette.ColorRole.Text, TEXT)
    palette.setColor(QPalette.ColorRole.WindowText, TEXT)
    palette.setColor(QPalette.ColorRole.Highlight, ACCENT_DEEP)
    palette.setColor(QPalette.ColorRole.HighlightedText, "#ffffff")
    app.setPalette(palette)

    _wallpaper = None
    source = str(getattr(settings, "ui_background", "") or "")
    _background_shade = max(0, min(100, int(getattr(settings, "ui_background_shade", 48))))
    if source and Path(source).is_file():
        try:
            with Image.open(source) as picture:
                picture = picture.convert("RGB")
                picture.thumbnail((2400, 1600))
                blur = max(0, min(80, int(getattr(settings, "ui_background_blur", 18))))
                if blur:
                    picture = picture.filter(ImageFilter.GaussianBlur(blur))
                buffer = io.BytesIO()
                picture.save(buffer, format="JPEG", quality=88)
                pixmap = QPixmap()
                pixmap.loadFromData(buffer.getvalue())
                if not pixmap.isNull():
                    _wallpaper = pixmap
        except (OSError, ValueError):
            _wallpaper = None
    for widget in app.allWidgets():
        widget.update()

    # 界面全是中文，但 Qt 不再自带字体。挑第一个真正装了的 CJK 字体，
    # 显式设成应用字体 —— 只靠 QSS 的 font-family 列表，回退并不可靠，
    # 缺字时会整片渲染成方框。
    family = pick_ui_font()
    if family:
        font = QFont(family, 10)
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        app.setFont(font)
    _ = QFontDatabase


# 优先级从高到低：Windows 中文 → macOS → Linux/Noto → 备选
UI_FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
    "PingFang SC",
    "Hiragino Sans GB",
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Source Han Sans SC",
    "Source Han Sans CN",
    "WenQuanYi Micro Hei",
    "WenQuanYi Zen Hei",
    "SimHei",
    "SimSun",
    "Malgun Gothic",
    "Yu Gothic UI",
    "Meiryo",
    "Segoe UI",
    "DejaVu Sans",
)

_UI_FONT_CACHE: str | None = None
_FONT_DIR_HINTED = False


def _hint_system_font_dir() -> None:
    """把系统字体目录告诉 Qt。

    Qt 6 不再自带字体，打包后（以及离屏渲染时）可能一个字体都找不到，
    中文会整片变成方框。Windows 上显式加一下 Fonts 目录最省事。
    """
    global _FONT_DIR_HINTED
    if _FONT_DIR_HINTED:
        return
    _FONT_DIR_HINTED = True

    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from PySide6.QtGui import QFontDatabase  # noqa: PLC0415

    candidates: list[Path] = []
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    candidates.append(Path(windir) / "Fonts")
    candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts")
    # Linux / macOS 常见位置
    candidates.extend(
        [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
         Path.home() / ".fonts", Path("/System/Library/Fonts")]
    )

    for path in candidates:
        try:
            if path.is_dir():
                # addApplicationFont 收的是「文件」，目录要自己展开
                for font_file in path.glob("*.tt[cf]"):
                    QFontDatabase.addApplicationFont(str(font_file))
                for font_file in path.glob("*.otf"):
                    QFontDatabase.addApplicationFont(str(font_file))
            elif path.is_file():
                QFontDatabase.addApplicationFont(str(path))
        except Exception:  # noqa: BLE001
            continue


def pick_ui_font() -> str | None:
    """返回第一个系统里真实存在的界面字体；一个都没有就返回 None。"""
    global _UI_FONT_CACHE
    if _UI_FONT_CACHE is not None:
        return _UI_FONT_CACHE or None

    from PySide6.QtGui import QFontDatabase  # noqa: PLC0415

    _hint_system_font_dir()
    try:
        installed = set(QFontDatabase.families())
    except Exception:  # noqa: BLE001
        installed = set()

    for name in UI_FONT_CANDIDATES:
        if name in installed:
            _UI_FONT_CACHE = name
            return name

    # 没命中已知名字就模糊找含 CJK 关键字的
    keywords = ("yahei", "pingfang", "noto sans cjk", "source han", "wenquanyi",
                "simhei", "simsun", "hei", "song", "kai", "ming")
    for family in sorted(installed):
        low = family.lower()
        if any(key in low for key in keywords):
            _UI_FONT_CACHE = family
            return family

    _UI_FONT_CACHE = ""
    return None
