# -*- coding: utf-8 -*-
"""指令模式：給其他程式、AI 工作流呼叫。不開視窗；進度寫到 stderr，結果是一個 JSON 物件寫到 stdout。

  QuickSub --cli transcribe 影片或網址… [選項]   AI 轉字幕（可以翻譯、燒進影片、放字幕軌）
  QuickSub --cli burn 影片 字幕檔                 把現成的字幕（srt/vtt/ass）燒進影片
  QuickSub --cli embed 影片 字幕檔                把現成的字幕放進影片當字幕軌
  QuickSub --cli info                             版本、顯示卡、模型、翻譯服務的狀態

安裝版請用旁邊的 quicksub-cli.exe（QuickSub.exe 是視窗程式，別的程式讀不到它的輸出）。
AI 模型、顯示卡、翻譯服務沿用視窗版存好的設定；其他選項每次都從預設值開始，也不會改到視窗版的設定。
結束代碼：0 全部成功、1 有失敗、2 參數錯誤。
"""
import argparse
import json
import os
import re
import sys
import tempfile
import threading
from types import SimpleNamespace

from . import engine, media, pipeline, subs, translate
from .common import APP_VERSION, DATA_DIR, DEFAULTS, IS_WINDOWS, Cancelled, Config, T

# 沿用視窗版的這些設定（跟電腦環境有關的）；其他每次從預設值開始
SHARED = ("model", "device", "engine", "ollama_model", "api_preset", "api_base", "api_key", "api_model", "dl_dir")


def _fix_stdio():
    """視窗版 exe 的 sys.stdout 可能是 None：改用繼承來的管線（quicksub-cli.exe 會接上）。一律用 UTF-8。"""
    for name, num in (("stdout", -11), ("stderr", -12)):
        stream = getattr(sys, name)
        if stream is None and IS_WINDOWS:
            try:
                import ctypes
                import msvcrt
                k32 = ctypes.windll.kernel32
                k32.GetStdHandle.restype = ctypes.c_void_p
                h = k32.GetStdHandle(num)
                if h and h != ctypes.c_void_p(-1).value:
                    stream = open(msvcrt.open_osfhandle(h, os.O_WRONLY), "w", encoding="utf-8", errors="replace",
                                  buffering=1)
            except OSError:
                stream = None
        if stream is None:
            stream = open(os.devnull, "w")
        else:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        setattr(sys, name, stream)


class Progress:
    """把進度印到 stderr：換步驟、或每多 10% 才印一行，不要洗版。"""

    def __init__(self, quiet):
        self.quiet = quiet
        self.label = ""
        self.last = None

    def __call__(self, job):
        if self.quiet:
            return
        step = re.sub(r"\s*\d+%.*$", "", job.status)
        bucket = int(job.frac * 10) if job.frac is not None else -1
        if (step, bucket) == self.last:
            return
        self.last = (step, bucket)
        eta = f"（{job.eta}）" if job.eta else ""
        print(f"{self.label}{job.status}{eta}", file=sys.stderr, flush=True)

    def log(self, msg):
        if not self.quiet:
            print(msg, file=sys.stderr, flush=True)

    def ff(self, what):
        """ffmpeg／下載進度的 callback(已完成, 總共)。"""
        def cb(done, total):
            self(SimpleNamespace(status=f"{what} {int(done * 100 / total)}%" if total else what,
                                 frac=done / total if total else None, eta=""))
        return cb


def _seg_json(s):
    d = {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text}
    if s.trans:
        d["translation"] = s.trans
    return d


def _emit(result, json_path):
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))  # 單行：給程式／AI 讀比較省
    if json_path:
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(text)
    print(text, flush=True)


