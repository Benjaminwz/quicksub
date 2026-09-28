# -*- coding: utf-8 -*-
"""影片相關：ffmpeg（燒字幕、放字幕軌、查影片大小）、yt-dlp（從網址下載影片）。
工具沒有的話第一次用時自動下載到 %LOCALAPPDATA%\\QuickSub\\tools。"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile

from .common import BASE_DIR, IS_WINDOWS, TOOLS_DIR, Cancelled, T, download

EXE = ".exe" if IS_WINDOWS else ""
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

MEDIA_EXT = {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg",
             ".3gp", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".amr"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".amr"}
LANG3 = {"zh": "chi", "zh-TW": "chi", "zh-CN": "chi", "yue": "chi", "en": "eng", "ja": "jpn", "ko": "kor", "fr": "fre", "de": "ger", "es": "spa",
         "it": "ita", "pt": "por", "ru": "rus", "th": "tha", "vi": "vie", "id": "ind"}


def find_tool(name):
    places = [os.path.join(TOOLS_DIR, name + EXE), os.path.join(BASE_DIR, name + EXE)]
    if name == "deno":  # Deno 常見的安裝位置（官方安裝程式、winget）
        places += [os.path.join(os.path.expanduser("~"), ".deno", "bin", "deno" + EXE),
                   os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", "deno" + EXE)]
    for p in places:
        if os.path.exists(p):
            return p
    return shutil.which(name)


def ensure_ffmpeg(progress=None, cancel=None):
    ff, fp = find_tool("ffmpeg"), find_tool("ffprobe")
    if ff and fp:
        return ff, fp
    zpath = os.path.join(TOOLS_DIR, "ffmpeg.zip")
    download(FFMPEG_URL, zpath, progress, cancel, timeout=120)
    with zipfile.ZipFile(zpath) as z:
        for n in z.namelist():
            if n.endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe")):
                with z.open(n) as src, open(os.path.join(TOOLS_DIR, os.path.basename(n)), "wb") as dst:
                    shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
    os.remove(zpath)
    return os.path.join(TOOLS_DIR, "ffmpeg.exe"), os.path.join(TOOLS_DIR, "ffprobe.exe")


def ensure_ytdlp(progress=None, cancel=None):
    """yt-dlp 和它要的 Deno（YouTube 現在要跑一段 JavaScript 才能下載）。"""
    yt = find_tool("yt-dlp")
    if not yt:
        yt = download(YTDLP_URL, os.path.join(TOOLS_DIR, "yt-dlp.exe"), progress, cancel, timeout=120)
    deno = find_tool("deno")
    if not deno:
        zpath = os.path.join(TOOLS_DIR, "deno.zip")
        download(DENO_URL, zpath, progress, cancel, timeout=120)
        with zipfile.ZipFile(zpath) as z:
            z.extract("deno.exe", TOOLS_DIR)
        os.remove(zpath)
        deno = os.path.join(TOOLS_DIR, "deno.exe")
    return yt, deno


def probe(path):
    """(寬, 高, 秒數, 有沒有畫面)。查不到就回傳預設值。"""
    fp = find_tool("ffprobe")
    if not fp:
        return 1920, 1080, 0.0, os.path.splitext(path)[1].lower() not in AUDIO_EXT
    try:
        out = subprocess.run([fp, "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                              "-of", "json", path], capture_output=True, timeout=60, creationflags=NO_WINDOW).stdout
        j = json.loads(out.decode("utf-8", "replace") or "{}")
        video = next((s for s in j.get("streams", []) if s.get("codec_type") == "video" and s.get("width")), None)
        dur = float((j.get("format") or {}).get("duration") or 0)
        if video:
            return int(video["width"]), int(video["height"]), dur, True
        return 1920, 1080, dur, False
    except (OSError, ValueError, subprocess.SubprocessError):
        return 1920, 1080, 0.0, True


def _kill_on_cancel(proc, cancel):
    """按了取消就結束這個程式（程式自己結束了，這個小幫手也跟著結束）。"""
    if cancel is None:
        return

    def watch():
        while proc.poll() is None:
            if cancel.wait(0.3):
                if proc.poll() is None:
                    proc.kill()
                return
    threading.Thread(target=watch, daemon=True).start()


def _run(cmd, duration, progress, cancel, cwd=None):
    """跑 ffmpeg，從它的輸出抓進度（time=00:01:23.45）。失敗丟出最後幾行錯誤訊息。"""
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW)
    tail = []
    _kill_on_cancel(proc, cancel)
    buf = b""
    while True:
        ch = proc.stderr.read(4096)
        if not ch:
            break
        buf += ch
        *lines, buf = re.split(rb"[\r\n]", buf)
        for line in lines:
            text = line.decode("utf-8", "replace").strip()
            if text:
                tail = (tail + [text])[-8:]
            m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", text)
            if m and progress and duration:
                t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                progress(min(t, duration), duration)
    code = proc.wait()
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if code != 0:
        raise RuntimeError("\n".join(tail[-4:]) or f"ffmpeg {code}")


def has_nvenc(ffmpeg):
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, timeout=30,
                             creationflags=NO_WINDOW).stdout.decode("utf-8", "replace")
        return "h264_nvenc" in out
    except (OSError, subprocess.SubprocessError):
        return False


def out_path(src, out_dir, suffix, ext):
    base = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(out_dir, base + suffix + ext)


def burn(video, ass_text, dest, duration, progress=None, cancel=None):
    """把字幕燒進畫面（重新編碼；有 NVIDIA 顯示卡就用顯示卡編碼，快很多）。
    NVENC 用 p2：實測 4K60 比 p5 快 3 倍（250 vs 79 fps），檔案大小和畫質幾乎一樣。"""
    ffmpeg, _ = ensure_ffmpeg()
    tmp = tempfile.mkdtemp(prefix="quicksub-")
    try:
        # subtitles 濾鏡對路徑裡的 : 和 \ 很敏感：字幕檔放在暫存資料夾、用相對路徑
        with open(os.path.join(tmp, "sub.ass"), "w", encoding="utf-8-sig") as f:
            f.write(ass_text)
        encoders = []
        if has_nvenc(ffmpeg):
            encoders.append(["-c:v", "h264_nvenc", "-preset", "p2", "-cq", "22", "-b:v", "0"])
        encoders.append(["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"])
        last = None
        for venc in encoders:
            for aenc in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "192k"]):
                try:
                    _run([ffmpeg, "-hide_banner", "-y", "-i", video, "-vf", "subtitles=sub.ass", *venc, *aenc,
                          "-pix_fmt", "yuv420p", "-movflags", "+faststart", dest], duration, progress, cancel, cwd=tmp)
                    return dest
                except Cancelled:
                    raise
                except RuntimeError as e:
                    last = e
        raise last
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def embed(video, srt_path, dest, lang, duration, progress=None, cancel=None):
    """把字幕放進影片當字幕軌（不重新編碼，很快；播放器裡可以開關）。mp4 用 mov_text，其他用 mkv。"""
    ffmpeg, _ = ensure_ffmpeg()
    scodec = "mov_text" if dest.lower().endswith((".mp4", ".m4v", ".mov")) else "srt"
    _run([ffmpeg, "-hide_banner", "-y", "-i", video, "-i", srt_path, "-map", "0:v?", "-map", "0:a?", "-map", "1:0",
          "-c:v", "copy", "-c:a", "copy", "-c:s", scodec, "-metadata:s:s:0", "language=" + LANG3.get(lang, "und"),
          "-disposition:s:0", "default", dest], duration, progress, cancel)
    return dest


def fetch_url(url, out_dir, progress=None, cancel=None, status=None):
    """用 yt-dlp 下載網址的影片（最高 1080p，合成 mp4）。回傳下載好的檔案路徑。"""
    ffmpeg, _ = ensure_ffmpeg()
    yt, deno = ensure_ytdlp()
    os.makedirs(out_dir, exist_ok=True)
    fd, listfile = tempfile.mkstemp(suffix=".txt", prefix="quicksub-")
    os.close(fd)
    cmd = [yt, "--no-playlist", "--newline", "--progress", "--no-colors",
           "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
           "--ffmpeg-location", os.path.dirname(ffmpeg), "--js-runtimes", "deno:" + deno, "--remote-components", "ejs:github",
           "-P", out_dir, "-o", "%(title).80B [%(id)s].%(ext)s",
           "--print-to-file", "after_move:filepath", listfile, url]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW)
    _kill_on_cancel(proc, cancel)
    tail = []
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if line:
            tail = (tail + [line])[-6:]
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m and progress:
            progress(float(m.group(1)), 100.0)
        if status and "[Merger]" in line:
            status("merge")
    code = proc.wait()
    try:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        with open(listfile, encoding="utf-8") as f:
            paths = [l.strip() for l in f if l.strip()]
        if code != 0 or not paths or not os.path.exists(paths[-1]):
            err = next((l for l in reversed(tail) if "ERROR" in l), tail[-1] if tail else "")
            raise RuntimeError(T("下載失敗：", "Download failed: ") + err[:300])
        return paths[-1]
    finally:
        try:
            os.remove(listfile)
        except OSError:
            pass


def open_player(video, sub):
    """用播放器預覽：有 VLC 就指定字幕檔；沒有就用系統預設的播放器（同檔名的字幕大多會自動載入）。"""
    vlc = shutil.which("vlc") or next((p for p in (r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                                                   r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe") if os.path.exists(p)), None)
    if vlc and video:  # --no-video-title-show：VLC 預設開頭會在下面顯示檔名，看起來像一行字幕
        subprocess.Popen([vlc, "--no-video-title-show", video, "--sub-file=" + sub], creationflags=NO_WINDOW)
    elif video and IS_WINDOWS:
        os.startfile(video)
    elif IS_WINDOWS:
        os.startfile(sub)
