# -*- coding: utf-8 -*-
"""AI 轉字幕（faster-whisper）。

轉字幕在另一個背景程式（worker）裡跑：主視窗不會卡住；顯示卡元件出問題讓它當掉時，主視窗還在，
會自動改用處理器再跑一次；按取消直接結束它，馬上就停。兩邊用本機的 socket 一行一個 JSON 溝通。
AI 模型和顯示卡元件（cuBLAS、cuDNN）第一次用的時候才下載，放在 %LOCALAPPDATA%\\QuickSub。
"""
import glob
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import zipfile

from .common import (CUDA_DIR, DATA_DIR, FROZEN, IS_WINDOWS, MODELS_DIR, Cancelled, T, download, fetch_json)

# (代碼, Hugging Face 上的位置, 大約大小 MB, 中文說明, 英文說明)
MODELS = [
    ("large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", 1620,
     "最推薦：又快又準", "Recommended: fast and accurate"),
    ("large-v3", "Systran/faster-whisper-large-v3", 3090, "最準，但比較慢", "Most accurate, slower"),
    ("medium", "Systran/faster-whisper-medium", 1530, "中等", "Medium"),
    ("small", "Systran/faster-whisper-small", 485, "小又快，適合沒有顯示卡的電腦", "Small and fast, for PCs without a graphics card"),
    ("base", "Systran/faster-whisper-base", 145, "很小，準確度普通", "Tiny, so-so accuracy"),
    ("tiny", "Systran/faster-whisper-tiny", 75, "最小，只適合試用", "Smallest, just for trying out"),
]
MODEL_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.json", "vocabulary.txt")

# 影片說的語言（Whisper 支援的常用語言；auto = 自動判斷）
LANGUAGES = [
    ("auto", "自動判斷", "Auto-detect"), ("zh", "中文", "Chinese"), ("en", "英文", "English"),
    ("ja", "日文", "Japanese"), ("ko", "韓文", "Korean"), ("yue", "粵語", "Cantonese"),
    ("fr", "法文", "French"), ("de", "德文", "German"), ("es", "西班牙文", "Spanish"),
    ("it", "義大利文", "Italian"), ("pt", "葡萄牙文", "Portuguese"), ("ru", "俄文", "Russian"),
    ("th", "泰文", "Thai"), ("vi", "越南文", "Vietnamese"), ("id", "印尼文", "Indonesian"),
    ("ms", "馬來文", "Malay"), ("tl", "菲律賓文", "Filipino"), ("hi", "印地文", "Hindi"),
    ("ar", "阿拉伯文", "Arabic"), ("tr", "土耳其文", "Turkish"), ("nl", "荷蘭文", "Dutch"),
    ("pl", "波蘭文", "Polish"), ("uk", "烏克蘭文", "Ukrainian"),
]


def model_info(mid):
    return next((m for m in MODELS if m[0] == mid), MODELS[0])


def model_dir(mid):
    return os.path.join(MODELS_DIR, mid)


def model_ready(mid):
    d = model_dir(mid)
    return os.path.exists(os.path.join(d, ".complete")) and os.path.exists(os.path.join(d, "model.bin"))


def ensure_model(mid, progress=None, cancel=None):
    """模型沒下載過就從 Hugging Face 下載。progress(已下載, 總共)。"""
    if model_ready(mid):
        return model_dir(mid)
    repo = model_info(mid)[1]
    tree = fetch_json(f"https://huggingface.co/api/models/{repo}/tree/main")
    files = [(f["path"], (f.get("lfs") or {}).get("size") or f.get("size") or 0, (f.get("lfs") or {}).get("oid"))
             for f in tree if f.get("type") == "file" and f.get("path") in MODEL_FILES]
    if not any(p == "model.bin" for p, _, _ in files):
        raise IOError(T("找不到這個 AI 模型的檔案", "Couldn't find the files for this AI model"))
    total = sum(s for _, s, _ in files) or None
    d = model_dir(mid)
    os.makedirs(d, exist_ok=True)
    done_before = 0
    for path, size, sha in files:
        dest = os.path.join(d, path)
        if not (os.path.exists(dest) and size and os.path.getsize(dest) == size):
            download(f"https://huggingface.co/{repo}/resolve/main/{path}", dest,
                     (lambda n, _t, base=done_before: progress(base + n, total)) if progress else None, cancel,
                     timeout=120)
            if sha and len(sha) == 64 and sha256_file(dest) != sha:
                os.remove(dest)
                raise IOError(T("下載的模型檔案有問題（校驗碼不對），請再試一次", "The downloaded model file is corrupted. Please try again"))
        done_before += size
    open(os.path.join(d, ".complete"), "w").close()
    return d


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ 顯示卡（NVIDIA）

