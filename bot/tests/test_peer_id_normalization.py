"""Регрессия: нормализация id каналов к Bot API форме (-100...).

Регрессионный тест: TRACKED_CHAT_IDS, записанные без знака (1004335857237),
проходили через abs() в _tracked_ids()/userbot и в resolve_channel_entity
как положительные peer'ы. Telethon GetChannels отвергает их с
"Invalid channel object" / ChannelInvalidError — реакции не снапшотились
(«baseline снапшотов на 0»), хотя MTProto-аккаунт является создателем
канала. Фикс: numeric_chat_id/_resolve_numeric_target сохраняют/восстанавливают
знак -100; сравнения идут по канонической форме без abs().
"""
from __future__ import annotations

from app.services.access import numeric_chat_id


def test_numeric_chat_id_restores_negative_prefix():
    # положительный id канала из настроек → каноническая Bot API форма
    assert numeric_chat_id("1004335857237") == -1004335857237
    assert numeric_chat_id(1004335857237) == -1004335857237
    # уже правильный id остаётся как есть
    assert numeric_chat_id(-1004335857237) == -1004335857237
    assert numeric_chat_id("-1004335857237") == -1004335857237
    # username → None; старый малый отрицательный id ЛС/базовой группы — как есть
    assert numeric_chat_id("@botovstest") is None
    assert numeric_chat_id(-456) == -456


def test_resolve_numeric_target_maps_positive_channel_to_bot_api_form():
    from app.services.mtproto_client import _resolve_numeric_target

    # главный кейс бага: +100... из настроек должен стать "-100..."
    assert _resolve_numeric_target(1004335857237) == "-1004335857237"
    assert _resolve_numeric_target("1004335857237") == "-1004335857237"
    # внутренний MTProto id без префикса тоже доводит до -100 формы
    assert _resolve_numeric_target(4335857237) == "-1004335857237"
    # каноническая форма не портится
    assert _resolve_numeric_target(-1004335857237) == "-1004335857237"
    # username проходит нетронутым
    assert _resolve_numeric_target("@botovstest") == "@botovstest"


def test_tracked_ids_are_canonical_even_when_settings_have_unsigned_ids(monkeypatch):
    from app.config import get_settings
    from app.services import mtproto_reactions as mr

    st = get_settings()
    monkeypatch.setattr(st, "tracked_chat_ids", [1004335857237, "-1004467842206"], raising=False)
    tracked = mr._tracked_ids()
    # ни одного положительного peer'а — иначе GetChannels падает с
    # "Invalid channel object" (ошибка из логов пользователя)
    assert all(t < 0 for t in tracked), tracked
    assert tracked == {-1004335857237, -1004467842206}


def test_reaction_gate_matches_canonical_chat_id_without_abs():
    # _chat_id_of(PeerChannel) возвращает -100...; сравнение с _tracked_ids()
    # теперь идёт напрямую (без abs), поэтому проверим совместимость форм.
    from app.services.access import numeric_chat_id

    chat_id = int(f"-100{4335857237}")   # то, что отдаёт _chat_id_of
    tracked = {numeric_chat_id(x) for x in (1004335857237,)}
    assert chat_id in tracked


def test_userbot_tracked_set_keeps_sign():
    """userbot сравнивает event.chat_id (-100...) с множеством отслеживаемых
    чатов; abs() там терял знак и сообщения игнорировались."""
    from app.services.access import numeric_chat_id

    required = [("1004467842206", ""), ("@botovstest", "")]
    normed = {int(numeric_chat_id(c)) for c, _ in required if numeric_chat_id(c) is not None}
    assert normed == {-1004467842206}
    assert -1004467842206 in normed  # ровно тот формат, что приходит от Telethon
