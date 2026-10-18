"""Разбор и валидация структурированного черновика (JSON от модели).

Схема черновика (docs/schema/structured-draft.schema.json):
  summary: str
  topics: [{title: str, segments: [int]}]
  statements: [{text: str, segments: [int], confidence: "supported"|"unclear"}]
  questionable_places: [{issue: str, segments: [int], quote?: str}]
  speakers_note: str

Ничего не выдумывается: валидатор только приводит/отбрасывает поля.
"""
from __future__ import annotations

from .errors import ValidationError

_CONFIDENCE = {"supported", "unclear"}
_MAX_LIST = 200


def validate_draft(draft: dict) -> dict:
    if not isinstance(draft, dict):
        raise ValidationError("draft must be an object")
    errors: list[str] = []

    summary = draft.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")

    topics = _list_of(draft.get("topics"), "topics", errors)
    topics_out = []
    for topic in topics:
        if not isinstance(topic, dict):
            errors.append("topics[] items must be objects")
            continue
        title = topic.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append("topics[].title must be a non-empty string")
            continue
        topics_out.append({"title": title.strip(),
                           "segments": _segments(topic.get("segments"),
                                                  "topics[].segments",
                                                  errors)})

    statements = _list_of(draft.get("statements"), "statements", errors)
    statements_out = []
    for statement in statements:
        if not isinstance(statement, dict):
            errors.append("statements[] items must be objects")
            continue
        text = statement.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append("statements[].text must be a non-empty string")
            continue
        confidence = statement.get("confidence", "unclear")
        if confidence not in _CONFIDENCE:
            confidence = "unclear"
        statements_out.append({"text": text.strip(),
                               "segments": _segments(
                                   statement.get("segments"),
                                   "statements[].segments", errors),
                               "confidence": confidence})

    places = _list_of(draft.get("questionable_places"),
                      "questionable_places", errors)
    places_out = []
    for place in places:
        if not isinstance(place, dict):
            errors.append("questionable_places[] items must be objects")
            continue
        issue = place.get("issue")
        if not isinstance(issue, str) or not issue.strip():
            errors.append("questionable_places[].issue must be a non-empty string")
            continue
        item = {"issue": issue.strip(),
                "segments": _segments(place.get("segments"),
                                       "questionable_places[].segments",
                                       errors)}
        quote = place.get("quote")
        if isinstance(quote, str) and quote.strip():
            item["quote"] = quote.strip()
        places_out.append(item)

    note = draft.get("speakers_note")
    if not isinstance(note, str):
        note = ""

    if errors:
        raise ValidationError("invalid structured draft: " + "; ".join(errors[:5]))
    return {
        "summary": summary.strip() if isinstance(summary, str) else "",
        "topics": topics_out[:_MAX_LIST],
        "statements": statements_out[:_MAX_LIST],
        "questionable_places": places_out[:_MAX_LIST],
        "speakers_note": note,
    }


def _list_of(value, name: str, errors: list[str]) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{name} must be an array")
        return []
    return value


def _segments(value, name: str = "segments", errors: list[str] | None = None) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        if errors is not None:
            errors.append(f"{name} must be an array of segment ids")
        return []
    out: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and item >= 0:
            out.append(item)
        elif isinstance(item, float) and item.is_integer() and item >= 0:
            out.append(int(item))
    return out
