---
name: quicksub
description: Turn speech in local videos, audio files or video URLs into subtitles with QuickSub (快字幕) — faster-whisper on the GPU, Traditional/Simplified Chinese conversion, smart line breaking — and plug the result into an LLM workflow: get a timed transcript as JSON, translate/proofread/summarize it with your own model, then burn the edited subtitles into the video or add them as a track. Use when asked to transcribe, caption or subtitle a video, make SRT/VTT/ASS files, translate subtitles, hardsub/burn or embed subtitles, or summarize what a video says (字幕, 上字幕, 聽打, 逐字稿, 翻譯字幕, 燒字幕).
---

# QuickSub (快字幕) for agents

QuickSub is a desktop subtitle app with a **command-line mode** for scripts and AI agents:
nothing opens on screen, progress goes to **stderr**, and the result is **one JSON object on stdout**.
Exit code: `0` all OK · `1` something failed (see `"error"`) · `2` bad arguments.

## 1. Find the command

| How QuickSub is installed | Command prefix |
|---|---|
| Windows installer (default) | `"%LOCALAPPDATA%\Programs\QuickSub\quicksub-cli.exe"` |
| Windows portable zip | `"<unzipped folder>\QuickSub\quicksub-cli.exe"` |
| From source (any OS, `pip install -r requirements.txt`) | `python quicksub.pyw --cli` (run in the repo folder) |

If the user set `QUICKSUB_CLI`, use that. Bash on Windows: `"$LOCALAPPDATA/Programs/QuickSub/quicksub-cli.exe"`.
Don't run `QuickSub.exe` itself — it is a GUI program, so you can't read its output.
Not installed? Download `QuickSub-Setup-*.exe` from https://github.com/Benjaminwz/quicksub/releases (ask the user first).

Always start with `info` — it is instant and tells you what will be slow:

```
quicksub-cli info
→ {"ok":true,"version":"1.2.0","gpu":true,"gpu_files_ready":true,"model":"large-v3-turbo",
   "models":[{"id":"large-v3-turbo","size_mb":1620,"downloaded":true},…],"ffmpeg":true,
   "translation":{"engine":"ollama","ollama_running":false,"api_key_set":false,…},"translate_targets":["zh-TW","en",…]}
```

First use downloads the speech model (`size_mb`), NVIDIA GPU files (~1.3 GB, only if `gpu` and not `gpu_files_ready`)
and ffmpeg. Use a long timeout (10+ min) or run it in the background then. After that, a modern NVIDIA GPU usually
needs only a few minutes for an hour of audio; CPU-only is much slower (`-m small` trades accuracy for speed).
For long videos, run the command in the background and wait for it rather than using a short timeout.

## 2. Commands

### transcribe — speech → subtitles

```
quicksub-cli transcribe <file or URL> [more…] [options]
```

| Option | Meaning |
|---|---|
| `--segments` | **Put every subtitle line in the JSON.** Use this whenever you'll process the text. |
| `-f srt,vtt,ass,txt` | Files to write (default `srt`). `txt` = plain text, one line per subtitle. |
| `-o DIR` | Output folder (default: next to the video). |
| `-l zh` | Spoken language if known (`auto` default; `zh`, `en`, `ja`, `ko`, `yue`…). |
| `--convert s2twp\|s2hk\|t2s\|none` | Chinese output script: Traditional-Taiwan (default), Traditional-HK, Simplified, as heard. |
| `--hotwords "名字, Term"` | Names/jargon so the model spells them right. |
| `--max-chars N` `--max-dur S` | Line length (0 = auto: 18 CJK / 42 Latin) and max seconds per subtitle (6). |
| `-t LANG` | Built-in translation (see §4). `--only-translation`, `--translation-first` change the layout. |
| `--burn` / `--embed` | Also make a hardsubbed copy / add a subtitle track (no re-encode). |
| `-m MODEL` `--cpu` | Speech model (`large-v3-turbo`, `large-v3`, `medium`, `small`, `base`, `tiny`); skip the GPU. |
| `--json PATH` `-q` | Also save the JSON to a file; no progress lines. |

