# -*- coding: utf-8 -*-
"""快字幕共用的東西：介面語言、資料夾位置、設定檔、下載工具。"""
import json
import os
import sys
import threading
import urllib.request

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes

APP_ID = "Benjaminwz.QuickSub"
APP_VERSION = "1.0.0"
UPDATE_REPO = "Benjaminwz/quicksub"  # 到這個 GitHub 專案檢查新版；空字串 = 不檢查
USER_AGENT = "QuickSub/" + APP_VERSION


def detect_lang():
    """系統是中文就用中文介面，其他一律英文。QUICKSUB_LANG=zh/en 可以強制指定。"""
    forced = os.environ.get("QUICKSUB_LANG", "").lower()
    if forced in ("zh", "en"):
        return forced
    if IS_WINDOWS:
        try:
            # 語言 ID 的低 10 位元是主要語言，0x04 = 中文
            return "zh" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x04 else "en"
        except Exception:
            pass
    loc = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    return "zh" if loc.startswith("zh") else "en"


ZH = detect_lang() == "zh"


def T(zh, en):
    """介面文字：中文系統顯示第一個，其他顯示第二個（英文）。"""
    return zh if ZH else en


APP_NAME = T("快字幕", "QuickSub")

FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else sys.argv[0]))
RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)


def data_dir():
    """放 AI 模型、顯示卡元件、工具和設定的地方（%LOCALAPPDATA%\\QuickSub）。QUICKSUB_DATA 可以改（測試用）。"""
    d = os.environ.get("QUICKSUB_DATA")
    if not d:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
        d = os.path.join(base, "QuickSub")
    os.makedirs(d, exist_ok=True)
    return d


DATA_DIR = data_dir()
MODELS_DIR = os.path.join(DATA_DIR, "models")
CUDA_DIR = os.path.join(DATA_DIR, "cuda")
TOOLS_DIR = os.path.join(DATA_DIR, "tools")

DEFAULTS = {
    "language": "auto",        # 影片說的語言：auto 或語言代碼
    "model": "large-v3-turbo",
    "device": "auto",          # auto：有 NVIDIA 顯示卡就用；cpu：只用處理器
    "convert": "s2twp",        # 中文轉換：s2twp 繁體台灣用語 / s2hk 繁體香港 / t2s 簡體 / none
    "formats": ["srt"],
    "max_chars": 0,            # 每行最多幾個字；0 = 自動（中日韓 18、其他 42）
    "max_dur": 6.0,            # 每句最長幾秒
    "prompt": "",              # 專有名詞（人名、術語），讓 AI 聽得更準
    "translate": "",           # 翻成哪種語言；空字串 = 不翻譯
    "bilingual": True,         # 翻譯後保留原文（雙語字幕）
    "trans_first": False,      # 雙語時翻譯放上面
    "engine": "ollama",        # 翻譯用：ollama（本機）或 api（線上 AI）
    "ollama_model": "",
    "api_preset": "gemini",
    "api_base": "",
    "api_key": "",
    "api_model": "",
    "burn": False,             # 把字幕燒進影片
    "embed": False,            # 把字幕放進影片當字幕軌
    "font_size": 0,            # 燒字幕的字型大小；0 = 依影片高度自動
    "out_mode": "same",        # same：跟影片同一個資料夾；folder：存到 out_dir
    "out_dir": "",
    "punct": "space",          # 中文標點：space 換成空格（台灣字幕習慣）/ trim 拿掉句尾 / keep 保留
    "vad": True,               # 先跳過沒有人聲的地方（比較不會亂加字）
    "beam": 5,
    "dl_dir": "",              # 網址下載的影片存哪裡；空 = 「影片／快字幕」
}


class Config:
    def __init__(self, path=None):
        self.path = path or os.path.join(DATA_DIR, "config.json")
        self.lock = threading.Lock()
        self.data = dict(DEFAULTS)
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                self.data.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass

    def get(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)


class Cancelled(Exception):
    """使用者按了取消。"""


def download(url, dest, progress=None, cancel=None, headers=None, timeout=60):
    """下載到 dest（先寫 .part，完整了才改名）。progress(已下載, 總共或 None)；cancel 是 threading.Event。"""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
        total = r.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        done = 0
        while True:
            if cancel is not None and cancel.is_set():
                break
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if cancel is not None and cancel.is_set():
        try:
            os.remove(part)
        except OSError:
            pass
        raise Cancelled()
    if total is not None and done != total:
        raise IOError(T("下載不完整，請再試一次", "The download was incomplete. Please try again"))
    os.replace(part, dest)
    return dest


def fetch_json(url, headers=None, data=None, timeout=30):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    h = {"User-Agent": USER_AGENT, **({"Content-Type": "application/json"} if body else {}), **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def human_size(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def human_time(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return T(f"{h} 小時 {m} 分", f"{h}h {m}m")
    if m:
        return T(f"{m} 分 {s} 秒", f"{m}m {s}s")
    return T(f"{s} 秒", f"{s}s")
