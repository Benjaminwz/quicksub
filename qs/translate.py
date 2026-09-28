# -*- coding: utf-8 -*-
"""翻譯字幕：本機的 Ollama（免費、不上網），或相容 OpenAI 格式的線上 AI（Gemini、DeepSeek、OpenAI…）。
一次送一批（有編號），請 AI 照編號一行一行回，對不上的那批再拆小一點重送。"""
import re
import urllib.error

from .common import Cancelled, T, fetch_json
from .subs import convert

# (代碼, 中文名稱, 英文名稱, 給 AI 看的語言名稱)
TARGETS = [
    ("zh-TW", "繁體中文（台灣）", "Traditional Chinese (Taiwan)", "Traditional Chinese as used in Taiwan (繁體中文，台灣用語)"),
    ("en", "英文", "English", "English"),
    ("ja", "日文", "Japanese", "Japanese"),
    ("ko", "韓文", "Korean", "Korean"),
    ("zh-CN", "簡體中文", "Simplified Chinese", "Simplified Chinese"),
    ("fr", "法文", "French", "French"), ("de", "德文", "German", "German"), ("es", "西班牙文", "Spanish", "Spanish"),
    ("th", "泰文", "Thai", "Thai"), ("vi", "越南文", "Vietnamese", "Vietnamese"), ("id", "印尼文", "Indonesian", "Indonesian"),
]

# 線上 AI 的預設值（網址和模型名稱都可以在設定裡改）
PRESETS = {
    "gemini": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    "custom": (T("自訂（相容 OpenAI 的服務）", "Custom (OpenAI-compatible)"), "", ""),
}
OLLAMA = "http://127.0.0.1:11434"
BATCH = 25


def target_name(code):
    return next((t[3] for t in TARGETS if t[0] == code), code)


def ollama_models():
    """本機 Ollama 裝了哪些模型；沒開 Ollama 就回傳空的。"""
    try:
        return [m["name"] for m in fetch_json(OLLAMA + "/api/tags", timeout=3).get("models", [])]
    except Exception:
        return []


def pick_ollama_model(models):
    """沒選的話，挑一個翻譯比較好的（有 qwen、gemma、llama 優先）。"""
    usable = [m for m in models if "embed" not in m.lower()]

    def size(m):  # 名稱裡的參數量，例如 deepseek-r1:14b → 14（大的通常翻得比較好）
        found = re.search(r":(\d+(?:\.\d+)?)b", m.lower())
        return float(found.group(1)) if found else 0.0
    # 大的優先；一樣大時 qwen、gemma、deepseek、llama 優先（中文比較好）。會思考的模型我們會叫它直接回答
    rank = {"qwen": 0, "gemma": 1, "deepseek": 2, "llama": 3, "mistral": 4}
    usable.sort(key=lambda m: (-size(m), next((v for k, v in rank.items() if k in m.lower()), 9)))
    return usable[0] if usable else ""


def engine_config(cfg):
    """從設定整理出要用哪個翻譯服務。回傳 (種類, 網址, 金鑰, 模型)。"""
    if cfg.get("engine") == "api":
        name, base, model = PRESETS.get(cfg.get("api_preset"), PRESETS["custom"])
        base = (cfg.get("api_base") or base).rstrip("/")
        model = cfg.get("api_model") or model
        if not cfg.get("api_key"):
            raise ValueError(T("還沒填線上 AI 的金鑰：到「設定 → 翻譯」填好再試", "No API key yet: add it in Settings → Translation"))
        if not base or not model:
            raise ValueError(T("線上 AI 的網址或模型名稱沒填", "The API address or model name is empty"))
        return "api", base, cfg.get("api_key"), model
    models = ollama_models()
    if not models:
        raise ValueError(T("找不到 Ollama：請先打開 Ollama，並下載一個模型（例如 ollama pull qwen2.5:7b），或改用線上 AI",
                           "Ollama isn't running: open it and pull a model (e.g. ollama pull qwen2.5:7b), or use an online AI"))
    model = cfg.get("ollama_model") if cfg.get("ollama_model") in models else pick_ollama_model(models)
    return "ollama", OLLAMA, "", model


