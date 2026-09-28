# -*- coding: utf-8 -*-
"""快字幕 QuickSub：影片拖進來，AI 自動產生字幕（可以翻譯、燒進影片）。

檔案分工：
  qs/common.py   語言、資料夾、設定、下載      qs/engine.py    AI 轉字幕（背景程式）、模型和顯示卡元件
  qs/subs.py     斷句、繁簡、字幕格式          qs/translate.py 翻譯（Ollama／線上 AI）
  qs/media.py    ffmpeg、yt-dlp                qs/pipeline.py  一個工作從頭到尾的流程
  qs/editor.py   字幕編輯器                    qs/ui.py        配色、按鈕
"""
import sys

if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] in ("--worker", "--live"):
    # 背景程式：只做 AI 轉字幕（--live：即時字幕），不開視窗
    if sys.argv[1] == "--worker":
        from qs.engine import run_worker
        run_worker(int(sys.argv[2]))
    else:
        from qs.live import run_live
        run_live(int(sys.argv[2]))
    sys.exit(0)

import json
import os
import queue
import socket
import subprocess
import tempfile
import threading
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from qs import engine, media, pipeline, subs, translate
from qs.common import (APP_ID, APP_NAME, APP_VERSION, BASE_DIR, DATA_DIR, IS_WINDOWS, RES_DIR, UPDATE_REPO,
                       Config, T, download, fetch_json)
from qs.editor import Editor
from qs.ui import (ACCENT, ACCENT_SOFT, BAD_COLOR, BG, BORDER, CARD, FIELD, FONT, HEAD_BG, HEAD_ONLINE, HEAD_TEXT,
                   MUTED, OK_COLOR, TEXT, Choice, FlatButton, card, px, set_scale, setup_styles)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    TkinterDnD = None

if IS_WINDOWS:
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass

SINGLE_PORT = int(os.environ.get("QUICKSUB_PORT", "47863"))  # 已經開著時，新開的把檔案交給它就結束（右鍵「傳送到」會用到）
SUB_EXT = (".srt", ".vtt")
DROP_IDLE, DROP_HOVER = "#c9d5fb", ACCENT


def version_tuple(v):
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except ValueError:
        return (0,)


