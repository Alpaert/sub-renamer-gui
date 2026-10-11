from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable


VIDEO_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
)
SUBTITLE_EXTENSIONS = frozenset({".ass", ".ssa", ".srt", ".vtt", ".sub"})
LANGUAGE_TAGS = frozenset(
    {
        "zh",
        "zh-cn",
        "zh-hans",
        "zh-tw",
        "zh-hant",
        "chs",
        "cht",
        "sc",
        "tc",
        "eng",
        "en",
        "jpn",
        "ja",
    }
)

_NUMBER_PATTERN = re.compile(r"\d+")


class ItemStatus(str, Enum):
    READY = "ready"
    ALREADY_CORRECT = "already_correct"
    UNMATCHED = "unmatched"
    INVALID = "invalid"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ScanResult:
    videos: tuple[Path, ...]
    subtitles: tuple[Path, ...]


@dataclass(frozen=True)
class RenameItem:
    source: Path
    target: Path | None
    episode: int | None
    status: ItemStatus
    detail: str = ""


@dataclass(frozen=True)
class RenamePlan:
    items: tuple[RenameItem, ...]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ready_items(self) -> tuple[RenameItem, ...]:
        return tuple(item for item in self.items if item.status is ItemStatus.READY)

    @property
    def can_execute(self) -> bool:
        return not self.errors and all(
            item.status is not ItemStatus.CONFLICT for item in self.items
        )


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    renamed: tuple[RenameItem, ...]
    rolled_back: tuple[RenameItem, ...] = ()
    error: str = ""
    rollback_errors: tuple[str, ...] = ()


RenameOperation = Callable[[Path, Path], None]


def _natural_sort_key(path: Path) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", path.name)
    )


def scan_directory(directory: Path | str) -> ScanResult:
    folder = Path(directory)
    if not folder.is_dir():
        raise ValueError(f"不是有效文件夹：{folder}")

    files = [entry for entry in folder.iterdir() if entry.is_file()]
    videos = tuple(
        sorted(
            (path for path in files if path.suffix.casefold() in VIDEO_EXTENSIONS),
            key=_natural_sort_key,
        )
    )
    subtitles = tuple(
        sorted(
            (path for path in files if path.suffix.casefold() in SUBTITLE_EXTENSIONS),
            key=_natural_sort_key,
        )
    )
    return ScanResult(videos=videos, subtitles=subtitles)


def numeric_fields(filename: str) -> tuple[tuple[int, int, str], ...]:
    """Return start, end and text for each numeric field in a complete filename."""
    return tuple((match.start(), match.end(), match.group()) for match in _NUMBER_PATTERN.finditer(filename))


def selected_numeric_field(filename: str, start: int, end: int) -> tuple[int, str]:
    """Validate an exact selection and return its zero-based numeric-field index."""
    for index, (field_start, field_end, text) in enumerate(numeric_fields(filename)):
        if start == field_start and end == field_end:
            return index, text
    raise ValueError("请完整且仅框选一段连续数字，例如 03。")


def extract_episode(filename: str, field_index: int) -> int:
    fields = numeric_fields(filename)
    if field_index < 0 or field_index >= len(fields):
        raise ValueError(f"文件名中没有第 {field_index + 1} 个数字字段")
    return int(fields[field_index][2])


def subtitle_language_suffix(filename: str) -> str:
    """Return the final recognized dot-delimited language tag, preserving its spelling."""
    path = Path(filename)
    stem = path.stem
    if "." not in stem:
        return ""
    tag = stem.rsplit(".", 1)[1]
    return f".{tag}" if tag.casefold() in LANGUAGE_TAGS else ""


def _directory_names(paths: Iterable[Path]) -> dict[Path, dict[str, Path]]:
    directories = {path.parent for path in paths}
    return {
        directory: {
            entry.name.casefold(): entry
            for entry in directory.iterdir()
            if entry.is_file()
        }
        for directory in directories
    }


