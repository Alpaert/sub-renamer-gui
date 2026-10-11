from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Sequence


TOOL_FALLBACK_PATHS = {
    "ffmpeg": (Path(r"C:\Windows\System32\ffmpeg.exe"), Path(r"C:\ffmpeg\bin\ffmpeg.exe")),
    "ffprobe": (Path(r"C:\Windows\System32\ffprobe.exe"), Path(r"C:\ffmpeg\bin\ffprobe.exe")),
}

# ffmpeg 根据输出文件扩展名推断封装格式，因此扩展名必须与编码族对应。
CODEC_EXTENSIONS = {
    "ass": ".ass",
    "ssa": ".ssa",
    "subrip": ".srt",
    "srt": ".srt",
    "webvtt": ".vtt",
    "mov_text": ".srt",
    "text": ".srt",
    "eia_608": ".srt",
    "microdvd": ".sub",
    "jacosub": ".sub",
    "sami": ".smi",
    "realtext": ".rt",
    "subviewer": ".sub",
    "subviewer1": ".sub",
    "vplayer": ".txt",
    "pjs": ".pjs",
    "stl": ".stl",
    "hdmv_pgs_subtitle": ".sup",
    "dvd_subtitle": ".sub",
    "dvb_subtitle": ".sub",
    "xsub": ".sub",
}
DEFAULT_SUBTITLE_EXTENSION = ".ass"
BITMAP_SUBTITLE_CODECS = frozenset(
    {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
)

PROBE_ENTRIES = (
    "stream=index,codec_name:stream_tags=language,title:stream_disposition=default,forced"
)

_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class ToolNotFoundError(RuntimeError):
    """ffmpeg 或 ffprobe 不可用。"""


class ExtractionError(RuntimeError):
    """单条字幕轨提取失败。"""


class ExtractionStatus(str, Enum):
    READY = "ready"
    ALREADY_EXISTS = "already_exists"
    MISSING_TRACK = "missing_track"
    PROBE_FAILED = "probe_failed"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class TrackInfo:
    """一条内嵌字幕轨。ordinal 是字幕轨的相对序号，直接用于 -map 0:s:<ordinal>。"""

    ordinal: int
    codec: str = ""
    language: str = ""
    title: str = ""
    is_default: bool = False
    is_forced: bool = False


@dataclass(frozen=True)
class VideoTracks:
    video: Path
    tracks: tuple[TrackInfo, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class ExtractionItem:
    video: Path
    track: TrackInfo | None
    destination: Path | None
    status: ExtractionStatus
    detail: str = ""


@dataclass(frozen=True)
class ExtractionPlan:
    items: tuple[ExtractionItem, ...]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    overwrite: bool = False

    @property
    def ready_items(self) -> tuple[ExtractionItem, ...]:
        return tuple(item for item in self.items if item.status is ExtractionStatus.READY)

    @property
    def can_execute(self) -> bool:
        return not self.errors and all(
            item.status is not ExtractionStatus.CONFLICT for item in self.items
        )


@dataclass(frozen=True)
class ExtractionResult:
    success: bool
    extracted: tuple[ExtractionItem, ...]
    rolled_back: tuple[ExtractionItem, ...] = ()
    error: str = ""
    rollback_errors: tuple[str, ...] = ()
    cancelled: bool = False


@dataclass(frozen=True)
class SlotReport:
    """同一个层级在各视频中的一致性报告。"""

    slot: int
    signatures: tuple[tuple[str, int], ...]
    missing: tuple[Path, ...]

    @property
    def consistent(self) -> bool:
        return len(self.signatures) <= 1 and not self.missing

    @property
    def dominant(self) -> str:
        return self.signatures[0][0] if self.signatures else ""


ProbeRunner = Callable[[Path], str]
ExtractOperation = Callable[[Path, TrackInfo, Path], None]
ProbeProgress = Callable[[int, int, Path], None]
ExtractProgress = Callable[[int, int, ExtractionItem], None]
CancelCheck = Callable[[], bool]


@lru_cache(maxsize=None)
def _resolve(tool: str) -> str:
    found = shutil.which(tool)
    if found:
        return found
    for candidate in TOOL_FALLBACK_PATHS.get(tool, ()):
        if candidate.is_file():
            return str(candidate)
    raise ToolNotFoundError(
        f"未找到 {tool}。请安装 ffmpeg 并加入 PATH，或把 {tool}.exe 放到 C:\\ffmpeg\\bin\\。"
    )


def missing_tools() -> tuple[str, ...]:
    """返回当前定位不到的外部程序名，供界面在开工前给出明确提示。"""
    missing: list[str] = []
    for tool in ("ffmpeg", "ffprobe"):
        try:
            _resolve(tool)
        except ToolNotFoundError:
            missing.append(tool)
    return tuple(missing)


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # ffprobe 直接输出 UTF-8，必须显式指定编码，否则中文轨道标题会乱码。
    return subprocess.run(
        list(command),
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATION_FLAGS,
    )


def _tail(text: str, limit: int = 300) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed[-limit:] if len(collapsed) > limit else collapsed


def build_ffprobe_command(video: Path, ffprobe: str = "ffprobe") -> list[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "s",
        "-show_entries",
        PROBE_ENTRIES,
        "-of",
        "json",
        str(video),
    ]


def build_ffmpeg_command(
    video: Path, track: TrackInfo, destination: Path, ffmpeg: str = "ffmpeg"
) -> list[str]:
    encoder = subtitle_encoder(track, destination)
    codec_args = ["-c", "copy"] if encoder is None else ["-c:s", encoder]
    return [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        str(video),
        "-map",
        f"0:s:{track.ordinal}",
        *codec_args,
        str(destination),
    ]


def run_ffprobe(video: Path) -> str:
    completed = _run(build_ffprobe_command(video, _resolve("ffprobe")))
    if completed.returncode != 0:
        raise ExtractionError(
            f"ffprobe 读取失败（{video.name}）：{_tail(completed.stderr)}"
        )
    return completed.stdout


def parse_tracks(payload: str) -> tuple[TrackInfo, ...]:
    """把 ffprobe 的 JSON 输出转换成按容器顺序排列的轨道列表。"""
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"无法解析 ffprobe 输出：{exc}") from exc

    tracks: list[TrackInfo] = []
    for ordinal, stream in enumerate(data.get("streams") or ()):
        if not isinstance(stream, dict):
            continue
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        tracks.append(
            TrackInfo(
                ordinal=ordinal,
                codec=str(stream.get("codec_name") or ""),
                language=str(tags.get("language") or ""),
                title=str(tags.get("title") or ""),
                is_default=bool(disposition.get("default")),
                is_forced=bool(disposition.get("forced")),
            )
        )
    return tuple(tracks)


def probe_tracks(video: Path, runner: ProbeRunner = run_ffprobe) -> tuple[TrackInfo, ...]:
    return parse_tracks(runner(video))


def probe_folder(
    videos: Iterable[Path],
    runner: ProbeRunner = run_ffprobe,
    on_progress: ProbeProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> tuple[VideoTracks, ...]:
    """逐个读取视频的字幕轨。单个视频失败只记录错误，不打断整批。"""
    ordered = tuple(videos)
    results: list[VideoTracks] = []
    for index, video in enumerate(ordered, start=1):
        if should_cancel is not None and should_cancel():
            break
        try:
            results.append(VideoTracks(video, probe_tracks(video, runner)))
        except (OSError, ValueError, ExtractionError, ToolNotFoundError, subprocess.SubprocessError) as exc:
            results.append(VideoTracks(video, (), str(exc)))
        if on_progress is not None:
            on_progress(index, len(ordered), video)
    return tuple(results)


def max_slot_count(scanned: Iterable[VideoTracks]) -> int:
    return max((len(item.tracks) for item in scanned), default=0)


def slot_track(item: VideoTracks, slot: int) -> TrackInfo | None:
    """按 1 起的层级号取轨道；该视频没有这一层时返回 None。"""
    if 1 <= slot <= len(item.tracks):
        return item.tracks[slot - 1]
    return None


def sanitize_filename_part(text: str) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("_", text or "")
    return cleaned.strip().rstrip(". ")


def track_extension(track: TrackInfo) -> str:
    return CODEC_EXTENSIONS.get(track.codec.strip().casefold(), DEFAULT_SUBTITLE_EXTENSION)


def track_label(track: TrackInfo) -> str:
    """轨道标签：优先用标题（弹幕、简日这类信息量更大），其次语言，最后层级号。"""
    title = sanitize_filename_part(track.title)
    if title:
        return title
    language = sanitize_filename_part(track.language)
    if language:
        return language
    return f"s{track.ordinal + 1}"


def slot_signature(track: TrackInfo) -> str:
    """用于跨视频比对同一层级的归一化指纹。"""
    return "|".join(
        (
            track.language.strip().casefold(),
            track.title.strip().casefold(),
            track.codec.strip().casefold(),
        )
    )


def describe_track(track: TrackInfo) -> str:
    language = track.language.strip() or "?"
    title = track.title.strip()
    text = f"{language}·{title}" if title else language
    if track.is_forced:
        text += "（强制）"
    return text


def describe_slot(track: TrackInfo) -> str:
    return f"第 {track.ordinal + 1} 条 · {describe_track(track)}"


def check_slot_consistency(
    scanned: Iterable[VideoTracks], slots: Iterable[int]
) -> tuple[SlotReport, ...]:
    """核对每个层级在各视频中是否真是同一条轨道。"""
    ordered = tuple(scanned)
    reports: list[SlotReport] = []
    for slot in sorted(set(slots)):
        counts: dict[str, int] = {}
        missing: list[Path] = []
        for item in ordered:
            track = slot_track(item, slot)
            if track is None:
                missing.append(item.video)
                continue
            signature = slot_signature(track)
            counts[signature] = counts.get(signature, 0) + 1
        ordered_counts = tuple(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))
        reports.append(SlotReport(slot=slot, signatures=ordered_counts, missing=tuple(missing)))
    return tuple(reports)


def subtitle_encoder(track: TrackInfo, destination: Path) -> str | None:
    """目标格式与源编码族一致时返回 None（直接流复制，无损）。"""
    suffix = destination.suffix.casefold()
    if suffix == track_extension(track):
        return None
    if suffix == ".ass":
        return "ass"
    if suffix == ".srt":
        return "srt"
    return None


def output_path_for(
    video: Path,
    track: TrackInfo,
    fixed_label: str = "",
    output_dir: Path | None = None,
    convert_to_ass: bool = False,
) -> Path:
    label = sanitize_filename_part(fixed_label) or track_label(track)
    extension = ".ass" if convert_to_ass else track_extension(track)
    directory = output_dir if output_dir is not None else video.parent
    return directory / f"{video.stem}.{label}{extension}"


def _with_slot_suffix(destination: Path, slot: int) -> Path:
    return destination.with_name(f"{destination.stem}.s{slot}{destination.suffix}")


def _staging_path(destination: Path) -> Path:
    """暂存文件名保留原扩展名，ffmpeg 才能据此推断输出格式。"""
    return destination.with_name(f"{destination.stem}.part{destination.suffix}")


def build_extraction_plan(
    scanned: Iterable[VideoTracks],
    slots: Iterable[int],
    fixed_label: str = "",
    output_dir: Path | None = None,
    overwrite: bool = False,
    convert_to_ass: bool = False,
) -> ExtractionPlan:
    ordered = tuple(scanned)
    selected = tuple(sorted(set(slots)))
    errors: list[str] = []
    warnings: list[str] = []

    if not selected:
        return ExtractionPlan((), (), ("尚未选择要提取的轨道层级。",), overwrite)

    items: list[ExtractionItem] = []
    for entry in ordered:
        if entry.error:
            items.append(
                ExtractionItem(entry.video, None, None, ExtractionStatus.PROBE_FAILED, entry.error)
            )
            continue
        for slot in selected:
            track = slot_track(entry, slot)
            if track is None:
                items.append(
                    ExtractionItem(
                        entry.video,
                        None,
                        None,
                        ExtractionStatus.MISSING_TRACK,
                        f"该视频只有 {len(entry.tracks)} 条字幕轨，没有第 {slot} 条",
                    )
                )
                continue
            if convert_to_ass and track.codec.strip().casefold() in BITMAP_SUBTITLE_CODECS:
                detail = f"第 {slot} 条是图形字幕（{track.codec}），无法转换为 ASS"
                errors.append(f"{entry.video.name}：{detail}")
                items.append(
                    ExtractionItem(entry.video, track, None, ExtractionStatus.CONFLICT, detail)
                )
                continue
            items.append(
                ExtractionItem(
                    entry.video,
                    track,
                    output_path_for(entry.video, track, fixed_label, output_dir, convert_to_ass),
                    ExtractionStatus.READY,
                )
            )

    # 同一视频里多条轨道标签相同时追加层级号，避免整批因重名而无法执行。
    groups: dict[tuple[Path, str], list[int]] = {}
    for index, item in enumerate(items):
        if item.destination is not None:
            key = (item.destination.parent, item.destination.name.casefold())
            groups.setdefault(key, []).append(index)
    for indices in groups.values():
        if len(indices) < 2:
            continue
        names = "、".join(f"第 {items[index].track.ordinal + 1} 条" for index in indices)  # type: ignore[union-attr]
        warnings.append(
            f"{items[indices[0]].video.name} 的 {names} 轨道标签相同，已追加层级号区分。"
        )
        for index in indices:
            item = items[index]
            assert item.destination is not None and item.track is not None
            items[index] = replace(
                item, destination=_with_slot_suffix(item.destination, item.track.ordinal + 1)
            )

    for index, item in enumerate(items):
        if item.status is not ExtractionStatus.READY or item.destination is None:
            continue
        if item.destination.exists() and not overwrite:
            detail = f"目标文件已存在：{item.destination.name}"
            warnings.append(f"{item.video.name}：{detail}")
            items[index] = replace(item, status=ExtractionStatus.ALREADY_EXISTS, detail=detail)

    return ExtractionPlan(
        items=tuple(items),
        errors=tuple(dict.fromkeys(errors)),
        warnings=tuple(dict.fromkeys(warnings)),
        overwrite=overwrite,
    )


def extract_track_ffmpeg(video: Path, track: TrackInfo, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_path(destination)
    try:
        completed = _run(build_ffmpeg_command(video, track, staging, _resolve("ffmpeg")))
        if completed.returncode != 0:
            raise ExtractionError(
                f"ffmpeg 提取第 {track.ordinal + 1} 条字幕轨失败：{_tail(completed.stderr)}"
            )
        if not staging.is_file():
            raise ExtractionError("ffmpeg 未生成输出文件。")
        os.replace(staging, destination)
    finally:
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass


def execute_extraction_plan(
    plan: ExtractionPlan,
    extract_operation: ExtractOperation = extract_track_ffmpeg,
    on_progress: ExtractProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> ExtractionResult:
    if not plan.can_execute:
        return ExtractionResult(False, (), error="提取计划包含冲突，未执行任何操作。")

    ready = plan.ready_items
    if not ready:
        return ExtractionResult(True, ())

    seen: set[tuple[Path, str]] = set()
    for item in ready:
        assert item.destination is not None and item.track is not None
        if not item.video.is_file():
            return ExtractionResult(False, (), error=f"视频文件已不存在：{item.video.name}")
        key = (item.destination.parent, item.destination.name.casefold())
        if key in seen:
            return ExtractionResult(False, (), error=f"目标文件重复：{item.destination.name}")
        seen.add(key)

    completed: list[ExtractionItem] = []

    def rollback() -> tuple[tuple[ExtractionItem, ...], tuple[str, ...]]:
        if plan.overwrite:
            # 覆盖模式下产物可能已经替换掉用户原有文件，删除只会损失更多，于是保留。
            return (), ()
        rolled: list[ExtractionItem] = []
        problems: list[str] = []
        for item in reversed(completed):
            assert item.destination is not None
            try:
                item.destination.unlink(missing_ok=True)
                rolled.append(item)
            except OSError as exc:
                problems.append(f"{item.destination.name}：{exc}")
        return tuple(rolled), tuple(problems)

    def failure(error: str, cancelled: bool = False) -> ExtractionResult:
        rolled, problems = rollback()
        if plan.overwrite and completed:
            error += f"（已勾选覆盖，本次产出的 {len(completed)} 个文件已保留）"
        return ExtractionResult(
            False,
            tuple(completed),
            rolled,
            error=error,
            rollback_errors=problems,
            cancelled=cancelled,
        )

    try:
        for index, item in enumerate(ready, start=1):
            if should_cancel is not None and should_cancel():
                return failure("已取消，本次产出的文件已清理。", cancelled=True)
            assert item.destination is not None and item.track is not None
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            extract_operation(item.video, item.track, item.destination)
            completed.append(item)
            if on_progress is not None:
                on_progress(index, len(ready), item)
    except (OSError, ExtractionError) as exc:
        return failure(str(exc))

    return ExtractionResult(True, tuple(completed))