# ------------------------------------------------------------------ transcribe
def cmd_transcribe(a):
    saved = Config()
    data = dict(DEFAULTS)
    data.update({k: saved.data[k] for k in SHARED if k in saved.data})
    formats = [f.strip().lower() for f in a.formats.split(",") if f.strip()]
    bad = [f for f in formats if f not in subs.FORMATS]
    if bad:
        raise ValueError(f"--formats: {', '.join(bad)}? ({', '.join(subs.FORMATS)})")
    data.update({
        "language": a.language, "convert": a.convert, "formats": formats or ["srt"], "punct": a.punct,
        "max_chars": a.max_chars, "max_dur": a.max_dur, "prompt": a.hotwords or "", "vad": not a.no_vad,
        "translate": a.translate or "", "bilingual": not a.only_translation, "trans_first": a.translation_first,
        "burn": a.burn, "embed": a.embed, "font_size": a.font_size,
        "out_mode": "folder" if a.out_dir else "same", "out_dir": os.path.abspath(a.out_dir) if a.out_dir else "",
    })
    if a.model:
        data["model"] = a.model
    if a.cpu:
        data["device"] = "cpu"
    api_flags = {"api_preset": a.api_preset, "api_base": a.api_base, "api_model": a.api_model, "api_key": a.api_key}
    data.update({k: v for k, v in api_flags.items() if v})
    if a.engine:
        data["engine"] = a.engine
    elif a.api_preset or a.api_base or a.api_model:  # 金鑰可能來自環境變數，不算「指定要用線上 AI」
        data["engine"] = "api"
    if a.ollama_model:
        data["ollama_model"] = a.ollama_model
    saved.data = data  # 只給這次用：不呼叫 set()／save()，視窗版的設定不會被改到

    prog = Progress(a.quiet)
    runner = pipeline.Runner(saved, prog, prog.log)
    results, total = [], len(a.inputs)
    try:
        for i, src in enumerate(a.inputs, 1):
            is_url = bool(re.match(r"https?://", src, re.I))
            job = pipeline.Job(src if is_url else os.path.abspath(src), is_url)
            job.cfg = dict(data)
            prog.label = f"[{i}/{total}] {job.name}: " if total > 1 else ""
            r = {"input": src}
            try:
                runner.process(job)
                r["ok"] = True
            except KeyboardInterrupt:
                job.cancel.set()
                r.update(ok=False, error="cancelled")
                results.append(r)
                break
            except Cancelled:
                r.update(ok=False, error="cancelled")
            except Exception as e:  # noqa: BLE001 — 一個失敗不影響下一個
                r.update(ok=False, error=str(e).strip() or type(e).__name__)
            r.update(path=job.path, language=job.lang, layout=job.layout, files=job.files)
            if job.note:
                r["note"] = job.note
            if a.segments and job.segs:
                r["segments"] = [_seg_json(s) for s in job.segs]
            results.append(r)
    finally:
        runner.worker.stop()
    ok = bool(results) and all(r["ok"] for r in results)
    _emit({"ok": ok, "version": APP_VERSION, "results": results}, a.json)
    return 0 if ok else 1


