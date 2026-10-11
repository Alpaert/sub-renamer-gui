from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from renamer_core import (
    ItemStatus,
    build_rename_plan,
    execute_rename_plan,
    extract_episode,
    scan_directory,
    selected_numeric_field,
    subtitle_language_suffix,
)


class RenamerCoreTests(unittest.TestCase):
    def test_selected_numeric_field_requires_exact_span(self) -> None:
        name = "Anime.S01E03.1080p.mkv"
        self.assertEqual(selected_numeric_field(name, 10, 12), (1, "03"))
        with self.assertRaises(ValueError):
            selected_numeric_field(name, 11, 12)

    def test_extracts_same_numeric_position_with_different_prefix_lengths(self) -> None:
        self.assertEqual(extract_episode("A.S01E03.mkv", 1), 3)
        self.assertEqual(extract_episode("Much Longer Anime.S01E12.mkv", 1), 12)

    def test_language_suffix_recognition(self) -> None:
        self.assertEqual(subtitle_language_suffix("Anime.03.zh-CN.ass"), ".zh-CN")
        self.assertEqual(subtitle_language_suffix("Anime.03.CHS.srt"), ".CHS")
        self.assertEqual(subtitle_language_suffix("Anime.03.release.ass"), "")
        self.assertEqual(subtitle_language_suffix("Anime_03.ass"), "")

    def test_scan_is_current_directory_only_and_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "2.MKV").touch()
            (folder / "1.mp4").touch()
            (folder / "1.ASS").touch()
            (folder / "notes.txt").touch()
            child = folder / "child"
            child.mkdir()
            (child / "3.mkv").touch()
            result = scan_directory(folder)
            self.assertEqual([path.name for path in result.videos], ["1.mp4", "2.MKV"])
            self.assertEqual([path.name for path in result.subtitles], ["1.ASS"])

    def test_standardizes_leading_zero_and_preserves_multiple_languages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "Show E03 1080p.mkv"
            simplified = folder / "Sub_3.zh-CN.ass"
            traditional = folder / "Sub_03.cht.ass"
            for path in (video, simplified, traditional):
                path.touch()
            plan = build_rename_plan((video,), (simplified, traditional), 0, 0)
            self.assertTrue(plan.can_execute)
            self.assertEqual(
                {item.target.name for item in plan.ready_items if item.target},
                {"Show E03 1080p.zh-CN.ass", "Show E03 1080p.cht.ass"},
            )

    def test_untagged_subtitle_keeps_only_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "Show 01.mkv"
            subtitle = folder / "Subtitle 1 release.srt"
            video.touch()
            subtitle.touch()
            plan = build_rename_plan((video,), (subtitle,), 0, 0)
            self.assertEqual(plan.items[0].target.name, "Show 01.srt")

    def test_unmatched_and_missing_field_are_left_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "Show 01.mkv"
            unmatched = folder / "Sub 02.ass"
            invalid = folder / "Special.ass"
            for path in (video, unmatched, invalid):
                path.touch()
            plan = build_rename_plan((video,), (unmatched, invalid), 0, 0)
            self.assertEqual(
                [item.status for item in plan.items],
                [ItemStatus.UNMATCHED, ItemStatus.INVALID],
            )
            self.assertTrue(plan.can_execute)

    def test_duplicate_video_episode_blocks_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            videos = (folder / "A 01.mkv", folder / "B 1.mp4")
            subtitle = folder / "Sub 01.ass"
            for path in (*videos, subtitle):
                path.touch()
            plan = build_rename_plan(videos, (subtitle,), 0, 0)
            self.assertFalse(plan.can_execute)
            self.assertIn("视频集数 1 重复", plan.errors[0])

    def test_existing_target_blocks_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "Show 01.mkv"
            subtitle = folder / "Sub 1.ass"
            existing = folder / "Show 01.ass"
            for path in (video, subtitle, existing):
                path.touch()
            plan = build_rename_plan((video,), (subtitle,), 0, 0)
            self.assertFalse(plan.can_execute)
            self.assertEqual(plan.items[0].status, ItemStatus.CONFLICT)

    def test_same_target_from_two_subtitles_is_a_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "Show 01.mkv"
            subtitles = (folder / "A 1.ass", folder / "B 01.ass")
            for path in (video, *subtitles):
                path.touch()
            plan = build_rename_plan((video,), subtitles, 0, 0)
            self.assertFalse(plan.can_execute)
            self.assertTrue(all(item.status is ItemStatus.CONFLICT for item in plan.items))

    def test_already_correct_is_not_renamed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "Show 01.mkv"
            subtitle = folder / "Show 01.zh-CN.ass"
            video.touch()
            subtitle.touch()
            plan = build_rename_plan((video,), (subtitle,), 0, 0)
            self.assertEqual(plan.items[0].status, ItemStatus.ALREADY_CORRECT)
            self.assertEqual(plan.ready_items, ())

    def test_execution_renames_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "Show 01.mkv"
            subtitle = folder / "Sub 1.srt"
            video.touch()
            subtitle.write_text("subtitle", encoding="utf-8")
            plan = build_rename_plan((video,), (subtitle,), 0, 0)
            result = execute_rename_plan(plan)
            self.assertTrue(result.success)
            self.assertFalse(subtitle.exists())
            self.assertEqual((folder / "Show 01.srt").read_text(encoding="utf-8"), "subtitle")

    def test_failure_rolls_back_completed_renames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            videos = (folder / "Show 01.mkv", folder / "Show 02.mkv")
            subtitles = (folder / "Sub 1.ass", folder / "Sub 2.ass")
            for path in (*videos, *subtitles):
                path.touch()
            plan = build_rename_plan(videos, subtitles, 0, 0)
            forward_calls = 0

            def fail_second_forward(source: Path, target: Path) -> None:
                nonlocal forward_calls
                if source.name.startswith("Sub"):
                    forward_calls += 1
                    if forward_calls == 2:
                        raise OSError("simulated failure")
                source.rename(target)

            result = execute_rename_plan(plan, fail_second_forward)
            self.assertFalse(result.success)
            self.assertEqual(len(result.renamed), 1)
            self.assertEqual(len(result.rolled_back), 1)
            self.assertTrue(all(path.exists() for path in subtitles))


if __name__ == "__main__":
    unittest.main()
