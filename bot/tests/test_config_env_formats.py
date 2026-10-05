"""Регрессия: форматы TRACKED_CHAT_IDS в .env и слияние CHAT_DISCUSSION_GROUP.

Баг пользователя: TRACKED_CHAT_IDS=[-100...,-100...] парсился, но при удалении
скобок (CSV) бот вообще не запускался (ValueError у pydantic-settings).
"""
import pytest

from app.config import Settings, _normalize_int_list


def test_normalize_json_brackets():
    assert _normalize_int_list("[-1004335857237,-1004467842206]") == \
        [-1004335857237, -1004467842206]


def test_normalize_csv_no_brackets():
    assert _normalize_int_list("-1004335857237,-1004467842206") == \
        [-1004335857237, -1004467842206]


def test_normalize_spaces_and_garbage():
    # «скобка» может быть у каждого элемента: [-1001], [-1002]
    assert _normalize_int_list("[-1001] , junk, [-1002] ,") == [-1001, -1002]
    assert _normalize_int_list("-1001, -1002") == [-1001, -1002]


def test_normalize_empty():
    assert _normalize_int_list("") == []
    assert _normalize_int_list("[]") == []
    assert _normalize_int_list(None) is None
    assert _normalize_int_list([1, 2]) == [1, 2]  # уже список — не трогаем


def test_settings_accepts_both_formats():
    a = Settings(tracked_chat_ids="[-1004335857237,-1004467842206]")
    b = Settings(tracked_chat_ids="-1004335857237,-1004467842206")
    assert a.tracked_chat_ids == b.tracked_chat_ids == \
        [-1004335857237, -1004467842206]


def test_admin_ids_csv(monkeypatch):
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    s = Settings(admin_ids="26533379, 8786498725")
    assert s.admin_ids == [26533379, 8786498725]


def test_discussion_group_merged_without_dup(monkeypatch):
    monkeypatch.setenv("TRACKED_CHAT_IDS", "[-1004335857237]")
    monkeypatch.setenv("CHAT_DISCUSSION_GROUP", "-1004467842206")
    from app import config as cfg
    cfg.get_settings.cache_clear()
    try:
        s = cfg.get_settings()
        assert s.tracked_chat_ids == [-1004335857237, -1004467842206]
        # дубликат не удваивается
        monkeypatch.setenv("TRACKED_CHAT_IDS",
                           "[-1004335857237,-1004467842206]")
        cfg.get_settings.cache_clear()
        s2 = cfg.get_settings()
        assert s2.tracked_chat_ids.count(-1004467842206) == 1
    finally:
        cfg.get_settings.cache_clear()


def test_numeric_chat_id_keeps_bot_api_form():
    """Регрессия: id вида -100... НЕ должен превращаться в положительный.

    Старая реализация срезала "-100" и watched-множество содержало 4467842206,
    тогда как message.chat.id = -1004467842206 — учёт статистики молча не
    работал («чат НЕ в списке отслеживаемых» при корректном .env).
    """
    from app.services.access import numeric_chat_id
    assert numeric_chat_id(-1004467842206) == -1004467842206
    assert numeric_chat_id("-1004467842206") == -1004467842206
    # «сырой» MTProto id -> каноническая Bot API форма
    assert numeric_chat_id(4467842206) == -1004467842206
    assert numeric_chat_id("4467842206") == -1004467842206
    # username -> None, ЛС-id остаётся как есть
    assert numeric_chat_id("@some_channel") is None
    assert numeric_chat_id(-456) == -456


def test_is_watched_matches_message_chat_ids(monkeypatch):
    monkeypatch.setenv("TRACKED_CHAT_IDS",
                       "[-1004335857237,-1004467842206]")
    monkeypatch.setenv("CHANNEL_CHAT_ID", "-1004335857237")
    monkeypatch.setenv("CHAT_DISCUSSION_GROUP", "-1004467842206")
    from app import config as cfg
    cfg.get_settings.cache_clear()
    try:
        from app.services.access import is_watched, watched_chat_ids
        assert watched_chat_ids() == {-1004335857237, -1004467842206}
        assert is_watched(-1004467842206) is True   # группа обсуждений
        assert is_watched(-1004335857237) is True   # канал
        assert is_watched(-1009999999999) is False  # посторонний чат
    finally:
        cfg.get_settings.cache_clear()


def test_bot_api_forms_no_bogus_concatenations():
    from app.services.access import _bot_api_forms
    forms = _bot_api_forms(-1004467842206)
    assert "-1004467842206" in forms
    assert "-1001004467842206" not in forms  # мусорная склейка исключена
    assert all(f.lstrip("-").isdigit() for f in forms)
    # короткие id (ЛС) не должны порождать обрезки/склейки
    assert _bot_api_forms(-456) == ["-456"]
    # положительный «сырой» MTProto id -> обе формы
    raw_forms = _bot_api_forms(4467842206)
    assert "4467842206" in raw_forms and "-1004467842206" in raw_forms


# --- Issue #8 аудита: секрет вебхука по умолчанию в production ---

def test_default_webhook_secret_rejected_in_prod_webhook_mode():
    """IS_DEV=false + WEBHOOK_URL + дефолтный секрет = отказ запуска."""
    with pytest.raises(ValueError, match="WEBHOOK_SECRET_TOKEN"):
        Settings(is_dev=False, webhook_url="https://example.com",
                 webhook_secret_token="change-me-in-env")


def test_custom_webhook_secret_allowed_in_prod():
    s = Settings(is_dev=False, webhook_url="https://example.com",
                 webhook_secret_token="s3cr3t-token-xyz")
    assert s.webhook_secret_token == "s3cr3t-token-xyz"


def test_default_secret_ok_in_dev_and_polling_modes():
    # dev — можно (вебхук не используется)
    Settings(is_dev=True, webhook_url="https://example.com",
             webhook_secret_token="change-me-in-env")
    # prod без WEBHOOK_URL (long polling) — тоже можно
    Settings(is_dev=False, webhook_url=None,
             webhook_secret_token="change-me-in-env")
