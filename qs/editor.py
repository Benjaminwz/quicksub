# -*- coding: utf-8 -*-
"""字幕編輯器：逐句改文字和時間、插入／刪除／合併／拆開、整體時間平移、搜尋取代、用播放器預覽。"""
import os
import re
import tempfile
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from . import media, subs
from .common import T
from .ui import (ACCENT, BG, BORDER, CARD, FIELD, FONT, MUTED, OK_COLOR, TEXT, FlatButton, px)


def fmt_time(t):
    t = max(0.0, t)
    h, rem = divmod(int(round(t * 1000)), 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def parse_time(text):
    """接受 01:02:03.456、2:03.5、123.4 這幾種寫法。看不懂回傳 None。"""
    text = text.strip().replace(",", ".")
    m = re.fullmatch(r"(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)", text)
    if not m:
        return None
    parts = [p for p in m.groups() if p is not None]
    vals = [float(p) for p in parts]
    sec = 0.0
    for v in vals:
        sec = sec * 60 + v
    return sec


class Editor:
    def __init__(self, app, job, standalone_file=None):
        """job：快字幕做好的工作；standalone_file：直接打開的字幕檔（存檔時寫回同一個檔案）。"""
        self.app = app
        self.job = job
        self.file = standalone_file
        self.segs = job.segs
        self.dirty = False
        self.sel = None
        self.has_trans = any(s.trans for s in self.segs) or job.layout != "orig"
        self.cjk = subs.is_cjk(job.lang) or any(subs.CJK_CHAR.search(s.text) for s in self.segs[:20])

        win = self.win = tk.Toplevel(app.root)
        win.title(T("編輯字幕 — ", "Edit subtitles — ") + job.name)
        win.configure(bg=BG)
        win.geometry("%dx%d" % px(980, 680))
        win.minsize(*px(720, 480))
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Control-s>", lambda e: self.save())

        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=px(14), pady=(px(12), px(6)))
        FlatButton(bar, T("存檔", "Save"), self.save, small=True).pack(side="left")
        FlatButton(bar, T("用播放器預覽", "Preview in player"), self.preview, primary=False, small=True).pack(side="left", padx=(px(6), 0))
        if job.video and not self.file:
            FlatButton(bar, T("用新字幕重做影片", "Redo video with new subtitles"), self.redo_video, primary=False,
                       small=True).pack(side="left", padx=(px(6), 0))
        tk.Frame(bar, bg=BG, width=px(16)).pack(side="left")
        for text, cmd in ((T("插入一句", "Insert"), self.insert), (T("刪除", "Delete"), self.delete),
                          (T("跟下一句合併", "Merge with next"), self.merge), (T("拆成兩句", "Split"), self.split),
                          (T("時間平移", "Shift times"), self.shift), (T("搜尋取代", "Find & replace"), self.replace)):
            FlatButton(bar, text, cmd, primary=False, small=True).pack(side="left", padx=(0, px(6)))

        body = tk.Frame(win, bg=BORDER)
        body.pack(fill="both", expand=True, padx=px(14))
        cols = ("n", "start", "end", "text") + (("trans",) if self.has_trans else ())
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="browse")
        heads = {"n": "#", "start": T("開始", "Start"), "end": T("結束", "End"), "text": T("字幕", "Text"), "trans": T("翻譯", "Translation")}
        widths = {"n": 50, "start": 110, "end": 110, "text": 380, "trans": 320}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=px(widths[c]), stretch=c in ("text", "trans"), anchor="w" if c in ("text", "trans") else "center")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True, padx=1, pady=1)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select())

        edit = tk.Frame(win, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        edit.pack(fill="x", padx=px(14), pady=px(10))
        row = tk.Frame(edit, bg=CARD)
        row.pack(fill="x", padx=px(12), pady=(px(10), 0))
        tk.Label(row, text=T("開始", "Start"), bg=CARD, fg=MUTED, font=(FONT, 10)).pack(side="left")
        self.e_start = tk.Entry(row, width=13, font=(FONT, 10), relief="flat", bg=FIELD, highlightthickness=1, highlightbackground=BORDER)
        self.e_start.pack(side="left", padx=(px(6), px(14)), ipady=px(3))
        tk.Label(row, text=T("結束", "End"), bg=CARD, fg=MUTED, font=(FONT, 10)).pack(side="left")
        self.e_end = tk.Entry(row, width=13, font=(FONT, 10), relief="flat", bg=FIELD, highlightthickness=1, highlightbackground=BORDER)
        self.e_end.pack(side="left", padx=(px(6), px(14)), ipady=px(3))
        self.status = tk.Label(row, text="", bg=CARD, fg=MUTED, font=(FONT, 9))
        self.status.pack(side="right")
        tk.Label(edit, text=T("字幕", "Text"), bg=CARD, fg=MUTED, font=(FONT, 10)).pack(anchor="w", padx=px(12), pady=(px(8), 0))
        self.t_text = tk.Text(edit, height=2, font=(FONT, 12), relief="flat", bg=FIELD, wrap="word", undo=True,
                              highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        self.t_text.pack(fill="x", padx=px(12), pady=(px(2), px(4) if self.has_trans else px(12)))
        self.t_trans = None
        if self.has_trans:
            tk.Label(edit, text=T("翻譯", "Translation"), bg=CARD, fg=MUTED, font=(FONT, 10)).pack(anchor="w", padx=px(12))
            self.t_trans = tk.Text(edit, height=2, font=(FONT, 12), relief="flat", bg=FIELD, wrap="word", undo=True,
                                   highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
            self.t_trans.pack(fill="x", padx=px(12), pady=(px(2), px(12)))
        for w in (self.e_start, self.e_end, self.t_text) + ((self.t_trans,) if self.t_trans else ()):
            w.bind("<KeyRelease>", lambda e: self.apply())
            w.bind("<FocusOut>", lambda e: self.apply())
        self.refresh()
        if self.segs:
            self.select(0)

    # ---- 顯示
    def values(self, i):
        s = self.segs[i]
        v = (i + 1, fmt_time(s.start), fmt_time(s.end), s.text.replace("\n", " "))
        return v + ((s.trans or "").replace("\n", " "),) if self.has_trans else v

    def refresh(self, keep=None):
        self.tree.delete(*self.tree.get_children())
        for i in range(len(self.segs)):
            self.tree.insert("", "end", iid=str(i), values=self.values(i))
        self.update_status()
        if keep is not None and self.segs:
            self.select(min(keep, len(self.segs) - 1))

    def update_status(self):
        self.status.configure(text=T(f"共 {len(self.segs)} 句", f"{len(self.segs)} lines") + (T("　・未存檔", "  · unsaved") if self.dirty else ""),
                              fg=ACCENT if self.dirty else MUTED)

    def select(self, i):
        self.tree.selection_set(str(i))
        self.tree.see(str(i))

    def on_select(self):
        sel = self.tree.selection()
        if not sel:
            return
        self.sel = int(sel[0])
        s = self.segs[self.sel]
        self._loading = True
        for e, v in ((self.e_start, fmt_time(s.start)), (self.e_end, fmt_time(s.end))):
            e.delete(0, "end")
            e.insert(0, v)
        self.t_text.delete("1.0", "end")
        self.t_text.insert("1.0", s.text)
        if self.t_trans:
            self.t_trans.delete("1.0", "end")
            self.t_trans.insert("1.0", s.trans or "")
        self._loading = False

    def apply(self):
        """下面的欄位一改，就寫回那一句。"""
        if self.sel is None or getattr(self, "_loading", False) or self.sel >= len(self.segs):
            return
        s = self.segs[self.sel]
        before = (s.start, s.end, s.text, s.trans)
        st, en = parse_time(self.e_start.get()), parse_time(self.e_end.get())
        if st is not None:
            s.start = st
        if en is not None:
            s.end = max(en, s.start + 0.05)
        s.text = self.t_text.get("1.0", "end-1c").strip()
        if self.t_trans:
            s.trans = self.t_trans.get("1.0", "end-1c").strip() or None
        if (s.start, s.end, s.text, s.trans) != before:
            self.dirty = True
            self.tree.item(str(self.sel), values=self.values(self.sel))
            self.update_status()

    # ---- 編輯
    def need_sel(self):
        if self.sel is None or self.sel >= len(self.segs):
            messagebox.showinfo(APP_TITLE(), T("先在上面點一句字幕", "Click a line first"), parent=self.win)
            return False
        return True

    def changed(self, keep):
        self.dirty = True
        self.refresh(keep)

    def insert(self):
        i = self.sel if self.sel is not None else len(self.segs) - 1
        after = self.segs[i].end if self.segs else 0.0
        nxt = self.segs[i + 1].start if i + 1 < len(self.segs) else after + 2.0
        seg = subs.Seg(after, max(after + 0.5, min(after + 2.0, nxt)), T("新的一句", "New line"), "" if self.has_trans else None)
        self.segs.insert(i + 1, seg)
        self.changed(i + 1)
        self.t_text.focus_set()
        self.t_text.tag_add("sel", "1.0", "end-1c")

    def delete(self):
        if self.need_sel():
            del self.segs[self.sel]
            self.changed(self.sel)

    def merge(self):
        if not self.need_sel() or self.sel + 1 >= len(self.segs):
            return
        a, b = self.segs[self.sel], self.segs[self.sel + 1]
        joiner = " "
        a.end, a.text = b.end, (a.text + joiner + b.text).strip()
        if a.trans is not None or b.trans is not None:
            a.trans = ((a.trans or "") + joiner + (b.trans or "")).strip()
        del self.segs[self.sel + 1]
        self.changed(self.sel)

    def split(self):
        """從游標的位置拆開（沒有游標就從中間拆），時間照字數比例分。"""
        if not self.need_sel():
            return
        s = self.segs[self.sel]
        text = s.text
        try:
            cut = len(self.t_text.get("1.0", "insert"))
        except tk.TclError:
            cut = 0
        if not 0 < cut < len(text):
            spaces = [m.start() for m in re.finditer(r"\s", text)]
            cut = min(spaces, key=lambda p: abs(p - len(text) / 2)) if spaces else len(text) // 2
        if not 0 < cut < len(text):
            return
        mid = s.start + (s.end - s.start) * cut / len(text)
        new = subs.Seg(mid, s.end, text[cut:].strip(), None)
        s.end, s.text = mid, text[:cut].strip()
        if s.trans:
            t = s.trans
            tcut = round(len(t) * cut / len(text))
            s.trans, new.trans = t[:tcut].strip(), t[tcut:].strip()
        self.segs.insert(self.sel + 1, new)
        self.changed(self.sel)

    def shift(self):
        sec = simpledialog.askfloat(T("時間平移", "Shift times"),
                                    T("全部字幕要往後移幾秒？（往前移打負數，例如 -1.5）", "Shift all lines by how many seconds? (negative = earlier, e.g. -1.5)"),
                                    parent=self.win)
        if not sec:
            return
        for s in self.segs:
            s.start, s.end = max(0.0, s.start + sec), max(0.05, s.end + sec)
        self.changed(self.sel or 0)

    def replace(self):
        d = tk.Toplevel(self.win)
        d.title(T("搜尋取代", "Find & replace"))
        d.configure(bg=CARD)
        d.transient(self.win)
        d.resizable(False, False)
        box = tk.Frame(d, bg=CARD)
        box.pack(padx=px(18), pady=px(14))
        entries = []
        for label in (T("尋找", "Find"), T("取代成", "Replace with")):
            tk.Label(box, text=label, bg=CARD, fg=TEXT, font=(FONT, 10)).pack(anchor="w")
            e = tk.Entry(box, width=34, font=(FONT, 11), relief="flat", bg=FIELD, highlightthickness=1, highlightbackground=BORDER)
            e.pack(fill="x", pady=(px(2), px(8)), ipady=px(3))
            entries.append(e)
        result = tk.Label(box, text="", bg=CARD, fg=OK_COLOR, font=(FONT, 10))
        result.pack(anchor="w")

        def run():
            find, rep = entries[0].get(), entries[1].get()
            if not find:
                return
            n = 0
            for s in self.segs:
                n += s.text.count(find) + (s.trans or "").count(find)
                s.text = s.text.replace(find, rep)
                if s.trans:
                    s.trans = s.trans.replace(find, rep)
            if n:
                self.changed(self.sel or 0)
            result.configure(text=T(f"取代了 {n} 處", f"Replaced {n}"))
        FlatButton(box, T("全部取代", "Replace all"), run, small=True).pack(anchor="e", pady=(px(6), 0))
        entries[0].focus_set()

    # ---- 存檔、預覽
    def save(self):
        self.apply()
        self.segs.sort(key=lambda s: s.start)
        try:
            if self.file:
                ext = os.path.splitext(self.file)[1].lower().lstrip(".")
                writer = subs.FORMATS.get(ext, subs.to_srt)
                text = writer(self.segs, self.job.layout) if ext != "ass" else subs.to_ass(self.segs, self.job.layout, *self.job.size)
                with open(self.file, "w", encoding="utf-8-sig" if ext in ("srt", "ass", "txt") else "utf-8") as f:
                    f.write(text)
            else:
                self.app.runner.save_subs(self.job)
        except OSError as e:
            messagebox.showerror(APP_TITLE(), T("存檔失敗：", "Couldn't save: ") + str(e), parent=self.win)
            return False
        self.dirty = False
        self.refresh(self.sel)
        self.status.configure(text=T("✓ 已存檔", "✓ Saved"), fg=OK_COLOR)
        return True

    def preview(self):
        self.apply()
        fd, tmp = tempfile.mkstemp(suffix=".srt", prefix="quicksub-preview-")
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(subs.to_srt(sorted(self.segs, key=lambda s: s.start), self.job.layout))
        if not media.find_tool("vlc") and not os.path.exists(r"C:\Program Files\VideoLAN\VLC\vlc.exe") and self.dirty:
            # 沒有 VLC：系統播放器只會自動載入影片旁邊的字幕，先存檔
            self.save()
        try:
            media.open_player(self.job.video, tmp)
        except OSError as e:
            messagebox.showerror(APP_TITLE(), str(e), parent=self.win)

    def redo_video(self):
        if self.dirty and not self.save():
            return
        self.app.redo_video(self.job)
        self.status.configure(text=T("已排進清單，主視窗可以看進度", "Queued; see progress in the main window"), fg=OK_COLOR)

    def close(self):
        if self.dirty:
            ans = messagebox.askyesnocancel(APP_TITLE(), T("字幕改過還沒存，要存檔嗎？", "Save your changes?"), parent=self.win)
            if ans is None:
                return
            if ans and not self.save():
                return
        self.win.destroy()


def APP_TITLE():
    return T("快字幕", "QuickSub")
