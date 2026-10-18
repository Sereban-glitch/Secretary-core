import json
import unittest

from secretary.errors import ValidationError
from secretary.proxy_contract import (build_request, build_structure_merge_request,
                                      build_structure_request, extract_structure_json,
                                      extract_text)
from secretary.structure import validate_draft


class ContractExtendedTests(unittest.TestCase):
    def test_structure_request_contains_prompt_and_text(self):
        request = build_structure_request("Текст транскрипта", "model-x")
        text = request["messages"][0]["content"][0]["text"]
        self.assertIn("JSON", text)
        self.assertIn("Текст транскрипта", text)
        self.assertFalse(request["stream"])
        self.assertEqual(request["model"], "model-x")
        # нет аудио-блока
        self.assertEqual(len(request["messages"][0]["content"]), 1)

    def test_structure_repair_suffix(self):
        plain = build_structure_request("T", "m")
        repair = build_structure_request("T", "m", repair=True)
        self.assertIn("корректным JSON", repair["messages"][0]["content"][0]["text"])
        self.assertNotIn("корректным JSON", plain["messages"][0]["content"][0]["text"])

    def test_structure_request_limits(self):
        with self.assertRaises(ValueError):
            build_structure_request("x" * 100, "m", max_chars=10)
        with self.assertRaises(ValueError):
            build_structure_request("", "m")
        with self.assertRaises(ValueError):
            build_structure_request("ok", "")

    def test_merge_request(self):
        drafts = [validate_draft({"summary": "a", "topics": [], "statements": [],
                                  "questionable_places": [],
                                  "speakers_note": ""})]
        request = build_structure_merge_request(drafts, "m", max_chars=1000)
        self.assertIn("объедин", request["messages"][0]["content"][0]["text"]
                      .lower())

    def test_extract_structure_json_variants(self):
        draft = {"summary": "s", "topics": [], "statements": [],
                 "questionable_places": [], "speakers_note": ""}

        def ok(text):
            return extract_structure_json({
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": text}]})

        self.assertEqual(ok(json.dumps(draft)), draft)
        self.assertEqual(ok("```json\n" + json.dumps(draft) + "\n```"), draft)
        self.assertEqual(ok("Пояснение: " + json.dumps(draft) + " конец"), draft)
        for bad in ["", "нет объекта", "{", '{"a": 1', "[]"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ok(bad)


class ValidateDraftTests(unittest.TestCase):
    def _full(self) -> dict:
        return {"summary": "Сводка.", "topics": [{"title": "Т", "segments": [1]}],
                "statements": [{"text": "Факт.", "segments": [0],
                                "confidence": "supported"}],
                "questionable_places": [{"issue": "Трудно слышно",
                                         "segments": [2], "quote": "..."}],
                "speakers_note": "Спикер А/Б"}

    def test_valid(self):
        draft = validate_draft(self._full())
        self.assertEqual(draft["summary"], "Сводка.")
        self.assertEqual(draft["topics"][0]["segments"], [1])

    def test_minimal(self):
        draft = validate_draft({"summary": "Только сводка"})
        self.assertEqual(draft["topics"], [])

    def test_missing_summary_rejected(self):
        payload = self._full()
        del payload["summary"]
        with self.assertRaises(ValidationError):
            validate_draft(payload)

    def test_bad_types_rejected(self):
        for key, value in [("summary", ""), ("topics", "x"),
                           ("topics", [{"title": ""}]),
                           ("topics", [{"title": "ok", "segments": "no"}]),
                           ("statements", [{"text": 5}]),
                           ("questionable_places", [{"issue": None}])]:
            with self.subTest(key=key, value=value):
                payload = self._full()
                payload[key] = value
                with self.assertRaises(ValidationError):
                    validate_draft(payload)

    def test_confidence_coerced_and_extras_dropped(self):
        payload = self._full()
        payload["statements"][0]["confidence"] = "certain"
        payload["extra_field"] = "dropped"
        draft = validate_draft(payload)
        self.assertEqual(draft["statements"][0]["confidence"], "unclear")
        self.assertNotIn("extra_field", draft)

    def test_segments_only_nonnegative_ints(self):
        payload = self._full()
        payload["topics"][0]["segments"] = [0, 1, -2, "3", 4.0, True, None]
        draft = validate_draft(payload)
        self.assertEqual(draft["topics"][0]["segments"], [0, 1, 4])


if __name__ == "__main__":
    unittest.main()
