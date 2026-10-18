"""Результат v1 и Markdown-экспорт.

Контракт результата (docs/schema/v1-result.schema.json):
  version, job_id, status, created_at, updated_at,
  source {filename, sha256, mime, duration, size},
  segments [{id, start, end, status, text, warnings}],
  transcript, structured_draft, warnings, provenance.

Политика: полный транскрипт неизменен; структурированный черновик —
отдельное поле; публикация в базу — на стороне клиента после проверки
человеком.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

RESULT_VERSION = "1"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def build_result(job: dict, segments: list[dict]) -> dict:
    profile = job.get("profile_json") or {}
    structured = job.get("structured_json")
    return {
        "version": RESULT_VERSION,
        "media_kind": job.get("media_kind") or "audio",
        "job_id": job["id"],
        "status": job["status"],
        "created_at": _iso(job["created_at"]),
        "updated_at": _iso(job["updated_at"]),
        "source": {
            "filename": job["source_filename"],
            "sha256": job["source_sha256"],
            "mime": job["source_mime"],
            "duration": round(float(job["source_duration"]), 3),
            "size": int(job["source_size"]),
        },
        "segments": [
            {
                "id": int(s["idx"]),
                "start": round(float(s["start"]), 3),
                "end": round(float(s["end"]), 3),
                "status": s["status"],
                "text": s["text"],
                "warnings": list(s.get("warnings_json") or []),
            }
            for s in segments
        ],
        "transcript": job.get("transcript"),
        "structured_draft": structured,
        "warnings": list(job.get("warnings_json") or []),
        "provenance": {
            "model": profile.get("model"),
            "transcribe_prompt_version": profile.get(
                "transcribe_prompt_version"),
            "structure_prompt_version": profile.get(
                "structure_prompt_version"),
            "structure_merge_prompt_version": profile.get(
                "structure_merge_prompt_version"),
            "segment_seconds": profile.get("segment_seconds"),
            "overlap_seconds": profile.get("overlap_seconds"),
            "audio_pipeline": profile.get("audio_pipeline"),
            "transport": profile.get("transport"),
            "language": profile.get("language"),
            "vocabulary_terms": profile.get("vocabulary_terms"),
            "video_prompt_version": profile.get("video_prompt_version"),
            "video_pipeline": profile.get("video_pipeline"),
            "has_audio": profile.get("has_audio"),
            "source_type": profile.get("source_type"),
            "source_url": profile.get("source_url"),
        },
        "error": job.get("error"),
    }


def render_markdown(result: dict) -> str:
    """Markdown-экспорт: метаданные → черновик → полная расшифровка."""
    source = result["source"]
    lines: list[str] = []
    title = "Видеоанализ" if result.get("media_kind") == "video" else "Расшифровка"
    lines.append(f"# {title}: {source['filename']}")
    lines.append("")
    lines.append("| Поле | Значение |")
    lines.append("|---|---|")
    lines.append(f"| job_id | `{result['job_id']}` |")
    lines.append(f"| Статус | {result['status']} |")
    lines.append(f"| SHA256 | `{source['sha256']}` |")
    lines.append(f"| MIME | {source['mime']} |")
    duration = int(round(source["duration"]))
    lines.append(f"| Длительность | {duration // 60} мин {duration % 60} с |")
    prov = result.get("provenance") or {}
    lines.append(f"| Модель | {prov.get('model') or '—'} |")
    lines.append(f"| Транспорт | {prov.get('transport') or '—'} |")
    if result.get("media_kind") == "video":
        lines.append(f"| Границы нарезки | {prov.get('segment_seconds')} с |")
        lines.append(f"| Видеоконвейер | {prov.get('video_pipeline') or '—'} |")
    else:
        lines.append(f"| Границы нарезки | {prov.get('segment_seconds')} с, "
                     f"перекрытие {prov.get('overlap_seconds')} с |")
    lines.append("")
    if result.get("warnings"):
        lines.append("## Предупреждения")
        lines.append("")
        for warning in result["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")
    draft = result.get("structured_draft")
    if draft:
        lines.append("## Структурированный черновик (не заменяет расшифровку)")
        lines.append("")
        lines.append(f"**Кратко:** {draft.get('summary', '')}")
        lines.append("")
        if draft.get("topics"):
            lines.append("### Темы")
            lines.append("")
            for topic in draft["topics"]:
                segs = ", ".join(str(s) for s in topic.get("segments", []))
                lines.append(f"- {topic['title']}"
                             + (f" — сегменты {segs}" if segs else ""))
            lines.append("")
        if draft.get("statements"):
            lines.append("### Утверждения")
            lines.append("")
            for statement in draft["statements"]:
                segs = ", ".join(str(s) for s in statement.get("segments", []))
                mark = "✓" if statement.get("confidence") == "supported" else "?"
                lines.append(f"- [{mark}] {statement['text']}"
                             + (f" — сегменты {segs}" if segs else ""))
            lines.append("")
        if draft.get("questionable_places"):
            lines.append("### Сомнительные места")
            lines.append("")
            for place in draft["questionable_places"]:
                segs = ", ".join(str(s) for s in place.get("segments", []))
                lines.append(f"- {place['issue']}"
                             + (f" — сегменты {segs}" if segs else ""))
                if place.get("quote"):
                    lines.append(f"  > «{place['quote']}»")
            lines.append("")
        if draft.get("speakers_note"):
            lines.append(f"**Говорящие:** {draft['speakers_note']}")
            lines.append("")
    lines.append("## Полный материал для проверки" if result.get("media_kind") == "video"
                 else "## Полная расшифровка")
    lines.append("")
    lines.append(result.get("transcript") or "(транскрипт недоступен)")
    lines.append("")
    return "\n".join(lines)


def dump_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)
