"""Secretary Core — независимый модуль качественной расшифровки аудио.

Публичные компоненты:
- config.SecretaryConfig — конфигурация из окружения;
- transport.HttpTransport / transport.Transport — транспорт к прокси;
- engine.TranscriptionEngine — оркестрация заданий;
- store.Store — постоянное хранилище задач (SQLite);
- server.serve — HTTP API + воркер;
- cli.main — командный интерфейс;
- zipimport.import_zip — слой импорта ZIP-архивов.

Публикация в базу знаний выполняется ТОЛЬКО клиентом после проверки
человеком; модуль сам ничего не отправляет наружу, кроме запросов
расшифровки к настроенному прокси.
"""

__version__ = "0.2.0"
