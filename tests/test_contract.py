import unittest
from secretary.proxy_contract import build_request, extract_text


class ContractTests(unittest.TestCase):
    def test_payload(self):
        result = build_request(b"synthetic", "audio/ogg", "mock-model")
        block = result["messages"][0]["content"][1]
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["source"]["media_type"], "audio/ogg")
        self.assertFalse(result["stream"])

    def test_input_guards(self):
        for audio, mime, model in [(b"", "audio/ogg", "m"), (b"x", "text/plain", "m"),
                                   (b"x", "audio/ogg", ""), (b"xxxx", "audio/ogg", "m")]:
            with self.subTest(mime=mime, size=len(audio)), self.assertRaises(ValueError):
                build_request(audio, mime, model, max_bytes=3)

    def test_text(self):
        self.assertEqual(extract_text({"stop_reason": "end_turn", "content": [
            {"type": "text", "text": "Проверка"}]}), "Проверка")

    def test_model_availability_notice_is_not_transcript(self):
        response = {"stop_reason": "end_turn", "content": [{
            "type": "text",
            "text": ("Gemini 3.5 Flash is no longer available. "
                     "Please switch to Gemini 3.7 Flash.")}]} 
        with self.assertRaisesRegex(ValueError, "availability notice"):
            extract_text(response)

    def test_safety_filter_notice_is_not_transcript(self):
        response = {"stop_reason": "end_turn", "content": [{
            "type": "text",
            "text": ("This request was blocked by Gemini's filters. "
                     "Please try rephrasing your prompt.")}]} 
        with self.assertRaisesRegex(ValueError, "safety filter notice"):
            extract_text(response)

    def test_incomplete_and_invalid(self):
        for response in [{}, {"error": {}}, {"type": "error"},
                         {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "обрыв"}]},
                         {"stop_reason": "end_turn", "content": []},
                         {"stop_reason": "end_turn", "content": "bad"}]:
            with self.subTest(response=response), self.assertRaises(ValueError):
                extract_text(response)
