# OPERATOR-GUIDE-RU.md — команды проверяющего и расшифровка результатов

Полное руководство для проверяющего по утилите проверки одной записи
`examples/proxy_check.py` (проход 5 + корректирующий проход по ревью
49a79b1). Владельцу предназначен отдельный простой документ:
OWNER-GUIDE-RU.md.

## 1. Назначение и границы

Утилита готовит и проводит проверку ОДНОЙ настоящей аудиозаписи через
приватный прокси в среде владельца. В среде разработки прокси недоступен,
поэтому живой успех не заявляется и не имитируется: проверяются только
механизмы, блокировки и честность результатов. Утилита НЕ интегрирована
с AgroBot — публикация невозможна по построению (в отчёте это поле
`agrobot.publication: none`). GitHub не используется.

Подкоманды: `doctor` (проверка среды), `run` (прогон записи:
dry-run по умолчанию / live с двойным подтверждением), `replay`
(возобновление незавершённых заданий последнего запуска),
`review-package` (пересборка пакета материалов).

Коды возврата: `0` — успех; `1` — ошибка выбора/аргументов; `2` — live
заблокирован (нет двойного подтверждения или настроек прокси);
`3` — среда не готова либо расшифровка не завершена (включая partial).

## 2. Переменные окружения

Основные (полный список — `.env.example` и `secretary/config.py`):

| Переменная | Назначение |
|---|---|
| `SECRETARY_PROXY_BASE_URL` | адрес прокси без `/v1`; https для удалённых, http только loopback |
| `SECRETARY_PROXY_API_KEY` | ключ; передаётся только в заголовке, не в URL/логах/отчётах |
| `SECRETARY_MODEL` | имя модели на экземпляре владельца (не выбирается самостоятельно) |
| `SECRETARY_PROXY_AUTH_HEADER` | `x-api-key` (по умолчанию) или `authorization` |
| `SECRETARY_DB_PATH` / `SECRETARY_JOBS_DIR` | по умолчанию `<dest>/db.sqlite` и `<dest>/jobs` |
| `SECRETARY_LIVE_CONFIRM` | `=1` — первое из двух подтверждений live |
| `SECRETARY_DISK_BUDGET_BYTES` | требуемый свободный объём под задания |
| `SECRETARY_KEEP_SEGMENT_FILES` | `=1` — диагностика: не удалять временные фрагменты |

Файл настроек `--env-file` — простой `KEY=VALUE`, рекомендуется права 0600.
Ключи не должны попадать в чат, репозиторий, логи и отчёты.

Ручки ТОЛЬКО для dry-run (тесты/демо отказов; на live не влияют):
`SECRETARY_FAKE_TRANSPORT_FAIL=1` — каждый запрос падает постоянно;
`SECRETARY_FAKE_TRANSPORT_TRANSIENT=N` — первые N запросов падают временно.

## 3. Команды

```
# готовность среды (ffmpeg/ffprobe, место, настройки, /proc)
python3 examples/proxy_check.py doctor --dest data/proxy-check

# dry-run: полная обработка записи БЕЗ сети (транспорт-двойник)
python3 examples/proxy_check.py run zapis.mp3 --dest data/proxy-check

# выбор записи из архива с несколькими аудио (молчаливого выбора нет)
python3 examples/proxy_check.py run arhiv.zip --select zapis.mp3 --dest data/proxy-check
python3 examples/proxy_check.py run arhiv.zip --index 2 --dest data/proxy-check
python3 examples/proxy_check.py run arhiv.zip --all --dest data/proxy-check

# live: только с ДВУМЯ подтверждениями (env + флаг); в прокси уходит
# ТОЛЬКО локально вырезанный короткий фрагмент (по умолчанию 60 c)
SECRETARY_LIVE_CONFIRM=1 python3 examples/proxy_check.py run zapis.mp3 \
    --dest data/proxy-check --env-file secretary.env --live --live-yes
# длина live-фрагмента: 1..600 c, например 30:
#   --live-fragment-seconds 30

# текст расшифровки в отчёт — ТОЛЬКО явным флагом (отчёт станет 0600)
python3 examples/proxy_check.py run zapis.mp3 --dest data/proxy-check --include-transcript

# возобновление после сбоя; --retry-failed — явный перезапуск упавших
python3 examples/proxy_check.py replay --dest data/proxy-check \
    --env-file secretary.env --live --live-yes --retry-failed

# пересборка пакета материалов из сохранённых артефактов (без транспорта)
python3 examples/proxy_check.py review-package --dest data/proxy-check
```

## 4. Артефакты в `--dest`

| Путь | Содержимое | Права |
|---|---|---|
| `report.json`, `report.md` | обезличенный отчёт (см. §5) | 0644 — можно передавать |
| `check-run.json` | состояние запуска: run_id, режим, job_ids, пути БД; без секретов | по umask |
| `doctor.json` | итоги doctor | по umask |
| `memory-samples.csv` | сэмплы RSS (t, self_kb, children_kb) | по umask |
| `review-<run_id>/` | пакет материалов: отчёты, `manifest.json`, `check-run.json`, csv, `doctor.json` (если был), `jobs/transcript-<id>.md` — ПОЛНЫЙ текст, и `package-manifest.json` — опись с sha256 каждого файла | каталоги 0700, файлы 0600 |
| `db.sqlite`, `jobs/` | задание, исходное аудио, текст | 0600 / 0700 |
| `imports/` | распакованное из ZIP (только выбранное) | внутри jobs-дерева не обязательны, но dest рекомендуется закрыть |