def chat(eng, system, user, timeout=300):
    kind, base, key, model = eng
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        raw = _raw_prompt(model, system, user) if kind == "ollama" else None
        if raw:
            # 會「思考」的模型就算設 think=false，還是會在背後想好幾百個字（每次要 5 秒）。
            # 直接在提示最後放一段空的思考，它就會馬上回答（實測 deepseek-r1:14b 從 5 秒變 0.3 秒）
            r = fetch_json(base + "/api/generate", data={"model": model, "prompt": raw, "raw": True, "stream": False,
                                                         "keep_alive": "30m", "options": {"temperature": 0.2, "num_predict": 1500}},
                           timeout=timeout)
            return _strip_think(r.get("response", ""))
        if kind == "ollama":
            body = {"model": model, "messages": msgs, "stream": False, "think": False, "keep_alive": "30m",
                    "options": {"temperature": 0.2}}
            try:
                r = fetch_json(base + "/api/chat", data=body, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code != 400:
                    raise
                del body["think"]  # 比較舊的 Ollama 或不會「思考」的模型不認得這個
                r = fetch_json(base + "/api/chat", data=body, timeout=timeout)
            return _strip_think((r.get("message") or {}).get("content", ""))
        r = fetch_json(base + "/chat/completions", headers={"Authorization": "Bearer " + key},
                       data={"model": model, "messages": msgs, "temperature": 0.2}, timeout=timeout)
        return _strip_think(r["choices"][0]["message"]["content"] or "")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if e.code in (401, 403):
            raise ValueError(T("線上 AI 的金鑰不對或沒有權限", "The API key was rejected") + f"（{e.code}）")
        raise IOError(f"HTTP {e.code} {detail}")


def _raw_prompt(model, system, user):
    """deepseek-r1 自己組提示（照它的格式），最後放一段空的 <think></think>，讓它跳過思考直接回答。
    （qwen3 的「只會思考」版本這招沒用，照常用 think=false）"""
    if "deepseek-r1" in model.lower():
        return f"{system}<｜User｜>{user}<｜Assistant｜><think>\n\n</think>\n\n"
    return None


def _strip_think(text):
    """會「思考」的模型（qwen3、deepseek-r1…）回答前面可能夾著 <think>…</think>，拿掉。"""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def _system(target, terms):
    s = (f"You are a professional subtitle translator. Translate each numbered subtitle line into {target_name(target)}.\n"
         "Rules:\n- Output exactly one line per input line, in the form `number|translation`, same numbers, same order.\n"
         "- Do not merge or split lines. No notes, no explanations, no quotes.\n"
         "- Natural spoken style suitable for subtitles; keep it short.\n"
         "- Keep names and terms consistent across lines.")
    if target == "zh-TW":
        s += "\n- Use Traditional Chinese characters and Taiwanese wording (e.g. 影片, 軟體, 網路, 資訊)."
    if terms:
        s += f"\n- Proper nouns / glossary (keep these spellings): {terms}"
    return s


def _parse(reply, n):
    got = {}
    for line in reply.splitlines():
        m = re.match(r"\s*(\d+)\s*[|｜:：.、)]\s*(.*)", line)
        if m:
            i = int(m.group(1))
            if 1 <= i <= n and m.group(2).strip():
                got[i] = m.group(2).strip()
    return got


def translate(segs, target, cfg, progress=None, cancel=None):
    """把每句的 text 翻好放進 trans。progress(已翻, 總共)。"""
    eng = engine_config(cfg)
    terms = (cfg.get("prompt") or "").strip()
    system = _system(target, terms)
    total = len(segs)
    done = 0

    def run(batch):
        user = "\n".join(f"{i}|{s.text.replace(chr(10), ' ')}" for i, s in enumerate(batch, 1))
        got = _parse(chat(eng, system, user), len(batch))
        if len(got) < len(batch) and len(batch) > 1:  # 對不上：拆兩半重送
            half = len(batch) // 2
            run(batch[:half])
            run(batch[half:])
            return
        for i, s in enumerate(batch, 1):
            s.trans = got.get(i, s.text)

    for start in range(0, total, BATCH):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        batch = segs[start:start + BATCH]
        run(batch)
        done += len(batch)
        if progress:
            progress(done, total)
    if target == "zh-TW":
        for s in segs:
            s.trans = convert(s.trans, "s2twp")
    elif target == "zh-CN":
        for s in segs:
            s.trans = convert(s.trans, "t2s")
    return segs
