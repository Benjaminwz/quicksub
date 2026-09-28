# -*- coding: utf-8 -*-
"""即時字幕：聽電腦正在播放的聲音（或麥克風），AI 邊聽邊寫，字幕顯示在螢幕最上層的字幕列。

背景程式（run_live）：一邊錄音，一邊用 VAD 找「有人在說話」的片段——
  說話中每 0.8 秒重聽一次目前這句（partial，字幕會一直更新）；停頓 0.6 秒就定稿（final）；
  一直沒停頓超過 10 秒，就先把前面的字定稿，免得一句拖太長。
主視窗那邊（LiveSession、Overlay、LivePanel）：開關背景程式、畫字幕列、定稿的句子送去翻譯、最後可以存成 SRT。
"""
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import engine, subs, translate
from .common import DATA_DIR, FROZEN, IS_WINDOWS, T

SR = 16000
PARTIAL_EVERY = 0.8   # 說話中多久更新一次字幕（秒）
END_SILENCE = 0.6     # 停頓多久算一句說完
MAX_UTTER = 10.0      # 一直沒停頓時，最長多久先定稿一次
PROMPTS = {"zh": "以下是繁體中文的句子，包含標點符號。", "yue": "以下是粵語的句子，使用繁體中文和標點符號。",
           "ja": "以下は日本語の文章です。句読点を付けます。"}


# ------------------------------------------------------------------ 背景程式那一邊

