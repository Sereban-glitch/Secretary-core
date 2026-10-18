# Инструкция подключения Secretary Core

Модуль расшифровки аудио для AgroBot и других приложений. **Модуль сам
ничего не публикует**: он готовит полный транскрипт и отдельный
структурированный черновик, а публикацию в базу знаний выполняет клиент
после проверки человеком.

## 1. Требования

| Компонент | Версия (проверено) | Назначение |
|---|---|---|
| Python | 3.12.14 (минимум 3.12) | stdlib only, внешних pip-зависимостей нет |
| ffmpeg / ffprobe | 7.1.5 | валидация формата, нарезка, конвертация mono 16 kHz Opus |

Зависимости pip отсутствуют намеренно (минимум зависимостей, версии
инструментов фиксируются в этом документе). ОС: Linux (rlimits для
дочерних ffmpeg-процессов применяются на POSIX, на других ОС пропускаются).

## 2. Конфигурация

Все переменные имеют префикс `SECRETARY_`. Минимум для живой работы:
`SECRETARY_PROXY_BASE_URL`, `SECRETARY_PROXY_API_KEY`, `SECRETARY_MODEL`.
Полный список — `.env.example`; ключевые:

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `SECRETARY_PROXY_BASE_URL` | — | адрес прокси **без** `/v1`; HTTPS для удалённых, http только loopback |
| `SECRETARY_PROXY_API_KEY` | — | ключ; передаётся только в заголовке, не в URL/логах |
| `SECRETARY_PROXY_AUTH_HEADER` | `x-api-key` | `x-api-key` или `authorization` (Bearer) — уточнить на экземпляре прокси |
| `SECRETARY_MODEL` | — | имя модели; задаёт владелец прокси, не выбирать произвольное |
| `SECRETARY_SEGMENT_SECONDS` | 900 | длина сегмента (сек) |
| `SECRETARY_OVERLAP_SECONDS` | 4 | перекрытие границ (без дублирования фраз: дубликаты стыка удаляются) |
| `SECRETARY_MAX_AUDIO_BYTES` | 10485760 | лимит сегмента для прокси; превышение → автоделение сегмента |
| `SECRETARY_MAX_ATTEMPTS` | 4 | bounded retry для временных ошибок |
| `SECRETARY_DB_PATH` | data/secretary.db | SQLite-хранилище задач |
| `SECRETARY_JOBS_DIR` | data/jobs | папка заданий (отдельный подкаталог на job) |
| `SECRETARY_API_TOKEN` | — | Bearer-токен HTTP API; **обязателен**, если сервер слушает не на loopback (иначе отказ старта) |
| `SECRETARY_CONCURRENCY` | 1 | тяжёлый воркер последовательный по умолчанию |
| `SECRETARY_RETRY_AFTER_CAP_SECONDS` | 120 | верхняя граница учёта Retry-After прокси (дольше не ждём) |
| `SECRETARY_WORKER_POLL_SECONDS` | 0.5 | интервал опроса очереди фоновым воркером |
| `SECRETARY_FFMPEG_TIMEOUT_SECONDS` | 180 | таймаут каждого запуска ffmpeg/ffprobe |
| `SECRETARY_MAX_ZIP_ENTRIES` | 200 | максимум записей в импортируемом ZIP |
| `SECRETARY_MAX_ZIP_TOTAL_BYTES` | 1073741824 | суммарный распакованный объём ZIP |
| `SECRETARY_MAX_ZIP_MEMBER_BYTES` | 268435456 | максимум одной записи ZIP |
| `SECRETARY_MAX_ZIP_RATIO` | 100 | максимум коэффициента сжатия (анти-зип-бомба) |
| `SECRETARY_URL_MAX_BYTES` | 536870912 | максимум файла по HTTPS/Google Drive (512 МиБ) |
| `SECRETARY_URL_MIN_FREE_AFTER_BYTES` | 536870912 | запас диска, который должен остаться после загрузки |
| `SECRETARY_URL_MAX_REDIRECTS` | 4 | максимум проверяемых перенаправлений |
| `SECRETARY_URL_TIMEOUT_SECONDS` | 120 | таймаут загрузки |
| `SECRETARY_VIDEO_MODEL` | `SECRETARY_MODEL` | мультимодальная модель для видео |
| `SECRETARY_VIDEO_MAX_DURATION_SECONDS` | 1800 | максимальная длительность видео |
| `SECRETARY_VIDEO_SEGMENT_SECONDS` | 120 | длина мультимодального фрагмента |
| `SECRETARY_VIDEO_KEEP_SOURCE` | false | хранить ли видео после успеха |

