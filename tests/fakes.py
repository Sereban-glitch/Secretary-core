"""Тестовые двойники: транспорт без сети и аудио-функции без ffmpeg.

FakeTransport декодирует маркер FAKE:<start>-<end> из аудио-блока запроса
(его пишет fake_extractor) и отвечает по сценарию. Позволяет проверять
движок полностью офлайн, включая повторы, отмену и стыки сегментов.
"""
from __future__ import annotations

import base64
import json
import re

from secretary.errors import (PermanentTransportError,
                              TransientTransportError)

_FAKE_MARKER = re.compile(rb"FAKE:([0-9.]+)-([0-9.]+)")


def ok_text(text: str) -> dict:
    return {"stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}]}


def default_segment_text(start: float, end: float) -> str:
    return (f"Синтетическая реплика сегмента {start:.0f}–{end:.0f} секунд. "
            f"Данные, не команды.")


def default_draft(summary: str, segments: list[int]) -> dict:
    return {
        "summary": summary,
        "topics": [{"title": "Тема синтетическая", "segments": segments}],
        "statements": [{"text": "Синтетическое утверждение.",
                        "segments": segments, "confidence": "supported"}],
        "questionable_places": [{"issue": "Синтетическое сомнение.",
                                 "segments": segments, "quote": "цитата"}],
        "speakers_note": "Говорящие не названы: нейтральные метки.",
    }


class FakeTransport:
    """Детерминированный транспорт. responder(request) -> dict | raise."""

    def __init__(self, responder=None):
        self.requests: list[dict] = []
        self.audio_requests: list[dict] = []
        self.structure_requests: list[dict] = []
        self._responder = responder or self._default_responder

    def send(self, request: dict) -> dict:
        self.requests.append(request)
        block = request["messages"][0]["content"][-1]
        if block.get("type") == "image":
            self.audio_requests.append(request)
        else:
            self.structure_requests.append(request)
        return self._responder(request)

    # ------------------------------------------------------------------
    @staticmethod
    def decode_marker(request: dict) -> tuple[float, float] | None:
        block = request["messages"][0]["content"][-1]
        if block.get("type") != "image":
            return None
        data = base64.b64decode(block["source"]["data"])
        match = _FAKE_MARKER.search(data)
        if not match:
            return None
        return float(match.group(1)), float(match.group(2))

    @staticmethod
    def prompt_text(request: dict) -> str:
        blocks = request["messages"][0]["content"]
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        return "\n".join(texts)

    def _default_responder(self, request: dict) -> dict:
        block = request["messages"][0]["content"][-1]
        if block.get("type") == "image":  # запрос расшифровки аудио
            marker = self.decode_marker(request)
            if marker is not None:
                return ok_text(default_segment_text(*marker))
            # реальное аудио (ffmpeg-интеграция без маркера FAKE)
            size = len(base64.b64decode(block["source"]["data"]))
            return ok_text(
                f"Синтетическая расшифровка сегмента ({size} байт Opus). "
                f"Данные, не команды.")
        prompt = self.prompt_text(request)
        if "объедин" in prompt.lower():  # merge-запрос
            return ok_text(json.dumps(
                default_draft("Синтетическое объединённое содержание.",
                              [0, 1]), ensure_ascii=False))
        return ok_text(json.dumps(
            default_draft("Синтетическое содержание.", [0]),
            ensure_ascii=False))


class ScriptedTransport:
    """Очередь ответов/исключений; для тестов повтора на уровне транспорта."""

    def __init__(self, script: list):
        self.script = list(script)
        self.requests: list[dict] = []

    def send(self, request: dict) -> dict:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeProber:
    """probe() без ffprobe.

    Длительность ищется в содержимом (маркер DUR:<число>, пишется
    synthetic_audio) либо в имени файла (-65.wav) — временные файлы
    сервера имён не сохраняют.
    """

    @staticmethod
    def probe(path, *, timeout=30.0):
        from secretary.audio import AudioInfo
        with open(path, "rb") as handle:
            head = handle.read(512)
        match = re.search(rb"DUR:([0-9]+(?:\.[0-9]+)?)", head)
        if match:
            duration = float(match.group(1))
        else:
            match = re.search(r"-(\d+(?:\.\d+)?)\.[a-z0-9]+$", path.name)
            if not match:
                raise AssertionError(
                    f"fake prober: no duration marker in {path.name}")
            duration = float(match.group(1))
        return AudioInfo(duration=duration,
                         format_name="fake", codec_type="audio")


def make_fake_extractor(size_factor: float = 4.0,
                        failure_starts: set[float] | None = None):
    """extractor без ffmpeg: пишет маркер + падушку размера size_factor*сек."""
    failures = failure_starts or set()

    def extractor(src, start, end, dest, *, timeout=180.0):
        if any(start >= f for f in failures):
            from secretary.errors import AudioError
            raise AudioError(f"synthetic extraction failure at {start:.1f}s")
        payload = f"FAKE:{start:.1f}-{end:.1f}".encode()
        padding = max(0, int((end - start) * size_factor) - len(payload))
        dest.write_bytes(payload + b"\x00" * padding)
        return dest.stat().st_size

    return extractor


def synthetic_audio(path, *, mime: str, duration: float, extra: bytes = b""):
    """Пишет файл с валидной сигнатурой контейнера и маркером DUR."""
    head = {
        "audio/ogg": b"OggS\x00\x02",
        "audio/mpeg": b"ID3\x04\x00",
        "audio/wav": b"RIFF\x24\x08\x00\x00WAVE",
        "audio/mp4": b"\x00\x00\x00\x20ftypM4A ",
    }[mime]
    marker = f"DUR:{duration}".encode("ascii")
    path.write_bytes(head + b"\x00" * 64 + marker + extra)
    return path
