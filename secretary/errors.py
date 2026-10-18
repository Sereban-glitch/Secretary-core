"""Иерархия ошибок Secretary Core.

Транспортные ошибки делятся на временные (можно повторять ограниченно)
и постоянные (повтор бессмысленен). Сообщения санитизируются: секрет
(ключ прокси) не должен попадать ни в текст ошибки, ни в лог.
"""


class SecretaryError(Exception):
    """Базовый класс всех ошибек модуля."""


class ConfigError(SecretaryError):
    """Некорректная конфигурация (env/файл)."""


class ValidationError(SecretaryError):
    """Некорректные входные данные задания (файл, имя, параметры)."""


class AudioError(SecretaryError):
    """Аудио не прошло проверку или ffmpeg/ffprobe завершились ошибкой."""


class VideoError(SecretaryError):
    """Видео не прошло проверку или ffmpeg/ffprobe завершились ошибкой."""


class TransportError(SecretaryError):
    """Ошибка обмена с прокси. retry_after — секунды из Retry-After, если были."""

    transient: bool = False

    def __init__(self, detail: str, *, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:  # pragma: no cover - тривиально
        return self.detail


class TransientTransportError(TransportError):
    """Временная ошибка: сеть, таймаут, 408/425/429, 5xx, битый JSON-ответ."""

    transient = True


class PermanentTransportError(TransportError):
    """Постоянная ошибка: 4xx (кроме временных), redirect, явный error-ответ.

    Повтор такого запроса не даёт нового результата и не выполняется.
    """


class JobCancelled(SecretaryError):
    """Операция прервана запросом отмены задания."""


class NotFoundError(SecretaryError):
    """Задание не найдено."""


class ZipImportError(SecretaryError):
    """ZIP не прошёл проверку безопасности/лимитов."""


class UrlImportError(SecretaryError):
    """Удалённый файл не прошёл проверку или не был безопасно загружен."""


class YouTubeImportError(SecretaryError):
    """YouTube-ссылка или загрузка yt-dlp не прошла безопасную проверку."""