Файл `.env` в текущем каталоге подхватывается автоматически
(переменные окружения важнее файла). Все переменные из `.env.example`
читаются и валидируются: неверное значение — понятная ошибка запуска,
а не молчаливый запуск с чужим лимитом (покрыто тестами
test_config.py).

## 3. Быстрый старт

```sh
# офлайн-проверка ядра без сети
python3 -m secretary.cli selftest

# полный цикл на тестовом транспорте (без сети, ffmpeg настоящий)
python3 -m secretary.cli transcribe zapis.wav --fake-transport --markdown -o out.md

# живой цикл: настроить .env, затем
python3 -m secretary.cli transcribe zapis.wav --markdown -o out.md
python3 -m secretary.cli analyze-video lesson.mp4 --markdown -o video.md

# очередь и HTTP API
python3 -m secretary.cli create zapis.wav            # -> job_id
python3 -m secretary.cli worker                      # обрабатывает очередь
python3 -m secretary.cli serve --host 127.0.0.1 --port 8080
```

### Файл больше лимита Telegram

```sh
python3 -m secretary.cli fetch-url 'https://drive.google.com/file/d/ID/view'
```

Команда принимает только HTTPS, скачивает частями по 1 МиБ и возвращает путь,
размер и SHA256. Каждый переход повторно проверяется; внутренние, loopback,
link-local и служебные IP запрещены. Закрытая Drive-ссылка вместо файла отдаёт
страницу входа и будет отклонена с понятным сообщением.

Рекомендуемый поток AgroBot: «Файл / Ссылка» → потоковое скачивание → задача
Secretary для аудио или штатный извлекатель для документа → черновик → ручная
проверка → публикация текста. Исходник после этого можно удалить по сроку
хранения или оставить только внешнюю ссылку.

Тесты: `python3 -m unittest discover -s tests -t . -v` (166 тестов;
включая ffmpeg-интеграцию на синтетическом аудио и приёмочный сценарий
точного состава Хорш.zip: PDF + PPTX + DOCX + 4 MP3 + 1 M4A).

## 4. HTTP API

Сервер: `python3 -m secretary.cli serve` (по умолчанию 127.0.0.1:8080;
за внешним доступом — TLS-прокси владельца). Если задан
`SECRETARY_API_TOKEN`, все запросы требуют `Authorization: Bearer <токен>`.

- **Bind не на loopback без токена запрещён**: сервер откажется
  стартовать (открытый интерфейс без авторизации — недопустимая
  конфигурация). Сравнение токена — constant-time.
- **Загрузка потоковая**: тело `POST /v1/jobs` пишется во временный
  файл кусками по 1 МиБ и сразу переносится в job-каталог — в
  оперативной памяти не держится ни исходник, ни его копия (целевая
  VM ~1 ГБ). Требуется `Content-Length`; `Transfer-Encoding: chunked`
  не поддерживается (отказ до чтения тела, соединение закрывается).
  Превышение лимита — отказ до дочитывания тела.
- **Внутренние ошибки не раскрываются**: 500 отвечает обезличенным
  JSON (`internal server error`), подробности — только в логе сервера.
- Создание задания отказоустойчиво: файл сначала попадает в staging
  внутри job-каталога, затем пишется строка SQLite, затем атомарное
  переименование; сбой на любом шаге не оставляет «сломанный
  дубликат» (компенсация + ленивое восстановление при запуске).

