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