def run_live(port):
    engine.setup_dll_path()
    import numpy as np
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    conn = socket.create_connection(("127.0.0.1", port))
    f = conn.makefile("rwb")
    lock = threading.Lock()

    def send(**ev):
        with lock:
            f.write((json.dumps(ev, ensure_ascii=True) + "\n").encode("ascii"))
            f.flush()

    job = json.loads(f.readline().decode("utf-8"))
    stop = threading.Event()
    try:
        send(ev="status", what="load")
        model = engine.load_model(job["model_dir"], job["device"])
    except Exception as e:
        send(ev="error", msg=f"{type(e).__name__}: {e}"[:600], gpu=job["device"] == "cuda")
        return
    q = queue.Queue()

    def capture():
        try:
            if job.get("file"):  # 測試用：把檔案當成即時的聲音，照真實速度送進來
                audio = decode_audio(job["file"], sampling_rate=SR)
                step = SR // 10
                for i in range(0, len(audio), step):
                    if stop.is_set():
                        return
                    q.put(audio[i:i + step])
                    time.sleep(0.1)
                time.sleep(2)
                q.put(None)
                return
            import soundcard as sc
            mic = sc.get_microphone(id=job["source"], include_loopback=True)
            with mic.recorder(samplerate=SR, channels=1, blocksize=SR // 20) as rec:
                while not stop.is_set():
                    q.put(rec.record(numframes=SR // 10)[:, 0].astype(np.float32))
        except Exception as e:
            send(ev="error", msg=T("錄不到聲音：", "Can't record audio: ") + f"{type(e).__name__}: {e}"[:400])
            stop.set()

    def control():  # 主視窗關掉連線或送 stop 就結束
        try:
            for line in f:
                if json.loads(line.decode("utf-8")).get("cmd") == "stop":
                    break
        except (OSError, ValueError):
            pass
        stop.set()

    threading.Thread(target=capture, daemon=True).start()
    threading.Thread(target=control, daemon=True).start()
    lang = job.get("language") or None
    fixed_lang = bool(lang)
    hotwords = job.get("hotwords") or None
    vad = VadOptions(min_silence_duration_ms=300, speech_pad_ms=150)

    def listen(audio, final, words=False):
        """聽一段聲音，回傳 (文字, 語言, 每個字)。"""
        nonlocal lang
        segs, info = model.transcribe(audio, language=lang, beam_size=5 if final else 1, vad_filter=False,
                                      word_timestamps=words, initial_prompt=PROMPTS.get(lang or "") or None,
                                      hotwords=hotwords, condition_on_previous_text=False, without_timestamps=not words)
        segs = list(segs)
        text = "".join(s.text for s in segs).strip()
        if not fixed_lang and final and info.language_probability > 0.7 and len(audio) > SR * 2:
            lang = info.language  # 聽到夠長、夠確定的一句，就固定語言（字幕比較穩定）
        ws = [(w.start, w.end, w.word) for s in segs for w in (s.words or [])] if words else []
        return text, (lang or info.language), ws

    send(ev="status", what="listen")
    buf = np.zeros(0, np.float32)
    offset = 0.0          # buf 第一個取樣點，是開始之後的第幾秒
    last_partial = 0.0
    last_level = 0.0
    while not stop.is_set():
        try:
            chunk = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if chunk is None:
            break
        parts = [chunk]
        while not q.empty():
            c = q.get_nowait()
            if c is None:
                stop.set()
                break
            parts.append(c)
        buf = np.concatenate([buf] + parts)
        now = time.time()
        if now - last_level > 0.2:
            last_level = now
            send(ev="level", rms=float(np.sqrt(np.mean(np.square(buf[-SR // 5:])))) if len(buf) else 0.0)
        try:
            speech = get_speech_timestamps(buf, vad)
            dur = len(buf) / SR
            if not speech:
                if dur > 2.0:  # 沒人說話：只留最後 1 秒（免得切掉下一句的開頭）
                    offset += dur - 1.0
                    buf = buf[-SR:]
                continue
            first = speech[0]["start"] / SR
            if first > 1.0:  # 丟掉前面沒人說話的部分
                cut = int((first - 0.3) * SR)
                buf, offset = buf[cut:], offset + cut / SR
                speech = get_speech_timestamps(buf, vad) or speech
                dur = len(buf) / SR
            end = min(dur, speech[-1]["end"] / SR)
            if dur - end >= END_SILENCE:  # 這句說完了：定稿（連每個字的時間一起送回去，主視窗會照字數重新斷句）
                text, lg, ws = listen(buf[:int(min(dur, end + 0.15) * SR)], final=True, words=True)
                if text and not subs.is_hallucination(text):
                    send(ev="final", text=text, lang=lg, start=offset + speech[0]["start"] / SR, end=offset + end,
                         words=[[offset + a, offset + b, w] for a, b, w in ws])
                cut = int(end * SR)
                buf, offset = buf[cut:], offset + cut / SR
                last_partial = 0.0
            elif dur > MAX_UTTER:  # 一直沒停：前面的先定稿（有句號就切在句號）
                text, lg, ws = listen(buf, final=True, words=True)
                keep = [w for w in ws if w[1] < dur - 2.5] or ws
                ends = [i for i, w in enumerate(keep) if w[2].strip()[-1:] in "。！？.!?"]
                if ends:
                    keep = keep[:ends[-1] + 1]
                if keep:
                    committed = "".join(w[2] for w in keep).strip()
                    if committed and not subs.is_hallucination(committed):
                        send(ev="final", text=committed, lang=lg, start=offset + keep[0][0], end=offset + keep[-1][1],
                             words=[[offset + a, offset + b, w] for a, b, w in keep])
                    cut = int(keep[-1][1] * SR)
                else:
                    cut = int((dur - 2.5) * SR)
                buf, offset = buf[cut:], offset + cut / SR
                last_partial = 0.0
            elif now - last_partial >= PARTIAL_EVERY and dur - first > 0.4:
                last_partial = now
                text, lg, _ = listen(buf, final=False)
                if text and not subs.is_hallucination(text):
                    send(ev="partial", text=text, lang=lg)
        except Exception as e:
            send(ev="error", msg=f"{type(e).__name__}: {e}"[:600], gpu=job["device"] == "cuda")
            return
    send(ev="stopped")


# ------------------------------------------------------------------ 主視窗那一邊

def sources():
    """可以聽的聲音來源：[(id, 顯示名稱)]，第一個是「電腦預設喇叭正在播的聲音」。"""
    out = []
    try:
        import soundcard as sc
        default = sc.default_speaker().name
        mics = sc.all_microphones(include_loopback=True)
        loops = sorted((m for m in mics if m.isloopback), key=lambda m: m.name != default)
        for m in loops:
            out.append((m.id, T("電腦播放的聲音：", "PC audio: ") + m.name))
        for m in mics:
            if not m.isloopback:
                out.append((m.id, T("麥克風：", "Microphone: ") + m.name))
    except Exception:
        pass
    return out


class LiveSession:
    """開一個即時字幕的背景程式，把它回報的事情丟進 events（在主執行緒讀）。"""

    def __init__(self, events):
        self.events = events
        self.proc = None
        self.f = None

    def start(self, job):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(90)
        port = srv.getsockname()[1]
        cmd = [sys.executable, "--live", str(port)] if FROZEN else [sys.executable, os.path.abspath(sys.argv[0]), "--live", str(port)]
        log = open(os.path.join(DATA_DIR, "live.log"), "ab")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     creationflags=0x08000000 if IS_WINDOWS else 0)
        log.close()
        try:
            conn, _ = srv.accept()
        finally:
            srv.close()
        self.f = conn.makefile("rwb")
        self.f.write((json.dumps(job, ensure_ascii=True) + "\n").encode("ascii"))
        self.f.flush()
        proc, f = self.proc, self.f

        def read():
            try:
                for line in f:
                    self.events.put(json.loads(line.decode("ascii")))
            except (OSError, ValueError):
                pass
            self.events.put({"ev": "ended", "code": proc.poll()})
        threading.Thread(target=read, daemon=True).start()

    def stop(self):
        proc, f = self.proc, self.f
        self.proc, self.f = None, None
        if f:
            try:
                f.write(b'{"cmd": "stop"}\n')
                f.flush()
            except OSError:
                pass
        if proc:
            threading.Thread(target=lambda: (time.sleep(1.5), proc.poll() is None and proc.kill()), daemon=True).start()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None


class Overlay:
    """螢幕最上層的字幕列：白字黑邊、沒有底色；拖字可以移動，右鍵有選單。點它不會搶走影片視窗的焦點。"""
    KEY = "#010203"  # 這個顏色會變透明

    def __init__(self, panel):
        self.panel = panel
        cfg = panel.cfg
        root = panel.app.root
        w = self.win = tk.Toplevel(root)
        w.title(T("快字幕 字幕列", "QuickSub captions"))
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=self.KEY)
        if IS_WINDOWS:
            w.attributes("-transparentcolor", self.KEY)
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        self.width = int(sw * 0.8)
        self.height = int(sh * 0.3)
        pos = cfg.get("live_pos")
        x, y = (pos if isinstance(pos, list) and len(pos) == 2 else ((sw - self.width) // 2, int(sh * 0.62)))
        w.geometry(f"{self.width}x{self.height}+{int(x)}+{int(y)}")
        self.canvas = tk.Canvas(w, bg=self.KEY, highlightthickness=0, width=self.width, height=self.height)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.drag_start)
        self.canvas.bind("<B1-Motion>", self.drag_move)
        self.canvas.bind("<ButtonRelease-1>", self.drag_end)
        self.canvas.bind("<Button-3>", self.menu)
        self.canvas.bind("<MouseWheel>", lambda e: self.panel.change_font(2 if e.delta > 0 else -2))
        self.lines = []
        w.update_idletasks()
        self.no_activate()
        self.keep_on_top()

    def no_activate(self):
        """點字幕列時不要讓它變成使用中的視窗（不然全螢幕的影片可能會跳出來），也不要出現在工作列。"""
        if not IS_WINDOWS:
            return
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x00000080
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, u.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        self.hwnd = hwnd

    def keep_on_top(self):
        """影片全螢幕時可能會蓋過來：每 1.5 秒把字幕列再拉到最上面。"""
        if not self.win.winfo_exists():
            return
        if IS_WINDOWS and getattr(self, "hwnd", None):
            import ctypes
            ctypes.windll.user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # TOPMOST, NOSIZE|NOMOVE|NOACTIVATE
        self.win.after(1500, self.keep_on_top)

    def drag_start(self, e):
        self.drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def drag_move(self, e):
        if getattr(self, "drag", None):
            self.win.geometry(f"+{e.x_root - self.drag[0]}+{e.y_root - self.drag[1]}")

    def drag_end(self, _e):
        self.drag = None
        self.panel.cfg.set("live_pos", [self.win.winfo_x(), self.win.winfo_y()])

    def menu(self, e):
        m = tk.Menu(self.win, tearoff=0)
        m.add_command(label=T("字變大", "Bigger text"), command=lambda: self.panel.change_font(4))
        m.add_command(label=T("字變小", "Smaller text"), command=lambda: self.panel.change_font(-4))
        m.add_command(label=T("回到預設位置", "Reset position"), command=self.reset_pos)
        m.add_separator()
        m.add_command(label=T("停止即時字幕", "Stop live subtitles"), command=self.panel.stop)
        m.tk_popup(e.x_root, e.y_root)

    def reset_pos(self):
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"+{(sw - self.width) // 2}+{int(sh * 0.62)}")
        self.panel.cfg.set("live_pos", None)

    def show(self, lines):
        """lines = [(文字, 字的大小, 顏色)]，從上往下排，整塊貼齊字幕列底部。"""
        c = self.canvas
        c.delete("all")
        font_name = subs.ASS_FONT
        items = []
        y = 0
        from .ui import px
        for text, size, color in lines:
            if not text:
                continue
            size = px(size)  # 跟著螢幕縮放放大（175% 的螢幕，字也要 1.75 倍）
            ids = []
            outline = max(2, size // 14)
            for dx in range(-outline, outline + 1, max(1, outline)):
                for dy in range(-outline, outline + 1, max(1, outline)):
                    if dx or dy:
                        ids.append(c.create_text(self.width // 2 + dx, y + dy, text=text, fill="#000000", anchor="n",
                                                 font=(font_name, -size, "bold"), width=self.width - 40, justify="center"))
            ids.append(c.create_text(self.width // 2, y, text=text, fill=color, anchor="n", font=(font_name, -size, "bold"),
                                     width=self.width - 40, justify="center"))
            box = c.bbox(ids[-1])
            items.append(ids)
            y = (box[3] if box else y + size) + size // 5
        shift = self.height - y - 8  # 貼齊底部
        c.move("all", 0, max(0, shift))

    def close(self):
        if self.win.winfo_exists():
            self.win.destroy()


class LivePanel:
    """即時字幕的控制視窗。語言、翻譯、中文轉換、專有名詞用主視窗右邊的設定。"""

    def __init__(self, app):
        from .ui import BG, BORDER, CARD, FONT, MUTED, TEXT, Choice, FlatButton, px
        self.app = app
        self.cfg = app.cfg
        self.events = queue.Queue()
        self.session = LiveSession(self.events)
        self.overlay = None
        self.finals = []          # 定稿的句子：subs.Seg（會慢慢補上翻譯）
        self.partial = ""
        self.trans_q = queue.Queue()
        self.started_at = None
        self.tried_cpu = False
        threading.Thread(target=self.translate_loop, daemon=True).start()

        d = self.win = tk.Toplevel(app.root)
        d.title(T("即時字幕", "Live subtitles"))
        d.configure(bg=CARD)
        d.resizable(False, False)
        d.protocol("WM_DELETE_WINDOW", self.close)
        box = tk.Frame(d, bg=CARD)
        box.pack(padx=px(22), pady=px(16))
        tk.Label(box, text=T("即時字幕", "Live subtitles"), bg=CARD, fg=TEXT, font=(FONT, 14, "bold")).pack(anchor="w")
        tk.Label(box, text=T("聽電腦正在播放的聲音，AI 即時上字幕：YouTube、Netflix、直播、線上課程、視訊會議都能用。\n"
                             "語言、翻譯、中文轉換、專有名詞用主視窗右邊的設定。",
                             "Listens to what your PC is playing and subtitles it live: YouTube, Netflix, streams, classes, meetings.\n"
                             "Language, translation and Chinese options come from the main window's settings."),
                 bg=CARD, fg=MUTED, font=(FONT, 10), justify="left", wraplength=px(470)).pack(anchor="w", pady=(px(4), px(10)))
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x")
        tk.Label(row, text=T("聽哪裡", "Listen to"), bg=CARD, fg=TEXT, font=(FONT, 10)).pack(side="left")
        self.srcs = sources() or [("", T("（找不到聲音裝置）", "(no audio devices found)"))]
        saved = self.cfg.get("live_source")
        self.c_src = Choice(row, self.srcs, saved if any(s[0] == saved for s in self.srcs) else self.srcs[0][0],
                            lambda v: self.cfg.set("live_source", v), width=42)
        self.c_src.box.pack(side="left", fill="x", expand=True, padx=(px(8), 0))
        frow = tk.Frame(box, bg=CARD)
        frow.pack(fill="x", pady=(px(8), 0))
        tk.Label(frow, text=T("字的大小", "Text size"), bg=CARD, fg=TEXT, font=(FONT, 10)).pack(side="left")
        self.font_var = tk.IntVar(value=int(self.cfg.get("live_font") or 44))
        ttk.Scale(frow, from_=24, to=96, variable=self.font_var, orient="horizontal",
                  command=lambda v: self.change_font(0)).pack(side="left", fill="x", expand=True, padx=(px(8), 0))
        self.prev_var = tk.BooleanVar(value=bool(self.cfg.get("live_prev")))
        ttk.Checkbutton(box, text=T("也顯示上一句（比較好跟上）", "Also show the previous line"), variable=self.prev_var,
                        style="Card.TCheckbutton", command=lambda: (self.cfg.set("live_prev", self.prev_var.get()), self.render())).pack(anchor="w", pady=(px(6), 0))
        lv = tk.Frame(box, bg=CARD)
        lv.pack(fill="x", pady=(px(10), 0))
        tk.Label(lv, text=T("音量", "Level"), bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left")
        self.level = ttk.Progressbar(lv, style="Accent.Horizontal.TProgressbar", maximum=100)
        self.level.pack(side="left", fill="x", expand=True, padx=(px(8), 0))
        self.status = tk.Label(box, text=T("按「開始」，字幕會出現在螢幕下方，可以用滑鼠拖到喜歡的位置。",
                                           "Press Start. Subtitles appear near the bottom of the screen; drag them anywhere."),
                               bg=CARD, fg=MUTED, font=(FONT, 10), justify="left", wraplength=px(470))
        self.status.pack(anchor="w", pady=(px(8), 0))
        foot = tk.Frame(box, bg=CARD)
        foot.pack(fill="x", pady=(px(14), 0))
        self.start_btn = FlatButton(foot, T("開始", "Start"), self.toggle)
        self.start_btn.pack(side="left")
        self.save_btn = FlatButton(foot, T("存成字幕檔…", "Save as subtitles…"), self.save, primary=False)
        self.save_btn.pack(side="left", padx=(px(8), 0))
        FlatButton(foot, T("關閉", "Close"), self.close, primary=False).pack(side="right")
        self.pump()

    # ---- 開始／停止
    def toggle(self):
        if self.session.running or self.start_btn.cget("text") == T("停止", "Stop"):
            self.stop()
        else:
            self.start()

    def start(self, force_cpu=False):
        src = self.c_src.get()
        if not src and not os.environ.get("QUICKSUB_LIVE_FILE"):
            messagebox.showinfo(T("即時字幕", "Live subtitles"), T("找不到可以錄音的裝置", "No audio device found"), parent=self.win)
            return
        runner = self.app.runner
        use_gpu = not force_cpu and self.cfg.get("device") == "auto" and engine.has_nvidia() and not runner.gpu_broken
        mid = self.cfg.get("model") if use_gpu else "small"  # 處理器跑大模型會跟不上，改用小的
        self.start_btn.configure(text=T("停止", "Stop"))
        self.set_status(T("◌ 準備中…", "◌ Getting ready…"))

        def prepare():
            try:
                if not engine.model_ready(mid):
                    engine.ensure_model(mid, lambda d, t: self.events.put({"ev": "prep", "text": T(f"下載 AI 模型 {d * 100 // (t or 1)}%（只有第一次）", f"Downloading the AI model {d * 100 // (t or 1)}% (first time only)")}))
                gpu = use_gpu
                if gpu and not engine.cuda_ready():
                    try:
                        engine.ensure_cuda(lambda d, t: self.events.put({"ev": "prep", "text": T(f"下載顯示卡加速元件 {d * 100 // (t or 1)}%（只有第一次）", f"Downloading GPU files {d * 100 // (t or 1)}% (first time only)")}))
                    except Exception:
                        gpu = False
                job = {"model_dir": engine.model_dir(mid), "device": "cuda" if gpu else "cpu", "source": src,
                       "language": None if self.cfg.get("language") == "auto" else self.cfg.get("language"),
                       "hotwords": (self.cfg.get("prompt") or "").strip()}
                if os.environ.get("QUICKSUB_LIVE_FILE"):
                    job["file"] = os.environ["QUICKSUB_LIVE_FILE"]
                self.events.put({"ev": "launch", "job": job})
            except Exception as e:
                self.events.put({"ev": "error", "msg": str(e)})
        threading.Thread(target=prepare, daemon=True).start()

    def stop(self):
        self.session.stop()
        self.start_btn.configure(text=T("開始", "Start"))
        self.level["value"] = 0
        self.partial = ""
        self.set_status(T("已停止。", "Stopped.") + (T(f"這次聽了 {len(self.finals)} 句，可以存成字幕檔。", f" {len(self.finals)} lines this time; you can save them.") if self.finals else ""))
        if self.overlay:
            self.overlay.close()
            self.overlay = None

    def close(self):
        if self.session.running:
            self.stop()
        self.win.destroy()
        self.app.live_panel = None

    # ---- 事件
    def pump(self):
        if not self.win.winfo_exists():
            return
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle(ev)
            except Exception as e:  # 一個事件出錯不要讓整個即時字幕停住
                self.set_status(T("出了點問題：", "Something went wrong: ") + f"{type(e).__name__}: {e}"[:200])
        now = time.time()
        if self.overlay and now - getattr(self, "last_tick", 0) > 1.0:  # 每秒重畫一次：講完的句子放一陣子後會自動消失
            self.last_tick = now
            self.render()
        self.win.after(60, self.pump)

    def handle(self, ev):
        kind = ev.get("ev")
        if kind == "prep":
            self.set_status("◌ " + ev["text"])
        elif kind == "redraw":
            self.render()
        elif kind == "launch":
            if self.start_btn.cget("text") != T("停止", "Stop"):
                return  # 準備的時候就按了停止
            self.job = ev["job"]
            try:
                self.session.start(ev["job"])
            except Exception as e:
                self.set_status(T("✗ 開不起來：", "✗ Couldn't start: ") + str(e))
                self.start_btn.configure(text=T("開始", "Start"))
                return
            self.started_at = self.started_at or time.time()
            if not self.overlay:
                self.overlay = Overlay(self)
                self.render()
        elif kind == "status":
            where = T("（顯示卡）", " (GPU)") if self.job["device"] == "cuda" else T("（處理器，可能會慢一點）", " (CPU, may lag)")
            self.set_status({"load": T("◌ 載入 AI 模型…", "◌ Loading the AI model…"),
                             "listen": T("● 聽寫中", "● Listening")}.get(ev["what"], "…") + where)
        elif kind == "level":
            self.level["value"] = min(100, ev["rms"] * 400)
        elif kind == "partial":
            self.last_lang = ev.get("lang") or getattr(self, "last_lang", None)
            self.partial = self.tidy(ev["text"], ev.get("lang"))
            self.render()
        elif kind == "final":
            lang = self.last_lang = ev.get("lang")
            cjk = subs.is_cjk(lang)
            raw = subs.Seg(ev["start"], ev["end"], ev["text"], words=[tuple(w) for w in ev.get("words") or []])
            lines = subs.resegment([raw], cjk, int(self.cfg.get("max_chars") or (18 if cjk else 42)), float(self.cfg.get("max_dur") or 6))
            group = []
            for s in lines or [raw]:
                s.text, s.words = self.tidy(s.text, lang), []
                if s.text:
                    group.append(s)
            if not group:
                return
            self.finals += group
            self.group = group
            self.final_at = time.time()
            self.partial = ""
            if self.cfg.get("translate") and not self.same_language(lang):
                self.trans_q.put(group)
            self.render()
        elif kind == "error":
            if ev.get("gpu") and not self.tried_cpu:  # 顯示卡出問題：改用處理器（小模型）再試一次
                self.tried_cpu = True
                self.app.runner.gpu_broken = True
                self.session.stop()
                self.set_status(T("顯示卡加速失敗，改用處理器再試…", "GPU failed, retrying on the CPU…"))
                self.start(force_cpu=True)
                return
            self.set_status("✗ " + ev.get("msg", ""))
            self.stop()
            self.set_status("✗ " + ev.get("msg", ""))
        elif kind == "ended":
            if self.start_btn.cget("text") == T("停止", "Stop") and not self.session.running and ev.get("code") not in (0, None):
                self.set_status(T("✗ 即時字幕意外停止，詳細原因在 live.log", "✗ Live subtitles stopped unexpectedly (see live.log)"))
                self.stop()

    def same_language(self, lang):
        target = self.cfg.get("translate") or ""
        return target.split("-")[0] == (lang or "") or (target.startswith("zh") and lang == "yue")

    def tidy(self, text, lang):
        cjk = subs.is_cjk(lang)
        text = subs.clean(text, cjk)
        if lang in ("zh", "yue"):
            text = subs.convert(text, self.cfg.get("convert"))
        return subs.tidy_punct(text, self.cfg.get("punct", "space"), cjk)

    def translate_loop(self):
        """定稿的句子一句一句送去翻譯（在背景，不會讓字幕卡住）。"""
        while True:
            groups = [self.trans_q.get()]
            while not self.trans_q.empty():  # 翻譯落後了：排隊的一次一起翻，才追得上
                groups.append(self.trans_q.get_nowait())
            group = [s for g in groups for s in g]
            try:
                copies = [subs.Seg(s.start, s.end, s.text) for s in group]
                translate.translate(copies, self.cfg.get("translate"), dict(self.cfg.data))
                tcjk = (self.cfg.get("translate") or "").split("-")[0] in subs.CJK_LANGS
                for s, c in zip(group, copies):
                    s.trans = subs.tidy_punct(c.trans or "", self.cfg.get("punct", "space"), tcjk)
                self.tgroup = groups[-1]
                self.trans_at = time.time()
            except Exception as e:
                self.events.put({"ev": "prep", "text": T("翻譯失敗：", "Translation failed: ") + str(e)[:120]})
            self.events.put({"ev": "redraw"})

    # ---- 畫字幕
    def change_font(self, delta):
        size = max(24, min(96, int(self.font_var.get()) + delta))
        self.font_var.set(size)
        self.cfg.data["live_font"] = size
        self.render()

    def render(self):
        if not self.overlay:
            return
        size = int(self.font_var.get())
        small = max(18, int(size * 0.7))
        layout = ("both_tf" if self.cfg.get("trans_first") else "both") if self.cfg.get("bilingual") else "trans"
        # 影片的語言跟要翻成的一樣（例如中文影片翻成中文）就不用翻，照一般字幕顯示
        translating = bool(self.cfg.get("translate")) and not self.same_language(getattr(self, "last_lang", None))
        lines = []

        def entry(seg, dim=False):
            main_c, sub_c = ("#d8dcea", "#b7bdd0") if dim else ("#ffffff", "#e4e8f5")
            s, sm = (small, max(16, int(small * 0.8))) if dim else (size, small)
            if not translating or seg.trans is None:
                return [(seg.text, s, main_c)]
            if layout == "trans":
                return [(seg.trans, s, main_c)]
            pair = [(seg.text, sm, sub_c), (seg.trans, s, main_c)]
            return pair[::-1] if layout == "both_tf" else pair

        group = getattr(self, "group", None) or []  # 最新講完的那段話（可能切成好幾行，最多顯示最後兩行）
        shown = group[-2:]
        before = [s for s in self.finals if s not in shown][-1:] if self.prev_var.get() else []
        if translating:
            # 像同步口譯：大字是最近翻好的那句（原文小字＋翻譯），比聲音晚幾秒；下面淡淡的小字是還沒翻好的、正在講的原文
            tgroup = getattr(self, "tgroup", None) or []
            idx = max((self.finals.index(x) for x in tgroup[-1:] if x in self.finals), default=-1)
            pending = " ".join([x.text for x in self.finals[idx + 1:]] + ([self.partial] if self.partial else []))
            limit = 28 if subs.CJK_CHAR.search(pending) else 64  # 最多一行，太長只留最後面
            if len(pending) > limit:
                pending = "…" + pending[-limit:]
            recent = time.time() - max(getattr(self, "trans_at", 0), getattr(self, "final_at", 0)) < 6
            if tgroup and (pending or recent):
                if self.prev_var.get():
                    prev = self.finals[max(0, self.finals.index(tgroup[0]) - 1)] if tgroup[0] in self.finals and self.finals.index(tgroup[0]) > 0 else None
                    if prev is not None and prev.trans:
                        lines += entry(prev, dim=True)
                for x in tgroup[-2:]:
                    lines += entry(x)
            if pending:
                lines.append((pending, small, "#c9cfe0"))
        elif self.partial:  # 沒翻譯：正在講的字直接當字幕；上一句（有開的話）變淡放上面
            for s in (shown[-1:] if self.prev_var.get() else []):
                lines += entry(s, dim=True)
            lines.append((self.partial, size, "#ffffff"))
        elif shown and time.time() - getattr(self, "final_at", 0) < 6:  # 講完的句子留 6 秒
            for s in before:
                lines += entry(s, dim=True)
            for s in shown:
                lines += entry(s)
        self.overlay.show(lines)

    def set_status(self, text):
        if self.status.winfo_exists():
            self.status.configure(text=text)

    def save(self):
        if not self.finals:
            messagebox.showinfo(T("即時字幕", "Live subtitles"), T("還沒有聽到任何句子", "Nothing has been transcribed yet"), parent=self.win)
            return
        name = time.strftime(T("即時字幕 %Y-%m-%d %H%M", "Live subtitles %Y-%m-%d %H%M"))
        path = filedialog.asksaveasfilename(parent=self.win, defaultextension=".srt", initialfile=name,
                                            filetypes=[("SRT", "*.srt"), ("TXT", "*.txt")])
        if not path:
            return
        segs = [subs.Seg(s.start, s.end, s.text, s.trans) for s in self.finals]
        layout = ("both_tf" if self.cfg.get("trans_first") else "both") if self.cfg.get("bilingual") else "trans"
        layout = layout if any(s.trans for s in segs) else "orig"
        text = subs.to_txt(segs, layout, times=True) if path.lower().endswith(".txt") else subs.to_srt(subs.fix_timing(segs), layout)
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(text)
        self.set_status(T("✓ 存好了：", "✓ Saved: ") + path)
