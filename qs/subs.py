# -*- coding: utf-8 -*-
"""字幕本身：重新斷句、繁簡轉換、濾掉 AI 亂加的句子、各種字幕格式的讀寫。"""
import re

from .common import IS_WINDOWS

CJK_LANGS = ("zh", "ja", "ko", "yue")
_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯＀-￯　-〿"
CJK_CHAR = re.compile(f"[{_CJK}]")
SENT_END = set("。！？!?…") | {"."}
CLAUSE_END = set("，、,;；：:")


class Seg:
    """一句字幕。trans = 翻譯（沒有就是 None）；words = [(開始, 結束, 字)]，重新斷句用。"""
    __slots__ = ("start", "end", "text", "trans", "words")

    def __init__(self, start, end, text, trans=None, words=None):
        self.start, self.end, self.text, self.trans, self.words = float(start), float(end), text, trans, words or []

    def copy(self):
        return Seg(self.start, self.end, self.text, self.trans, list(self.words))


def is_cjk(lang):
    return (lang or "") in CJK_LANGS


def disp_len(text, cjk):
    """一行看起來有多長：中日韓一個字算 1，英數算 0.5（中文字幕裡夾英文時）。"""
    if not cjk:
        return len(text)
    return sum(1 if CJK_CHAR.match(ch) else 0.5 for ch in text)


_FULL = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；"}


def clean(text, cjk):
    text = re.sub(r"\s+", " ", text or "").strip()
    if cjk:  # 中文字之間不要有空格；中文後面的半形標點換成全形
        text = re.sub(f"(?<=[{_CJK}]) (?=[{_CJK}])", "", text)
        text = re.sub(f"(?<=[{_CJK}])[,?!:;]", lambda m: _FULL[m.group(0)], text)
    return text


# ---- AI（Whisper）在沒聲音的地方常常「聽」出來的句子：影片網站字幕組的廣告詞之類
HALLUCINATIONS = [re.compile(p, re.I) for p in (
    r"請不吝點[讚赞]", r"訂閱.{0,6}轉發.{0,6}打賞", r"订阅.{0,6}转发", r"明鏡.{0,3}點點欄目", r"明镜.{0,3}点点栏目",
    r"字幕由.{0,12}(amara|提供)", r"amara\.org", r"中文字幕.{0,4}志願者", r"字幕志愿者",
    r"^\W*(點讚|点赞)?\W*訂閱\W*(我的頻道)?\W*$", r"優優獨播劇場", r"YoYo Television Series",
    r"^\W*字幕[：:]", r"^\W*(本字幕|字幕製作|字幕制作)", r"subtitles by the amara", r"^\W*thanks? for watching\W*$",
    r"ご視聴ありがとうございました", r"チャンネル登録",
)]


def is_hallucination(text):
    return any(p.search(text) for p in HALLUCINATIONS)


def drop_hallucinations(segs):
    """拿掉廣告詞；同一句連續重複超過兩次（AI 鬼打牆）只留一次。"""
    out = []
    for s in segs:
        t = s.text.strip()
        if not t or is_hallucination(t):
            continue
        if len(out) >= 2 and out[-1].text.strip() == t and out[-2].text.strip() == t:
            out[-1].end = max(out[-1].end, s.end)
            continue
        out.append(s)
    return out


# 英文不要切在這些字後面（切了會很怪，例如 "about these / guys"、"welcome back to the / channel"）
WEAK_WORDS = {"a", "an", "the", "to", "of", "in", "on", "at", "for", "and", "or", "but", "with", "from", "by", "as",
              "my", "your", "our", "his", "her", "their", "its", "this", "that", "these", "those", "some", "any", "every",
              "each", "no", "not", "very", "really", "so", "too", "is", "are", "was", "were", "be", "been", "am", "i", "we",
              "you", "they", "he", "she", "it", "can", "will", "would", "should", "could", "have", "has", "had", "do", "does",
              "did", "about", "into", "onto", "over", "under", "after", "before", "because", "if", "when", "than", "then",
              "just", "more", "most", "such", "what", "which", "who", "where", "how", "there", "here", "going", "gonna"}