| Метод и путь | Назначение |
|---|---|
| `GET /healthz` | проверка живости |
| `POST /v1/jobs` | создать задание; тело — байты аудио |
| `POST /v1/url-jobs` | скачать публичную HTTPS/Drive-ссылку и создать аудио-задание |
| `POST /v1/video-jobs` | создать задание; тело — MP4/MOV/WebM |
| `POST /v1/video-url-jobs` | скачать видео по HTTPS/Drive-ссылке |
| `POST /v1/youtube-jobs` | безопасно скачать одно публичное YouTube-видео и создать видео-задание |
| `GET /v1/jobs?status=&limit=` | список заданий |
| `GET /v1/jobs/{id}` | прогресс: status + счётчики сегментов |
| `GET /v1/jobs/{id}/result` | результат v1 (JSON) — только терминальные статусы |
| `GET /v1/jobs/{id}/markdown` | результат (Markdown) — только терминальные статусы |
| `POST /v1/jobs/{id}/cancel` | отмена (между сегментами/попытками) |
| `POST /v1/jobs/{id}/retry-failed` | вернуть failed-сегменты в очередь |

Серверный адаптер с несколькими владельцами может добавлять к чтению задания
`X-Secretary-Expected-Owner`. Если владелец задания не совпадает, API отвечает
404 и не раскрывает чужую расшифровку. Старые локальные клиенты без заголовка
остаются совместимыми.

Заголовки создания: `X-Secretary-Filename` (обязательный ASCII fallback),
`X-Secretary-Filename-B64` (необязательное base64 от исходного UTF-8 имени;
имеет приоритет над fallback и позволяет сохранить украинские/русские имена),
`X-Secretary-Language` (например `ru`/`uk`), `X-Secretary-Vocabulary`
(термины через запятую), `X-Secretary-Owner` (изоляция пользователей:
идемпотентность считается в пределах владельца).

Коды: 201 создано; 200 дубликат (идемпотентность: тот же владелец +
SHA256 + параметры) или OK; 400 неверный запрос/аудио; 401 без токена;
404 нет задания; 409 ещё не завершено; 413/400 превышен лимит тела.

Пример задания по ссылке (API должен быть закрыт токеном):

```sh
curl -sS -X POST http://127.0.0.1:8080/v1/url-jobs \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer ...' \
  --data '{"url":"https://drive.google.com/file/d/ID/view","language":"uk","vocabulary":["BERTHOUD","Raptor"]}'
```

YouTube принимает только `https://youtube.com/...` и `https://youtu.be/...`.
Плейлисты, live, приватные и слишком длинные ролики отклоняются. Требуется
`yt-dlp`; путь и лимиты задаются `SECRETARY_YTDLP_PATH`,
`SECRETARY_YOUTUBE_MAX_DURATION_SECONDS`, `SECRETARY_YOUTUBE_MAX_BYTES` и
`SECRETARY_YOUTUBE_TIMEOUT_SECONDS`. Временная копия после успешного анализа
удаляется по общей политике видео, а исходная ссылка сохраняется в provenance.

```sh
curl -sS -X POST http://127.0.0.1:8080/v1/youtube-jobs \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer ...' \
  --data '{"url":"https://youtu.be/VIDEO_ID","language":"uk","vocabulary":["BERTHOUD"]}'
```

```sh
curl -sS -X POST http://127.0.0.1:8080/v1/jobs \
  -H "X-Secretary-Filename: zapis.ogg" -H "X-Secretary-Language: uk" \
  -H "X-Secretary-Vocabulary: Опрыскувач,Berthoud" \
  -H "Authorization: Bearer $SECRETARY_API_TOKEN" \
  --data-binary @zapis.ogg
curl -sS http://127.0.0.1:8080/v1/jobs/$JOB_ID/result
```

## 5. Результат v1

