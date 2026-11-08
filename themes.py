"""配色。界面和图表的颜色都从这里出。

为什么单独一个文件：颜色原来散在 gui.py 的 QSS 字符串和 charts.py 的常量里，
想加一套浅色得改两处还不一定改全。现在只有一个来源，切主题 = 换一个字典。

三套：
  深色   现在这副（默认）
  米白   暖白纸感，白天看舒服
  夜蓝   更冷更沉，晚上不刺眼

加新主题：往 THEMES 里加一条，键齐了就能用；`tests/test_themes.py` 会检查
每套都定义了全部颜色，缺一个直接报错，不会画出一块黑底。
"""
import json
import os
from pathlib import Path

_HERE = Path(os.environ.get("WXBRIDGE_ROOT") or Path(__file__).resolve().parent).resolve()
CONFIG = _HERE / "config.json"

# 一套配色要定义的键。少一个 qss() 就会 KeyError，测试也拦着。
KEYS = (
    "bg", "panel", "line", "text", "muted", "accent", "ok", "warn", "bad",
    "btn", "btn_fg", "btn_disabled", "danger_bg", "danger_border",
    "input_bg", "nav_sel", "nav_hover", "header_bg", "scroll", "log_bg",
    "on_accent", "table_sel", "warn_bg", "warn_border",
)

THEMES = {
    "深色": {
        "bg": "#0e1014", "panel": "#151922", "line": "#232836",
        "text": "#e6e9ef", "muted": "#8b93a3",
        "accent": "#4c8dff", "ok": "#5ad48a", "warn": "#ffb454", "bad": "#ff7a8a",
        "btn": "#1c2431", "btn_fg": "#e6e9ef", "btn_disabled": "#4a5262",
        "danger_bg": "#2a1a1e", "danger_border": "#43222a",
        "input_bg": "#0f131a", "nav_sel": "#1e2a40", "nav_hover": "#1a1f2b",
        "header_bg": "#12161d", "scroll": "#2a3140", "log_bg": "#0b0e13",
        "on_accent": "#ffffff", "table_sel": "#1e2a40",
        "warn_bg": "#241d10", "warn_border": "#3b2f16",
    },
    "米白": {
        "bg": "#f6f1e7", "panel": "#fffdf7", "line": "#e3d9c8",
        "text": "#2f2a22", "muted": "#8b7f6b",
        "accent": "#c2703a", "ok": "#3f8a5c", "warn": "#c08a1e", "bad": "#c0504a",
        "btn": "#f1e9db", "btn_fg": "#2f2a22", "btn_disabled": "#b8ad99",
        "danger_bg": "#f7e6e3", "danger_border": "#e6c8c3",
        "input_bg": "#fffdf8", "nav_sel": "#efe4d2", "nav_hover": "#f3ebdd",
        "header_bg": "#f1e9db", "scroll": "#d8cbb4", "log_bg": "#fbf7ee",
        "on_accent": "#ffffff", "table_sel": "#f0e5d2",
        "warn_bg": "#fbf1dd", "warn_border": "#e8d3a6",
    },
    "夜蓝": {
        "bg": "#0b1220", "panel": "#111a2b", "line": "#1e2b40",
        "text": "#e8eef8", "muted": "#8fa0b8",
        "accent": "#5aa9ff", "ok": "#4fd68a", "warn": "#ffc65a", "bad": "#ff7b8a",
        "btn": "#16233a", "btn_fg": "#e8eef8", "btn_disabled": "#46536a",
        "danger_bg": "#2a1b22", "danger_border": "#452833",
        "input_bg": "#0c1524", "nav_sel": "#1b2a44", "nav_hover": "#16203a",
        "header_bg": "#0e1726", "scroll": "#26344c", "log_bg": "#080e1a",
        "on_accent": "#06203a", "table_sel": "#1b2a44",
        "warn_bg": "#2a2416", "warn_border": "#453a20",
    },
}

ORDER = ["深色", "米白", "夜蓝"]
DEFAULT = "深色"


def get(name=None):
    return THEMES.get(name or DEFAULT) or THEMES[DEFAULT]


def names():
    return list(ORDER)


def next_name(name):
    """循环切：深色 → 米白 → 夜蓝 → 深色。"""
    name = name if name in ORDER else DEFAULT
    return ORDER[(ORDER.index(name) + 1) % len(ORDER)]


