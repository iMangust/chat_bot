"""Управление «полным Telegram API» (MTProto) из чата с ботом.. Только для ADMIN_IDS, только в ЛС (гейт и фильтр чата).

Команды:
    /mtproto   — статус Telethon-настройки (без показа секретов!)
    /syncnow   — немедленная синхронизация реестра доступа подписчиков
                 (дельта; «/syncnow full» — первичная полная загрузка;
                  «/syncnow rescan» — полный скан всех участников каждого чата)
    /accessdebug <user_id> — v2.0.4 подробный дебаг доступа: прогоняет ВСЮ
                 цепочку гейта для конкретного пользователя и показывает,
                 чем ответил каждый источник (Bot API / реестр / MTProto /
                 живой скан) — вместо гадания по логам

v2.0: механика приветствий удалена — подписчик канала/группы получает доступ
к боту автоматически (гейт проверяет реестр channel_subscribers).
"""
from __future__ import annotations

import asyncio
import contextlib
import html

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from app.config import get_settings

router = Router(name="mtproto_admin")

def _is_admin(message: Message) -> bool:
    ids = get_settings().admin_ids
    return bool(ids) and message.from_user is not None and message.from_user.id in ids

def _parse_ids(text: str | None) -> list[int]:
    """Достаёт числа user_id из строки («/accessdebug 123, 456» → [123, 456])."""
    import re
    return [int(m) for m in re.findall(r"\d{3,}", text or "")]

@router.message(F.chat.type == "private", Command("mtproto"))
async def cmd_mtproto_status(message: Message) -> None:
    if not _is_admin(message):
        return
    st = get_settings()
    from app.services import mtproto_client as mc
    lines = ["🔌 <b>Telegram API (MTProto / Telethon)</b>", ""]
    lines.append(f"• telethon установлен: {'✅' if mc.telethon_available() else '❌ (pip install telethon>=1.36)'}")
    has_id = bool(st.telegram_api_id)
    has_hash = bool(st.telegram_api_hash)
    lines.append(f"• API_ID: {'✅ задан' if has_id else '❌ нет (API_ID / TELEGRAM_API_ID в .env)'}")
    lines.append(f"• API_HASH: {'✅ задан' if has_hash else '❌ нет (API_HASH / TELEGRAM_API_HASH в .env)'}")
    sess = "строковая сессия (MTPROTO_SESSION_STRING)" if st.mtproto_session_string \
        else f"файл {st.mtproto_session}.session" if mc.session_configured() else "❌ нет"
    lines.append(f"• Сессия: {sess}")
    lines.append(f"• Подключено сейчас: {'✅' if mc.holder.is_connected() else '— (ленивое подключение)'}")
    lines.append(f"• Режим ответов: {st.mtproto_answer_mode}")
    lines.append(f"• Автосинк при старте: {'вкл' if st.mtproto_autosync else 'выкл'}, "
                 f"дельта каждые {st.mtproto_sync_minutes} мин" if st.mtproto_sync_minutes
                 else "• Дельта-синк по расписанию: выкл")
    lines.append("")
    lines.append("Настройка ключей: https://my.telegram.org → API development tools. "
                 "Используйте ВТОРОЙ аккаунт. Логины: "
                 "<code>python -m app.services.mtproto_sync --login</code>")
    await message.answer("\n".join(lines))

@router.message(F.chat.type == "private", Command("syncnow"))
async def cmd_syncnow(message: Message, bot: Bot) -> None:
    if not _is_admin(message):
        return
    st = get_settings()
    text = (message.text or "").lower()
    first_run = "full" in text or (st.mtproto_autosync and "delta" not in text)
    if "rescan" in text:
        msg = await message.answer("⏳ Полный скан участников всех чатов…")
        from app.services.mtproto_sync import full_rescan_subscribers
        try:
            res = await asyncio.wait_for(full_rescan_subscribers(), timeout=300)
        except Exception as exc:
            logger.error("/syncnow rescan failed: {}", exc)
            await msg.edit_text(f"❌ Скан не удался: <code>{exc}</code>")
            return
        if "skipped" in res:
            await msg.edit_text(f"⚠️ Скан пропущен: {res['skipped']}")
            return
        lines = [
            "✅ Полный скан завершён:",
            f"• просмотрено участников: {res['seen']}",
            f"• новых в реестре доступа: {res['added']}",
            f"• имён обновлено: {res['updated']}",
        ]
        for err in res.get("errors", []):
            lines.append(f"⚠️ {err}")
        lines.append("Все занесённые сразу получают доступ к боту "
                     "(подписчик = доступ).")
        await msg.edit_text("\n".join(lines))
        return
    msg = await message.answer(
        f"⏳ Запускаю MTProto-синхронизацию участников ({'полная' if first_run else 'дельта'})…")
    from app.services.mtproto_sync import sync_subscribers
    try:
        res = await asyncio.wait_for(sync_subscribers(first_run=first_run), timeout=300)
    except Exception as exc:
        logger.error("/syncnow failed: {}", exc)
        await msg.edit_text(f"❌ Синхронизация не удалась: <code>{exc}</code>")
        return
    await msg.edit_text(
        "✅ Синхронизация завершена:\n"
        f"• участников найдено: {res['total']}\n"
        f"• новых в реестре доступа: {res['added']}\n"
        "Занесённые участники сразу получают доступ к боту.")