Схема: `docs/schema/v1-result.schema.json`; пример на вымышленных
данных: `docs/examples/v1-result.example.json`. Состав:

```
version, media_kind, job_id, status, created_at, updated_at
source {filename, sha256, mime, duration, size}
segments [{id, start, end, status, text, warnings}]
transcript        — полный текст (неизменен, с маркерами границ нарезки)
structured_draft  — ОТДЕЛЬНЫЙ черновик: summary, topics, statements,
                    questionable_places (со ссылками на сегменты), speakers_note
warnings          — [неразборчиво], дубликаты стыков, сбойные сегменты...
provenance        — модель, версии промптов, параметры нарезки, транспорт
```

Правила честности:
- `status=completed` только при полном успешном покрытии сегментов;
  ошибка части — `partial`/`failed`;
- границы `[SEG n] ЧЧ:ММ:СС–ЧЧ:ММ:СС` — **измеренные границы нарезки**
  ffmpeg, не временные отметки модели (временные отметки в тексте
  помечаются предупреждением и не считаются проверенными);
- неразборчивое помечается `[неразборчиво]`, имена не выдумываются;
- структурированный черновик не заменяет транскрипт; никаких
  неподтверждённых заявлений о точности.

## 6. Подключение к AgroBot (пример без кода/ключей AgroBot)

Сценарий: эксперт присылает аудио → приложение создаёт задание →
показывает прогресс → по завершении человек проверяет результат →
**только после подтверждения** приложение публикует документ в AgroBot
(обычная загрузка документа в админку «База знаний»).

Точка входа AgroBot (конкретный URL/авторизация — на стороне владельца):
`POST {AGROBOT_BASE_URL}/api/admin/knowledge/documents`,
multipart-поля `files` + `category` (+ `comment`, `force`), заголовок
`Authorization: tma <URL-encoded initData>` — ровно как настоящий
Mini App (`encodeURIComponent`; Python-эквивалент —
`examples/agrobot_tma.py:encode_init_data_header`).

**initData — кратковременный документ (≤ 24 ч по умолчанию), НЕ
сервисный токен**: клиент проверяет возраст перед публикацией и
предупреждает, когда срок на исходе; для новой партии нужна свежая
сессия Mini App.

Готовые примеры:
- `examples/agrobot_client.py` — клиент реального контракта (multipart
  БАТЧАМИ не больше 5 файлов и 16 МиБ на запрос — как лимиты админки;
  разбор per-file результатов, свежесть initData; живость —
  `GET /api/healthz`, реальный маршрут AgroBot);
- `examples/agrobot_adapter.py` — флоу «расшифровка → проверка
  человеком → публикация»;
- `examples/agrobot_orchestrator.py` — полный приёмочный сценарий
  «ZIP → AgroBot» с РАЗДЕЛЬНЫМИ потоками: документы из архива
  загружаются как есть (их расшифровывать не нужно), аудио идут через
  расшифровку и публикуются только после явного подтверждения
  (`--demo` — полностью офлайн). ОБЛАСТЬ ДЕЙСТВИЯ ЗАПУСКА: уникальные
  run_id/owner на каждый импорт; расшифровка (`engine.run_pending(
  job_ids=...)`), сводка и публикация — строго job_id из манифеста
  текущего ZIP, чужие/старые задания общей SQLite не примешиваются.
  Отчёт — три независимых статуса (этапы не смешиваются):
  `transcription_status` complete|partial|failed (по ВСЕМ заданиям
  текущего импорта — учитываются и отсутствующие, и оставшиеся в
  очереди), `publication_status`
  complete|partial|failed|nothing_to_publish|not_confirmed + счётчики
  {attempted, ok, failed} и transcription-счётчики, итоговый
  `workflow_status` (задаёт код возврата) + история
  `reports/report-<run_id>.json`; коды возврата 0/1/2/3
  (partial=2, failed=3). Итог честный: упавшее/пропавшее/оставшееся в
  очереди аудио — минимум partial; ни одного опубликованного материала
  (включая «нечего публиковать») — failed; «complete» только когда весь
  ожидаемый сценарий выполнен;
