# -*- coding: utf-8 -*-
"""介面共用：配色、螢幕縮放、扁平按鈕、ttk 樣式。"""
import sys
import tkinter as tk
from tkinter import ttk

from .common import IS_WINDOWS

# 螢幕縮放比例（175% = 1.75）。字型用「點」會自己縮放，像素值要自己乘
UI_SCALE = 1.0


def set_scale(root):
    global UI_SCALE
    UI_SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)


def px(*values):
    scaled = tuple(round(v * UI_SCALE) for v in values)
    return scaled[0] if len(scaled) == 1 else scaled


# 配色：深藍＋寶藍（跟口袋快傳、藍色大肥魚同一套）
BG = "#f4f6fc"
CARD = "#ffffff"
TEXT = "#1f2a3d"
MUTED = "#6b7688"
BORDER = "#dde2f1"
ACCENT = "#3558d4"
ACCENT_DARK = "#2542ad"
ACCENT_SOFT = "#eef2ff"
FIELD = "#f6f8fe"
OK_COLOR = "#22a35a"
WARN_COLOR = "#c98a00"
BAD_COLOR = "#d64545"
HEAD_BG = "#1a2656"
HEAD_TEXT = "#c1cbee"
HEAD_ONLINE = "#7be0a4"
FONT = "Microsoft JhengHei UI" if IS_WINDOWS else "PingFang TC" if sys.platform == "darwin" else "Noto Sans CJK TC"


class FlatButton(tk.Label):
    """用 Label 做的扁平按鈕（tk.Button 在 Mac 上不能換顏色）。"""

    def __init__(self, parent, text, command, primary=True, small=False, danger=False):
        if danger:
            self.colors = ("#fdecec", "#f8d7d7", BAD_COLOR)
        elif primary:
            self.colors = (ACCENT, ACCENT_DARK, "#ffffff")
        else:
            self.colors = (ACCENT_SOFT, "#dfe6ff", ACCENT)
        super().__init__(parent, text=text, bg=self.colors[0], fg=self.colors[2], cursor="hand2",
                         font=(FONT, 9 if small else 10, "bold"), padx=px(12 if small else 16), pady=px(5 if small else 8))
        self.command = command
        self.enabled = True
        self.bind("<Enter>", lambda e: self.enabled and self.configure(bg=self.colors[1]))
        self.bind("<Leave>", lambda e: self.configure(bg=self.colors[0]))
        self.bind("<Button-1>", lambda e: self.enabled and self.command())

    def set_enabled(self, on):
        self.enabled = on
        self.configure(fg=self.colors[2] if on else "#aab3c5", cursor="hand2" if on else "arrow")


def card(parent, title=None, **pack):
    """白色卡片（外面一圈淡框）。回傳裡面放東西的框。"""
    outer = tk.Frame(parent, bg=BORDER)
    outer.pack(**pack)
    inner = tk.Frame(outer, bg=CARD)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    box = tk.Frame(inner, bg=CARD)
    box.pack(fill="both", expand=True, padx=px(16), pady=px(12))
    if title:
        tk.Label(box, text=title, bg=CARD, fg=TEXT, font=(FONT, 11, "bold")).pack(anchor="w", pady=(0, px(6)))
    return box


def setup_styles(root):
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure("Treeview", font=(FONT, 10), rowheight=px(30), background=CARD, fieldbackground=CARD,
                 foreground=TEXT, bordercolor=BORDER, borderwidth=0)
    st.configure("Treeview.Heading", font=(FONT, 9, "bold"), background=ACCENT_SOFT, foreground=MUTED,
                 relief="flat", borderwidth=0, padding=(px(6), px(5)))
    st.map("Treeview", background=[("selected", "#dfe6ff")], foreground=[("selected", TEXT)])
    st.map("Treeview.Heading", background=[("active", "#dfe6ff")])
    st.configure("TCombobox", padding=(px(6), px(4)), arrowsize=px(14))
    st.map("TCombobox", fieldbackground=[("readonly", FIELD)], selectbackground=[("readonly", FIELD)],
           selectforeground=[("readonly", TEXT)])
    for name in ("Card.TCheckbutton", "Card.TRadiobutton"):  # 勾選框在高縮放螢幕上要放大，不然幾乎看不到
        st.configure(name, background=CARD, foreground=TEXT, font=(FONT, 10), indicatorsize=px(15),
                     indicatormargin=(0, 0, px(6), 0), indicatorbackground="#ffffff", indicatorforeground=ACCENT,
                     upperbordercolor=BORDER, lowerbordercolor=BORDER)
        st.map(name, background=[("active", CARD)], indicatorbackground=[("selected", "#ffffff"), ("active", ACCENT_SOFT)])
    st.configure("Accent.Horizontal.TProgressbar", troughcolor=BORDER, background=ACCENT, bordercolor=BORDER,
                 lightcolor=ACCENT, darkcolor=ACCENT, thickness=px(8))
    st.configure("Vertical.TScrollbar", arrowsize=px(12))
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)


class Choice:
    """下拉選單：顯示中文說明，存的是代碼。"""

    def __init__(self, parent, items, value, on_change=None, width=26):
        self.items = list(items)  # [(代碼, 顯示文字)]
        self.var = tk.StringVar()
        self.box = ttk.Combobox(parent, textvariable=self.var, state="readonly", width=width, font=(FONT, 10),
                                values=[t for _, t in self.items])
        self.on_change = on_change
        self.set(value)
        self.box.bind("<<ComboboxSelected>>", lambda e: self.on_change and self.on_change(self.get()))

    def set(self, value):
        label = next((t for v, t in self.items if v == value), self.items[0][1] if self.items else "")
        self.var.set(label)

    def get(self):
        label = self.var.get()
        return next((v for v, t in self.items if t == label), self.items[0][0] if self.items else None)

    def set_items(self, items, value=None):
        cur = self.get() if value is None else value
        self.items = list(items)
        self.box.configure(values=[t for _, t in self.items])
        self.set(cur)