# faster-whisper 用顯示卡時要的 NVIDIA 元件：從 PyPI 的官方套件裡把 DLL 拿出來
CUDA_PACKAGES = [("nvidia-cublas-cu12", "12."), ("nvidia-cudnn-cu12", "9."), ("nvidia-cuda-nvrtc-cu12", "12.")]
CUDA_NEEDED = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll")
CUDA_SIZE_MB = 1300


def has_nvidia():
    if not IS_WINDOWS:
        return False
    return bool(shutil.which("nvidia-smi") or os.path.exists(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")))


def gpu_name():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True,
                             timeout=10, creationflags=0x08000000 if IS_WINDOWS else 0).stdout
        return out.strip().splitlines()[0].strip() if out.strip() else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def _dev_cuda_dirs():
    """用原始碼跑、而且 pip 裝過 nvidia-*-cu12 的話，直接用那些 DLL。"""
    if FROZEN:
        return []
    out = []
    for p in sys.path:
        out += glob.glob(os.path.join(p, "nvidia", "*", "bin"))
    return out


def cuda_ready():
    if all(os.path.exists(os.path.join(CUDA_DIR, n)) for n in CUDA_NEEDED):
        return True
    found = {os.path.basename(f) for d in _dev_cuda_dirs() for f in glob.glob(os.path.join(d, "*.dll"))}
    return all(n in found for n in CUDA_NEEDED)


def ensure_cuda(progress=None, cancel=None):
    """下載顯示卡加速元件（約 1.3 GB，只有第一次）。"""
    if cuda_ready():
        return
    os.makedirs(CUDA_DIR, exist_ok=True)
    wheels = []
    for pkg, major in CUDA_PACKAGES:
        meta = fetch_json(f"https://pypi.org/pypi/{pkg}/json")
        versions = sorted((v for v in meta.get("releases", {}) if v.startswith(major) and v.replace(".", "").isdigit()),
                          key=lambda v: [int(x) for x in v.split(".")])
        if not versions:
            raise IOError(f"{pkg}: no version")
        files = meta["releases"][versions[-1]]
        wheel = next((f for f in files if f["filename"].endswith("win_amd64.whl")), None)
        if not wheel:
            raise IOError(f"{pkg}: no Windows file")
        wheels.append(wheel)
    total = sum(w.get("size", 0) for w in wheels) or None
    done_before = 0
    for w in wheels:
        tmp = os.path.join(CUDA_DIR, "download.whl")
        download(w["url"], tmp, (lambda n, _t, base=done_before: progress(base + n, total)) if progress else None,
                 cancel, timeout=120)
        want = (w.get("digests") or {}).get("sha256")
        if want and sha256_file(tmp) != want:
            os.remove(tmp)
            raise IOError(T("下載的顯示卡元件有問題（校驗碼不對），請再試一次", "The downloaded GPU files are corrupted. Please try again"))
        with zipfile.ZipFile(tmp) as z:
            for name in z.namelist():
                if name.lower().endswith(".dll") and "/bin/" in name:
                    with z.open(name) as src, open(os.path.join(CUDA_DIR, os.path.basename(name)), "wb") as dst:
                        shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
        os.remove(tmp)
        done_before += w.get("size", 0)


def setup_dll_path():
    """讓 CTranslate2 找得到顯示卡元件（cuDNN 會自己再載入其他 DLL，所以 PATH 也要加）。"""
    dirs = [d for d in [CUDA_DIR] + _dev_cuda_dirs() if os.path.isdir(d)]
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except (AttributeError, OSError):
            pass
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])


# ------------------------------------------------------------------ 背景程式（worker）那一邊

def load_model(path, device):
    """載入 Whisper 模型，選顯示卡／處理器支援的最快格式（舊顯示卡不支援 float16，舊處理器不支援 int8）。"""
    import ctranslate2
    from faster_whisper import WhisperModel
    ok = ctranslate2.get_supported_compute_types(device)
    wanted = ("float16", "int8_float16", "int8_float32", "float32") if device == "cuda" else ("int8", "int8_float32", "float32")
    compute = next((t for t in wanted if t in ok), "default")
    return WhisperModel(path, device=device, compute_type=compute, cpu_threads=os.cpu_count() or 4)


