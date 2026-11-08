# -*- coding: utf-8 -*-
"""翻譯字幕：本機的 Ollama（免費、不上網），或相容 OpenAI 格式的線上 AI（Gemini、DeepSeek、OpenAI…）。
一次送一批（有編號），請 AI 照編號一行一行回，對不上的那批再拆小一點重送。"""
import json
import re
import time
import urllib.error
import urllib.parse

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

# 線上 AI 的預設值（網址和模型名稱都可以在設定裡改）。2026-11 照各家官方文件更新：
# Gemini、DeepSeek 用「永遠指向最新版」的名稱，舊版停用時不用跟著改；OpenAI 沒有這種名稱，用官方建議的最便宜模型
PRESETS = {
    "gemini": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-flash-latest"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com", "deepseek-flash"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "gpt-6-luna"),
    "custom": (T("自訂（相容 OpenAI 的服務）", "Custom (OpenAI-compatible)"), "", ""),
}
KEY_PAGES = {"gemini": "https://aistudio.google.com/apikey", "deepseek": "https://platform.deepseek.com/api_keys",
             "openai": "https://platform.openai.com/api-keys"}
# 以前的預設值，已經停用或淘汰（deepseek-chat 2026-07-24 停用；gemini-2.5-flash、gpt-4o-mini 已淘汰）。
# 設定裡存的是這些就自動換成新的，舊版的使用者不用自己改
RETIRED_MODELS = {"deepseek-chat": "deepseek-flash", "deepseek-reasoner": "deepseek-flash",
                  "gemini-2.5-flash": "gemini-flash-latest", "gemini-2.0-flash": "gemini-flash-latest",
                  "gemini-1.5-flash": "gemini-flash-latest", "gpt-4o-mini": "gpt-6-luna"}
OLD_BASES = {"https://api.deepseek.com/v1": "https://api.deepseek.com"}
OLLAMA = "http://127.0.0.1:11434"
BATCH = 25


def target_name(code):
    return next((t[3] for t in TARGETS if t[0] == code), code)


def upgrade_model(model):
    return RETIRED_MODELS.get((model or "").strip(), (model or "").strip())


def upgrade_base(base):
    base = (base or "").strip().rstrip("/")
    return OLD_BASES.get(base, base)


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
        base = upgrade_base(cfg.get("api_base") or base)
        model = upgrade_model(cfg.get("api_model") or model)
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
        return _strip_think(_api_chat(base, key, model, msgs, timeout))
    except urllib.error.HTTPError as e:
        raise IOError(f"HTTP {e.code} {_error_detail(e)}") from None
    except urllib.error.URLError as e:
        raise IOError(T("連不上翻譯服務：", "Can't reach the translation service: ") + str(e.reason)) from None


def _extras(base, model):
    """各家關掉「思考」的寫法不一樣（翻字幕不用思考：快很多也便宜）。這個模型不認得的話，_api_chat 會拿掉重送。"""
    host = (urllib.parse.urlparse(base).hostname or "").lower()
    if host.endswith("deepseek.com"):
        return {"thinking": {"type": "disabled"}}
    if host.endswith("googleapis.com"):  # 只有 2.5 Flash 能完全關掉；Gemini 3 以後最少是 minimal
        return {"reasoning_effort": "none" if "2.5-flash" in model.lower() else "minimal"}
    if host.endswith("openai.com"):
        return {"reasoning_effort": "none"}
    return {"temperature": 0.2}  # 自訂的服務（常是本機的小模型）：溫度低一點比較會照格式回答


def _api_chat(base, key, model, msgs, timeout):
    """相容 OpenAI 格式的 /chat/completions。參數不被接受（400）→ 拿掉額外參數重送；太忙、超過頻率（429、5xx）→ 等一下重試。"""
    body = {"model": model, "messages": msgs, **_extras(base, model)}
    waits = [2, 6, 15]
    while True:
        try:
            r = fetch_json(base + "/chat/completions", headers={"Authorization": "Bearer " + key}, data=body,
                           timeout=timeout)
            break
        except urllib.error.HTTPError as e:
            detail = _error_detail(e)
            if e.code in (400, 422) and len(body) > 2 and not _is_key_error(e.code, detail):
                body = {"model": model, "messages": msgs}
                continue
            if e.code in (429, 500, 502, 503, 504) and waits:
                time.sleep(min(_retry_after(e) or waits[0], 30))
                waits.pop(0)
                continue
            raise _friendly_error(e.code, detail, model) from None
    choice = (r.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if isinstance(content, list):  # 少數服務把回答分段回傳
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content  # 空的（例如被內容審查擋掉）：translate() 會拆小重送，還是不行就保留原文


def _error_detail(e):
    """錯誤內容裡的訊息（各家格式不同：{"error": {...}}、Gemini 是 [{"error": {...}}]）。"""
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        j = json.loads(raw)
        j = j[0] if isinstance(j, list) and j else j
        err = j.get("error", j) if isinstance(j, dict) else j
        msg = err.get("message") if isinstance(err, dict) else err
        return str(msg or raw)[:300]
    except ValueError:
        return raw.strip()[:300]


def _is_key_error(code, detail):
    return code in (401, 403) or bool(re.search(r"api[ _-]?key|authorization|authenticat|unauthori", detail, re.I))


def _retry_after(e):
    try:
        return float(e.headers.get("Retry-After") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _friendly_error(code, detail, model):
    d = detail.lower()
    if _is_key_error(code, detail):
        return ValueError(T("線上 AI 的金鑰不對或沒有權限：到「設定 → 翻譯」重新貼上金鑰", "The API key was rejected: paste it again in Settings → Translation") + f"（{code}）")
    if code == 402 or "balance" in d or "billing" in d or "credit" in d:
        return ValueError(T("線上 AI 的帳戶餘額不足，請先到該服務的網站儲值", "Your account with the online AI is out of credit") + f"（{code}）")
    if code == 404 and "model" not in d:
        return IOError(T("翻譯服務的網址好像不對：檢查「設定 → 翻譯」的網址", "The service address looks wrong: check it in Settings → Translation") + f"（{code}）")
    if code == 404 or ("model" in d and re.search(r"not (be )?found|not exist|invalid|not supported|unknown", d)):
        return ValueError(T(f"找不到模型「{model}」（可能已經停用）：到「設定 → 翻譯」換一個模型名稱",
                            f"Model “{model}” wasn't found (it may have been retired): change it in Settings → Translation") + f"（{code}）")
    if code == 429:
        return ValueError(T("線上 AI 說用量超過上限（免費額度用完或送得太快），請稍後再試", "The online AI's usage limit was reached (free quota used up or too many requests). Try again later") + f"（{code}）")
    return IOError(f"HTTP {code} {detail}")


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
