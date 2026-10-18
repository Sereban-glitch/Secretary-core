from __future__ import annotations

import base64
import json
import unittest

from secretary.video_contract import build_video_request, extract_video_json


class VideoContractTests(unittest.TestCase):
    def test_builds_proxy_inline_video(self):
        request = build_video_request(
            b"video-bytes", "video/mp4", "gemini-video", "Analyze",
            max_bytes=100)
        block = request["messages"][0]["content"][1]
        self.assertEqual(block["source"]["media_type"], "video/mp4")
        self.assertEqual(base64.b64decode(block["source"]["data"]),
                         b"video-bytes")

    def test_accepts_only_complete_strict_json(self):
        value = {
            "summary": "Кратко", "speech": "Речь",
            "visible_text": ["Надпись"],
            "events": [{"time": 2.5, "description": "Открыт экран"}],
            "facts": [{"text": "Показана кнопка", "source": "visual"}],
            "uncertain": [],
        }
        response = {"stop_reason": "end_turn", "content": [
            {"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
        self.assertEqual(extract_video_json(response)["events"][0]["time"], 2.5)
        response["stop_reason"] = "max_tokens"
        with self.assertRaises(ValueError):
            extract_video_json(response)


if __name__ == "__main__":
    unittest.main()
