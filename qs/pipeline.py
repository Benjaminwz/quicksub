# -*- coding: utf-8 -*-
"""一個工作從頭到尾：（網址先下載）→ AI 轉字幕 → 斷句、繁簡轉換 → 翻譯 → 存字幕檔 → 燒進影片／放字幕軌。"""
import itertools
import os
import threading
import time

from . import engine, media, subs, translate
from .common import Cancelled, T, human_size, human_time

_ids = itertools.count(1)

# 依語言給 AI 的開頭提示：讓中文寫成繁體、加標點；日文加標點
PROMPTS = {
    "zh": "以下是繁體中文的句子，包含標點符號。",
    "yue": "以下是粵語的句子，使用繁體中文和標點符號。",
    "ja": "以下は日本語の文章です。句読点を付けます。",
}
CONVERT_TAG = {"s2twp": "zh-TW", "s2hk": "zh-HK", "t2s": "zh-CN"}


class Job:
    def __init__(self, src, is_url=False):
        self.id = next(_ids)
        self.src = src
        self.is_url = is_url
        self.path = None if is_url else src
        self.name = src if is_url else os.path.basename(src)
        self.state = "wait"            # wait / run / done / fail / cancel
        self.status = T("等待中", "Waiting")
        self.frac = None               # 目前這一步的進度 0～1（None = 不知道）
        self.eta = ""
        self.cancel = threading.Event()
        self.segs = None
        self.lang = None
        self.layout = "orig"
        self.files = {}                # 格式 -> 存好的檔案
        self.video = None              # 有畫面的原始影片（編輯器預覽、燒字幕用）
        self.size = (1920, 1080)
        self.note = ""                 # 完成時的附註（例如翻譯失敗但字幕已存好）
        self.redo_video = False        # 編輯器改完字幕後，只重做「燒進影片／字幕軌」
        self.cfg = None                # 開始時的設定（之後改設定不影響正在跑的）


