"""Интеграционные тесты аудио-слоя с реальными ffmpeg/ffprobe.

Генерируют синтетическое аудио (синус) — реальных записей нет и не должно
быть (AGENTS.md: автономная работа на синтетических данных).
"""
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from secretary import audio
from secretary.errors import AudioError

FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_sine(path: Path, duration: float, *, codec: str = "wav") -> Path:
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            str(path)]
    subprocess.run(argv, check=True, capture_output=True)
    return path


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe недоступны")
class ProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_probe_wav(self):
        path = make_sine(self.tmp / "tone.wav", 3.0)
        info = audio.probe(path)
        self.assertEqual(info.codec_type, "audio")
        self.assertAlmostEqual(info.duration, 3.0, delta=0.2)

    def test_probe_rejects_non_audio(self):
        path = self.tmp / "fake.wav"
        path.write_bytes(b"RIFF\x24\x08\x00\x00WAVEjunkjunkjunk")
        with self.assertRaises(AudioError):
            audio.probe(path)

    def test_probe_rejects_text(self):
        path = self.tmp / "text.wav"
        path.write_bytes(b"RIFF____WAVE" + b"not audio data" * 10)
        with self.assertRaises(AudioError):
            audio.probe(path)


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe недоступны")
class SegmentPlanTests(unittest.TestCase):
    def test_full_coverage_with_overlap(self):
        ranges = audio.segment_plan(100.0, segment_seconds=30.0,
                                    overlap_seconds=4.0,
                                    min_segment_seconds=10.0)
        self.assertEqual(ranges[0].start, 0.0)
        self.assertEqual(ranges[-1].end, 100.0)
        for prev, nxt in zip(ranges, ranges[1:]):
            self.assertAlmostEqual(nxt.start, prev.end - 4.0, delta=1e-9)
            self.assertLess(nxt.start, prev.end)
        for r in ranges:
            self.assertGreaterEqual(r.end - r.start, 10.0)

    def test_tail_merges_into_previous(self):
        # последний хвост 9 с < min 10 → вливается в предыдущий сегмент
        ranges = audio.segment_plan(61.0, segment_seconds=30.0,
                                    overlap_seconds=4.0,
                                    min_segment_seconds=10.0)
        self.assertEqual(len(ranges), 2)
        self.assertEqual(ranges[-1].end, 61.0)
        self.assertEqual(ranges[-1].start, 26.0)

    def test_short_audio_single_segment(self):
        ranges = audio.segment_plan(8.0, segment_seconds=30.0,
                                    overlap_seconds=4.0,
                                    min_segment_seconds=10.0)
        self.assertEqual(len(ranges), 1)
        self.assertEqual((ranges[0].start, ranges[0].end), (0.0, 8.0))

    def test_invalid_args(self):
        with self.assertRaises(AudioError):
            audio.segment_plan(100.0, segment_seconds=30.0,
                               overlap_seconds=30.0, min_segment_seconds=10.0)
        with self.assertRaises(AudioError):
            audio.segment_plan(100.0, segment_seconds=30.0,
                              overlap_seconds=4.0, min_segment_seconds=30.0)


@unittest.skipUnless(FFMPEG, "ffmpeg/ffprobe недоступны")
class ExtractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extract_produces_opus_ogg(self):
        src = make_sine(self.tmp / "tone.wav", 3.0)
        dest = self.tmp / "seg.ogg"
        size = audio.extract_segment(src, 1.0, 2.0, dest)
        self.assertGreater(size, 0)
        info = audio.probe(dest)
        self.assertAlmostEqual(info.duration, 1.0, delta=0.15)
        self.assertIn("ogg", audio.MIME_BY_EXTENSION[
            dest.suffix.lower().lstrip(".")])

    def test_extract_rejects_garbage(self):
        src = self.tmp / "garbage.wav"
        src.write_bytes(b"RIFF____WAVE" + b"\x00" * 200)
        dest = self.tmp / "seg.ogg"
        with self.assertRaises(AudioError):
            audio.extract_segment(src, 0.0, 1.0, dest)
        self.assertFalse(dest.exists())

    def test_extract_long_duration_ok(self):
        src = make_sine(self.tmp / "tone.wav", 5.0)
        dest = self.tmp / "seg.ogg"
        audio.extract_segment(src, 0.0, 5.0, dest, timeout=30.0)
        info = audio.probe(dest)
        self.assertAlmostEqual(info.duration, 5.0, delta=0.3)


class MimeSniffTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_mime_for_supported(self):
        cases = {"ogg": b"OggS\x00", "mp3": b"ID3\x04",
                 "wav": b"RIFF\x24\x08\x00\x00WAVE",
                 "m4a": b"\x00\x00\x00\x20ftypM4A "}
        for ext, head in cases.items():
            with self.subTest(ext=ext):
                path = self.tmp / f"file.{ext}"
                path.write_bytes(head + b"\x00" * 64)
                self.assertEqual(
                    audio.mime_for_file(path), audio.MIME_BY_EXTENSION[ext])

    def test_raw_mp3_frame_without_id3(self):
        path = self.tmp / "file.mp3"
        path.write_bytes(b"\xff\xfb" + b"\x00" * 64)
        self.assertEqual(audio.mime_for_file(path), "audio/mpeg")

    def test_mismatch_rejected(self):
        path = self.tmp / "file.ogg"
        path.write_bytes(b"ID3" + b"\x00" * 64)
        with self.assertRaises(AudioError):
            audio.mime_for_file(path)

    def test_unsupported_extension(self):
        path = self.tmp / "file.flac"
        path.write_bytes(b"OggS" + b"\x00" * 64)
        with self.assertRaises(AudioError):
            audio.mime_for_file(path)

    def test_sniff_mime(self):
        self.assertEqual(audio.sniff_mime(b"OggS"), "audio/ogg")
        self.assertEqual(audio.sniff_mime(b"ID3xx"), "audio/mpeg")
        self.assertEqual(audio.sniff_mime(b"\xff\xfb\x00"), "audio/mpeg")
        self.assertEqual(audio.sniff_mime(b"RIFF____WAVE"), "audio/wav")
        self.assertEqual(audio.sniff_mime(b"\x00\x00\x00\x20ftypM4A"),
                         "audio/mp4")
        self.assertIsNone(audio.sniff_mime(b"random bytes"))


class MissingToolsTests(unittest.TestCase):
    def test_probe_reports_missing_ffprobe_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.mp3"
            path.write_bytes(b"ID3" + b"\x00" * 64)
            with mock.patch("secretary.audio._run",
                            side_effect=FileNotFoundError("ffprobe")):
                with self.assertRaisesRegex(AudioError, "ffprobe не найден"):
                    audio.probe(path)

    def test_extract_reports_missing_ffmpeg_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "voice.mp3"
            dest = Path(tmp) / "segment.ogg"
            src.write_bytes(b"ID3" + b"\x00" * 64)
            dest.write_bytes(b"partial")
            with mock.patch("secretary.audio._run",
                            side_effect=FileNotFoundError("ffmpeg")):
                with self.assertRaisesRegex(AudioError, "ffmpeg не найден"):
                    audio.extract_segment(src, 0, 1, dest)
            self.assertFalse(dest.exists())


class DiskBudgetTests(unittest.TestCase):
    def test_budget_exceeds_free_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AudioError):
                audio.check_disk_budget(Path(tmp), 2 ** 62)

    def test_reasonable_budget_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio.check_disk_budget(Path(tmp), 1024)  # 1 КиБ точно есть


class FormatTsTests(unittest.TestCase):
    def test_format_ts(self):
        self.assertEqual(audio.format_ts(0), "00:00:00")
        self.assertEqual(audio.format_ts(65), "00:01:05")
        self.assertEqual(audio.format_ts(3661.4), "01:01:01")


if __name__ == "__main__":
    unittest.main()