URLs (YouTube and most video sites) are downloaded first to the user's `Videos\QuickSub` folder.

```json
{"ok":true,"version":"1.2.0","results":[{"input":"talk.mp4","ok":true,"path":"C:\\…\\talk.mp4",
  "language":"en","layout":"orig","files":{"srt":"C:\\…\\talk.en.srt"},
  "segments":[{"start":0.0,"end":1.1,"text":"Welcome to QuickSub."},
              {"start":1.61,"end":3.17,"text":"This short clip tests"}]}]}
```

Each input gets its own result; one failing (`"ok":false,"error":"…"`) doesn't stop the others.
With translation, segments also have `"translation"`. `"note"` explains partial success (e.g. translation failed,
original subtitles saved). Files are named `<video>.<lang>.<fmt>` so players load them automatically.

### burn / embed — put a subtitle file into the video

```
quicksub-cli burn  <video> <subs.srt|.vtt|.ass> [-o out.mp4] [--font-size PX]
quicksub-cli embed <video> <subs.srt|.vtt|.ass> [-o out.mkv] [--lang zh-TW]
→ {"ok":true,"version":"1.2.0","file":"C:\\…\\Subtitled videos\\talk.hardsub.mp4"}
```

`burn` re-encodes (NVENC when available) with white outlined text sized to the video; a cue with **two lines is drawn as
original + smaller translation underneath**. `embed` is fast and lossless (mp4 → mov_text track, otherwise mkv).
Default output goes to a `字幕影片` / `Subtitled videos` subfolder so players don't stack the loose .srt on top.

## 3. Workflow: your model does the language work

QuickSub does the audio (timing, recognition, line breaks, rendering); you do the language. This is usually better
than the built-in translator because you know the context and the user's wishes.

1. `transcribe video.mp4 --segments -q` → read `results[0].segments`.
2. Work on the text, keeping **the same number of segments, in order, with the same `start`/`end`**:
   translate, fix misheard names, clean up filler words, adapt tone — or summarize / take notes / answer questions
   (stop here if the user only wanted information from the video).
3. Write an SRT. One block per segment; times `HH:MM:SS,mmm`; blank line between blocks; UTF-8:
   ```
   1
   00:00:00,000 --> 00:00:01,100
   Welcome to QuickSub.
   歡迎使用快字幕。

   ```
   Line 1 = original, line 2 = translation (bilingual), or a single line for translation-only.
   Keep lines short (≈18 CJK or ≈42 Latin characters); if a translation runs long, shorten it rather than adding lines.
4. `burn video.mp4 video.zh-TW.srt` (or `embed`) → give the user `file`.

For long videos, translate in batches of ~50 segments and keep a running glossary of names so wording stays consistent.
To fix recognition itself (not just text), re-run `transcribe` with `--hotwords`.

## 4. Built-in translation (no agent tokens)

`-t zh-TW|en|ja|ko|zh-CN|fr|de|es|th|vi|id` translates with whatever the user set up in the app, or override it:

- Local Ollama: `--engine ollama --ollama-model qwen2.5:7b` (Ollama must be running; `info` shows the models).
- Any OpenAI-compatible API: `--api-preset gemini|deepseek|openai`, or `--api-base https://…/v1 --api-model NAME`.
  Pass the key in the `QUICKSUB_API_KEY` environment variable rather than `--api-key`, and never print it.

Default output is bilingual (original + translation). If translation fails the original subtitles are still saved and
`note` says why.

## Good to know

- The command line reuses the app's saved model, GPU and translation settings, but every other option starts from the
  defaults each run — and it never changes the user's settings.
- Progress text on stderr follows the system language (Chinese on a Chinese Windows); only parse stdout.
- Audio-only inputs work for transcription; `--burn`/`--embed` need a video stream.
- `"error":"…No speech was found"` / `沒有聽到說話的聲音` means silence or music only — try `--no-vad` or the right `-l`.
- GPU failure falls back to the CPU automatically (slower); details are in `%LOCALAPPDATA%\QuickSub\worker.log`.
