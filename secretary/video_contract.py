"""Контракт мультимодального запроса и строгая проверка JSON-ответа."""
from __future__ import annotations

import base64
import json

from .proxy_contract import extract_text

SUPPORTED_VIDEO_MIME = {"video/mp4"}


def build_video_request(video: bytes, mime: str, model: str, prompt: str,
                        *, max_bytes: int) -> dict:
    if not model.strip():
        raise ValueError("video model must be explicitly configured")
    if mime not in SUPPORTED_VIDEO_MIME:
        raise ValueError("unsupported normalized video MIME")
    if not video or len(video) > max_bytes:
        raise ValueError("empty or oversized video segment")
    return {
        "model": model, "max_tokens": 4096, "stream": False,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image", "source": {
                "type": "base64", "media_type": mime,
                "data": base64.b64encode(video).decode("ascii")}},
        ]}],
    }


def extract_video_json(response: dict) -> dict:
    raw = extract_text(response).strip()
    if raw.startswith("```"):
        raw = raw[raw.find("\n") + 1:]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("video response has no JSON object")
    try:
        value = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"video response JSON is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("video response must be an object")
    result = {
        "summary": _string(value.get("summary"), 3000),
        "speech": _string(value.get("speech"), 30_000),
        "visible_text": _string_list(value.get("visible_text"), 80, 500),
        "events": _events(value.get("events")),
        "facts": _facts(value.get("facts")),
        "uncertain": _string_list(value.get("uncertain"), 80, 1000),
    }
    return result


def _string(value, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("video response string field has wrong type")
    return value.strip()[:limit]


def _string_list(value, count: int, width: int) -> list[str]:
    if not isinstance(value, list) or len(value) > count:
        raise ValueError("video response list field has wrong type or size")
    if any(not isinstance(item, str) for item in value):
        raise ValueError("video response list must contain strings")
    return [item.strip()[:width] for item in value if item.strip()]


def _events(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("video events have wrong type or size")
    result = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("description"), str):
            raise ValueError("invalid video event")
        time_value = item.get("time")
        if not isinstance(time_value, (int, float)) or time_value < 0:
            raise ValueError("invalid video event time")
        result.append({"time": round(float(time_value), 3),
                       "description": item["description"].strip()[:1000]})
    return result


def _facts(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError("video facts have wrong type or size")
    result = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError("invalid video fact")
        source = item.get("source")
        if source not in {"audio", "visual", "both"}:
            raise ValueError("invalid video fact source")
        result.append({"text": item["text"].strip()[:1200], "source": source})
    return result