def qss(c):
    """把一套颜色铺成 QSS。所有颜色都从 c 里取，别再写字面量。"""
    return f"""
QWidget {{ background: {c['bg']}; color: {c['text']};
           font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
           font-size: 13px; }}
/* 文字控件不铺底色，否则卡片里会浮出一条比卡片深/浅的色带 */
QLabel, QCheckBox {{ background: transparent; }}
QToolTip {{ background: {c['panel']}; color: {c['text']};
            border: 1px solid {c['line']}; padding: 4px; }}
#Sidebar {{ background: {c['panel']}; border-right: 1px solid {c['line']}; }}
#Brand {{ font-size: 17px; font-weight: 700; padding: 18px 18px 6px 18px; }}
#BrandSub {{ color: {c['muted']}; font-size: 11px; padding: 0 18px 14px 18px; }}
QListWidget#Nav {{ background: transparent; border: 0; outline: 0; padding: 6px 8px; }}
QListWidget#Nav::item {{ padding: 10px 12px; border-radius: 8px; color: {c['muted']}; }}
QListWidget#Nav::item:selected {{ background: {c['nav_sel']}; color: {c['text']}; }}
QListWidget#Nav::item:hover {{ background: {c['nav_hover']}; }}
#Title {{ font-size: 20px; font-weight: 700; }}
#Sub {{ color: {c['muted']}; font-size: 12px; }}
QFrame#Card {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 10px; }}
QLabel#CardNum {{ font-size: 26px; font-weight: 700; }}
QLabel#CardLabel {{ color: {c['muted']}; font-size: 12px; }}
QPushButton {{ background: {c['btn']}; border: 1px solid {c['line']}; border-radius: 8px;
               padding: 7px 14px; color: {c['btn_fg']}; }}
QPushButton:hover {{ border-color: {c['accent']}; }}
QPushButton:disabled {{ color: {c['btn_disabled']}; border-color: {c['line']}; }}
QPushButton#Primary {{ background: {c['accent']}; border: 0; color: {c['on_accent']};
                       font-weight: 600; }}
QPushButton#Primary:hover {{ background: {c['accent']}; }}
QPushButton#Primary:disabled {{ background: {c['line']}; color: {c['btn_disabled']}; }}
QPushButton#Danger {{ background: {c['danger_bg']}; border-color: {c['danger_border']};
                      color: {c['bad']}; }}
QPushButton#Mini {{ background: transparent; border: 1px solid {c['line']};
                    border-radius: 6px; padding: 2px 8px; font-size: 11px;
                    color: {c['muted']}; }}
QPushButton#Mini:hover {{ border-color: {c['accent']}; color: {c['text']}; }}
QPushButton#Mini:disabled {{ color: {c['btn_disabled']}; }}
QLineEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background: {c['input_bg']}; border: 1px solid {c['line']}; border-radius: 8px;
    padding: 7px 10px; color: {c['text']}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {c['accent']}; }}
QTableWidget {{ background: {c['panel']}; border: 1px solid {c['line']};
                border-radius: 10px; gridline-color: {c['line']};
                alternate-background-color: {c['input_bg']}; }}
QTableWidget::item {{ padding: 6px; }}
QTableWidget::item:selected {{ background: {c['table_sel']}; color: {c['text']}; }}
QHeaderView::section {{ background: {c['header_bg']}; color: {c['muted']}; border: 0;
                        padding: 8px; font-weight: 600; }}
QCheckBox {{ spacing: 8px; }}
QListWidget#KwList {{ background: {c['input_bg']}; border: 1px solid {c['line']};
                      border-radius: 8px; padding: 4px; }}
QLabel#Banner {{ background: {c['warn_bg']}; border: 1px solid {c['warn_border']};
                 border-radius: 8px; padding: 10px; color: {c['warn']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {c['scroll']}; border-radius: 5px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
#Log {{ background: {c['log_bg']}; border: 1px solid {c['line']}; border-radius: 10px;
        color: {c['muted']}; font-family: Consolas, monospace; font-size: 12px; }}
#LogMini {{ background: {c['log_bg']}; border: 1px solid {c['line']}; border-radius: 8px;
            color: {c['muted']}; font-family: Consolas, monospace; font-size: 12px; }}
#LogMono {{ background: {c['log_bg']}; border: 1px solid {c['line']}; border-radius: 8px;
            font-family: Consolas, monospace; font-size: 12px; color: {c['text']}; }}
"""


def saved():
    """上次选的主题。读不到就当默认。"""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
        name = cfg.get("ui_theme")
        return name if name in THEMES else DEFAULT
    except Exception:
        return DEFAULT


def save(name):
    """记住选择。

    只在**文件不存在**时才新建；文件存在但读不动（尾逗号、编码变了、被写坏）
    就什么都不写、返回 False。

    以前读不动时 `cfg = {}` 然后整份覆盖 —— config.json 是手改的，
    一个尾逗号就让 api_key、接口 token、vault、rooms、记忆开关全没，
    而且这发生在**每次启动**（apply_theme 会调 save），无备份无提示。
    """
    if name not in THEMES:
        return False
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            print(f"[themes] config.json 读不出来，这次不改写它："
                  f"{type(exc).__name__}: {exc}", flush=True)
            return False
        if not isinstance(cfg, dict):
            print("[themes] config.json 不是对象，这次不改写它", flush=True)
            return False
    else:
        cfg = {}
    cfg["ui_theme"] = name
    try:
        tmp = CONFIG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8", newline="\n")
        os.replace(tmp, CONFIG)
        return True
    except Exception:
        return False


def main():
    for n in ORDER:
        c = get(n)
        missing = [k for k in KEYS if k not in c]
        print(f"{n}: {len(c)} 个颜色" + (f"，缺 {missing}" if missing else ""))
    print("下一个（从深色开始）:", next_name("深色"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
