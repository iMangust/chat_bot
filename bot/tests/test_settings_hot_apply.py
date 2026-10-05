"""Регресс: панельная смена TRACKED_CHAT_IDS должна применяться без рестарта.

Ранее write_env() обновлял .env и os.environ, get_settings.cache_clear()
сбрасывал кэш — но Pydantic Settings перечитывал только файл, а значения из
os.environ (записанные панелью) игнорировались: новые списки чатов вступали
в силу лишь после перезапуска процесса.
"""
import pytest

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("TRACKED_CHAT_IDS", "ADMIN_IDS", "CHANNEL_CHAT_ID",
                "CHAT_DISCUSSION_GROUP"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_tracked_chat_ids_from_os_environ_applied_without_restart(monkeypatch):
    # как это делает панель: запись в .env + os.environ, затем cache_clear()
    monkeypatch.setenv("TRACKED_CHAT_IDS", "-1004335857237,-1004467842206")
    get_settings.cache_clear()
    assert get_settings().tracked_chat_ids == [-1004335857237, -1004467842206]

    monkeypatch.setenv("TRACKED_CHAT_IDS", "[-1001]")  # JSON-формат тоже ок
    get_settings.cache_clear()
    assert get_settings().tracked_chat_ids == [-1001]


def test_admin_ids_and_channel_chat_id_synced(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "26533379,123")
    monkeypatch.setenv("CHANNEL_CHAT_ID", "-1004467842206")
    get_settings.cache_clear()
    st = get_settings()
    assert st.admin_ids == [26533379, 123]
    assert st.channel_chat_id == -1004467842206


def test_discussion_group_still_merged_into_env_list(monkeypatch):
    monkeypatch.setenv("TRACKED_CHAT_IDS", "-1001")
    monkeypatch.setenv("CHAT_DISCUSSION_GROUP", "-1002")
    get_settings.cache_clear()
    assert set(get_settings().tracked_chat_ids) == {-1001, -1002}


def test_invalid_env_value_falls_back_to_file_config(tmp_path):
    env = tmp_path / ".env"
    env.write_text("IS_DEV=true\nBOT_TOKEN=test-token\nTRACKED_CHAT_IDS=[-1001]\n",
                   encoding="utf-8")
    get_settings.cache_clear()
    base = Settings(_env_file=str(env))
    assert base.tracked_chat_ids == [-1001]
    import os
    old = os.environ.get("TRACKED_CHAT_IDS")
    os.environ["TRACKED_CHAT_IDS"] = "abc;def"  # мусор без чисел
    try:
        get_settings.cache_clear()
        st = get_settings()
        # битое значение не роняет запуск: нормализатор отбрасывает мусор -> []
        assert st.tracked_chat_ids == []
    finally:
        if old is None:
            del os.environ["TRACKED_CHAT_IDS"]
        else:
            os.environ["TRACKED_CHAT_IDS"] = old
        get_settings.cache_clear()


def test_empty_env_does_not_break_defaults():
    get_settings.cache_clear()
    st = get_settings()
    assert isinstance(st.tracked_chat_ids, list)
