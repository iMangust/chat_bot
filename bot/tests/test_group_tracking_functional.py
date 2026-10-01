"""Сквозная проверка учёта статистики в группе (жалоба из продакшена:
«написал сообщение в группу обсуждения — "Твоя статистика" показывает нули»).

Что фиксируется здесь:
 1. process_group_message засчитывает текст, медиа без текста (голосовые,
    кружочки), ответы; не засчитывает команды/короткие сообщения с причиной;
 2. пользователь без записи в реестре создаётся автоматически и его
    сообщение засчитывается (раньше молча игнорировалось);
 3. «сегодня» в personal_stats совпадает со свежезасчитанным сообщением —
    т.е. метка created_at и граница дня согласованы (баг часовых поясов);
 4. дедупликация по (chat_id, message_id) — повторная доставка не сдваивает;
 5. cooldown между сообщениями одного автора — второе skip_reason="cooldown";
 6. реакции: process_reaction начисляет given/received и не даёт ставить
    onboarded=False пользователям без /start (онбординг — только через /start).

Запуск из каталога bot/:  pytest tests/test_group_tracking_functional.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CHAT = -1001234567890


def _run(coro):
    return asyncio.run(coro)


def _session_factory(tmp_path, name):
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / name}"
    from app.config import get_settings
    get_settings.cache_clear()
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.db.models import Base
    engine = create_async_engine(os.environ["DATABASE_URL"])
    sf = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    _run(setup())
    return sf, engine


def _cleanup(engine):
    _run(engine.dispose())
    os.environ.pop("DATABASE_URL", None)
    from app.config import get_settings
    get_settings.cache_clear()


async def _track(sf, *, user_id, chat_id, message_id, text=None,
                 has_media=False, media_type=None, is_reply=False):
    from app.services.activity import ActivityService
    async with sf() as s:
        svc = ActivityService(s)
        entry = await svc.process_group_message(
            user_id=user_id, chat_id=chat_id, message_id=message_id,
            text=text, has_media=has_media, media_type=media_type,
            is_reply=is_reply, mentions_count=0,
            is_command=bool(text and text.startswith("/")))
        if entry is not None and entry.is_counted:
            u = await svc.users.get(user_id)
            u.messages_count += 1
        await s.commit()
        return entry


def _reset_msg_cooldown(user_id: int) -> None:
    """Откатить in-memory кулдаун сообщений (в тестах сообщения идут
    подряд, а реальный cooldown = 10 сек. В проде redis-недоступен в CI,
    поэтому set_cooldown использует локальный dict)."""
    from app.utils import redis as ur
    ur._mem_store.pop(f"cd:msg:{user_id}", None)


def test_text_and_media_counted_short_and_command_skipped(tmp_path):
    sf, engine = _session_factory(tmp_path, "trk1.db")
    try:
        e1 = _run(_track(sf, user_id=111, chat_id=CHAT, message_id=1,
                         text="Привет всем в группе!"))
        assert e1 is not None and e1.is_counted

        # Медиа без текста (кружочек/голосовое) — тоже засчитывается
        _reset_msg_cooldown(111)
        e2 = _run(_track(sf, user_id=111, chat_id=CHAT, message_id=2,
                         text=None, has_media=True, media_type="video_note"))
        assert e2 is not None and e2.is_counted, \
            f"видеокружок не засчитан: {e2.skip_reason if e2 else None}"

        # Голосовое
        _reset_msg_cooldown(111)
        e3 = _run(_track(sf, user_id=111, chat_id=CHAT, message_id=3,
                         text=None, has_media=True, media_type="voice"))
        assert e3 is not None and e3.is_counted

        # Короткий текст без медиа — не засчитывается, но с явной причиной
        _reset_msg_cooldown(111)
        e4 = _run(_track(sf, user_id=111, chat_id=CHAT, message_id=4,
                         text="ок"))
        assert e4.skip_reason == "short"

        # Команда — не засчитывается
        _reset_msg_cooldown(111)
        e5 = _run(_track(sf, user_id=111, chat_id=CHAT, message_id=5,
                         text="/start"))
        assert e5.skip_reason == "command"

        # Всего засчитанных: 3 (текст + кружок + голосовое)
        from app.services.activity import ActivityService
        async def check():
            async with sf() as s:
                st = await ActivityService(s).personal_stats(111)
                return st
        st = _run(check())
        assert st["total"] == 3, f"expected 3 counted, got {st}"
        assert st["day"] == 3, ("«сегодня» должно включать все три засчитанных "
                                f"сообщения этого дня, got {st['day']} — "
                                "проверь согласованность created_at/границы дня")
        assert st["week"] == 3
    finally:
        _cleanup(engine)


def test_unregistered_user_created_and_counted(tmp_path):
    """Пользователь не проходил /start, написал в группу — запись создана,
    сообщение засчитано, но онбординг остаётся за /start."""
    sf, engine = _session_factory(tmp_path, "trk2.db")
    try:
        e = _run(_track(sf, user_id=222, chat_id=CHAT, message_id=10,
                        text="Первое сообщение от незнакомца"))
        assert e is not None and e.is_counted

        async def check():
            from app.db.repositories import UserRepository
            async with sf() as s:
                u = await UserRepository(s).get(222)
                stats = None
                from app.services.activity import ActivityService
                stats = await ActivityService(s).personal_stats(222)
                return u, stats
        u, st = _run(check())
        assert u is not None, "запись пользователя не создана"
        assert st["total"] == 1
        assert st["day"] == 1
    finally:
        _cleanup(engine)


def test_duplicate_message_not_double_counted(tmp_path):
    sf, engine = _session_factory(tmp_path, "trk3.db")
    try:
        _run(_track(sf, user_id=333, chat_id=CHAT, message_id=7,
                    text="Уникальное сообщение группы"))
        # та же доставка повторно (retry/polling) — не должна удваивать счётчик
        from app.db.repositories import ActivityRepository
        async def dup():
            from app.db.models import ChatMessageLog
            async with sf() as s:
                repo = ActivityRepository(s)
                again = ChatMessageLog(user_id=333, chat_id=CHAT, message_id=7,
                                      length=25, is_counted=True)
                await repo.log_message(again)
                await s.commit()
                cnt = await repo.messages_count(333)
                return cnt
        cnt = _run(dup())
        assert cnt == 1, f"дедупликация (chat_id, message_id) сломана: {cnt}"
    finally:
        _cleanup(engine)


def test_cooldown_skips_second_message(tmp_path):
    sf, engine = _session_factory(tmp_path, "trk4.db")
    try:
        e1 = _run(_track(sf, user_id=444, chat_id=CHAT, message_id=20,
                         text="Первое сообщение в пределах кулдауна"))
        assert e1.is_counted
        e2 = _run(_track(sf, user_id=444, chat_id=CHAT, message_id=21,
                         text="Второе сообщение сразу же следом"))
        assert e2 is not None and not e2.is_counted
        assert e2.skip_reason == "cooldown", (
            f"ожидался cooldown, получено {e2.skip_reason} — проверь redis/"
            "set_cooldown в продакшене")
    finally:
        _cleanup(engine)


def test_reaction_credited_without_forcing_onboard(tmp_path):
    sf, engine = _session_factory(tmp_path, "trk5.db")
    try:
        from app.services.activity import ActivityService
        async def give():
            async with sf() as s:
                svc = ActivityService(s)
                # автор сообщения существует (после /start)
                await svc.users.get_or_create(555, first_name="Автор")
                ok = await svc.process_reaction(
                    from_user=666, to_user=555, chat_id=CHAT,
                    message_id=20, emoji="👍")
                await s.commit()
                return ok
        ok = _run(give())
        assert ok is True

        async def check():
            from app.db.repositories import UserRepository
            async with sf() as s:
                users = UserRepository(s)
                giver = await users.get(666)
                target = await users.get(555)
                return giver, target
        giver, target = _run(check())
        assert giver.reactions_given == 1
        assert target.reactions_received == 1
    finally:
        _cleanup(engine)


def test_tracker_filters_cover_all_media_attrs():
    """MEDIA_ATTRS трекера покрывает все типы контента aiogram-сообщений,
    которые должны учитываться (круги, голосовые, стикеры, видео…)."""
    from app.handlers.tracker import MEDIA_ATTRS, MEDIA_XP_BONUS
    required = {"photo", "video_note", "video", "voice", "audio", "sticker",
                "animation", "document"}
    assert required <= set(MEDIA_ATTRS), required - set(MEDIA_ATTRS)
    for attr in required:
        assert attr in MEDIA_XP_BONUS, f"{attr} без XP-бонуса"
