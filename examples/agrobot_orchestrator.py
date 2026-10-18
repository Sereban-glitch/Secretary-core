#!/usr/bin/env python3
"""Оркестратор приёмочного сценария «архив → AgroBot» (пример интеграции).

ЧЁТКОЕ РАЗДЕЛЕНИЕ двух независимых потоков (ревью pass2, P1):
  А. Документы из ZIP — НЕ расшифровываются и НЕ публикуются транскрибатором:
     слой zipimport извлекает их и передаёт клиенту (нас), оркестратор
     загружает их в AgroBot существующим upload API как есть.
  Б. Аудио из ZIP — НЕ публикуется автоматически: расшифровка → результат
     v1 → ПРОВЕРКА ЧЕЛОВЕКОМ → только после явного подтверждения загрузка
     подтверждённого Markdown как документа AgroBot.

ОБЛАСТЬ ДЕЙСТВИЯ ЗАПУСКА (ревью pass3, P1 «смешение импортов»):
каждый запуск получает уникальные run_id и владельца (owner) заданий;
расшифровываются, показываются и публикуются ТОЛЬКО job_id из манифеста
ТЕКУЩЕГО импорта. Старые/чужие задания в общей SQLite (data/orchestrator)
не обрабатываются и не публикуются, каким бы ни был их статус; фильтр
«по статусу» или «весь owner» без уникальной сессии не используется.

Фазы:
  1) import:  import_zip → аудио→задания Secretary, документы→список;
  2) transcribe: engine.run_pending(job_ids=манифеста) — только текущий
     импорт (настоящий ffmpeg-конвейер; транспорт живой или
     --fake-transport для офлайн-демо);
  3) review:  сводка для человека (документы + статусы расшифровок);
  4) publish: ТОЛЬКО после подтверждения — upload документов, затем
     upload Markdown расшифровок (только completed); отказы (HTTP/сеть)
     фиксируются поштучно и не прерывают оставшиеся загрузки;
  5) report:  report.json (+ история reports/report-<run_id>.json) с
     решениями, ответами AgroBot и агрегатами публикации.

ТРИ НЕЗАВИСИМЫХ СТАТУСА (ревью pass4, P1 «ложноположительный итог»):
  transcription_status — этап расшифровки по ВСЕМ job_id текущего импорта:
     complete (аудио не было или все completed) | partial (часть готова,
     включая частичные) | failed (ни одного успешного результата);
     любое аудио не в статусе completed, отсутствующее в БД или оставшееся
     в очереди понижает статус как минимум до partial;
  publication_status   — этап публикации по фактическим загрузкам:
     complete | partial | failed | nothing_to_publish | not_confirmed;
     пустой счётчик (attempted=0) при непустом ожидании — НЕ complete;
  workflow_status      — ИТОГ запуска, задаёт код возврата:
     complete       0   весь ожидаемый сценарий выполнен: все аудио текущего
                        импорта completed И все ожидавшиеся загрузки прошли;
     not_confirmed  1   человек не подтвердил публикацию (нет --yes);
     partial        2   часть материалов не получена/не опубликована
                        (успешный документ не отменяет упавшее аудио);
     failed         3   ни одного опубликованного материала (в т.ч. случай
                        «нечего публиковать»: все аудио упали, задания
                        пропали/остались в очереди, архив полностью
                        отклонён или пуст).
Стадии не смешиваются: поле этапа описывает только свой этап, итог
считается отдельно по обоим этапам и факту появления материалов в базе.

initData — кратковременные данные сессии Mini App, НЕ сервисный токен:
возраст проверяется до публикации (свежая сессия обязательна).

Демо (полностью офлайн):
  терминал 1: python3 examples/mock_agrobot.py 8099
  терминал 2: python3 examples/agrobot_orchestrator.py archive.zip \
                 --category "Хорш" --fake-transport --yes --demo

Переменные: AGROBOT_BASE_URL, AGROBOT_INITDATA (демо: подписывается
MOCK_AGROBOT_BOT_TOKEN), AGROBOT_UPLOAD_BATCH_* (батчинг клиента),
SECRETARY_* (движок, как в CLI). SECRETARY_FAKE_TRANSPORT_FAIL=1 — только
с --fake-transport: транспорт отвечает отказом (демо/тесты упавшего
прокси, на живой режим не влияет).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # репозиторий
sys.path.insert(0, str(Path(__file__).parent))         # examples/

from secretary.config import SecretaryConfig          # noqa: E402
from secretary.engine import TranscriptionEngine      # noqa: E402
from secretary.result import build_result, render_markdown  # noqa: E402
from secretary.store import Store                     # noqa: E402
from secretary.zipimport import import_zip            # noqa: E402
from agrobot_client import (AgrobotClient, AgrobotClientError)  # noqa: E402
from agrobot_tma import make_demo_init_data           # noqa: E402

DEMO_AGROBOT = "http://127.0.0.1:8099"
DEMO_BOT_TOKEN = "mock-bot-token"
TERMINAL = {"completed", "partial", "failed", "cancelled"}

#: коды возврата по итоговому workflow_status (см. докстринг модуля)
EXIT_CODES = {"complete": 0, "not_confirmed": 1, "partial": 2,
              "failed": 3}


def build_engine(args) -> TranscriptionEngine:
    """Движок Secretary: live-транспорт или детерминированный fake."""
    if args.fake_transport:
        from tests.fakes import FakeTransport
        env = dict(os.environ)
        env.setdefault("SECRETARY_MODEL", "mock-model")
        env.setdefault("SECRETARY_DB_PATH", str(Path(args.dest) / "db.sqlite"))
        env.setdefault("SECRETARY_JOBS_DIR", str(Path(args.dest) / "jobs"))
        config = SecretaryConfig.from_env(env)
        transport = FakeTransport()
        if env.get("SECRETARY_FAKE_TRANSPORT_FAIL") == "1":
            # демо/тесты упавшего прокси: каждый запрос — постоянный отказ
            # (PermanentTransportError не ретраится). Живой режим не затронут.
            from secretary.errors import PermanentTransportError

            def _fail(request: dict) -> dict:
                raise PermanentTransportError(
                    "demo: simulated proxy failure (SECRETARY_FAKE_TRANSPORT_FAIL)")

            transport = FakeTransport(responder=_fail)
        return TranscriptionEngine(config, transport, Store(config.db_path))
    config = SecretaryConfig.from_env()
    if not config.proxy_base_url:
        raise SystemExit("SECRETARY_PROXY_BASE_URL не задан (см. .env.example) "
                         "или используйте --fake-transport")
    from secretary.transport import HttpTransport
    transport = HttpTransport(
        config.proxy_base_url, config.proxy_api_key,
        auth_header=config.auth_header,
        timeout=config.request_timeout_seconds,
        allow_loopback_http=config.allow_loopback_http)
    return TranscriptionEngine(config, transport, Store(config.db_path))


def compute_transcription_status(job_ids: list[str],
                                 statuses: list[str | None]) -> tuple[str, dict]:
    """Статус этапа расшифровки по ВСЕМ заданиям текущего импорта (pass4, P1).

    job_ids — точный набор импорта (run scope); statuses — статус каждого
    задания из БД (None = задание отсутствует). Итог этапа:
      complete — аудио в импорте не было либо все задания completed;
      partial  — есть хоть один готовый (completed/partial) результат,
                но не все задания completed;
      failed   — ни одного успешного результата (все failed/cancelled/
                отсутствующие/оставшиеся в очереди).
    """
    counts = {"expected": len(job_ids), "completed": 0, "partial": 0,
              "failed": 0, "cancelled": 0, "not_finished": 0, "missing": 0}
    for status in statuses:
        if status is None:
            counts["missing"] += 1
        elif status in ("completed", "partial", "failed", "cancelled"):
            counts[status] += 1
        else:  # queued / running — до терминального статуса не дошло
            counts["not_finished"] += 1
    if counts["expected"] == 0:
        result = "complete"        # аудио в этом импорте не было
    elif counts["completed"] == counts["expected"]:
        result = "complete"        # все расшифрованы полностью
    elif counts["completed"] + counts["partial"] > 0:
        result = "partial"         # часть материала получена
    else:
        result = "failed"          # ни одного успешного результата
    return result, counts


def compute_publication_status(publication: dict, *, had_error: bool = False,
                               expected: int = 0) -> str:
    """Статус этапа публикации по фактическим результатам (pass3, P1 + pass4).

    publication — агрегаты {attempted, ok, failed} фазы publish;
    had_error   — публикация не началась (initData просрочено и т.п.);
    expected    — сколько элементов фаза ДОЛЖНА была опубликовать:
                  документы + расшифровки со статусом completed.

    Пустой счётчик (attempted=0) при непустом ожидании, при ошибке старта
    или при полном отсутствии материалов — НЕ complete (pass4, P1).
    """
    ok = int(publication.get("ok", 0))
    failed = int(publication.get("failed", 0))
    if had_error and ok == 0:
        return "failed"              # публикация не началась
    if expected <= 0 and ok + failed == 0:
        return "nothing_to_publish"  # публиковать было нечего
    if failed == 0 and ok >= expected:
        return "complete"
    if ok == 0:
        return "failed"
    return "partial"


def compute_workflow_status(*, transcription_status: str,
                            publication_status: str,
                            publication: dict) -> str:
    """Итоговый статус запуска — определяет код возврата (pass4, P1).

    Учитывает ОБА этапа: успешная загрузка документов не отменяет упавшую
    расшифровку (partial); ни одного опубликованного материала — failed,
    каким бы ни был статус этапов (ложноположительный «complete» при
    attempted=0 невозможен).
    """
    ok = int(publication.get("ok", 0))
    if publication_status == "not_confirmed":
        return "not_confirmed"
    if ok == 0:
        return "failed"
    if (transcription_status == "complete"
            and publication_status == "complete"):
        return "complete"
    return "partial"


def _failure_reason(transcription_status: str, publication: dict,
                    expected: int) -> str:
    """Человекочитаемое пояснение к workflow_status=failed."""
    if transcription_status != "complete":
        return ("transcription incomplete for this import "
                "(see transcription counts); no publishable transcripts")
    ok = int(publication.get("ok", 0))
    if expected > 0 and ok == 0:
        return "publication produced no successful uploads (see per-item results)"
    return "nothing was published: import yielded no publishable materials"


def phase_import(args, engine: TranscriptionEngine) -> dict:
    """Фаза 1: ZIP → аудио-задания + список документов (публикации НЕТ)."""
    documents: list[dict] = []

    def document_handler(path: Path, meta: dict) -> None:
        # поток А: документы только собираются; публикация — отдельная фаза
        documents.append({"path": str(path), "name": meta["name"],
                          "size": meta["size"], "sha256": meta["sha256"],
                          "mime": meta.get("mime")})

    dest = Path(args.dest) / "imports" / f"{Path(args.zip).stem}-{args.run_id}"
    manifest = import_zip(args.zip, engine, document_handler=document_handler,
                          dest_dir=dest, owner_id=args.owner,
                          language=args.language)
    print(f"[1/5] импорт (run {args.run_id}): аудио-заданий "
          f"{manifest['totals']['audio_jobs']}, "
          f"документов {manifest['totals']['documents']}, "
          f"отклонено {manifest['totals']['rejected']}")
    for entry in manifest["entries"]:
        if entry.get("kind") == "rejected":
            print(f"      отклонено: {entry['name']} — {entry['reason']}")
    return {"manifest": manifest, "documents": documents,
            "import_dir": str(dest)}


def phase_transcribe(engine: TranscriptionEngine,
                     job_ids: list[str]) -> dict:
    """Фаза 2: расшифровка ТОЛЬКО заданий текущего импорта (run scope).

    engine.run_pending(job_ids=...) не трогает чужие queued-задания в
    общей БД; transcripts собираются строго из этого же набора job_id.
    Возвращает статус этапа расшифровки и счётчики по ВСЕМ заданиям
    импорта — включая отсутствующие и оставшиеся в очереди (pass4, P1:
    итог запуска учитывает не только HTTP-загрузки).
    """
    print("[2/5] расшифровка аудио (воркер; область — текущий импорт)…")
    stats = engine.run_pending(job_ids=job_ids)
    extra = ""
    if stats.get("skipped_terminal"):
        extra += f", уже терминальных: {stats['skipped_terminal']}"
    if stats.get("missing"):
        extra += f", отсутствующих в БД: {stats['missing']}"
    print(f"      обработано заданий: {stats['processed']}{extra}")
    transcripts = []
    statuses: list[str | None] = []
    for job_id in job_ids:
        job = engine.store.find_job(job_id)
        statuses.append(None if job is None else job["status"])
        if job is None or job["status"] not in TERMINAL:
            continue  # незаконченные не показываются как готовые
        transcripts.append(
            build_result(job, engine.store.list_segments(job_id)))
    status, counts = compute_transcription_status(job_ids, statuses)
    detail = ", ".join(f"{key} {counts[key]}"
                       for key in ("completed", "partial", "failed",
                                   "cancelled", "not_finished", "missing")
                       if counts[key]) or "аудио не ожидалось"
    print(f"      этап расшифровки: {status} ({detail})")
    return {"stats": stats, "transcripts": transcripts,
            "status": status, "counts": counts}


def phase_review(documents: list[dict], transcripts: list[dict],
                 *, yes: bool, run_id: str) -> bool:
    """Фаза 3: сводка для человека и явное подтверждение публикации."""
    print("\n===== СВОДКА ДЛЯ ПРОВЕРКИ ЧЕЛОВЕКОМ =====")
    print(f"запуск {run_id}: документов к загрузке {len(documents)}, "
          f"расшифровок {len(transcripts)}")
    for doc in documents:
        print(f"  - {doc['name']} ({doc['size']} байт, {doc['mime']})")
    for result in transcripts:
        draft = result.get("structured_draft") or {}
        print(f"  - {result['source']['filename']}: {result['status']}, "
              f"сводка: {draft.get('summary', '—')}")
        for warning in (result.get("warnings") or [])[:3]:
            print(f"      предупреждение: {warning}")
    print("=========================================\n")
    if yes:
        print("подтверждение получено флагом --yes")
        return True
    if not sys.stdin.isatty():
        print("нет интерактивного терминала и нет --yes — публикация "
              "пропущена (проверка человеком не пройдена)")
        return False
    answer = input("Опубликовать документы и подтверждённые расшифровки "
                   "в базу знаний AgroBot? введите «да»: ").strip().lower()
    return answer in {"да", "yes", "y"}


def phase_publish(args, documents: list[dict], transcripts: list[dict]) -> dict:
    """Фаза 4: публикация ПОСЛЕ подтверждения — реальный upload API.

    Отказы (HTTP/сеть/расширение) фиксируются поштучно и не прерывают
    оставшиеся загрузки; агрегаты publication.{attempted,ok,failed}
    считаются по фактическим результатам — «published: true» при ошибке
    невозможно (ревью pass3, P1). Клиент шлёт ≤5 файлов за запрос.
    """
    client = AgrobotClient(args.agrobot_url, args.init_data)
    age = client.check_init_data_fresh()  # просрочено → ошибка наружу
    print(f"[4/5] публикация (initData возраст {age:.0f} с)…")
    report: dict = {"documents": [], "transcripts": []}
    ok = failed = 0

    if documents:
        paths = [Path(d["path"]) for d in documents]
        try:
            data = client.upload_documents(
                paths, category=args.category, comment=args.comment,
                continue_on_error=True)
            results = data["results"]
        except (AgrobotClientError, ValueError) as exc:
            results = [{"ok": False, "name": d["name"],
                        "code": "upload_failed", "error": str(exc)}
                       for d in documents]
        report["documents"] = results
        for item in results:
            if item.get("ok"):
                ok += 1
                print(f"      документ {item['name']}: OK, "
                      f"id={item.get('documentId')}")
            else:
                failed += 1
                print(f"      документ {item['name']}: ОТКАЗ "
                      f"({item.get('code')}) — {item.get('error', '')}")

    for result in transcripts:
        if result["status"] != "completed":
            print(f"      {result['source']['filename']}: статус "
                  f"{result['status']} — публикация расшифровки пропущена")
            report["transcripts"].append(
                {"name": result["source"]["filename"],
                 "status": result["status"], "published": False,
                 "reason": "not completed"})
            continue
        markdown = render_markdown(result)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"transcript-{result['job_id'][:8]}.md"
            path.write_text(markdown, encoding="utf-8")
            try:
                data = client.upload_documents(
                    [path], category=args.category,
                    comment=(args.comment or "") + " [расшифровка аудио]",
                    continue_on_error=True)
                item = data["results"][0]
            except (AgrobotClientError, ValueError) as exc:
                item = {"ok": False, "name": path.name,
                        "code": "upload_failed", "error": str(exc)}
            if item.get("ok"):
                ok += 1
            else:
                failed += 1
            report["transcripts"].append(
                {"name": path.name, "status": "completed",
                 "published": bool(item.get("ok")), "result": item})
            print(f"      расшифровка {path.name}: "
                  + ("OK" if item.get("ok")
                     else f"ОТКАЗ — {item.get('error', '')}"))
    report["publication"] = {"attempted": ok + failed, "ok": ok,
                             "failed": failed}
    return report


def finalize_run(imported: dict, transcribe: dict, upload_report: dict,
                 *, category: str | None) -> tuple[dict, str]:
    """Фаза 5 (сборка): три независимых статуса + итог (pass4, P1).

    transcription_status — из фазы 2 (все job_id импорта);
    publication_status   — по фактическим загрузкам против ожидавшихся;
    workflow_status      — итог по ОБЕИМ этапам, задаёт код возврата.
    Возвращает (report, workflow_status).
    """
    transcripts = transcribe["transcripts"]
    publication = upload_report.get(
        "publication", {"attempted": 0, "ok": 0, "failed": 0})
    # сколько публикация ДОЛЖНА была выдать: документы + completed-расшифровки
    expected = len(imported["documents"]) + sum(
        1 for t in transcripts if t["status"] == "completed")
    publication_status = compute_publication_status(
        publication, had_error="error" in upload_report, expected=expected)
    workflow_status = compute_workflow_status(
        transcription_status=transcribe["status"],
        publication_status=publication_status,
        publication=publication)
    report = {
        "published": int(publication.get("ok", 0)) > 0,
        "workflow_status": workflow_status,
        "transcription_status": transcribe["status"],
        "publication_status": publication_status,
        "transcription": dict(transcribe["counts"]),
        "publication": dict(publication),
        "category": category,
    }
    if workflow_status == "failed":
        report["reason"] = _failure_reason(
            transcribe["status"], publication, expected)
    return report, workflow_status


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ZIP → (расшифровка аудио + документы) → проверка "
                    "человеком → публикация в AgroBot",
        epilog="коды возврата — по итоговому workflow_status: 0 complete, "
               "1 not_confirmed, 2 partial, 3 failed (см. report.json: "
               "workflow_status / transcription_status / publication_status)")
    parser.add_argument("zip", help="архив (например, Хорш.zip)")
    parser.add_argument("--category", required=True,
                        help="категория базы знаний (1–120 символов)")
    parser.add_argument("--comment", default=None)
    parser.add_argument("--dest", default="data/orchestrator")
    parser.add_argument("--owner", default=None,
                        help="владелец заданий; по умолчанию "
                             "agrobot-orchestrator-<run_id> — уникален "
                             "для каждого запуска")
    parser.add_argument("--language", default=None)
    parser.add_argument("--fake-transport", action="store_true",
                        help="офлайн-демо: детерминированный транспорт")
    parser.add_argument("--demo", action="store_true",
                        help="мок AgroBot + подписанная демо-initData")
    parser.add_argument("--yes", action="store_true",
                        help="явное подтверждение публикации (для демо)")
    parser.add_argument("--agrobot-url",
                        default=os.environ.get("AGROBOT_BASE_URL", ""))
    parser.add_argument("--init-data",
                        default=os.environ.get("AGROBOT_INITDATA", ""))
    args = parser.parse_args()
    if not Path(args.zip).is_file():
        parser.error(f"архив не найден: {args.zip}")

    args.run_id = uuid.uuid4().hex[:12]
    if args.owner is None:
        args.owner = f"agrobot-orchestrator-{args.run_id}"

    if args.demo:
        args.agrobot_url = args.agrobot_url or DEMO_AGROBOT
        if not args.init_data:
            args.init_data = make_demo_init_data(DEMO_BOT_TOKEN)

    Path(args.dest).mkdir(parents=True, exist_ok=True)
    engine = build_engine(args)

    imported = phase_import(args, engine)                     # фаза 1
    # точные job_id ТЕКУЩЕГО импорта — из манифеста (kind == "audio");
    # порядок сохранён, дубликаты внутри архива схлопнуты
    job_ids = list(dict.fromkeys(
        entry["job_id"] for entry in imported["manifest"]["entries"]
        if entry.get("kind") == "audio"))
    transcribe = phase_transcribe(engine, job_ids)            # фаза 2
    transcripts = transcribe["transcripts"]
    run_info = {"run_id": args.run_id, "owner": args.owner,
                "zip": args.zip, "job_ids": job_ids,
                "import_dir": imported["import_dir"],
                "manifest": imported["manifest"].get("manifest_path"),
                "transcribe": transcribe["stats"]}

    if not phase_review(imported["documents"], transcripts, yes=args.yes,
                        run_id=args.run_id):                  # фаза 3
        report = {"published": False,
                  "workflow_status": "not_confirmed",
                  "transcription_status": transcribe["status"],
                  "publication_status": "not_confirmed",
                  "transcription": dict(transcribe["counts"]),
                  "publication": {"attempted": 0, "ok": 0, "failed": 0},
                  "reason": "human review not confirmed",
                  "run": run_info, "documents": [], "transcripts": []}
        _write_report(args, report)
        print("публикация НЕ выполнена: подтверждение не получено "
              f"(расшифровка: {transcribe['status']})")
        return EXIT_CODES["not_confirmed"]

    try:
        upload_report = phase_publish(args, imported["documents"],
                                      transcripts)             # фаза 4
    except AgrobotClientError as exc:
        # публикация не началась: initData просрочено / нет соединения
        print(f"ошибка публикации: {exc}")
        upload_report = {"documents": [], "transcripts": [],
                         "publication": {"attempted": 0, "ok": 0,
                                         "failed": 0},
                         "error": str(exc)}

    report, workflow = finalize_run(imported, transcribe, upload_report,
                                    category=args.category)   # фаза 5
    report["import"] = imported["manifest"]["totals"]
    report["run"] = run_info
    report["documents"] = upload_report.get("documents", [])
    report["transcripts"] = upload_report.get("transcripts", [])
    if "error" in upload_report:
        report["error"] = upload_report["error"]
    _write_report(args, report)
    publication = report["publication"]
    print(f"[5/5] отчёт: {Path(args.dest) / 'report.json'} "
          f"(история: reports/report-{args.run_id}.json)")
    print(f"      итог: {workflow} (расшифровка: "
          f"{report['transcription_status']}, публикация: "
          f"{report['publication_status']}; успешно загрузок "
          f"{publication.get('ok', 0)}, отказов "
          f"{publication.get('failed', 0)})")
    if report.get("reason"):
        print(f"      причина: {report['reason']}")
    return EXIT_CODES[workflow]


def _write_report(args, report: dict) -> None:
    """report.json (последний запуск) + отчёт конкретного запуска в историю."""
    dest = Path(args.dest)
    history = dest / "reports"
    history.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    (dest / "report.json").write_text(payload, encoding="utf-8")
    (history / f"report-{args.run_id}.json").write_text(payload,
                                                        encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