class App:
    def __init__(self, root, cfg, dnd_ok, q):
        self.root = root
        self.cfg = cfg
        self.dnd_ok = dnd_ok
        self.q = q
        set_scale(root)
        setup_styles(root)
        self.rows = {}           # job.id -> Treeview 的列
        self.live_panel = None
        self.jobs = {}
        self.update_info = None
        self.runner = pipeline.Runner(cfg, lambda job: self.q.put(("job", job)), lambda msg: self.q.put(("log", msg)))

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.geometry("%dx%d" % px(1040, 760))
        root.minsize(*px(900, 640))
        ico = os.path.join(RES_DIR, "icon.ico")
        if IS_WINDOWS and os.path.exists(ico):
            try:
                root.iconbitmap(default=ico)
            except tk.TclError:
                pass
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build()
        self.pump()
        threading.Thread(target=self.check_gpu, daemon=True).start()
        threading.Thread(target=self.check_update, daemon=True).start()
        if os.environ.get("QUICKSUB_LIVE_FILE"):  # 測試用：打開就開始即時字幕（聲音從這個檔案來）
            root.after(1500, lambda: (self.open_live(), self.live_panel.start()))

    # ------------------------------------------------------------------ 版面
    def build(self):
        root = self.root
        head = tk.Frame(root, bg=HEAD_BG)
        head.pack(fill="x")
        inner = tk.Frame(head, bg=HEAD_BG)
        inner.pack(fill="x", padx=px(20), pady=px(14))
        self.logo = None
        logo = os.path.join(RES_DIR, "icon-96.png" if px(100) >= 150 else "icon-48.png")
        if os.path.exists(logo):
            try:
                self.logo = tk.PhotoImage(file=logo)
                tk.Label(inner, image=self.logo, bg=HEAD_BG).pack(side="left", padx=(0, px(12)))
            except tk.TclError:
                pass
        titles = tk.Frame(inner, bg=HEAD_BG)
        titles.pack(side="left")
        tk.Label(titles, text=APP_NAME, bg=HEAD_BG, fg="#ffffff", font=(FONT, 17, "bold")).pack(anchor="w")
        tk.Label(titles, text=T("影片拖進來，AI 自動上字幕", "Drop in a video, get subtitles"), bg=HEAD_BG, fg=HEAD_TEXT,
                 font=(FONT, 10)).pack(anchor="w")
        right = tk.Frame(inner, bg=HEAD_BG)
        right.pack(side="right")
        self.update_btn = FlatButton(right, T("更新", "Update"), self.do_update, small=True)
        self.gpu_label = tk.Label(right, text=T("◌ 檢查顯示卡…", "◌ Checking the GPU…"), bg=HEAD_BG, fg=HEAD_TEXT, font=(FONT, 10))
        self.gpu_label.pack(side="right")

        self.log_label = tk.Label(root, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w")
        self.log_label.pack(side="bottom", fill="x", padx=px(18), pady=(0, px(8)))
        body = tk.Frame(root, bg=BG)
        body.pack(fill="both", expand=True, padx=px(16), pady=px(14))
        side = tk.Frame(body, bg=BG, width=px(370))
        side.pack(side="right", fill="y", padx=(px(14), 0))
        side.pack_propagate(False)
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True)

        self.build_drop(left)
        self.build_queue(left)
        self.build_settings(side)

    def build_drop(self, parent):
        box = card(parent, None, fill="x")
        self.drop = tk.Frame(box, bg=ACCENT_SOFT, highlightthickness=2, highlightbackground=DROP_IDLE)
        self.drop.pack(fill="x")
        tk.Label(self.drop, text=T("把影片或音樂拖到這裡", "Drop videos or audio here"), bg=ACCENT_SOFT, fg=ACCENT,
                 font=(FONT, 15, "bold")).pack(pady=(px(18), px(2)))
        tk.Label(self.drop, text=T("可以一次拖很多個，也可以拖整個資料夾；拖字幕檔進來會打開編輯器",
                                   "Many at once or a whole folder; drop a subtitle file to edit it"),
                 bg=ACCENT_SOFT, fg=MUTED, font=(FONT, 10)).pack()
        btns = tk.Frame(self.drop, bg=ACCENT_SOFT)
        btns.pack(pady=(px(10), px(16)))
        FlatButton(btns, T("選檔案", "Choose files"), self.pick_files).pack(side="left")
        FlatButton(btns, T("選資料夾", "Choose a folder"), self.pick_folder, primary=False).pack(side="left", padx=(px(8), 0))
        FlatButton(btns, T("打開字幕檔來編輯", "Edit a subtitle file"), self.pick_subtitle, primary=False).pack(side="left", padx=(px(8), 0))
        FlatButton(btns, T("● 即時字幕（線上影片、直播）", "● Live subtitles (streams, online video)"), self.open_live,
                   primary=False).pack(side="left", padx=(px(8), 0))
        url = tk.Frame(box, bg=CARD)
        url.pack(fill="x", pady=(px(10), 0))
        tk.Label(url, text=T("或貼上影片網址：", "Or paste a video link:"), bg=CARD, fg=TEXT, font=(FONT, 10)).pack(side="left")
        self.url_entry = tk.Entry(url, font=(FONT, 10), relief="flat", bg=FIELD, highlightthickness=1, highlightbackground=BORDER)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(px(6), px(8)), ipady=px(4))
        self.url_entry.bind("<Return>", lambda e: self.add_url())
        FlatButton(url, T("加入", "Add"), self.add_url, small=True).pack(side="left")
        if self.dnd_ok:
            try:
                self.root.drop_target_register(DND_FILES)
                self.root.dnd_bind("<<Drop>>", self.on_drop)
                self.root.dnd_bind("<<DropEnter>>", self.on_drop_enter)
                self.root.dnd_bind("<<DropLeave>>", self.on_drop_leave)
            except Exception:
                self.dnd_ok = False

    def build_queue(self, parent):
        box = card(parent, T("工作清單", "Jobs"), fill="both", expand=True, pady=(px(12), 0))
        acts = tk.Frame(box, bg=CARD)
        acts.pack(side="bottom", fill="x", pady=(px(6), 0))
        FlatButton(acts, T("編輯字幕", "Edit subtitles"), self.edit_selected, small=True).pack(side="left")
        FlatButton(acts, T("打開資料夾", "Open folder"), self.open_folder, primary=False, small=True).pack(side="left", padx=(px(6), 0))
        FlatButton(acts, T("重新做", "Redo"), self.redo_selected, primary=False, small=True).pack(side="left", padx=(px(6), 0))
        FlatButton(acts, T("取消", "Cancel"), self.cancel_selected, primary=False, small=True).pack(side="left", padx=(px(6), 0))
        FlatButton(acts, T("從清單移除", "Remove"), self.remove_selected, primary=False, small=True).pack(side="left", padx=(px(6), 0))
        prog = tk.Frame(box, bg=CARD)
        prog.pack(side="bottom", fill="x", pady=(px(8), 0))
        self.bar = ttk.Progressbar(prog, style="Accent.Horizontal.TProgressbar", maximum=1000)
        self.bar.pack(fill="x")
        self.eta = tk.Label(prog, text="", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w", justify="left", wraplength=px(560))
        self.eta.pack(fill="x", pady=(px(2), 0))

        frame = tk.Frame(box, bg=BORDER)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("name", "status"), show="headings", selectmode="extended")
        self.tree.heading("name", text=T("檔案", "File"))
        self.tree.heading("status", text=T("狀態", "Status"))
        self.tree.column("name", width=px(260), stretch=True)
        self.tree.column("status", width=px(300), stretch=True)
        self.tree.tag_configure("done", foreground=OK_COLOR)
        self.tree.tag_configure("fail", foreground=BAD_COLOR)
        self.tree.tag_configure("cancel", foreground=MUTED)
        self.tree.tag_configure("run", foreground=ACCENT)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=1, pady=1)
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_progress())
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.empty = tk.Label(self.tree, text=T("還沒有工作。把影片拖進上面的框框就會開始。", "No jobs yet. Drop a video above to start."),
                              bg=CARD, fg=MUTED, font=(FONT, 10))
        self.empty.place(relx=0.5, rely=0.45, anchor="center")

    def build_settings(self, parent):
        box = card(parent, T("設定", "Settings"), fill="both", expand=True)
        c = self.cfg
        grid = tk.Frame(box, bg=CARD)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        rows = [0]

        def row(label, widget):
            tk.Label(grid, text=label, bg=CARD, fg=TEXT, font=(FONT, 10)).grid(row=rows[0], column=0, sticky="w", pady=px(3), padx=(0, px(8)))
            widget.grid(row=rows[0], column=1, sticky="ew", pady=px(3))
            rows[0] += 1

        self.c_lang = Choice(grid, [(k, T(z, e)) for k, z, e in engine.LANGUAGES], c.get("language"),
                             lambda v: c.set("language", v), width=22)
        row(T("影片語言", "Spoken language"), self.c_lang.box)
        self.c_model = Choice(grid, self.model_items(), c.get("model"), lambda v: c.set("model", v), width=22)
        row(T("AI 模型", "AI model"), self.c_model.box)
        self.c_conv = Choice(grid, [("s2twp", T("繁體（台灣用語）", "Traditional (Taiwan)")), ("s2hk", T("繁體（香港）", "Traditional (Hong Kong)")),
                                    ("t2s", T("簡體", "Simplified")), ("none", T("不轉換", "Don't convert"))],
                             c.get("convert"), lambda v: c.set("convert", v), width=22)
        row(T("中文字幕", "Chinese text"), self.c_conv.box)
        self.c_punct = Choice(grid, [("space", T("標點換成空格（台灣習慣）", "Punctuation → spaces")), ("trim", T("只拿掉句尾標點", "Trim end punctuation")),
                                     ("keep", T("保留標點", "Keep punctuation"))], c.get("punct"), lambda v: c.set("punct", v), width=22)
        row(T("中文標點", "Punctuation"), self.c_punct.box)
        chars = [(0, T("自動", "Auto"))] + [(n, str(n)) for n in (12, 14, 16, 18, 20, 22, 25, 30, 36, 42, 50, 60)]
        self.c_chars = Choice(grid, chars, int(c.get("max_chars") or 0), lambda v: c.set("max_chars", v), width=22)
        row(T("每行字數", "Max per line"), self.c_chars.box)
        self.c_trans = Choice(grid, [("", T("不翻譯", "Don't translate"))] + [(k, T("翻成", "To ") + T(z, e)) for k, z, e, _ in translate.TARGETS],
                              c.get("translate"), self.on_translate, width=22)
        row(T("翻譯", "Translate"), self.c_trans.box)
        layout = "trans" if not c.get("bilingual") else ("both_tf" if c.get("trans_first") else "both")
        self.c_layout = Choice(grid, [("both", T("雙語：原文在上", "Bilingual: original on top")), ("both_tf", T("雙語：翻譯在上", "Bilingual: translation on top")),
                                      ("trans", T("只要翻譯", "Translation only"))], layout, self.on_layout, width=22)
        row(T("雙語字幕", "Layout"), self.c_layout.box)

        tk.Label(box, text=T("存成", "Save as"), bg=CARD, fg=TEXT, font=(FONT, 10)).pack(anchor="w", pady=(px(8), 0))
        fm = tk.Frame(box, bg=CARD)
        fm.pack(fill="x")
        self.fmt_vars = {}
        for f in ("srt", "vtt", "ass", "txt"):
            v = tk.BooleanVar(value=f in (c.get("formats") or []))
            ttk.Checkbutton(fm, text=f.upper(), variable=v, style="Card.TCheckbutton", command=self.on_formats).pack(side="left", padx=(0, px(8)))
            self.fmt_vars[f] = v
        self.burn_var = tk.BooleanVar(value=bool(c.get("burn")))
        self.embed_var = tk.BooleanVar(value=bool(c.get("embed")))
        ttk.Checkbutton(box, text=T("把字幕燒進影片（任何播放器都看得到）", "Burn subtitles into the video"), variable=self.burn_var,
                        style="Card.TCheckbutton", command=lambda: c.set("burn", self.burn_var.get())).pack(anchor="w", pady=(px(6), 0))
        ttk.Checkbutton(box, text=T("放進影片當字幕軌（可以開關，很快）", "Add as a subtitle track (toggleable, fast)"), variable=self.embed_var,
                        style="Card.TCheckbutton", command=lambda: c.set("embed", self.embed_var.get())).pack(anchor="w")

        tk.Label(box, text=T("專有名詞（人名、術語，用逗號隔開）", "Names & terms (comma separated)"), bg=CARD, fg=TEXT,
                 font=(FONT, 10)).pack(anchor="w", pady=(px(10), 0))
        self.terms = tk.Entry(box, font=(FONT, 10), relief="flat", bg=FIELD, highlightthickness=1, highlightbackground=BORDER)
        self.terms.insert(0, c.get("prompt") or "")
        self.terms.pack(fill="x", pady=(px(2), 0), ipady=px(4))
        self.terms.bind("<FocusOut>", lambda e: c.set("prompt", self.terms.get().strip()))
        self.terms.bind("<Return>", lambda e: c.set("prompt", self.terms.get().strip()))

        out = tk.Frame(box, bg=CARD)
        out.pack(fill="x", pady=(px(10), 0))
        tk.Label(out, text=T("存到", "Save to"), bg=CARD, fg=TEXT, font=(FONT, 10)).pack(side="left")
        self.c_out = Choice(out, [("same", T("跟影片同一個資料夾", "Same folder as the video")), ("folder", T("指定的資料夾…", "A folder of my choice…"))],
                            c.get("out_mode"), self.on_out_mode, width=20)
        self.c_out.box.pack(side="left", fill="x", expand=True, padx=(px(8), 0))
        self.out_label = tk.Label(box, text="", bg=CARD, fg=MUTED, font=(FONT, 9), anchor="w", justify="left", wraplength=px(320))
        self.out_label.pack(fill="x")
        self.show_out_dir()

        FlatButton(box, T("進階設定（翻譯服務、顯示卡…）", "More settings (translation, GPU…)"), self.show_advanced,
                   primary=False, small=True).pack(anchor="w", pady=(px(10), 0))
        self.hint = tk.Label(box, text="", bg=CARD, fg=MUTED, font=(FONT, 9), justify="left", wraplength=px(320), anchor="w")
        self.hint.pack(fill="x", pady=(px(6), 0))
        self.update_hint()

    def model_items(self):
        out = []
        for mid, _repo, mb, zh, en in engine.MODELS:
            size = f"{mb / 1024:.1f} GB" if mb >= 1000 else f"{mb} MB"
            mark = T("　✓ 已下載", "  ✓ ready") if engine.model_ready(mid) else ""
            out.append((mid, f"{T(zh, en)}（{size}）{mark}"))
        return out

    # ---- 設定改變
    def on_translate(self, v):
        self.cfg.set("translate", v)
        self.update_hint()

    def update_layout_state(self):
        self.c_layout.box.configure(state="readonly" if self.cfg.get("translate") else "disabled")

    def on_layout(self, v):
        self.cfg.data["bilingual"] = v != "trans"
        self.cfg.data["trans_first"] = v == "both_tf"
        self.cfg.save()

    def on_formats(self):
        fmts = [f for f, v in self.fmt_vars.items() if v.get()]
        if not fmts:  # 至少要有一種
            self.fmt_vars["srt"].set(True)
            fmts = ["srt"]
        self.cfg.set("formats", fmts)

    def on_out_mode(self, v):
        if v == "folder":
            d = filedialog.askdirectory(title=T("字幕要存到哪裡？", "Where to save subtitles?"), initialdir=self.cfg.get("out_dir") or None)
            if not d:
                self.c_out.set("same")
                v = "same"
            else:
                self.cfg.set("out_dir", os.path.normpath(d))
        self.cfg.set("out_mode", v)
        self.show_out_dir()

    def show_out_dir(self):
        self.out_label.configure(text=self.cfg.get("out_dir") if self.cfg.get("out_mode") == "folder" else "")

    def update_hint(self):
        tips = []
        self.update_layout_state()
        if self.cfg.get("translate"):
            if self.cfg.get("engine") == "api":
                name = translate.PRESETS.get(self.cfg.get("api_preset"), translate.PRESETS["custom"])[0]
                tips.append(T(f"翻譯用：{name}（線上）", f"Translating with {name} (online)"))
            else:
                tips.append(T("翻譯用：本機的 Ollama（要先打開 Ollama）。想換成線上 AI，到進階設定。",
                              "Translating with Ollama on this PC (open it first). Switch to an online AI in More settings."))
        self.hint.configure(text="\n".join(tips))

    # ------------------------------------------------------------------ 加入工作
    def on_drop_enter(self, event):
        self.drop.configure(highlightbackground=DROP_HOVER)
        return event.action

    def on_drop_leave(self, event):
        self.drop.configure(highlightbackground=DROP_IDLE)
        return event.action

    def on_drop(self, event):
        self.drop.configure(highlightbackground=DROP_IDLE)
        self.add_paths(self.root.tk.splitlist(event.data))
        return event.action

    def pick_files(self):
        exts = " ".join("*" + e for e in sorted(media.MEDIA_EXT))
        paths = filedialog.askopenfilenames(title=T("選影片或音樂", "Choose videos or audio"),
                                            filetypes=[(T("影片和音樂", "Video and audio"), exts), (T("所有檔案", "All files"), "*.*")])
        self.add_paths(paths)

    def pick_folder(self):
        d = filedialog.askdirectory(title=T("選一個資料夾（裡面的影片都會加入）", "Choose a folder (all videos inside are added)"))
        if d:
            self.add_paths([d])

    def pick_subtitle(self):
        p = filedialog.askopenfilename(title=T("選字幕檔", "Choose a subtitle file"),
                                       filetypes=[(T("字幕", "Subtitles"), "*.srt *.vtt"), (T("所有檔案", "All files"), "*.*")])
        if p:
            self.open_subtitle(p)

    def add_paths(self, paths):
        added, subs_found = 0, []
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isdir(p):
                for root, _dirs, files in os.walk(p):
                    for f in sorted(files):
                        if os.path.splitext(f)[1].lower() in media.MEDIA_EXT:
                            self.add_job(pipeline.Job(os.path.join(root, f)))
                            added += 1
            elif os.path.splitext(p)[1].lower() in SUB_EXT:
                subs_found.append(p)
            elif os.path.isfile(p):
                self.add_job(pipeline.Job(p))
                added += 1
        for s in subs_found[:3]:
            self.open_subtitle(s)
        if not added and not subs_found and paths:
            self.set_log(T("沒有找到影片或聲音檔", "No video or audio files found"))

    def add_url(self):
        url = self.url_entry.get().strip()
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showinfo(APP_NAME, T("請貼上完整的網址（http 開頭）", "Please paste a full link (starting with http)"))
            return
        self.url_entry.delete(0, "end")
        self.add_job(pipeline.Job(url, is_url=True))

    def add_job(self, job):
        self.jobs[job.id] = job
        self.rows[job.id] = self.tree.insert("", "end", values=(job.name, job.status))
        self.empty.place_forget()
        self.runner.add(job)

    # ------------------------------------------------------------------ 更新畫面
    def pump(self):
        try:
            while True:
                kind, item = self.q.get_nowait()
                if kind == "job":
                    self.on_job(item)
                elif kind == "log":
                    self.set_log(item)
                elif kind == "gpu":
                    self.gpu_label.configure(text=item[0], fg=item[1])
                elif kind == "update":
                    self.show_update(item)
                elif kind == "files":
                    self.add_paths(item)
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def on_job(self, job):
        row = self.rows.get(job.id)
        if row is None or not self.tree.exists(row):
            return
        self.tree.item(row, values=(job.name, job.status), tags=(job.state,))
        if job.state in ("done", "fail"):
            self.c_model.set_items(self.model_items())  # 模型可能剛下載好
        self.show_progress()

    def show_progress(self):
        """進度條顯示選取的那個工作（沒選、或選的沒在跑，就顯示正在跑的）。"""
        sel = self.tree.selection()
        job = None
        if sel:
            job = next((j for j in self.jobs.values() if self.rows.get(j.id) == sel[0]), None)
        if job is None or job.state != "run":
            job = next((j for j in self.jobs.values() if j.state == "run"), job)
        if job is None:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self.bar["value"] = 0
            self.eta.configure(text="")
            return
        if job.state == "run":
            if job.frac is None:
                if str(self.bar["mode"]) != "indeterminate":
                    self.bar.configure(mode="indeterminate")
                    self.bar.start(15)
            else:
                if str(self.bar["mode"]) != "determinate":
                    self.bar.stop()
                    self.bar.configure(mode="determinate")
                self.bar["value"] = job.frac * 1000
            self.eta.configure(text=f"{job.name}　{job.status}　{job.eta}")
        else:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self.bar["value"] = 1000 if job.state == "done" else 0
            files = [os.path.basename(p) for p in job.files.values()]
            self.eta.configure(text=(T("存好了：", "Saved: ") + T("、", ", ").join(files)) if job.state == "done" and files else job.status)

    def set_log(self, msg):
        self.log_label.configure(text=msg)

    def check_gpu(self):
        if self.cfg.get("device") == "cpu":
            self.q.put(("gpu", (T("● 只用處理器（在進階設定可以改）", "● CPU only (change in More settings)"), HEAD_TEXT)))
        elif engine.has_nvidia():
            name = engine.gpu_name() or "NVIDIA"
            self.q.put(("gpu", (T("● 顯示卡加速：", "● GPU: ") + name.replace("NVIDIA GeForce ", ""), HEAD_ONLINE)))
        else:
            self.q.put(("gpu", (T("● 用處理器（沒有 NVIDIA 顯示卡，會比較慢）", "● CPU (no NVIDIA GPU, slower)"), HEAD_TEXT)))

    # ------------------------------------------------------------------ 清單的按鈕
    def selected_jobs(self):
        sel = set(self.tree.selection())
        return [j for j in self.jobs.values() if self.rows.get(j.id) in sel]

    def edit_selected(self):
        job = next((j for j in self.selected_jobs() if j.segs and j.state != "run"), None)
        if not job:
            messagebox.showinfo(APP_NAME, T("先選一個已經完成的工作", "Select a finished job first"))
            return
        Editor(self, job)

    def open_folder(self):
        jobs = self.selected_jobs() or [j for j in self.jobs.values() if j.files][-1:]
        for job in jobs[:1]:
            target = next(iter(job.files.values()), None) or job.path
            if target and os.path.exists(target) and IS_WINDOWS:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])

    def cancel_selected(self):
        for job in self.selected_jobs():
            if job.state in ("wait", "run"):
                job.cancel.set()
                if job.state == "wait":
                    job.state, job.status = "cancel", T("已取消", "Cancelled")
                    self.on_job(job)

    def redo_selected(self):
        for job in self.selected_jobs():
            if job.state in ("done", "fail", "cancel"):
                job.cancel = threading.Event()
                job.redo_video = False
                job.state, job.status, job.note, job.files, job.segs = "wait", T("等待中", "Waiting"), "", {}, None
                self.on_job(job)
                self.runner.wake.set()

    def redo_video(self, job):
        """編輯器改完字幕：只重做燒進影片／字幕軌。"""
        if job.state in ("wait", "run") or job.id not in self.jobs:
            return
        job.cancel = threading.Event()
        job.redo_video = True
        job.state, job.status = "wait", T("等待重做影片", "Waiting to redo the video")
        self.on_job(job)
        self.runner.wake.set()

    def remove_selected(self):
        for job in self.selected_jobs():
            if job.state == "run":
                continue
            job.cancel.set()
            job.state = "cancel"
            self.tree.delete(self.rows.pop(job.id))
            self.jobs.pop(job.id, None)
            if job in self.runner.jobs:
                self.runner.jobs.remove(job)
        if not self.jobs:
            self.empty.place(relx=0.5, rely=0.45, anchor="center")
        self.show_progress()

    def open_subtitle(self, path):
        """直接打開一個字幕檔來編輯（同資料夾有同名影片的話，預覽時一起用）。"""
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                segs = subs.parse_srt(f.read())
        except OSError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        if not segs:
            messagebox.showinfo(APP_NAME, T("這個檔案讀不到字幕（目前支援 SRT 和 VTT）", "No subtitles found in this file (SRT and VTT are supported)"))
            return
        job = pipeline.Job(path)
        job.segs = segs
        job.layout = "both" if any(s.trans for s in segs) else "orig"
        folder, name = os.path.split(path)
        stem = name.split(".")[0]
        try:
            job.video = next((os.path.join(folder, f) for f in os.listdir(folder)
                              if f.split(".")[0] == stem and os.path.splitext(f)[1].lower() in media.MEDIA_EXT - media.AUDIO_EXT), None)
        except OSError:
            job.video = None
        Editor(self, job, standalone_file=path)

    def open_live(self):
        if self.live_panel and self.live_panel.win.winfo_exists():
            self.live_panel.win.deiconify()
            self.live_panel.win.lift()
            return
        from qs.live import LivePanel
        self.live_panel = LivePanel(self)

    # ------------------------------------------------------------------ 進階設定
    def show_advanced(self):
        c = self.cfg
        d = tk.Toplevel(self.root)
        d.title(T("進階設定", "More settings"))
        d.configure(bg=CARD)
        d.transient(self.root)
        d.resizable(False, False)
        box = tk.Frame(d, bg=CARD)
        box.pack(padx=px(22), pady=px(16))

        tk.Label(box, text=T("翻譯服務", "Translation service"), bg=CARD, fg=TEXT, font=(FONT, 12, "bold")).pack(anchor="w")
        eng = tk.StringVar(value=c.get("engine"))
        ttk.Radiobutton(box, text=T("本機的 Ollama（免費、不上網，要先裝 Ollama）", "Ollama on this PC (free, offline; needs Ollama)"),
                        variable=eng, value="ollama", style="Card.TRadiobutton").pack(anchor="w", pady=(px(4), 0))
        orow = tk.Frame(box, bg=CARD)
        orow.pack(fill="x", padx=(px(24), 0))
        models = translate.ollama_models()
        o_items = [(m, m) for m in models] or [("", T("（找不到 Ollama 或還沒有模型）", "(Ollama not found or no models)"))]
        o_choice = Choice(orow, o_items, c.get("ollama_model") or translate.pick_ollama_model(models), width=30)
        o_choice.box.pack(side="left")
        FlatButton(orow, T("下載 Ollama", "Get Ollama"), lambda: webbrowser.open("https://ollama.com/download"), primary=False,
                   small=True).pack(side="left", padx=(px(8), 0))
        ttk.Radiobutton(box, text=T("線上 AI（翻得比較好，要填金鑰）", "Online AI (better quality, needs an API key)"),
                        variable=eng, value="api", style="Card.TRadiobutton").pack(anchor="w", pady=(px(10), 0))
        arow = tk.Frame(box, bg=CARD)
        arow.pack(fill="x", padx=(px(24), 0))
        arow.columnconfigure(1, weight=1)
        preset = Choice(arow, [(k, v[0]) for k, v in translate.PRESETS.items()], c.get("api_preset"), width=30)
        key_e = tk.Entry(arow, font=(FONT, 10), relief="flat", bg=FIELD, show="•", highlightthickness=1, highlightbackground=BORDER)
        key_e.insert(0, c.get("api_key") or "")
        model_e = tk.Entry(arow, font=(FONT, 10), relief="flat", bg=FIELD, highlightthickness=1, highlightbackground=BORDER)
        base_e = tk.Entry(arow, font=(FONT, 10), relief="flat", bg=FIELD, highlightthickness=1, highlightbackground=BORDER)
        for i, (label, w) in enumerate(((T("服務", "Service"), preset.box), (T("金鑰", "API key"), key_e),
                                        (T("模型", "Model"), model_e), (T("網址", "Base URL"), base_e))):
            tk.Label(arow, text=label, bg=CARD, fg=MUTED, font=(FONT, 10)).grid(row=i, column=0, sticky="w", pady=px(3), padx=(0, px(8)))
            w.grid(row=i, column=1, sticky="ew", pady=px(3), ipady=px(2))

        def fill_preset(_v=None, first=False):
            _name, base, model = translate.PRESETS.get(preset.get(), translate.PRESETS["custom"])
            same = first and preset.get() == c.get("api_preset")
            model_e.delete(0, "end")
            model_e.insert(0, c.get("api_model") if same and c.get("api_model") else model)
            base_e.delete(0, "end")
            base_e.insert(0, c.get("api_base") if same and c.get("api_base") else base)
        preset.on_change = fill_preset
        fill_preset(first=True)
        keys = {"gemini": "https://aistudio.google.com/apikey", "deepseek": "https://platform.deepseek.com/api_keys",
                "openai": "https://platform.openai.com/api-keys"}
        links = tk.Frame(box, bg=CARD)
        links.pack(fill="x", padx=(px(24), 0), pady=(px(2), 0))
        get_key = tk.Label(links, text=T("怎麼拿金鑰？", "Get a key"), bg=CARD, fg=ACCENT, font=(FONT, 9, "underline"), cursor="hand2")
        get_key.pack(side="left")
        get_key.bind("<Button-1>", lambda e: webbrowser.open(keys.get(preset.get(), keys["gemini"])))
        tk.Label(links, text=T("　金鑰只存在這台電腦", "  The key stays on this PC"), bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left")
        test_label = tk.Label(box, text="", bg=CARD, fg=MUTED, font=(FONT, 9), wraplength=px(440), justify="left")

        def collect():
            c.data.update(engine=eng.get(), ollama_model=o_choice.get() or "", api_preset=preset.get(),
                          api_key=key_e.get().strip(), api_model=model_e.get().strip(), api_base=base_e.get().strip())

        def test():
            collect()
            test_label.configure(text=T("◌ 測試中…", "◌ Testing…"), fg=ACCENT)

            def run():
                try:
                    segs = [subs.Seg(0, 1, "Good morning, everyone.")]
                    translate.translate(segs, "zh-TW", dict(c.data))
                    msg, color = T("✓ 可以用：", "✓ Works: ") + (segs[0].trans or ""), OK_COLOR
                except Exception as e:
                    msg, color = T("✗ 不能用：", "✗ Failed: ") + str(e)[:200], BAD_COLOR
                d.after(0, lambda: test_label.winfo_exists() and test_label.configure(text=msg, fg=color))
            threading.Thread(target=run, daemon=True).start()
        FlatButton(box, T("測試翻譯", "Test translation"), test, primary=False, small=True).pack(anchor="w", pady=(px(8), 0))
        test_label.pack(anchor="w", pady=(px(4), 0))

        ttk.Separator(box).pack(fill="x", pady=px(12))
        tk.Label(box, text=T("聽寫", "Transcription"), bg=CARD, fg=TEXT, font=(FONT, 12, "bold")).pack(anchor="w")
        g = tk.Frame(box, bg=CARD)
        g.pack(fill="x", pady=(px(4), 0))
        device = Choice(g, [("auto", T("自動（有 NVIDIA 顯示卡就用）", "Auto (use NVIDIA GPU if present)")), ("cpu", T("只用處理器", "CPU only"))],
                        c.get("device"), width=30)
        dur = Choice(g, [(float(n), T(f"{n} 秒", f"{n} s")) for n in (3, 4, 5, 6, 7, 8, 10)], float(c.get("max_dur") or 6), width=30)
        font = Choice(g, [(0, T("自動（依影片大小）", "Auto (by video size)"))] + [(n, str(n)) for n in (32, 40, 48, 56, 64, 72, 84)],
                      int(c.get("font_size") or 0), width=30)
        for i, (label, w) in enumerate(((T("用顯示卡", "Use GPU"), device.box), (T("每句最長", "Longest line"), dur.box),
                                        (T("燒字幕字型大小", "Burn-in font size"), font.box))):
            tk.Label(g, text=label, bg=CARD, fg=MUTED, font=(FONT, 10)).grid(row=i, column=0, sticky="w", pady=px(3), padx=(0, px(8)))
            w.grid(row=i, column=1, sticky="w", pady=px(3))
        vad = tk.BooleanVar(value=bool(c.get("vad", True)))
        ttk.Checkbutton(box, text=T("先跳過沒有人聲的地方（比較不會亂加字，建議開著）", "Skip parts without speech (fewer made-up lines; recommended)"),
                        variable=vad, style="Card.TCheckbutton").pack(anchor="w", pady=(px(6), 0))
        dlrow = tk.Frame(box, bg=CARD)
        dlrow.pack(fill="x", pady=(px(8), 0))
        tk.Label(dlrow, text=T("網址的影片下載到：", "Downloaded videos go to: "), bg=CARD, fg=MUTED, font=(FONT, 10)).pack(side="left")
        dl_var = tk.StringVar(value=c.get("dl_dir") or pipeline.default_download_dir())
        tk.Label(dlrow, textvariable=dl_var, bg=CARD, fg=TEXT, font=(FONT, 9)).pack(side="left")

        def pick_dl():
            p = filedialog.askdirectory(parent=d, initialdir=dl_var.get())
            if p:
                dl_var.set(os.path.normpath(p))
        FlatButton(dlrow, T("改", "Change"), pick_dl, primary=False, small=True).pack(side="left", padx=(px(8), 0))
        tk.Label(box, text=T(f"AI 模型和工具放在：{DATA_DIR}", f"AI models and tools are stored in: {DATA_DIR}"), bg=CARD, fg=MUTED,
                 font=(FONT, 9), wraplength=px(440), justify="left").pack(anchor="w", pady=(px(8), 0))

        def done():
            collect()
            c.data.update(device=device.get(), max_dur=dur.get(), font_size=font.get(), vad=vad.get(),
                          dl_dir="" if dl_var.get() == pipeline.default_download_dir() else dl_var.get())
            c.save()
            self.update_hint()
            threading.Thread(target=self.check_gpu, daemon=True).start()
            d.destroy()
        foot = tk.Frame(box, bg=CARD)
        foot.pack(fill="x", pady=(px(14), 0))
        FlatButton(foot, T("好了", "Done"), done).pack(side="right")
        FlatButton(foot, T("取消", "Cancel"), d.destroy, primary=False).pack(side="right", padx=(0, px(8)))
        d.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - d.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - d.winfo_reqheight()) // 4)
        d.geometry(f"+{max(0, x)}+{max(0, y)}")

    # ------------------------------------------------------------------ 更新
    def check_update(self):
        if not UPDATE_REPO:
            return
        try:
            rel = fetch_json(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest", timeout=15)
        except Exception:
            return
        if version_tuple(rel.get("tag_name", "")) > version_tuple(APP_VERSION):
            self.q.put(("update", rel))

    def show_update(self, rel):
        self.update_info = rel
        self.update_btn.configure(text=T(f"更新到 {rel['tag_name']}", f"Update to {rel['tag_name']}"))
        self.update_btn.pack(side="right", padx=(px(12), 0))

    def do_update(self):
        rel = self.update_info
        if not rel:
            return
        if any(j.state == "run" for j in self.jobs.values()):
            messagebox.showinfo(APP_NAME, T("等工作做完再更新", "Please wait for the current job to finish"))
            return
        asset = next((a for a in rel.get("assets", []) if a["name"].lower().startswith("quicksub-setup") and a["name"].endswith(".exe")), None)
        installed = os.path.exists(os.path.join(BASE_DIR, "unins000.exe"))
        if not asset or not installed:  # 免安裝版或用原始碼跑：打開下載頁
            webbrowser.open(rel.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases/latest")
            return
        self.update_btn.configure(text=T("下載中…", "Downloading…"))

        def run():
            try:
                dest = os.path.join(tempfile.gettempdir(), asset["name"])
                download(asset["browser_download_url"], dest, timeout=120)
                digest = (asset.get("digest") or "").replace("sha256:", "")
                if digest and engine.sha256_file(dest) != digest:
                    raise IOError(T("下載的更新檔校驗碼不對", "The update file failed its checksum"))
                subprocess.Popen([dest, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
                self.root.after(0, self.quit)
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda: (self.update_btn.configure(text=T("更新失敗，再試一次", "Update failed, retry")), self.set_log(msg)))
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ 關閉
    def on_close(self):
        if any(j.state in ("run", "wait") for j in self.jobs.values()):
            if not messagebox.askyesno(APP_NAME, T("還有工作沒做完，確定要關掉嗎？", "Some jobs aren't finished. Quit anyway?")):
                return
        self.quit()

    def quit(self):
        if self.live_panel:
            self.live_panel.session.stop()
        for j in self.jobs.values():
            j.cancel.set()
        self.runner.worker.stop()
        self.root.destroy()


def listen_single(q):
    """已經開著的快字幕：接收新開的那個送來的檔案。回傳 None = 已經有一個開著了。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if IS_WINDOWS:
        srv.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
    try:
        srv.bind(("127.0.0.1", SINGLE_PORT))
    except OSError:
        return None
    srv.listen(5)

    def loop():
        while True:
            conn, _ = srv.accept()
            with conn:
                conn.settimeout(5)
                data = b""
                try:
                    while True:
                        ch = conn.recv(65536)
                        if not ch:
                            break
                        data += ch
                except OSError:
                    pass
            try:
                files = json.loads(data.decode("utf-8"))
                if isinstance(files, list):
                    q.put(("files", [str(f) for f in files]))
            except ValueError:
                pass
    threading.Thread(target=loop, daemon=True).start()
    return srv


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    q = queue.Queue()
    srv = listen_single(q)
    if srv is None:  # 已經開著：把檔案交給它就好
        try:
            with socket.create_connection(("127.0.0.1", SINGLE_PORT), timeout=3) as s:
                s.sendall(json.dumps(args).encode("utf-8"))
            return
        except OSError:
            pass
    cfg = Config()
    root, dnd_ok = None, False
    if TkinterDnD is not None:
        try:
            root, dnd_ok = TkinterDnD.Tk(), True
        except Exception:
            root = None
    if root is None:
        root = tk.Tk()
    App(root, cfg, dnd_ok, q)
    if args:
        q.put(("files", args))
    root.mainloop()


if __name__ == "__main__":
    main()
