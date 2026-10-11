from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from extractor_core import (
    ExtractionError,
    ExtractionStatus,
    TrackInfo,
    VideoTracks,
    build_extraction_plan,
    build_ffmpeg_command,
    build_ffprobe_command,
    check_slot_consistency,
    describe_track,
    execute_extraction_plan,
    max_slot_count,
    parse_tracks,
    probe_folder,
    sanitize_filename_part,
    slot_track,
    subtitle_encoder,
    track_extension,
    track_label,
)


def stream(codec: str, language: str = "", title: str = "", forced: int = 0) -> dict:
    tags = {}
    if language:
        tags["language"] = language
    if title:
        tags["title"] = title
    return {
        "index": 0,
        "codec_name": codec,
        "disposition": {"default": 0, "forced": forced},
        "tags": tags,
    }


def payload(*streams: dict) -> str:
    return json.dumps({"streams": list(streams)})


def runner_for(mapping: dict[str, str]):
    def runner(video: Path) -> str:
        if video.name not in mapping:
            raise ExtractionError("模拟读取失败")
        return mapping[video.name]

    return runner


def video_tracks(name: str, *tracks: TrackInfo, error: str = "") -> VideoTracks:
    return VideoTracks(Path(name), tracks, error)


DANMAKU = TrackInfo(ordinal=2, codec="ass", language="chi", title="弹幕")
SIMPLIFIED = TrackInfo(ordinal=0, codec="ass", language="chi", title="简日")


class ParseTracksTests(unittest.TestCase):
    def test_reads_codec_language_title_and_disposition(self) -> None:
        tracks = parse_tracks(
            payload(stream("ass", "chi", "弹幕", forced=1), stream("subrip", "eng"))
        )
        self.assertEqual([track.ordinal for track in tracks], [0, 1])
        self.assertEqual(tracks[0].title, "弹幕")
        self.assertTrue(tracks[0].is_forced)
        self.assertEqual(tracks[1].language, "eng")
        self.assertEqual(tracks[1].title, "")

    def test_missing_or_empty_payload_yields_no_tracks(self) -> None:
        self.assertEqual(parse_tracks(""), ())
        self.assertEqual(parse_tracks('{"streams": []}'), ())

    def test_invalid_json_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_tracks("not json")

    def test_probe_folder_records_failure_without_aborting_batch(self) -> None:
        videos = (Path("good.mkv"), Path("broken.mkv"), Path("other.mkv"))
        runner = runner_for({"good.mkv": payload(stream("ass")), "other.mkv": payload()})
        scanned = probe_folder(videos, runner)
        self.assertEqual(len(scanned), 3)
        self.assertEqual(len(scanned[0].tracks), 1)
        self.assertEqual(scanned[1].tracks, ())
        self.assertIn("模拟读取失败", scanned[1].error)
        self.assertEqual(scanned[2].error, "")


class SlotTests(unittest.TestCase):
    def test_slot_track_is_one_based(self) -> None:
        entry = video_tracks("a.mkv", SIMPLIFIED, DANMAKU)
        self.assertIs(slot_track(entry, 1), SIMPLIFIED)
        self.assertIs(slot_track(entry, 2), DANMAKU)
        self.assertIsNone(slot_track(entry, 3))
        self.assertIsNone(slot_track(entry, 0))

    def test_max_slot_count_uses_the_longest_video(self) -> None:
        scanned = (video_tracks("a.mkv", SIMPLIFIED), video_tracks("b.mkv", SIMPLIFIED, DANMAKU))
        self.assertEqual(max_slot_count(scanned), 2)
        self.assertEqual(max_slot_count(()), 0)

    def test_consistency_flags_missing_and_divergent_slots(self) -> None:
        scanned = (
            video_tracks("a.mkv", SIMPLIFIED, DANMAKU),
            video_tracks("b.mkv", SIMPLIFIED, DANMAKU),
            video_tracks("c.mkv", SIMPLIFIED),
        )
        first, second = check_slot_consistency(scanned, (1, 2))
        self.assertTrue(first.consistent)
        self.assertFalse(second.consistent)
        self.assertEqual(second.missing, (Path("c.mkv"),))

    def test_consistency_detects_swapped_track_order(self) -> None:
        swapped = TrackInfo(ordinal=1, codec="ass", language="jpn", title="日文")
        scanned = (video_tracks("a.mkv", SIMPLIFIED, DANMAKU), video_tracks("b.mkv", SIMPLIFIED, swapped))
        (second,) = check_slot_consistency(scanned, (2,))
        self.assertFalse(second.consistent)
        self.assertEqual(len(second.signatures), 2)