- `examples/mock_agrobot.py` — мок, проверяющий авторизацию НАСТОЯЩИМ
  образом (подпись HMAC-SHA256 по алгоритму Telegram, свежесть
  auth_date, allowlist админов), а не «любую непустую строку»;
  `GET /api/healthz` — как реальный сервер.

Демо оркестратора (два терминала, офлайн):

```sh
python3 examples/mock_agrobot.py 8099
python3 examples/agrobot_orchestrator.py arhiv.zip \
    --category "Хорш" --fake-transport --yes --demo
```

Код AgroBot и секреты в примеры не включены; всё через переменные
окружения (`AGROBOT_BASE_URL`, `AGROBOT_INITDATA`, `MOCK_AGROBOT_*`,
`AGROBOT_UPLOAD_BATCH_MAX_FILES`, `AGROBOT_UPLOAD_BATCH_MAX_BYTES`).

Важно: расшифровка публикуется как **документ на проверку**
(`status=needs_review`/аналогичный механизм AgroBot), не как готовая
запись базы — публикация всегда после явного подтверждения человека.

## 7. ZIP-импорт (отдельный слой)

`python3 -m secretary.cli import-zip arhiv.zip --fake-transport` —
аудио из архива становятся заданиями, **документы** передаются
колбэку клиента (существующему обработчику AgroBot); транскрибатор
не создаёт «второй редактор базы». Allowlist документов
синхронизирован с реальной админкой AgroBot (kb-docs/storage.ts):
pdf, docx, **pptx**, xlsx, doc, ppt, xls, txt, md, csv, json,
jpg, jpeg, png, webp — приёмочный состав Хорш.zip (PDF+PPTX+DOCX +
4 MP3 + 1 M4A) проходит целиком, PPTX больше не отклоняется.
Защита: лимиты записей/объёма/коэффициента (настраиваются
`SECRETARY_MAX_ZIP_*`); запрет traversal, симлинков, вложенных
архивов; потоковая запись с контролем размера; общий манифест
`manifest.json` (в т.ч. mime документов и метка `?` при
расхождении сигнатуры).

## 8. Хранение и очистка (отдельно от публикации)

- `data/jobs/{job_id}/` — исходник (безопасная копия) и результаты;
  конвертированные сегменты удаляются после обработки
  (`SECRETARY_KEEP_SEGMENT_FILES=true` — оставить);
- SQLite `data/secretary.db` — задачи/сегменты (WAL);
- очистка: `python3 -m secretary.cli purge --older-than-days N
  [--dry-run]` — только завершённые задачи старше порога;
- перезапуск продолжает незавершённые сегменты (running → queued,
  временные ошибки получают новый бюджет повторов; постоянные — нет).

## 9. Безопасность

- ключ только в заголовке; в лог/ошибки не попадает (санитизация);
- HTTPS для удалённых прокси; http — только loopback
  (`SECRETARY_ALLOW_LOOPBACK_HTTP`);
- редиректы не отслеживаются (SSRF); userinfo в URL запрещён;
- ffmpeg/ffprobe — списком аргументов, без shell; таймауты; на Linux
  rlimits (AS 512 МБ, CPU 120 с);
- речь в записи и тексты — данные, не инструкции (промпты содержат
  явное указание);
- отмена — best-effort между сегментами и попытками (запрос в полёте
  ограничен таймаутом).

## 10. Ограничения

- один тяжёлый воркер (последовательная обработка) — осознанно;
  `SECRETARY_CONCURRENCY` зарезервирован;
- отмена в полёте не прерывает текущий HTTP-запрос мгновенно;
- точность расшифровки зависит от прокси/модели и не гарантируется;
  сомнительные места выносятся в `questionable_places`;
- Google Drive-ссылки — следующий этап (SSRF/права/лимиты), не реализован.

Что проверено автономно и что требует живого прокси — см. `HANDOFF.md`.
