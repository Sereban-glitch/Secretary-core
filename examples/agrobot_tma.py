#!/usr/bin/env python3
"""Утилиты Telegram Mini App initData — зеркало алгоритма AgroBot.

Портировано 1:1 с `core/artifacts/api-server/src/lib/initdata.ts` (ветка
feature/knowledge-admin-miniapp) на stdlib Python, чтобы:

- клиент (адаптер/оркестратор) отправлял заголовок ровно в том же виде,
  что и настоящий Mini App: `Authorization: tma <encodeURIComponent(initData)>`;
- мок (examples/mock_agrobot.py) проверял РЕАЛЬНЫЙ формат авторизации:
  подпись HMAC-SHA256 по официальному алгоритму Telegram, свежесть
  auth_date, allowlist админов — а не «любая непустая строка».

Официальный алгоритм (core.telegram.org/bots/webapps):
  secret_key    = HMAC_SHA256(bot_token, "WebAppData")
  check_string  = поля initData без hash, отсортированные по алфавиту,
                  "key=value", разделитель "\n"
  hash          = hex(HMAC_SHA256(check_string, secret_key))

initData — КРАТКОВРЕМЕННЫЙ документ (по умолчанию ≤ 24 ч, env
TMA_AUTH_MAX_AGE_SECONDS на сервере AgroBot), НЕ сервисный токен:
не хранить как постоянный, перед публикацией проверять возраст.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, quote

INIT_DATA_MAX_AGE_DEFAULT_SECONDS = 24 * 60 * 60
INIT_DATA_FUTURE_SKEW_SECONDS = 60


class InitDataError(ValueError):
    """initData не прошло проверку (причина — в .args[0])."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def encode_init_data_header(init_data: str) -> str:
    """Значение заголовка Authorization — как настоящий Mini App.

    Клиент AgroBot шлёт `tma ${encodeURIComponent(initData)}`; Python-экв
    encodeURIComponent — quote(s, safe="!*'()"): кодирует всё, кроме
    латиники/цифр/`-_.!~*'()` — байт в байт поведение JS.
    """
    return "tma " + quote(init_data, safe="!~*'()")


def _data_check_string(pairs: list[tuple[str, str]]) -> str:
    without_hash = [(k, v) for k, v in pairs if k != "hash"]
    ordered = sorted(without_hash, key=lambda kv: kv[0])
    return "\n".join(f"{k}={v}" for k, v in ordered)


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Подписывает поля как это делает Telegram (для тестов/демо)."""
    pairs = list(fields.items())
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"),
                      hashlib.sha256).digest()
    digest = hmac.new(secret, _data_check_string(pairs).encode("utf-8"),
                      hashlib.sha256).hexdigest()
    # URL-encoded форма: значения кодируются, hash — hex-строка
    from urllib.parse import urlencode
    return urlencode(pairs + [("hash", digest)])


def verify_init_data(init_data: str, bot_token: str, *,
                     max_age_seconds: int = INIT_DATA_MAX_AGE_DEFAULT_SECONDS,
                     now: float | None = None,
                     admin_ids: set[str] | None = None) -> dict:
    """Проверка как в AgroBot tmaAuth. Возвращает данные пользователя.

    Шаги: парсинг query-string → подпись (constant-time) → user JSON →
    auth_date (не старше max_age, не из будущего) → (опц.) allowlist.
    Кидает InitDataError с причиной: missing/malformed/missing_hash/
    bad_signature/missing_user/bad_user/stale/future/not_admin.
    """
    if not init_data or not init_data.strip():
        raise InitDataError("missing")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
    except ValueError as exc:
        raise InitDataError("malformed") from exc
    fields = dict(pairs)
    if "hash" not in fields:
        raise InitDataError("missing_hash")

    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"),
                      hashlib.sha256).digest()
    computed = hmac.new(secret, _data_check_string(pairs).encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, fields["hash"].lower()):
        raise InitDataError("bad_signature")

    user_raw = fields.get("user")
    if not user_raw:
        raise InitDataError("missing_user")
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise InitDataError("bad_user") from exc
    if not isinstance(user, dict) or not isinstance(user.get("id"), int) \
            or user["id"] <= 0:
        raise InitDataError("bad_user")

    try:
        auth_date = int(fields.get("auth_date", ""))
    except ValueError as exc:
        raise InitDataError("malformed") from exc

    current = now if now is not None else time.time()
    if current - auth_date > max_age_seconds:
        raise InitDataError("stale")
    if auth_date - current > INIT_DATA_FUTURE_SKEW_SECONDS:
        raise InitDataError("future")

    if admin_ids is not None and str(user["id"]) not in admin_ids:
        raise InitDataError("not_admin")

    return {"userId": user["id"],
            "firstName": user.get("first_name"),
            "username": user.get("username"),
            "authDate": auth_date}


def init_data_age_seconds(init_data: str,
                          now: float | None = None) -> float:
    """Возраст initData в секундах (для предупреждения о просрочке)."""
    try:
        auth_date = int(dict(parse_qsl(init_data,
                                       keep_blank_values=True)).get(
                                           "auth_date", ""))
    except ValueError:
        return float("inf")
    return (now if now is not None else time.time()) - auth_date


def make_demo_init_data(bot_token: str, user_id: int = 700000001,
                        age_seconds: float = 5.0,
                        first_name: str = "Demo",
                        username: str = "demo_admin") -> str:
    """Валидная подписанная initData на вымышленном пользователе (демо/тесты)."""
    user = json.dumps({"id": user_id, "first_name": first_name,
                       "username": username}, ensure_ascii=False)
    return sign_init_data(
        {"auth_date": str(int(time.time() - age_seconds)),
         "query_id": "AAHdemo-query-id",
         "user": user},
        bot_token)


if __name__ == "__main__":  # ручная проверка: подпись → проверка → возраст
    token = "demo-bot-token"
    data = make_demo_init_data(token)
    print("initData:", data[:80] + "…")
    verified = verify_init_data(data, token)
    print("verified:", verified)
    print("age:", init_data_age_seconds(data), "s")
    print("header:", encode_init_data_header(data)[:80] + "…")