@router.message(F.chat.type == "private", Command("accessdebug"))
async def cmd_access_debug(message: Message, bot: Bot) -> None:
    """v2.0.4: прогоняет ВСЮ цепочку гейта для конкретного пользователя.

    v2.0.5: принимает НЕСКОЛЬКО id («/accessdebug 162968450 712408242» или
    через запятую). Раньше при отказе вывод зависел от внутреннего снимка
    gate_last_reason, который заполняется только в одной ветке цепочки — и
    админ получал «нет данных», хотя лог всё знал. Теперь диагностика
    строится НЕЗАВИСИМО: по чатам гоняются getChatMember, MTProto-проба и
    (при недоказанности) живой скан; итог совпадает с вердиктом гейта, но
    объясняет его всегда.
    """
    if not _is_admin(message):
        return
    uid_list = _parse_ids(message.text)
    if not uid_list:
        await message.answer(
            "Использование: <code>/accessdebug &lt;user_id&gt; [user_id ...]</code>"
            " — покажет, чем ответил каждый источник проверки доступа "
            "(Bot API / реестр / MTProto / живой скан).")
        return
    uid_list = uid_list[:10]
    msg = await message.answer(
        f"⏳ Прогоняю цепочку доступа для {len(uid_list)} пользователь(ей)…")
    from app.middlewares import gate
    from app.handlers.access import ensure_registry_fresh, last_scan_stats
    st = get_settings()
    chats = gate.required_chats()
    lines = [f"🔎 <b>Диагностика доступа</b> ({len(uid_list)} пользователь(ей))", ""]
    if not chats:
        lines.append("⚠️ Обязательные чаты НЕ настроены (TRACKED_CHAT_IDS/"
                     "CHANNEL_* пустые) — гейт выключен, доступ есть всем.")
    for uid in uid_list:
        gate.reset_subscribe_cache(uid)
        allowed = await gate.is_channel_subscribed(bot, uid)
        cached = gate.is_subscribed_cached(uid)
        api_info: dict[str, str] = {}
        mt_info: list[str] = []
        registry = await gate._registry_row_state(uid)
        for cid, uname in chats:
            target = f"@{uname}" if uname else cid
            try:
                m = await bot.get_chat_member(target, uid)
                api_info[target] = getattr(m, "status", "?")
            except Exception as exc:
                api_info[target] = f"{type(exc).__name__}: {str(exc)[:70]}"
            s_mt = str(uname or cid).lstrip("@")
            if s_mt.startswith("-100") or s_mt.lstrip("-").isdigit():
                kind, res = await gate._mtproto_status(uname or cid, uid)
                mt_info.append(f"{target}: {kind}={str(res)[:80]}")
        scan_note = ""
        if not allowed and chats:
            ran = False
            with contextlib.suppress(Exception):
                ran = await ensure_registry_fresh(bot, uid)
            registry2 = await gate._registry_row_state(uid)
            if registry2 != registry:
                registry = registry2 + " (после живого скана)"
            seen, total = last_scan_stats()
            scan_note = (f"скан запускался: собрано {seen}, всего в чате ~{total}"
                         if ran or seen is not None else "живой скан не запускался")
        verdict = "✅ ДОСТУП РАЗРЕШЁН" if allowed else "⛔ ОТКАЗ"
        lines.append(f"<b>{uid}</b> — {verdict}"
                     + ("" if cached is None else
                        f" (кэш гейта: {'подписан' if cached else 'нет'})"))
        lines.append(f"   • Bot API: {html.escape(str(api_info)) if api_info else '—'}")
        lines.append(f"   • Реестр: {html.escape(str(registry))}")
        if mt_info:
            lines.append(f"   • MTProto: {html.escape('; '.join(mt_info))}")
        err = await gate.last_scan_error_safe()
        if err:
            lines.append(f"   • Причина молчания MTProto: {html.escape(str(err)[:160])}")
        if scan_note:
            lines.append(f"   • {html.escape(scan_note)}")
        if allowed:
            lines.append("   Гейт пускает. Если человек говорит «не могу» — "
                         "проблема НЕ в проверке подписки: он мог заблокировать "
                         "бота, писать не в ЛС этого бота, или апдейты до него "
                         "не доходят (см. лог «gate: allow»).")
        else:
            lines.append("   Отказ выдаётся ТОЛЬКО когда ни один источник не "
                         "подтвердил членство. Частые причины: бот не админ в "
                         "чате (тогда был бы fail-open — значит статус реально "
                         "'left'), MTProto-аккаунт не состоит в чате, у группы "
                         "выключен показ списка участников. См. также команду "
                         "/syncnow rescan.")
        lines.append("")
    lines.append("Подробности каждого тапа — в логе по строкам «gate:»/"
                 "«access:» для этих id.")
    await msg.edit_text("\n".join(lines)[:3900])