def _text(ws, cjk):
    return clean("".join(w[2] for w in ws), cjk)


def _len(ws, cjk):
    """算長度時不算標點（中文字幕的標點最後大多會拿掉或變空格）。"""
    text = _text(ws, cjk)
    return disp_len(re.sub(r"[，。、！？!?,.；;：:…]", "", text) if cjk else text, cjk)


def _break_cost(ws, k, cjk):
    """在第 k 個字後面換句的代價：句號最好、逗號次之、有停頓的地方也好；英文切在 the、to 後面很怪。"""
    word = ws[k][2].strip()
    last = word[-1:]
    cost = 0.0 if last in SENT_END else 0.25 if last in CLAUSE_END else 1.0
    cost -= min(max(ws[k + 1][0] - ws[k][1], 0.0), 1.0) * 0.6
    if not cjk and word.lower().strip(",.!?") in WEAK_WORDS:
        cost += 1.2
    return cost


def _split(ws, cjk, max_chars, max_dur):
    """把一句太長的話切成幾行：行數越少越好、每行長度盡量平均、換行的位置要自然（動態規劃找最好的切法）。"""
    n = len(ws)
    total = _len(ws, cjk)
    soft = max_chars * 1.1  # 超過一點點沒關係，免得差一個字就多切一行
    if n <= 1 or (total <= soft and ws[-1][1] - ws[0][0] <= max_dur):
        return [ws]
    target = total / max(1, -(-total // max_chars))
    best = [0.0] + [float("inf")] * n
    prev = [0] * (n + 1)
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            line = ws[i:j]
            length = _len(line, cjk)
            if length > soft and j - i > 1:
                break  # 再往前只會更長
            cost = 1.5 + abs(length - target) / max(target, 1) * 0.8
            dur = line[-1][1] - line[0][0]
            if dur > max_dur and j - i > 1:
                cost += 2.0 + dur - max_dur
            if j < n:
                cost += _break_cost(ws, j - 1, cjk)
            if best[i] + cost < best[j]:
                best[j], prev[j] = best[i] + cost, i
    out, j = [], n
    while j > 0:
        out.append(ws[prev[j]:j])
        j = prev[j]
    return out[::-1]


def resegment(raw, cjk, max_chars, max_dur):
    """用每個字的時間點重新斷句：先在句號切開，太長的再找好位置切。"""
    lines = []
    for s in raw:
        words = [w for w in s.words if w[2].strip()]
        if not words:
            text = clean(s.text, cjk)
            if text:
                lines.append(Seg(s.start, s.end, text))
            continue
        spans, cur = [], []
        for w in words:
            cur.append(w)
            if w[2].strip()[-1:] in SENT_END:
                spans.append(cur)
                cur = []
        if cur:
            spans.append(cur)
        for span in spans:
            for part in _split(span, cjk, max_chars, max_dur):
                text = _text(part, cjk)
                if text:
                    lines.append(Seg(part[0][0], part[-1][1], text, words=list(part)))
    return fix_timing(lines)


def fix_timing(segs, min_dur=0.6):
    """每句至少顯示 0.6 秒（不蓋到下一句），也不能跟下一句重疊。"""
    segs = sorted(segs, key=lambda s: s.start)
    for i, s in enumerate(segs):
        nxt = segs[i + 1].start if i + 1 < len(segs) else None
        if s.end - s.start < min_dur:
            s.end = s.start + min_dur if nxt is None else min(s.start + min_dur, max(nxt, s.end))
        if nxt is not None and s.end > nxt:
            s.end = max(s.start + 0.05, nxt)
    return segs


def tidy_punct(text, mode, cjk):
    """台灣字幕習慣：trim = 拿掉句尾的逗號、句號；space = 逗號句號都換成空格；keep = 不動。"""
    if not cjk or mode == "keep":
        return text
    if mode == "space":
        text = re.sub(r"[，。、]+", " ", text).strip()
        return re.sub(r" {2,}", " ", text)
    return re.sub(r"[，。、,.]+$", "", text).strip()


# ---- 繁簡轉換（OpenCC）
_converters = {}


def convert(text, mode):
    """s2twp：簡體 → 繁體＋台灣用語；s2hk：香港繁體；t2s：繁體 → 簡體；none：不轉。"""
    if not text or mode in ("", "none"):
        return text
    cc = _converters.get(mode)
    if cc is None:
        from opencc import OpenCC
        cc = _converters[mode] = OpenCC(mode)
    return cc.convert(text)


# ---- 字幕格式
def _ts(t, sep=","):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def lines_of(seg, layout):
    """一句字幕要顯示的行：orig 原文 / trans 翻譯 / both 原文在上 / both_tf 翻譯在上。"""
    if seg.trans is None or layout == "orig":
        return [seg.text]
    if layout == "trans":
        return [seg.trans]
    return [seg.trans, seg.text] if layout == "both_tf" else [seg.text, seg.trans]


def to_srt(segs, layout="orig"):
    out = []
    for i, s in enumerate(segs, 1):
        out.append(f"{i}\n{_ts(s.start)} --> {_ts(s.end)}\n" + "\n".join(lines_of(s, layout)) + "\n")
    return "\n".join(out)


def to_vtt(segs, layout="orig"):
    out = ["WEBVTT\n"]
    for s in segs:
        out.append(f"{_ts(s.start, '.')} --> {_ts(s.end, '.')}\n" + "\n".join(lines_of(s, layout)) + "\n")
    return "\n".join(out)


def to_txt(segs, layout="orig", times=False):
    out = []
    for s in segs:
        text = " / ".join(lines_of(s, layout))
        out.append(f"[{_ts(s.start, '.')[:-4]}] {text}" if times else text)
    return "\n".join(out) + "\n"


ASS_FONT = "Microsoft JhengHei" if IS_WINDOWS else "PingFang TC"


def to_ass(segs, layout="orig", width=1920, height=1080, font_size=0, font=ASS_FONT):
    """ASS 字幕（燒進影片也用這個）：白字黑邊，雙語時第二行小一點。"""
    size = font_size or max(16, round(height * 0.055))
    small = max(12, round(size * 0.72))
    outline = max(1.0, round(size / 18, 1))
    head = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,"
        f"{outline},{round(outline * 0.4, 1)},2,{round(width * 0.04)},{round(width * 0.04)},{round(height * 0.05)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")

    def ts(t):
        cs = int(round(max(0.0, t) * 100))
        h, cs = divmod(cs, 360000)
        m, cs = divmod(cs, 6000)
        s, cs = divmod(cs, 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    def esc(t):
        return t.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")

    rows = []
    for s in segs:
        ls = [esc(x) for x in lines_of(s, layout)]
        text = ls[0] + "".join(f"\\N{{\\fs{small}}}{x}" for x in ls[1:])
        rows.append(f"Dialogue: 0,{ts(s.start)},{ts(s.end)},Default,,0,0,0,,{text}")
    return head + "\n".join(rows) + "\n"


FORMATS = {"srt": to_srt, "vtt": to_vtt, "ass": to_ass, "txt": to_txt}


def parse_srt(text):
    """讀 SRT／VTT（給編輯器開舊字幕用）。兩行以上的字幕：第一行當原文，其餘當翻譯。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    segs = []
    tre = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            m = tre.search(line)
            if not m:
                continue
            g = [int(x) for x in m.groups()]
            start = g[0] * 3600 + g[1] * 60 + g[2] + int(m.group(4).ljust(3, "0")) / 1000
            end = g[4] * 3600 + g[5] * 60 + g[6] + int(m.group(8).ljust(3, "0")) / 1000
            body = [re.sub(r"<[^>]+>", "", l).strip() for l in lines[i + 1:]]
            body = [b for b in body if b]
            if body:
                segs.append(Seg(start, end, body[0], "\n".join(body[1:]) or None))
            break
    return segs
