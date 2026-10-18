"""CLI Secretary Core. python3 -m secretary.cli --help"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .config import SecretaryConfig
from .engine import TranscriptionEngine
from .media_engine import MediaEngine
from .errors import SecretaryError
from .result import build_result, render_markdown
from .store import Store
from .transport import HttpTransport
from .video_engine import VideoEngine


def _build_engine(args, *, require_transport: bool = True) -> MediaEngine:
    config = SecretaryConfig.from_env(env_file=args.env_file)
    transport = None
    if config.proxy_base_url:
        transport = HttpTransport(
            config.proxy_base_url, config.proxy_api_key,
            auth_header=config.auth_header,
            timeout=config.request_timeout_seconds,
            allow_loopback_http=config.allow_loopback_http)
    elif require_transport:
        raise SystemExit(
            "SECRETARY_PROXY_BASE_URL не задан: живой транспорт недоступен.\n"
            "Для офлайн-прогона используйте --fake-transport (только тесты),\n"
            "для serve/worker задайте прокси в окружении (см. .env.example).")
    if require_transport and not config.model:
        raise SystemExit("SECRETARY_MODEL не задан (см. .env.example)")
    store = Store(config.db_path)
    audio_engine = TranscriptionEngine(config, transport, store)
    return MediaEngine(audio_engine, VideoEngine(config, transport, store))


def _build_fake_engine(args) -> MediaEngine:
    """Движок с детерминированным тестовым транспортом (без сети)."""
    from tests.fakes import FakeTransport
    merged_env = dict(os.environ)
    merged_env.setdefault("SECRETARY_MODEL", "mock-model")
    config = SecretaryConfig.from_env(merged_env, env_file=args.env_file)
    store = Store(config.db_path)
    transport = FakeTransport()
    audio_engine = TranscriptionEngine(config, transport, store)
    return MediaEngine(audio_engine, VideoEngine(config, transport, store))


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------ commands
def cmd_serve(args) -> int:
    from .server import serve
    if args.fake_transport:
        engine = _build_fake_engine(args)
    else:
        engine = _build_engine(args)
    serve(engine, host=args.host, port=args.port,
          api_token=engine.config.api_token)
    return 0


def cmd_worker(args) -> int:
    engine = _build_engine(args)
    stats = engine.run_pending(max_jobs=1 if args.once else None)
    _print_json(stats)
    return 0


def cmd_create(args) -> int:
    engine = (_build_fake_engine(args) if args.fake_transport
              else _build_engine(args))
    created = engine.create_job(
        Path(args.file), owner_id=args.owner, language=args.language,
        vocabulary=[v.strip() for v in (args.vocabulary or "").split(",")
                    if v.strip()])
    _print_json(created)
    return 0


def cmd_transcribe(args) -> int:
    engine = (_build_fake_engine(args) if args.fake_transport
              else _build_engine(args))
    created = engine.create_job(
        Path(args.file), owner_id=args.owner, language=args.language,
        vocabulary=[v.strip() for v in (args.vocabulary or "").split(",")
                    if v.strip()])
    engine.run_pending(max_jobs=1)
    result = build_result(engine.store.get_job(created["job_id"]),
                          engine.store.list_segments(created["job_id"]))
    if args.markdown:
        output = render_markdown(result)
    else:
        output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        _print_json({"job_id": result["job_id"], "status": result["status"],
                     "written": args.output})
    else:
        print(output)
    return 0


def cmd_video(args) -> int:
    engine = _build_engine(args)
    created = engine.create_video_job(
        Path(args.file), owner_id=args.owner, language=args.language,
        vocabulary=[v.strip() for v in (args.vocabulary or "").split(",")
                    if v.strip()])
    engine.run_pending(max_jobs=1)
    result = build_result(engine.store.get_job(created["job_id"]),
                          engine.store.list_segments(created["job_id"]))
    output = render_markdown(result) if args.markdown else json.dumps(
        result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        _print_json({"job_id": result["job_id"], "status": result["status"],
                     "written": args.output})
    else:
        print(output)
    return 0


def cmd_status(args) -> int:
    engine = _build_engine(args, require_transport=False)
    _print_json(engine.progress(args.job_id))
    return 0


def cmd_result(args) -> int:
    engine = _build_engine(args, require_transport=False)
    result = build_result(engine.store.get_job(args.job_id),
                          engine.store.list_segments(args.job_id))
    if args.markdown:
        output = render_markdown(result)
    else:
        output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        _print_json({"job_id": args.job_id, "written": args.output})
    else:
        print(output)
    return 0


def cmd_cancel(args) -> int:
    engine = _build_engine(args, require_transport=False)
    _print_json({"job_id": args.job_id, "status": engine.cancel(args.job_id)})
    return 0


def cmd_retry_failed(args) -> int:
    engine = _build_engine(args, require_transport=False)
    _print_json(engine.retry_failed(args.job_id))
    return 0


def cmd_jobs(args) -> int:
    engine = _build_engine(args, require_transport=False)
    jobs = engine.store.list_jobs(status=args.status, limit=args.limit)
    _print_json({"jobs": [
        {"job_id": j["id"], "status": j["status"],
         "filename": j["source_filename"], "created_at": j["created_at"]}
        for j in jobs]})
    return 0


def cmd_purge(args) -> int:
    engine = _build_engine(args, require_transport=False)
    if args.dry_run:
        cutoff = time.time() - args.older_than_days * 86400
        jobs = engine.store.list_jobs(limit=10_000)
        stale = [j for j in jobs if j["updated_at"] < cutoff
                 and j["status"] in ("completed", "failed", "cancelled")]
        _print_json({"dry_run": True, "would_purge": [j["id"] for j in stale]})
        return 0
    cutoff = time.time() - args.older_than_days * 86400
    _print_json({"purged": engine.purge_finished_before(cutoff)})
    return 0


def cmd_import_zip(args) -> int:
    from .zipimport import import_zip
    engine = (_build_fake_engine(args) if args.fake_transport
              else _build_engine(args))
    dest = Path(args.dest or "data/imports") / Path(args.zip).stem
    handled: list[dict] = []

    def document_handler(path: Path, meta: dict) -> None:
        # Клиентский обработчик документов: здесь только фиксация факта.
        handled.append({"name": meta["name"], "size": meta["size"],
                        "sha256": meta["sha256"]})

    manifest = import_zip(args.zip, engine, document_handler=document_handler,
                          dest_dir=dest, owner_id=args.owner,
                          language=args.language)
    manifest["documents"] = handled
    _print_json(manifest)
    return 0


def cmd_fetch_url(args) -> int:
    from .urlimport import download_url
    config = SecretaryConfig.from_env(env_file=args.env_file)
    result = download_url(
        args.url, args.dest,
        max_bytes=config.url_max_bytes,
        max_redirects=config.url_max_redirects,
        timeout=config.url_timeout_seconds,
        min_free_after_bytes=config.url_min_free_after_bytes)
    _print_json({
        "status": "downloaded",
        "path": result.path,
        "filename": result.filename,
        "size": result.size,
        "sha256": result.sha256,
        "content_type": result.content_type,
        "source_host": result.source_host,
        "next": f"python3 -m secretary.cli transcribe {result.path!r}",
    })
    return 0


def cmd_selftest(args) -> int:
    """Офлайн-проверка ядра без сети и ffmpeg: контракт + сборка payload."""
    from .proxy_contract import build_request, extract_text
    request = build_request(b"synthetic", "audio/ogg", "selftest-model")
    assert request["messages"][0]["content"][1]["source"]["media_type"] == "audio/ogg"
    text = extract_text({"stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "ok"}]})
    assert text == "ok"
    _print_json({"selftest": "ok", "transport": "offline"})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secretary",
                                     description="Secretary Core — "
                                                 "модуль расшифровки аудио")
    parser.add_argument("--env-file", default=".env",
                        help="файл окружения (по умолчанию .env, если существует)")
    parser.add_argument("--log-level", default="info",
                        choices=["debug", "info", "warning", "error"])
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="HTTP API + воркер")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8080)
    serve_p.add_argument("--fake-transport", action="store_true",
                         help="демо: детерминированный транспорт без сети")
    serve_p.set_defaults(func=cmd_serve)

    worker_p = sub.add_parser("worker", help="обработать очередь (однократно/бесконечно)")
    worker_p.add_argument("--once", action="store_true")
    worker_p.set_defaults(func=cmd_worker)

    for name, help_text in (("create", "создать задание"),
                            ("transcribe", "создать + выполнить + результат")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("file")
        p.add_argument("--owner", default="local")
        p.add_argument("--language", default=None)
        p.add_argument("--vocabulary", default=None,
                       help="термины через запятую")
        p.add_argument("--fake-transport", action="store_true",
                       help="детерминированный тестовый транспорт (без сети)")
        if name == "transcribe":
            p.add_argument("--markdown", action="store_true")
            p.add_argument("-o", "--output", default=None)
        p.set_defaults(func=cmd_create if name == "create" else cmd_transcribe)

    video_p = sub.add_parser(
        "analyze-video", help="создать и выполнить анализ видео")
    video_p.add_argument("file")
    video_p.add_argument("--owner", default="local")
    video_p.add_argument("--language", default=None)
    video_p.add_argument("--vocabulary", default=None,
                         help="термины через запятую")
    video_p.add_argument("--markdown", action="store_true")
    video_p.add_argument("-o", "--output", default=None)
    video_p.set_defaults(func=cmd_video)

    status_p = sub.add_parser("status", help="прогресс задания")
    status_p.add_argument("job_id")
    status_p.set_defaults(func=cmd_status)

    result_p = sub.add_parser("result", help="результат v1 (JSON/Markdown)")
    result_p.add_argument("job_id")
    result_p.add_argument("--markdown", action="store_true")
    result_p.add_argument("-o", "--output", default=None)
    result_p.set_defaults(func=cmd_result)

    cancel_p = sub.add_parser("cancel", help="отменить задание")
    cancel_p.add_argument("job_id")
    cancel_p.set_defaults(func=cmd_cancel)

    retry_p = sub.add_parser("retry-failed", help="вернуть failed-сегменты в очередь")
    retry_p.add_argument("job_id")
    retry_p.set_defaults(func=cmd_retry_failed)

    jobs_p = sub.add_parser("jobs", help="список заданий")
    jobs_p.add_argument("--status", default=None)
    jobs_p.add_argument("--limit", type=int, default=50)
    jobs_p.set_defaults(func=cmd_jobs)

    purge_p = sub.add_parser("purge", help="очистка старых завершённых задач")
    purge_p.add_argument("--older-than-days", type=float, required=True)
    purge_p.add_argument("--dry-run", action="store_true")
    purge_p.set_defaults(func=cmd_purge)

    zip_p = sub.add_parser("import-zip", help="ZIP: аудио→задания, документы→колбэк")
    zip_p.add_argument("zip")
    zip_p.add_argument("--dest", default=None)
    zip_p.add_argument("--owner", default="local")
    zip_p.add_argument("--language", default=None)
    zip_p.add_argument("--fake-transport", action="store_true")
    zip_p.set_defaults(func=cmd_import_zip)

    url_p = sub.add_parser(
        "fetch-url", help="безопасно скачать большой файл по HTTPS/Google Drive")
    url_p.add_argument("url")
    url_p.add_argument("--dest", default="data/inbox")
    url_p.set_defaults(func=cmd_fetch_url)

    selftest_p = sub.add_parser("selftest", help="офлайн-проверка ядра")
    selftest_p.set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        return int(args.func(args) or 0)
    except SecretaryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