def run_worker(port):
    """背景程式的主迴圈：收到一個工作就轉一個，模型留著給下一個用。"""
    setup_dll_path()
    conn = socket.create_connection(("127.0.0.1", port))
    f = conn.makefile("rwb")

    def send(**ev):
        f.write((json.dumps(ev, ensure_ascii=True) + "\n").encode("ascii"))
        f.flush()

    model, key = None, None
    for line in f:
        job = json.loads(line.decode("utf-8"))
        try:
            k = (job["model_dir"], job["device"])
            if k != key:
                model = None
                send(ev="status", what="load")
                model = load_model(job["model_dir"], job["device"])
                key = k
            send(ev="status", what="read")
            from faster_whisper.audio import decode_audio
            audio = decode_audio(job["path"], sampling_rate=model.feature_extractor.sampling_rate)
            lang = job.get("language") or None
            if not lang:  # 先聽前面幾段判斷語言，才知道要用哪種提示（中文提示會讓它寫繁體、加標點）
                send(ev="status", what="detect")
                try:
                    lang = model.detect_language(audio, vad_filter=True, language_detection_segments=3)[0]
                except Exception:
                    lang = None
            prompts = job.get("prompts") or {}
            send(ev="status", what="listen")
            segments, info = model.transcribe(
                audio, language=lang, beam_size=int(job.get("beam") or 5),
                vad_filter=bool(job.get("vad", True)), vad_parameters={"min_silence_duration_ms": 500},
                word_timestamps=True, initial_prompt=prompts.get(lang or "") or None,
                hotwords=job.get("hotwords") or None, condition_on_previous_text=False)
            del audio
            send(ev="info", language=info.language, prob=info.language_probability, duration=info.duration)
            out = []
            for s in segments:
                out.append({"start": s.start, "end": s.end, "text": s.text,
                            "words": [[w.start, w.end, w.word] for w in (s.words or [])]})
                send(ev="progress", done=s.end, total=info.duration)
            send(ev="result", language=info.language, duration=info.duration, segments=out)
        except Exception as e:  # 模型壞掉、檔案讀不到、顯示卡出錯……都回報給主視窗
            send(ev="error", msg=f"{type(e).__name__}: {e}"[:800])


# ------------------------------------------------------------------ 主視窗那一邊

class WorkerCrashed(Exception):
    """背景程式自己結束了（通常是顯示卡元件出問題）。"""


class Worker:
    """管理背景程式：需要時才開，取消時直接結束它。"""

    def __init__(self):
        self.proc = None
        self.f = None
        self.lock = threading.Lock()

    def _start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(90)
        port = srv.getsockname()[1]
        if FROZEN:
            cmd = [sys.executable, "--worker", str(port)]
        else:
            cmd = [sys.executable, os.path.abspath(sys.argv[0]), "--worker", str(port)]
        log = open(os.path.join(DATA_DIR, "worker.log"), "ab")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     creationflags=0x08000000 if IS_WINDOWS else 0)  # 不要跳出黑色視窗
        log.close()
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            self.stop()
            raise WorkerCrashed(T("背景程式開不起來", "The background process didn't start"))
        finally:
            srv.close()
        self.f = conn.makefile("rwb")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.proc, self.f = None, None

    def transcribe(self, job, on_event, cancel):
        """送出一個工作，一直讀回報直到有結果。取消 → Cancelled；背景程式當掉 → WorkerCrashed。"""
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                self._start()
            proc = self.proc
            done = threading.Event()

            def watch():  # 按了取消就直接結束背景程式
                while not done.wait(0.2):
                    if cancel.is_set():
                        self.stop()
                        return
            threading.Thread(target=watch, daemon=True).start()
            try:
                self.f.write((json.dumps(job, ensure_ascii=True) + "\n").encode("ascii"))
                self.f.flush()
                while True:
                    line = self.f.readline()
                    if not line:
                        break
                    ev = json.loads(line.decode("ascii"))
                    if ev["ev"] in ("result", "error"):
                        return ev
                    on_event(ev)
            except (OSError, ValueError, AttributeError):
                pass
            finally:
                done.set()
            if cancel.is_set():
                raise Cancelled()
            code = proc.poll()
            self.stop()
            raise WorkerCrashed(T(f"背景程式意外結束（代碼 {code}）", f"The background process stopped unexpectedly (code {code})"))