# ------------------------------------------------------------------ burn / embed
def cmd_video(a, embed):
    video, sub = os.path.abspath(a.video), os.path.abspath(a.subtitles)
    for p in (video, sub):
        if not os.path.isfile(p):
            raise FileNotFoundError(T("找不到檔案：", "File not found: ") + p)
    prog = Progress(a.quiet)
    cancel = threading.Event()
    media.ensure_ffmpeg(prog.ff(T("下載 ffmpeg（只有第一次）", "Downloading ffmpeg (first time only)")), cancel)
    w, h, duration, has_video = media.probe(video)
    if not has_video:
        raise ValueError(T("這個檔案沒有畫面", "This file has no video"))
    out_dir = os.path.join(os.path.dirname(video), T("字幕影片", "Subtitled videos"))
    ext = os.path.splitext(sub)[1].lower()
    with open(sub, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    tmp = None
    try:
        if embed:
            vext = os.path.splitext(video)[1].lower()
            dest = a.output or media.out_path(video, out_dir, T(".字幕軌", ".softsub"),
                                              vext if vext in (".mp4", ".m4v", ".mov", ".mkv") else ".mkv")
            srt = sub
            if ext != ".srt":  # vtt／ass 先轉成 srt
                segs = subs.parse_srt(text) if ext == ".vtt" else _parse_ass(text)
                fd, tmp = tempfile.mkstemp(suffix=".srt", prefix="quicksub-")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(subs.to_srt(segs, "both"))
                srt = tmp
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            media.embed(video, srt, dest, a.lang or "", duration, prog.ff(T("放進字幕軌", "Adding subtitle track")), cancel)
        else:
            dest = a.output or media.out_path(video, out_dir, T(".燒字幕", ".hardsub"), ".mp4")
            ass = text if ext == ".ass" else subs.to_ass(subs.parse_srt(text), "both", w, h, a.font_size)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            media.burn(video, ass, dest, duration, prog.ff(T("燒進影片", "Burning into video")), cancel)
    except KeyboardInterrupt:
        cancel.set()
        raise
    finally:
        if tmp:
            os.remove(tmp)
    _emit({"ok": True, "version": APP_VERSION, "file": os.path.abspath(dest)}, a.json)
    return 0


def _parse_ass(text):
    """很簡單地讀 ASS 的 Dialogue（給 embed 轉 srt 用）。"""
    out = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue

        def t(x):
            hh, mm, ss = x.strip().split(":")
            return int(hh) * 3600 + int(mm) * 60 + float(ss)
        body = re.sub(r"\{[^}]*\}", "", parts[9]).replace("\\N", "\n").replace("\\n", "\n").split("\n")
        body = [b.strip() for b in body if b.strip()]
        if body:
            out.append(subs.Seg(t(parts[1]), t(parts[2]), body[0], "\n".join(body[1:]) or None))
    return out


# ------------------------------------------------------------------ info
def cmd_info(a):
    cfg = Config()
    ollama = translate.ollama_models()
    _emit({
        "ok": True, "version": APP_VERSION, "data_dir": DATA_DIR,
        "gpu": engine.has_nvidia(), "gpu_files_ready": engine.cuda_ready(),
        "model": cfg.get("model"),
        "models": [{"id": m[0], "size_mb": m[2], "downloaded": engine.model_ready(m[0])} for m in engine.MODELS],
        "ffmpeg": bool(media.find_tool("ffmpeg") and media.find_tool("ffprobe")),
        "translation": {
            "engine": cfg.get("engine"), "api_preset": cfg.get("api_preset"), "api_model": cfg.get("api_model"),
            "api_key_set": bool(cfg.get("api_key")), "ollama_running": bool(ollama), "ollama_models": ollama,
            "ollama_model": cfg.get("ollama_model"), "api_presets": list(translate.PRESETS),
        },
        "translate_targets": [t[0] for t in translate.TARGETS],
        "formats": list(subs.FORMATS),
    }, a.json)
    return 0


# ------------------------------------------------------------------ 參數
def build_parser():
    p = argparse.ArgumentParser(prog="quicksub-cli", description="QuickSub command-line mode (JSON on stdout, progress on stderr).")
    p.add_argument("--version", action="version", version=APP_VERSION)
    sp = p.add_subparsers(dest="cmd", required=True)

    def common(q):
        q.add_argument("--json", metavar="PATH", help="also write the result JSON to this file")
        q.add_argument("-q", "--quiet", action="store_true", help="no progress on stderr")

    t = sp.add_parser("transcribe", help="speech → subtitles (optionally translate / burn / embed)")
    t.add_argument("inputs", nargs="+", metavar="FILE_OR_URL")
    t.add_argument("-l", "--language", default="auto", help="spoken language code (auto, zh, en, ja, ko, yue, …)")
    t.add_argument("-m", "--model", choices=[m[0] for m in engine.MODELS], help="speech model (default: the app's setting)")
    t.add_argument("--cpu", action="store_true", help="don't use the NVIDIA GPU")
    t.add_argument("-f", "--formats", default="srt", help="comma list: srt,vtt,ass,txt (default srt)")
    t.add_argument("-o", "--out-dir", help="where to save (default: next to the video)")
    t.add_argument("--convert", default="s2twp", choices=["s2twp", "s2hk", "t2s", "none"],
                   help="Chinese script: s2twp Traditional (Taiwan, default) / s2hk / t2s Simplified / none")
    t.add_argument("--hotwords", help="names and terms to help recognition")
    t.add_argument("--max-chars", type=int, default=0, help="max characters per line (0 = auto)")
    t.add_argument("--max-dur", type=float, default=6.0, help="max seconds per subtitle")
    t.add_argument("--punct", default="space", choices=["space", "trim", "keep"], help="Chinese punctuation style")
    t.add_argument("--no-vad", action="store_true", help="don't skip non-speech parts")
    t.add_argument("-t", "--translate", metavar="LANG", help="translate to: " + ", ".join(x[0] for x in translate.TARGETS))
    t.add_argument("--only-translation", action="store_true", help="output only the translation (default: bilingual)")
    t.add_argument("--translation-first", action="store_true", help="bilingual with the translation on top")
    t.add_argument("--engine", choices=["ollama", "api"], help="translation engine (default: the app's setting)")
    t.add_argument("--ollama-model")
    t.add_argument("--api-preset", choices=list(translate.PRESETS))
    t.add_argument("--api-base", help="OpenAI-compatible base URL (…/v1)")
    t.add_argument("--api-model")
    t.add_argument("--api-key", default=os.environ.get("QUICKSUB_API_KEY"), help="or set QUICKSUB_API_KEY")
    t.add_argument("--burn", action="store_true", help="also burn the subtitles into a copy of the video")
    t.add_argument("--embed", action="store_true", help="also add them as a subtitle track (no re-encode)")
    t.add_argument("--font-size", type=int, default=0, help="burn font size in px (0 = auto)")
    t.add_argument("--segments", action="store_true", help="include every subtitle line in the JSON")
    common(t)

    for name, hlp in (("burn", "burn an existing subtitle file into a copy of the video"),
                      ("embed", "add an existing subtitle file as a track (no re-encode)")):
        v = sp.add_parser(name, help=hlp)
        v.add_argument("video")
        v.add_argument("subtitles", help=".srt / .vtt / .ass (2-line SRT cues = original + translation)")
        v.add_argument("-o", "--output", help="output file (default: 「Subtitled videos」 folder next to the video)")
        if name == "burn":
            v.add_argument("--font-size", type=int, default=0)
        else:
            v.add_argument("--lang", help="language of the track, e.g. zh-TW, en, ja")
        common(v)

    i = sp.add_parser("info", help="version, GPU, models and translation settings")
    common(i)
    return p


def run_cli(argv):
    _fix_stdio()
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "transcribe":
            return cmd_transcribe(a)
        if a.cmd in ("burn", "embed"):
            return cmd_video(a, a.cmd == "embed")
        return cmd_info(a)
    except KeyboardInterrupt:
        _emit({"ok": False, "version": APP_VERSION, "error": "cancelled"}, getattr(a, "json", None))
        return 1
    except Exception as e:  # noqa: BLE001
        _emit({"ok": False, "version": APP_VERSION, "error": str(e).strip() or type(e).__name__}, getattr(a, "json", None))
        return 1
