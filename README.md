# Subtitle Batch Renamer

**English** | [简体中文](README.zh-CN.md)

A zero-dependency Windows Python GUI tool. It matches media files with subtitles by episode number and renames each subtitle to the media file's base name, so PotPlayer can load external subtitles automatically.

It can also batch-extract embedded subtitle / danmaku (bullet comment) tracks from MKV files by "track level" — see [Batch extracting embedded subtitles / danmaku](#batch-extracting-embedded-subtitles--danmaku) below.

## Running

Requires Python 3.10 or later, with Tkinter available (it is included by default in the official Windows installer).

For everyday use, just double-click `字幕批量重命名.pyw` (rename) or `弹幕批量提取.pyw` (extract). Windows launches them through `pythonw.exe`, so only the GUI appears and no console window shows up. Both features can also share a single window — the rename window has a "弹幕/字幕提取..." button in its top-right corner.

> **Extraction requires ffmpeg.** The program looks for it in `PATH`, then `C:\Windows\System32`, then `C:\ffmpeg\bin`.
> If it cannot be found, a dialog appears and the rename feature is unaffected.

If the program fails to start, run the following in PowerShell to see the error:

```powershell
python subtitle_renamer.py
python subtitle_extractor.py
```

## Steps

1. Choose a folder containing both media and subtitles. Only that folder is scanned; subdirectories are not recursed into.
2. Pick one media file and one subtitle file as samples.
3. In each of the two full file names, drag with the mouse to select a complete episode number, then click "确认所选集数" (Confirm selected episode number).
4. Click "生成预览" (Generate preview) and check each subtitle's old name, new name and status.
5. Once you confirm there are no conflicts, click "执行重命名" (Run rename).

The program records which numeric field of the file name the selected number is. For example, selecting `03` in `Anime.S01E03.1080p.mkv` means using the second numeric field; the other media files in the same batch also use their second numeric field. Episode comparison ignores leading zeros, so `03` matches `3`.

## Naming rules

- The new subtitle name is "media base name + subtitle language tag + subtitle extension".
- Recognised language tags include `zh`, `zh-CN`, `zh-Hans`, `zh-TW`, `zh-Hant`, `chs`, `cht`, `sc`, `tc`, `eng`, `en`, `jpn`, `ja`.
- When no language tag is recognised, only the subtitle extension is kept.
- A single episode may have subtitles in several languages or formats, but the final target names must not collide.
- Non-integer episode numbers such as `OVA`, `SP` and `12.5` are not supported as the match field.

Supported video formats: `mkv`, `mp4`, `avi`, `mov`, `wmv`, `flv`, `webm`, `m4v`, `ts`.

Supported subtitle formats: `ass`, `ssa`, `srt`, `vtt`, `sub`.

## Batch extracting embedded subtitles / danmaku

Episodes of the same series are usually encoded by the same release group, and **the track order is fixed**: if track 3 is the danmaku track in episode 1, it is still the danmaku track in episode 12. So there is no need to guess from keywords here — just pick the "track level" and apply it to the whole batch.

### Steps

1. Choose the folder holding the videos and click "扫描" (Scan). Only that folder is scanned; subdirectories are not recursed into.
2. When the scan finishes, the "层级核对" (track level check) table lists what subtitle track 1, 2, ... of each video actually is (for example `chi·弹幕`).
3. Under "要提取的轨道层级" (track levels to extract), tick the levels you need; multiple levels can be selected. Ticking "全部字幕轨道" (all subtitle tracks) selects every level at once.
4. Click "生成预览" (Generate preview) and check the status, track and output file name.
5. Once confirmed, click "开始提取" (Start extraction). Extraction runs in the background and can be cancelled at any time.

### Track level check

A fixed track order is only the usual case. If a video's levels differ from the rest of the batch (different order, different encoding, or simply a missing level), that row is highlighted in **amber** and a hint is shown at the bottom; it is confirmed once more when the preview is generated. This is the safeguard against "episode 7 has a different order and the wrong track is silently extracted".

### Naming rules

- By default the output goes next to the video, named "video base name + track label + extension", for example `EP01.弹幕.ass`.
- The track label prefers the track title (`弹幕`, `简日`); when there is no title it uses the language (`zh-CN`); only when both are missing does it use the level number such as `s3`.
- If you enter a name in "统一命名标签" (unified naming label), the whole batch uses it — handy for keeping things tidy when extracting a single track.
- When labels collide across several tracks of the same video, the level number is appended automatically (`EP01.chi.s1.ass`, `EP01.chi.s2.ass`), so the batch is never blocked by it.

### Output format

- By default the **original format is kept** (stream copy, `-c copy`): lossless, fastest, and danmaku positioning and effects are preserved intact.
- The extension follows each track's actual codec, so within one batch an `srt` track outputs `.srt` and an `ass` track outputs `.ass`.
- With "统一转 ASS" (convert everything to ASS) ticked, all tracks are converted to `.ass`. Graphical subtitles (PGS / VobSub) cannot be converted; they are marked as conflicts, which blocks execution.

### Safety behaviour

- A full pre-check of the batch runs before any extraction; when there is a conflict, nothing is executed.
- If the target file already exists it is skipped by default with a notice; ticking "覆盖已存在文件" (overwrite existing files) overwrites it.
- Extraction first writes a `.part` temporary file and only replaces the target after success, so an interruption never leaves a half-written file.
- If a failure or cancellation happens mid-run, the files produced by this run are deleted (**except in overwrite mode**: deleting would only lose you more, so the produced files are kept with a clear notice).

## Safety behaviour

- A full pre-check of the batch runs before any renaming.
- Execution is blocked when episode numbers are duplicated, target files already exist, or several subtitles produce the same target.
- Subtitles whose episode cannot be extracted, or which have no corresponding video, are left untouched and shown in the preview.
- If a filesystem error occurs mid-run, completed renames are rolled back in reverse order as far as possible.

## Tests

```powershell
python -m unittest -v
```