class LabellingTests(unittest.TestCase):
    def test_title_wins_over_language(self) -> None:
        self.assertEqual(track_label(DANMAKU), "弹幕")

    def test_language_is_used_when_title_is_absent(self) -> None:
        self.assertEqual(track_label(TrackInfo(ordinal=0, language="zh-CN")), "zh-CN")

    def test_slot_number_is_the_last_resort(self) -> None:
        self.assertEqual(track_label(TrackInfo(ordinal=4)), "s5")

    def test_illegal_windows_characters_are_replaced(self) -> None:
        self.assertEqual(sanitize_filename_part('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(track_label(TrackInfo(ordinal=0, title="a/b")), "a_b")

    def test_describe_track_shows_language_and_title(self) -> None:
        self.assertEqual(describe_track(DANMAKU), "chi·弹幕")
        self.assertEqual(describe_track(TrackInfo(ordinal=0, language="eng")), "eng")
        self.assertEqual(describe_track(TrackInfo(ordinal=0)), "?")

    def test_extension_follows_the_codec(self) -> None:
        self.assertEqual(track_extension(DANMAKU), ".ass")
        self.assertEqual(track_extension(TrackInfo(ordinal=0, codec="subrip")), ".srt")
        self.assertEqual(track_extension(TrackInfo(ordinal=0, codec="hdmv_pgs_subtitle")), ".sup")
        self.assertEqual(track_extension(TrackInfo(ordinal=0, codec="unknown")), ".ass")


class CommandTests(unittest.TestCase):
    def test_probe_command_selects_subtitle_streams(self) -> None:
        command = build_ffprobe_command(Path("a.mkv"), "ffprobe")
        self.assertEqual(command[0], "ffprobe")
        self.assertIn("-select_streams", command)
        self.assertIn("s", command)
        self.assertEqual(command[-1], "a.mkv")

    def test_ffmpeg_maps_the_ordinal_and_stream_copies(self) -> None:
        command = build_ffmpeg_command(Path("a.mkv"), DANMAKU, Path("out.ass"), "ffmpeg")
        self.assertEqual(command[command.index("-map") + 1], "0:s:2")
        self.assertEqual(command[command.index("-c") + 1], "copy")

    def test_ffmpeg_encodes_only_when_the_format_changes(self) -> None:
        converted = build_ffmpeg_command(Path("a.mkv"), DANMAKU, Path("out.srt"), "ffmpeg")
        self.assertEqual(converted[converted.index("-c:s") + 1], "srt")
        self.assertNotIn("-c", converted)

    def test_encoder_choice(self) -> None:
        self.assertIsNone(subtitle_encoder(DANMAKU, Path("out.ass")))
        self.assertEqual(subtitle_encoder(DANMAKU, Path("out.srt")), "srt")
        srt_track = TrackInfo(ordinal=0, codec="subrip")
        self.assertEqual(subtitle_encoder(srt_track, Path("out.ass")), "ass")
        self.assertIsNone(subtitle_encoder(srt_track, Path("out.srt")))


class BuildPlanTests(unittest.TestCase):
    def test_extracts_the_chosen_slot_for_every_video(self) -> None:
        scanned = (
            video_tracks("EP01.mkv", SIMPLIFIED, DANMAKU),
            video_tracks("EP02.mkv", SIMPLIFIED, DANMAKU),
        )
        plan = build_extraction_plan(scanned, (2,))
        self.assertTrue(plan.can_execute)
        self.assertEqual(
            [item.destination.name for item in plan.ready_items],
            ["EP01.弹幕.ass", "EP02.弹幕.ass"],
        )

    def test_missing_slot_is_skipped_not_an_error(self) -> None:
        scanned = (video_tracks("EP01.mkv", SIMPLIFIED, DANMAKU), video_tracks("EP02.mkv", SIMPLIFIED))
        plan = build_extraction_plan(scanned, (2,))
        self.assertTrue(plan.can_execute)
        self.assertEqual(len(plan.ready_items), 1)
        self.assertEqual(plan.items[1].status, ExtractionStatus.MISSING_TRACK)
        self.assertIn("只有 1 条字幕轨", plan.items[1].detail)

    def test_probe_failure_is_reported_per_video(self) -> None:
        scanned = (video_tracks("EP01.mkv", SIMPLIFIED), video_tracks("EP02.mkv", error="读不到"))
        plan = build_extraction_plan(scanned, (1,))
        self.assertEqual(plan.items[1].status, ExtractionStatus.PROBE_FAILED)
        self.assertEqual(len(plan.ready_items), 1)

    def test_no_slot_selected_returns_a_warning_only(self) -> None:
        plan = build_extraction_plan((video_tracks("EP01.mkv", SIMPLIFIED),), ())
        self.assertEqual(plan.items, ())
        self.assertTrue(plan.can_execute)
        self.assertIn("尚未选择", plan.warnings[0])

    def test_identical_labels_get_a_slot_suffix_instead_of_blocking(self) -> None:
        first = TrackInfo(ordinal=0, codec="ass", language="chi")
        second = TrackInfo(ordinal=1, codec="ass", language="chi")
        plan = build_extraction_plan((video_tracks("EP01.mkv", first, second),), (1, 2))
        self.assertTrue(plan.can_execute)
        self.assertEqual(plan.errors, ())
        self.assertEqual(
            [item.destination.name for item in plan.ready_items],
            ["EP01.chi.s1.ass", "EP01.chi.s2.ass"],
        )
        self.assertIn("已追加层级号区分", plan.warnings[0])

    def test_existing_target_is_skipped_with_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "EP01.mkv"
            video.touch()
            (folder / "EP01.弹幕.ass").write_text("old", encoding="utf-8")
            plan = build_extraction_plan((video_tracks(str(video), SIMPLIFIED, DANMAKU),), (2,))
            self.assertEqual(plan.items[0].status, ExtractionStatus.ALREADY_EXISTS)
            self.assertTrue(plan.can_execute)
            self.assertEqual(plan.ready_items, ())
            self.assertIn("目标文件已存在", plan.warnings[0])

    def test_overwrite_keeps_the_target_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            video = folder / "EP01.mkv"
            video.touch()
            (folder / "EP01.弹幕.ass").write_text("old", encoding="utf-8")
            plan = build_extraction_plan(
                (video_tracks(str(video), SIMPLIFIED, DANMAKU),), (2,), overwrite=True
            )
            self.assertEqual(len(plan.ready_items), 1)
            self.assertTrue(plan.overwrite)

    def test_bitmap_subtitle_cannot_be_converted_to_ass(self) -> None:
        bitmap = TrackInfo(ordinal=0, codec="hdmv_pgs_subtitle", language="chi")
        plan = build_extraction_plan((video_tracks("EP01.mkv", bitmap),), (1,), convert_to_ass=True)
        self.assertFalse(plan.can_execute)
        self.assertEqual(plan.items[0].status, ExtractionStatus.CONFLICT)
        self.assertIn("图形字幕", plan.errors[0])

    def test_output_dir_and_fixed_label_override_defaults(self) -> None:
        plan = build_extraction_plan(
            (video_tracks(r"D:\shows\EP01.mkv", SIMPLIFIED, DANMAKU),),
            (2,),
            fixed_label="弹幕轨",
            output_dir=Path(r"D:\out"),
        )
        self.assertEqual(plan.ready_items[0].destination, Path(r"D:\out\EP01.弹幕轨.ass"))

    def test_convert_to_ass_rewrites_the_extension(self) -> None:
        srt = TrackInfo(ordinal=0, codec="subrip", language="chi", title="简日")
        plan = build_extraction_plan((video_tracks("EP01.mkv", srt),), (1,), convert_to_ass=True)
        self.assertEqual(plan.ready_items[0].destination.name, "EP01.简日.ass")


class ExecuteTests(unittest.TestCase):
    def _plan(self, folder: Path, count: int = 2):
        videos = []
        for index in range(1, count + 1):
            path = folder / f"EP{index:02d}.mkv"
            path.touch()
            videos.append(video_tracks(str(path), SIMPLIFIED, DANMAKU))
        return build_extraction_plan(tuple(videos), (2,))

    def test_extraction_writes_every_ready_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            plan = self._plan(folder)
            written: list[Path] = []

            def operation(video: Path, track: TrackInfo, destination: Path) -> None:
                destination.write_text(f"{video.name}#{track.ordinal}", encoding="utf-8")
                written.append(destination)

            result = execute_extraction_plan(plan, operation)
            self.assertTrue(result.success)
            self.assertEqual(len(result.extracted), 2)
            self.assertEqual((folder / "EP01.弹幕.ass").read_text(encoding="utf-8"), "EP01.mkv#2")

    def test_failure_cleans_up_already_produced_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            plan = self._plan(folder)
            calls = 0

            def fail_second(video: Path, track: TrackInfo, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ExtractionError("模拟失败")
                destination.write_text("partial", encoding="utf-8")

            result = execute_extraction_plan(plan, fail_second)
            self.assertFalse(result.success)
            self.assertEqual(len(result.extracted), 1)
            self.assertEqual(len(result.rolled_back), 1)
            self.assertIn("模拟失败", result.error)
            self.assertEqual(list(folder.glob("*.ass")), [])

    def test_overwrite_failure_keeps_produced_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            plan = self._plan(folder)
            plan = build_extraction_plan(
                tuple(VideoTracks(Path(name), (SIMPLIFIED, DANMAKU)) for name in
                      (str(folder / "EP01.mkv"), str(folder / "EP02.mkv"))),
                (2,),
                overwrite=True,
            )
            calls = 0

            def fail_second(video: Path, track: TrackInfo, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise ExtractionError("模拟失败")
                destination.write_text("kept", encoding="utf-8")

            result = execute_extraction_plan(plan, fail_second)
            self.assertFalse(result.success)
            self.assertEqual(result.rolled_back, ())
            self.assertEqual((folder / "EP01.弹幕.ass").read_text(encoding="utf-8"), "kept")
            self.assertIn("已保留", result.error)

    def test_cancellation_cleans_up_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            plan = self._plan(folder)
            calls = 0

            def operation(video: Path, track: TrackInfo, destination: Path) -> None:
                destination.write_text("partial", encoding="utf-8")

            def cancel() -> bool:
                nonlocal calls
                calls += 1
                return calls > 1

            result = execute_extraction_plan(plan, operation, should_cancel=cancel)
            self.assertFalse(result.success)
            self.assertTrue(result.cancelled)
            self.assertEqual(list(folder.glob("*.ass")), [])

    def test_plan_with_conflicts_is_refused(self) -> None:
        bitmap = TrackInfo(ordinal=0, codec="hdmv_pgs_subtitle")
        plan = build_extraction_plan((video_tracks("EP01.mkv", bitmap),), (1,), convert_to_ass=True)
        result = execute_extraction_plan(plan, lambda *args: None)
        self.assertFalse(result.success)
        self.assertIn("包含冲突", result.error)

    def test_missing_video_is_refused_before_any_write(self) -> None:
        plan = build_extraction_plan(
            (video_tracks(str(Path(tempfile.gettempdir()) / "does-not-exist.mkv"), SIMPLIFIED, DANMAKU),),
            (2,),
        )
        result = execute_extraction_plan(plan, lambda *args: None)
        self.assertFalse(result.success)
        self.assertIn("已不存在", result.error)


if __name__ == "__main__":
    unittest.main()