Временные аудиофрагменты (сегменты) удаляются после успешной обработки;
при ошибке — тоже (движок удаляет сегмент сразу после чтения); остаются
только с явным диагностическим флагом `--keep-segments` /
`SECRETARY_KEEP_SEGMENT_FILES=1`. Временный live-фрагмент (вырезанный
кусок записи) удаляется всегда — сразу после импорта, у задания
появляется собственная копия.

## 5. Чтение report.json

- `workflow_status` — итог: `complete` / `partial` / `failed`;
  `exit_code` соответствует (0 / 3 / 3).
- `transcription_status` + `transcription` — этап расшифровки по ВСЕМ
  заданиям запуска (`expected/completed/partial/failed/cancelled/
  not_finished/missing`): любой незавершённый статус понижает итог.
- `jobs[].status` — статус задания; `segments` — {total, done, failed};
  `transcript_chars/lines` — размеры текста (только счётчики);
  `duration_seconds` — длительность записи (не null); `source_bytes` /
  `source_sha256` — размер и контрольная сумма файла задания;
  `filename` — имя исходной записи. Текста расшифровки в отчёте НЕТ
  вообще (ни одного символа), пока не передан `--include-transcript`;
  тогда появляется `jobs[].transcript`, `transcript_included: true`,
  а файлы отчёта получают права 0600 вместо 0644.
- `source` — источник: имя, `size_bytes`, `sha256`; в live добавляется
  `live_fragment` (запрошенные/фактические секунды, размер, sha256,
  смещение) — доказательство, что наружу ушёл только фрагмент.
- `timings` — раздельное время этапов, с: `select_seconds`,
  `fragment_seconds` (только live), `import_seconds`,
  `transcribe_seconds`, `network_seconds` (время в транспорте),
  `report_seconds`, `total_seconds`.
- `memory` — `available`, `peak_rss_kb`, `avg_rss_kb`, `samples`:
  RSS процесса утилиты + дочерних ffmpeg/ffprobe (self+children).
  При недоступном /proc — честное `"unavailable"` (не 0 и не выдумка).
- `transport.requests` — число запросов к транспорту (в dry-run это
  локальный двойник, сети нет).
- `environment` — версии ffmpeg/ffprobe, свободное место, /proc, Python.
- `agrobot` — `{"publication": "none", "requests": 0}`: публикации нет.
- `replay` (если был) — `retried_failed`, `processed`,
  `skipped_terminal`, `original_mode`.
- Секретов нет: ключ прокси не читается в отчёт вообще; полный URL не
  включается (в live — только поле `proxy.host`); текст расшифровки —
  только в пакете материалов или по явному флагу.

## 6. Что проверять при приёмке

1. `doctor` корректен в обоих состояниях среды (готово/не готово).
2. Dry-run не делает сетевых запросов (в тестах — патч сокета).
3. Live без пары «env + флаг» блокируется ДО создания заданий.
4. Live обрабатывает только локально вырезанный фрагмент (по умолчанию
   60 c): задание создаётся из фрагмента, исходник не изменяется,
   временный фрагмент удаляется; `--live-fragment-seconds` вне
   1..600 отклоняется с кодом 1; в live нельзя `--all` с несколькими
   аудио.
5. Прерванный запуск возобновляется `replay` без повторной загрузки
   завершённых сегментов; упавшие — через `--retry-failed`.
6. Пустые/битые ответы прокси дают `failed`, а не успех.
7. В отчётах нет ключа, полного URL и ТЕКСТА расшифровки вообще
   (ни одного символа); с `--include-transcript` текст появляется,
   а отчёт закрывается правами 0600.
8. Права review-пакета: каталоги 0700, файлы 0600; в пакете есть
   опись `package-manifest.json` с sha256 каждого файла, и она
   сходится с фактом.
9. Длительность в отчёте не null; есть sha256/размеры источника и
   заданий; время этапов раздельно (select/fragment/import/
   transcribe+network/report).
10. Сегменты удалены после успеха (флаг диагностики — исключение).
11. Автотесты: `python3 -m unittest discover -s tests -t .`
   (246 тестов; медиатесты пропускаются без ffmpeg/ffprobe).

## 7. Регрессионный баг, закрытый в проходе 5

`recover_stale_jobs()` при каждом открытии БД возвращает временно-упавшие
сегменты в очередь, но задание оставалось `failed` — после перезапуска
процесса явный retry-failed видел «нечего перезапускать» и не оживлял
задание (расшифровка застревала навсегда). Исправлено в
`TranscriptionEngine.retry_failed`: queued делается и при наличии
незавершённых сегментов; регрессия — тест
`ResumptionRegressionTests.test_retry_failed_after_process_restart`
и CLI-сценарий replay `--retry-failed` (проходил только в одном
процессе, теперь — через реальный рестарт).
