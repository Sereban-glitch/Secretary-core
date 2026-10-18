"""Pure, offline-testable адаптер протокола прокси. See docs/PROXY.md.

Сетевого кода здесь нет: сборка payload и строгая проверка ответа.
Любая ошибка ответа — ValueError; транспорт/движок решают, временная она
или постоянная (контракт: 200 с ошибкой/пустым ответом — не успех).
"""
from __future__ import annotations

import base64
import json

from .prompts import (STRUCTURE_MERGE_PROMPT, STRUCTURE_PROMPT,
                      STRUCTURE_REPAIR_SUFFIX, TRANSCRIBE_PROMPT)

SUPPORTED_AUDIO_MIME = {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav"}


def build_request(audio: bytes, mime: str, model: str, *,
                  max_bytes: int = 10_485_760,
                  prompt: str = TRANSCRIBE_PROMPT) -> dict:
    if not model.strip():
        raise ValueError("model must be explicitly configured")
    if mime not in SUPPORTED_AUDIO_MIME:
        raise ValueError("unsupported audio MIME")
    if not audio or len(audio) > max_bytes:
        raise ValueError("empty or oversized audio segment")
    # 'image' — это transport-обёртка inlineData данного прокси (см.
    # docs/PROXY.md), а не изображение: media_type остаётся аудио.
    return {
        "model": model, "max_tokens": 8192, "stream": False,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image", "source": {"type": "base64", "media_type": mime,
                                         "data": base64.b64encode(audio).decode("ascii")}},
        ]}],
    }


def build_structure_request(text: str, model: str, *,
                            max_chars: int = 50_000,
                            prompt: str = STRUCTURE_PROMPT,
                            repair: bool = False) -> dict:
    """Текстовый запрос структурирования (без аудио-блока)."""
    if not model.strip():
        raise ValueError("model must be explicitly configured")
    if not text.strip():
        raise ValueError("empty transcript for structuring")
    if len(text) > max_chars:
        raise ValueError(f"transcript chunk exceeds max_chars ({len(text)} > {max_chars})")
    full_prompt = prompt + (STRUCTURE_REPAIR_SUFFIX if repair else "")
    return {
        "model": model, "max_tokens": 8192, "stream": False,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": full_prompt + "\n\n---\n\n" + text},
        ]}],
    }


def build_structure_merge_request(drafts: list[dict], model: str,
                                  *, max_chars: int = 50_000) -> dict:
    payload = json.dumps({"drafts": drafts}, ensure_ascii=False)
    return build_structure_request(payload, model, max_chars=max_chars,
                                   prompt=STRUCTURE_MERGE_PROMPT)


def extract_text(response: dict) -> str:
    if not isinstance(response, dict) or response.get("error") or response.get("type") == "error":
        raise ValueError("proxy returned an error")
    if response.get("stop_reason") != "end_turn":
        raise ValueError("response is incomplete or has unverified completion status")
    content = response.get("content")
    if not isinstance(content, list):
        raise ValueError("invalid response content")
    texts = [b["text"] for b in content if isinstance(b, dict)
             and b.get("type") == "text" and isinstance(b.get("text"), str)]
    text = "\n".join(texts).strip()
    if not text:
        raise ValueError("empty transcript")
    lower = text.casefold()
    # Некоторые маршруты прокси отвечают служебным уведомлением обычным
    # текстовым блоком и stop_reason=end_turn. Это не расшифровка и не должно
    # превращаться в ложный completed.
    if (("no longer available" in lower or "model is unavailable" in lower)
            and ("please switch" in lower or "switch to" in lower)):
        raise ValueError("proxy returned a model availability notice")
    if (("request was blocked" in lower and "filter" in lower)
            or "blocked by gemini's filters" in lower):
        raise ValueError("proxy returned a safety filter notice")
    return text


def extract_structure_json(response: dict) -> dict:
    """Ответ модели структурирования → dict черновика (строгая проверка)."""
    raw = extract_text(response)
    text = raw.strip()
    if text.startswith("```"):
        # отделить ```json ... ```
        first_newline = text.find("\n")
        text = text[first_newline + 1:] if first_newline != -1 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start = text.find("{")
    if start == -1:
        raise ValueError("structure response has no JSON object")
    # взять сбалансированный outermost {...}
    depth = 0
    end = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        raise ValueError("structure response JSON is truncated")
    try:
        draft = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"structure response JSON is invalid: {exc}") from exc
    if not isinstance(draft, dict):
        raise ValueError("structure draft must be a JSON object")
    return draft