def build_rename_plan(
    videos: Iterable[Path],
    subtitles: Iterable[Path],
    video_field_index: int,
    subtitle_field_index: int,
) -> RenamePlan:
    videos = tuple(videos)
    subtitles = tuple(subtitles)
    errors: list[str] = []
    warnings: list[str] = []
    videos_by_episode: dict[int, Path] = {}
    duplicate_episodes: set[int] = set()

    for video in videos:
        try:
            episode = extract_episode(video.name, video_field_index)
        except ValueError as exc:
            warnings.append(f"视频无法提取集数：{video.name}（{exc}）")
            continue
        if episode in videos_by_episode:
            duplicate_episodes.add(episode)
        else:
            videos_by_episode[episode] = video

    for episode in sorted(duplicate_episodes):
        matches = []
        for video in videos:
            try:
                if extract_episode(video.name, video_field_index) == episode:
                    matches.append(video.name)
            except ValueError:
                pass
        errors.append(f"视频集数 {episode} 重复：{'、'.join(matches)}")

    items: list[RenameItem] = []
    for subtitle in subtitles:
        try:
            episode = extract_episode(subtitle.name, subtitle_field_index)
        except ValueError as exc:
            items.append(
                RenameItem(
                    source=subtitle,
                    target=None,
                    episode=None,
                    status=ItemStatus.INVALID,
                    detail=str(exc),
                )
            )
            continue

        video = videos_by_episode.get(episode)
        if video is None or episode in duplicate_episodes:
            detail = "对应视频集数重复" if episode in duplicate_episodes else "没有对应集数的视频"
            items.append(
                RenameItem(
                    source=subtitle,
                    target=None,
                    episode=episode,
                    status=ItemStatus.UNMATCHED,
                    detail=detail,
                )
            )
            continue

        language = subtitle_language_suffix(subtitle.name)
        target = subtitle.with_name(f"{video.stem}{language}{subtitle.suffix}")
        status = (
            ItemStatus.ALREADY_CORRECT
            if subtitle.name.casefold() == target.name.casefold()
            else ItemStatus.READY
        )
        items.append(
            RenameItem(
                source=subtitle,
                target=target,
                episode=episode,
                status=status,
            )
        )

    target_groups: dict[tuple[Path, str], list[int]] = {}
    for index, item in enumerate(items):
        if item.target is not None:
            key = (item.target.parent, item.target.name.casefold())
            target_groups.setdefault(key, []).append(index)

    for indices in target_groups.values():
        if len(indices) > 1:
            sources = "、".join(items[index].source.name for index in indices)
            target_name = items[indices[0]].target.name  # type: ignore[union-attr]
            detail = f"多个字幕会生成同一目标 {target_name}：{sources}"
            errors.append(detail)
            for index in indices:
                items[index] = replace(items[index], status=ItemStatus.CONFLICT, detail=detail)

    file_names = _directory_names(
        item.source for item in items if item.target is not None
    ) if any(item.target is not None for item in items) else {}
    for index, item in enumerate(items):
        if item.status is not ItemStatus.READY or item.target is None:
            continue
        existing = file_names[item.target.parent].get(item.target.name.casefold())
        if existing is not None and existing.name.casefold() != item.source.name.casefold():
            detail = f"目标文件已存在：{existing.name}"
            errors.append(detail)
            items[index] = replace(item, status=ItemStatus.CONFLICT, detail=detail)

    return RenamePlan(
        items=tuple(items),
        errors=tuple(dict.fromkeys(errors)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _rename(source: Path, target: Path) -> None:
    source.rename(target)


def execute_rename_plan(
    plan: RenamePlan,
    rename_operation: RenameOperation = _rename,
) -> ExecutionResult:
    if not plan.can_execute:
        return ExecutionResult(False, (), error="重命名计划包含冲突，未执行任何操作。")

    ready = plan.ready_items
    if not ready:
        return ExecutionResult(True, ())

    target_keys: set[tuple[Path, str]] = set()
    for item in ready:
        assert item.target is not None
        if not item.source.is_file():
            return ExecutionResult(False, (), error=f"源文件已不存在：{item.source.name}")
        key = (item.target.parent, item.target.name.casefold())
        if key in target_keys:
            return ExecutionResult(False, (), error=f"目标名称重复：{item.target.name}")
        target_keys.add(key)
        existing = next(
            (
                path
                for path in item.target.parent.iterdir()
                if path.is_file() and path.name.casefold() == item.target.name.casefold()
            ),
            None,
        )
        if existing is not None:
            return ExecutionResult(False, (), error=f"目标文件已存在：{existing.name}")

    completed: list[RenameItem] = []
    try:
        for item in ready:
            assert item.target is not None
            rename_operation(item.source, item.target)
            completed.append(item)
    except OSError as exc:
        rolled_back: list[RenameItem] = []
        rollback_errors: list[str] = []
        for item in reversed(completed):
            assert item.target is not None
            try:
                rename_operation(item.target, item.source)
                rolled_back.append(item)
            except OSError as rollback_exc:
                rollback_errors.append(
                    f"{item.target.name} -> {item.source.name}：{rollback_exc}"
                )
        return ExecutionResult(
            False,
            tuple(completed),
            tuple(rolled_back),
            error=str(exc),
            rollback_errors=tuple(rollback_errors),
        )

    return ExecutionResult(True, tuple(completed))