class Runner:
    """在背景一個一個處理排隊的工作。emit(job) 會在狀態改變時被呼叫（從背景執行緒）。"""

    def __init__(self, cfg, emit, log):
        self.cfg = cfg
        self.emit = emit
        self.log = log
        self.jobs = []
        self.worker = engine.Worker()
        self.gpu_broken = False        # 顯示卡跑失敗過，這次開著的期間都改用處理器
        self.wake = threading.Event()
        threading.Thread(target=self.loop, daemon=True).start()

    def add(self, job):
        self.jobs.append(job)
        self.emit(job)
        self.wake.set()

    def loop(self):
        while True:
            job = next((j for j in self.jobs if j.state == "wait"), None)
            if job is None:
                self.wake.wait()
                self.wake.clear()
                continue
            job.cfg = dict(self.cfg.data)
            job.state = "run"
            try:
                self.process(job)
                job.state = "done"
                job.status = T("✓ 完成", "✓ Done") + (f"（{job.note}）" if job.note else "")
            except Cancelled:
                job.state = "cancel"
                job.status = T("已取消", "Cancelled")
            except Exception as e:
                job.state = "fail"
                job.status = T("✗ 失敗：", "✗ Failed: ") + str(e).strip().splitlines()[-1][:200] if str(e).strip() else T("✗ 失敗", "✗ Failed")
                self.log(f"{job.name}: {e}")
            job.frac, job.eta = None, ""
            self.emit(job)

    def set(self, job, status, frac=None, eta=""):
        job.status, job.frac, job.eta = status, frac, eta
        self.emit(job)

    def dl_progress(self, job, what):
        started = time.time()

        def cb(done, total):
            if total:
                speed = done / max(time.time() - started, 0.1)
                self.set(job, f"{what} {done * 100 // total}%（{human_size(done)} / {human_size(total)}）", done / total,
                         T("剩下約 ", "about ") + human_time((total - done) / max(speed, 1)) + T("", " left"))
            else:
                self.set(job, f"{what}（{human_size(done)}）")
        return cb

    # ------------------------------------------------------------------ 主流程
    def process(self, job):
        c = job.cfg
        if job.redo_video:  # 只重做影片那一步（字幕已經改好存好了）
            job.redo_video = False
            self.video_step(job, media.probe(job.video)[2])
            return
        if job.is_url:
            out_dir = c.get("dl_dir") or default_download_dir()
            self.set(job, T("準備下載工具…", "Getting the download tools…"))
            media.ensure_ffmpeg(self.dl_progress(job, T("下載 ffmpeg（只有第一次）", "Downloading ffmpeg (first time only)")), job.cancel)
            media.ensure_ytdlp(self.dl_progress(job, T("下載 yt-dlp（只有第一次）", "Downloading yt-dlp (first time only)")), job.cancel)
            self.set(job, T("下載影片 0%", "Downloading video 0%"), 0)
            job.path = media.fetch_url(job.src, out_dir,
                                       lambda d, t: self.set(job, T(f"下載影片 {d:.0f}%", f"Downloading video {d:.0f}%"), d / t),
                                       job.cancel, lambda _s: self.set(job, T("合併影片和聲音…", "Merging video and audio…")))
            job.name = os.path.basename(job.path)
            job.is_url = False  # 之後「重新做」不用再下載一次
        if not os.path.exists(job.path):
            raise FileNotFoundError(T("找不到這個檔案", "File not found"))
        w, h, duration, has_video = media.probe(job.path)
        job.size = (w, h)
        job.video = job.path if has_video else None

        # AI 模型
        mid = c.get("model")
        if not engine.model_ready(mid):
            engine.ensure_model(mid, self.dl_progress(job, T("下載 AI 模型（只有第一次）", "Downloading the AI model (first time only)")), job.cancel)

        # 顯示卡
        use_gpu = c.get("device") == "auto" and engine.has_nvidia() and not self.gpu_broken
        if use_gpu and not engine.cuda_ready():
            try:
                engine.ensure_cuda(self.dl_progress(job, T("下載顯示卡加速元件（只有第一次）", "Downloading GPU acceleration files (first time only)")), job.cancel)
            except Cancelled:
                raise
            except Exception as e:
                self.log(T(f"顯示卡元件下載失敗，改用處理器：{e}", f"Couldn't download the GPU files, using the CPU: {e}"))
                use_gpu = False

        result = self.transcribe(job, mid, "cuda" if use_gpu else "cpu", duration)
        if result is None and use_gpu:  # 顯示卡跑不起來：改用處理器再來一次
            self.gpu_broken = True
            self.log(T("顯示卡加速失敗，這次改用處理器（比較慢）。詳細原因在 worker.log", "GPU acceleration failed; using the CPU this time (slower). Details in worker.log"))
            result = self.transcribe(job, mid, "cpu", duration)
        if result is None:
            raise RuntimeError(T("AI 轉字幕失敗", "Transcription failed"))
        if result["ev"] == "error":
            raise RuntimeError(result["msg"])

        # 斷句、繁簡轉換、標點
        lang = job.lang = result.get("language") or c.get("language")
        cjk = subs.is_cjk(lang)
        raw = [subs.Seg(s["start"], s["end"], s["text"], words=s["words"]) for s in result["segments"]]
        raw = subs.drop_hallucinations(raw)
        segs = subs.resegment(raw, cjk, int(c.get("max_chars") or (18 if cjk else 42)), float(c.get("max_dur") or 6))
        conv = c.get("convert") if lang in ("zh", "yue") else "none"
        for s in segs:
            s.text = subs.tidy_punct(subs.convert(s.text, conv), c.get("punct", "space"), cjk)
        segs = [s for s in segs if s.text.strip()]
        if not segs:
            raise RuntimeError(T("沒有聽到說話的聲音", "No speech was found"))
        job.segs = segs

        # 翻譯（跟原本語言一樣就不用翻）
        target = c.get("translate")
        same = target and (target.split("-")[0] == (lang or "") or (target.startswith("zh") and lang == "yue"))
        job.layout = "orig"
        if target and not same:
            try:
                self.set(job, T("翻譯中 0%", "Translating 0%"), 0)
                translate.translate(segs, target, c, lambda d, t: self.set(
                    job, T(f"翻譯中 {d * 100 // t}%", f"Translating {d * 100 // t}%"), d / t), job.cancel)
                job.layout = ("both_tf" if c.get("trans_first") else "both") if c.get("bilingual") else "trans"
                tcjk = target.split("-")[0] in subs.CJK_LANGS
                for s in segs:
                    s.trans = subs.tidy_punct(s.trans, c.get("punct", "space"), tcjk)
            except Cancelled:
                raise
            except Exception as e:
                for s in segs:
                    s.trans = None
                job.note = T("翻譯沒有成功，先存了原文字幕：", "Translation failed; saved the original subtitles: ") + str(e)[:120]
                self.log(f"{job.name}: {job.note}")
        tag = target if job.layout != "orig" else CONVERT_TAG.get(conv, lang or "sub")
        self.save_subs(job, tag)
        self.video_step(job, duration)

    def video_dir(self, job):
        """燒好字幕、加了字幕軌的影片放在「字幕影片」子資料夾：旁邊沒有字幕檔，播放器才不會再自動疊一層字幕上去
        （VLC 比對檔名時會忽略中文，「影片.燒字幕.mp4」會被當成「影片.zh-TW.srt」的同名影片）。"""
        d = os.path.join(self.out_dir(job), T("字幕影片", "Subtitled videos"))
        os.makedirs(d, exist_ok=True)
        return d

    def video_step(self, job, duration):
        """燒進影片、放字幕軌。"""
        c = job.cfg
        lang = (c.get("translate") or job.lang) if job.layout != "orig" else job.lang  # 字幕軌標成畫面上主要的語言
        if job.video and (c.get("burn") or c.get("embed")):
            self.set(job, T("準備 ffmpeg…", "Getting ffmpeg…"))
            media.ensure_ffmpeg(self.dl_progress(job, T("下載 ffmpeg（只有第一次）", "Downloading ffmpeg (first time only)")), job.cancel)
        elif not job.video and (c.get("burn") or c.get("embed")):
            job.note = job.note or T("這是聲音檔，沒有畫面可以燒字幕", "This is an audio file, so there's no video to add subtitles to")
        if job.video and c.get("embed"):
            srt = job.files.get("srt") or self.write_temp_srt(job)
            ext = os.path.splitext(job.video)[1].lower()
            dest = media.out_path(job.video, self.video_dir(job), T(".字幕軌", ".softsub"), ext if ext in (".mp4", ".m4v", ".mov", ".mkv") else ".mkv")
            media.embed(job.video, srt, dest, lang, duration, self.ff_progress(job, T("放進字幕軌", "Adding subtitle track"), duration), job.cancel)
            job.files["embed"] = dest
        if job.video and c.get("burn"):
            ass = subs.to_ass(job.segs, job.layout, job.size[0], job.size[1], int(c.get("font_size") or 0))
            dest = media.out_path(job.video, self.video_dir(job), T(".燒字幕", ".hardsub"), ".mp4")
            media.burn(job.video, ass, dest, duration, self.ff_progress(job, T("燒進影片", "Burning into video"), duration), job.cancel)
            job.files["burn"] = dest

    def transcribe(self, job, mid, device, duration):
        """交給背景程式轉字幕。顯示卡出問題（背景程式當掉或回報顯示卡錯誤）回傳 None，讓上面改用處理器。"""
        c = job.cfg
        started = [None]
        names = {"load": T("載入 AI 模型…", "Loading the AI model…"), "read": T("讀取聲音…", "Reading the audio…"),
                 "detect": T("判斷是哪種語言…", "Detecting the language…"), "listen": T("AI 聽寫中…", "Transcribing…")}
        where = T("（顯示卡）", " (GPU)") if device == "cuda" else T("（處理器）", " (CPU)")

        def on_event(ev):
            if ev["ev"] == "status":
                self.set(job, names.get(ev["what"], "…") + where)
            elif ev["ev"] == "info" and ev.get("language"):
                job.lang = ev["language"]
            elif ev["ev"] == "progress" and ev.get("total"):
                if started[0] is None:
                    started[0] = (time.time(), ev["done"])
                done, total = ev["done"], ev["total"]
                t0, d0 = started[0]
                speed = (done - d0) / max(time.time() - t0, 0.1)
                eta = T("剩下約 ", "about ") + human_time((total - done) / speed) + T("", " left") if speed > 0 and done > d0 else ""
                self.set(job, T(f"AI 聽寫中 {min(99, int(done * 100 / total))}%", f"Transcribing {min(99, int(done * 100 / total))}%") + where,
                         min(1.0, done / total), eta)

        req = {"path": job.path, "model_dir": engine.model_dir(mid), "device": device,
               "language": None if c.get("language") == "auto" else c.get("language"),
               "prompts": PROMPTS, "hotwords": (c.get("prompt") or "").strip(), "vad": c.get("vad", True), "beam": c.get("beam", 5)}
        try:
            ev = self.worker.transcribe(req, on_event, job.cancel)
        except engine.WorkerCrashed as e:
            self.log(f"{job.name}: {e}")
            return None if device == "cuda" else {"ev": "error", "msg": str(e)}
        if ev["ev"] == "error" and device == "cuda" and any(k in ev["msg"].lower() for k in ("cuda", "cudnn", "cublas", "gpu", "dll")):
            self.log(f"{job.name}: {ev['msg']}")
            self.worker.stop()
            return None
        return ev

    # ------------------------------------------------------------------ 存檔
    def out_dir(self, job):
        c = job.cfg or self.cfg.data
        d = c.get("out_dir") if c.get("out_mode") == "folder" and c.get("out_dir") else os.path.dirname(job.path)
        os.makedirs(d, exist_ok=True)
        return d

    def save_subs(self, job, tag=None):
        """把字幕存成選的格式：影片名稱.語言.srt（播放器大多會自動載入）。編輯器存檔也用這個。"""
        c = job.cfg or self.cfg.data
        if tag is None:
            tag = getattr(job, "tag", "sub")
        job.tag = tag
        fmts = [f for f in (c.get("formats") or ["srt"]) if f in subs.FORMATS] or ["srt"]
        base = os.path.splitext(os.path.basename(job.path))[0]
        for fmt in fmts:
            if fmt == "ass":
                text = subs.to_ass(job.segs, job.layout, job.size[0], job.size[1], int(c.get("font_size") or 0))
            else:
                text = subs.FORMATS[fmt](job.segs, job.layout)
            dest = os.path.join(self.out_dir(job), f"{base}.{tag}.{fmt}")
            with open(dest, "w", encoding="utf-8-sig" if fmt in ("srt", "ass", "txt") else "utf-8") as f:
                f.write(text)
            job.files[fmt] = dest
        return job.files

    def write_temp_srt(self, job):
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".srt", prefix="quicksub-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(subs.to_srt(job.segs, job.layout))
        return p

    def ff_progress(self, job, what, duration):
        started = time.time()

        def cb(done, total):
            speed = done / max(time.time() - started, 0.1)
            eta = T("剩下約 ", "about ") + human_time((total - done) / speed) + T("", " left") if speed > 0 else ""
            self.set(job, f"{what} {int(done * 100 / total)}%", done / total, eta)
        return cb


def default_download_dir():
    base = os.path.join(os.path.expanduser("~"), "Videos")
    return os.path.join(base if os.path.isdir(base) else os.path.expanduser("~"), T("快字幕", "QuickSub"))
