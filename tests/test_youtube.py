from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secretary.errors import YouTubeImportError
from secretary.youtube import download_youtube, normalize_youtube_url


class YouTubeTests(unittest.TestCase):
    def test_normalizes_supported_links(self):
        expected = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        for value in (
            "https://youtu.be/dQw4w9WgXcQ?t=3",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx",
            "https://m.youtube.com/shorts/dQw4w9WgXcQ",
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_youtube_url(value)[0], expected)

    def test_rejects_non_youtube_and_playlist_only(self):
        for value in ("http://youtu.be/dQw4w9WgXcQ",
                      "https://evil.example/watch?v=dQw4w9WgXcQ",
                      "https://youtube.com/playlist?list=PLx"):
            with self.subTest(value=value), self.assertRaises(YouTubeImportError):
                normalize_youtube_url(value)

    def test_download_is_bounded_and_returns_one_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            meta = {"id": "dQw4w9WgXcQ", "duration": 42,
                    "title": "Тест видео", "is_live": False}

            def fake_run(command, _timeout):
                if "--dump-single-json" in command:
                    return subprocess.CompletedProcess(command, 0,
                                                       json.dumps(meta), "")
                (dest / "youtube-dQw4w9WgXcQ.mp4").write_bytes(b"video")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch("secretary.youtube.shutil.which", return_value="/bin/true"), \
                    mock.patch("secretary.youtube._run", side_effect=fake_run):
                result = download_youtube(
                    "https://youtu.be/dQw4w9WgXcQ", dest,
                    executable="yt-dlp", max_duration=60,
                    max_bytes=1000, timeout=10)
            self.assertEqual(result.video_id, "dQw4w9WgXcQ")
            self.assertEqual(result.duration, 42)
            self.assertEqual(result.size, 5)

    def test_rejects_live_and_overlong_video(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("secretary.youtube.shutil.which", return_value="/bin/true"):
            for meta in (
                {"id": "dQw4w9WgXcQ", "duration": 42, "is_live": True},
                {"id": "dQw4w9WgXcQ", "duration": 61, "is_live": False},
            ):
                with self.subTest(meta=meta), \
                        mock.patch("secretary.youtube._run", return_value=
                                   subprocess.CompletedProcess([], 0, json.dumps(meta), "")), \
                        self.assertRaises(YouTubeImportError):
                    download_youtube("https://youtu.be/dQw4w9WgXcQ", tmp,
                                     executable="yt-dlp", max_duration=60,
                                     max_bytes=1000, timeout=10)


if __name__ == "__main__":
    unittest.main()
