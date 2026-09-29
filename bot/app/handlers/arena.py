from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import PetRepository, UserRepository
from app.keyboards.inline import open_slot_of, style_keyboard
from app.handlers.tamagotchi import set_pet_page
from app.services.pet_duels import arena_screen, fight
from app.services.tamagotchi import TamagotchiService
from app.utils.safe_edit import safe_edit_or_answer

router = Router(name="arena")

async def _pet_or_alert(cb: CallbackQuery, session: AsyncSession):
    pet = await PetRepository(session).get_by_user(cb.from_user.id)
    if pet is None:
        await cb.answer("🥚 Сначала заведи питомца: /start", show_alert=True)
    return pet

@router.callback_query(F.data == "arena:open")
async def arena_open(cb: CallbackQuery, session: AsyncSession) -> None:
    set_pet_page(cb.message.chat.id, 2)
    text, kb = await arena_screen(session, cb.from_user.id)
    await safe_edit_or_answer(cb.message, text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "arena:noop")
async def arena_noop(cb: CallbackQuery) -> None:
    await cb.answer("Питомец пока не готов к бою ⏳", show_alert=True)

@router.message(Command("arena"), F.chat.type == "private")
async def cmd_arena(message: Message, session: AsyncSession) -> None:
    text, kb = await arena_screen(session, message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "arena:fight")
async def arena_fight(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await _pet_or_alert(cb, session)
    if pet is None:
        return
    result = await fight(session, pet)
    try:
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "limit":
                msg = "😤 Лимит боёв на сегодня исчерпан — приходи завтра!"
            elif reason == "cooldown":
                msg = f"⏳ Питомец отдыхает. Следующий бой через {result.get('sec', 0)} сек."
            else:
                msg = "🔍 Равных соперников не нашлось — попробуй позже."
            await cb.answer(msg, show_alert=True)
        else:
            opp = result["opponent"]
            head = ("🏆 Победа! " if result["i_won"] else "🛡 Поражение — реванш рядом! ")
            report = (f"{head}{pet.name} ({result['power_a']}) vs "
                      f"{opp.name} ({result['power_b']}) · "
                      f"очков недели: {result['my_score']} · осталось боёв: {result['left']}")
            text, kb = await arena_screen(session, cb.from_user.id)
            await safe_edit_or_answer(cb.message, f"{report}\n\n{text}", reply_markup=kb)
            await cb.answer("🥊 Бой сыгран!")
    finally:
        await session.commit()

_BONUS_LABELS = {
    "duel_power": "💪 сила боя", "xp_pct": "⭐ XP", "coin_mult": "🪙 монеты",
    "feed_bonus_pct": "🍖 усвоение еды", "sleep_regen_pct": "⚡ восстановление сна",
    "happy_decay_pct": "😿 грусть/ч", "energy_decay_pct": "⚡ расход/ч",
    "hunger_decay_pct": "🍖 голод/ч", "hygiene_decay_pct": "🧼 грязь/ч",
    "health_decay_pct": "❤️ здоровье/ч", "train_yield_pct": "🏋️ тренировки",
    "flat_train": "🏋️ +к тренировке", "walk_coin_pct": "🚶 монеты прогулок",
    "walk_xp_pct": "🚶 XP прогулок", "play_happy_pct": "🎾 счастье в играх",
    "sick_chance_pct": "🤧 шанс болезни", "heal_boost": "💊 лечение",
    "happy_gain_flat": "😺 счастье за дело",
}

def _fmt_bonuses(b: dict[str, float]) -> str:
    parts = []
    for k, v in b.items():
        label = _BONUS_LABELS.get(k, k)
        if k.endswith("_pct") or k == "coin_mult":
            parts.append(f"{label} {'+' if v > 0 else ''}{v * 100:.0f}%")
        elif k.endswith("decay_pct"):
            parts.append(f"{label} {'−' if v < 0 else '+'}{abs(v) * 100:.0f}%")
        else:
            parts.append(f"{label} {'+' if v > 0 else ''}{v:g}")
    return ", ".join(parts) if parts else "—"

def _style_text(pet, user, svc, notice: str = "", slot_key: str | None = None) -> str:
    color_key, worn = svc.customization(pet)
    cinfo = svc.PET_COLORS.get(color_key or "")
    lines = [
        f"🎨 <b>Гардероб · {pet.name}</b>",
        f"Баланс владельца: 🪙 {user.coins}",
        "",
    ]
    if color_key:
        lines.append(f"Окрас: {cinfo['title']} — {cinfo.get('desc', '')}")
    else:
        lines.append("Окрас: ⚪ Классический (бесплатно)")
    gear = svc.gear_map(pet)
    equipped = [f"{svc.GEAR_SLOTS[k]} {e}" for k, e in gear.items() if e]
    lines.append("Надето: " + (" · ".join(equipped) if equipped else "ничего"))
    g = svc.gear_bonuses(pet)
    lines.append(f"Суммарные бонусы экипировки: {_fmt_bonuses(g)}")
    sets_now = svc.active_sets(pet)
    if sets_now:
        lines.append("🔥 Комбо-наборы: " + ", ".join(sets_now))
    owned_n = len((pet.settings_extra or {}).get("owned") or [])
    if owned_n:
        lines.append(f"🗄 В шкафу предметов: {owned_n} (снятые надеваются бесплатно)")
    if slot_key:
        item = next(((e, it) for e, it in svc.PET_ACCESSORIES.items()
                     if it["slot"] == slot_key and gear.get(slot_key) == e), None)
        head = svc.GEAR_SLOTS[slot_key]
        lines += ["", f"Открыт слот: <b>{head}</b>"]
        if item:
            lines.append(f"   Надето: {item[0]} {item[1]['title']} — {item[1].get('desc','')}")
    lines += ["", "<i>Купленную вещь можно снимать и надевать бесплатно;</i>",
              "<i>замена на другую в том же слоте — новая покупка.</i>"]
    if notice:
        lines.insert(0, notice)
    return "\n".join(lines)

async def _style_screen(cb: CallbackQuery, session: AsyncSession,
                        pet, notice: str = "", slots_page: int = 0,
                        item_page: int = 0) -> None:
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    svc = TamagotchiService(session)
    slot_keys = list(svc.GEAR_SLOTS.keys())
    slot_key = slot_keys[slots_page] if 0 <= slots_page < len(slot_keys) else None
    kb = style_keyboard(svc, pet, slots_page=slots_page, item_page=item_page)
    await safe_edit_or_answer(cb.message,
                              _style_text(pet, user, svc, notice, slot_key),
                              reply_markup=kb)

@router.callback_query(F.data == "pet:style")
async def style_open(cb: CallbackQuery, session: AsyncSession) -> None:
    set_pet_page(cb.message.chat.id, 1)
    pet = await _pet_or_alert(cb, session)
    if pet is None:
        return
    await _style_screen(cb, session, pet)
    await cb.answer()

@router.callback_query(F.data.startswith("style:slot:"))
async def style_slot(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await _pet_or_alert(cb, session)
    if pet is None:
        return
    parts = cb.data.split(":")
    svc = TamagotchiService(session)
    try:
        slots_page = list(svc.GEAR_SLOTS.keys()).index(parts[2])
        item_page = int(parts[3]) if len(parts) > 3 else 0
    except (IndexError, ValueError):
        return await cb.answer("Битая кнопка 😅", show_alert=True)
    await _style_screen(cb, session, pet, slots_page=slots_page, item_page=item_page)
    await cb.answer()

@router.callback_query(F.data.startswith("style:page:"))
async def style_page(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await _pet_or_alert(cb, session)
    if pet is None:
        return
    parts = cb.data.split(":")
    try:
        slots_page, item_page = int(parts[2]), int(parts[3])
    except (IndexError, ValueError):
        return await cb.answer("Битая кнопка 😅", show_alert=True)
    await _style_screen(cb, session, pet, slots_page=slots_page, item_page=item_page)
    await cb.answer()

@router.callback_query(F.data == "style:noop")
async def style_noop(cb: CallbackQuery) -> None:
    await cb.answer()

@router.callback_query(F.data.startswith("style:color:"))
async def style_color(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await _pet_or_alert(cb, session)
    if pet is None:
        return
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    if user is None:
        await cb.answer("Сначала /start", show_alert=True)
        return
    key = cb.data.split(":", 2)[2]
    svc = TamagotchiService(session)
    notice = await svc.buy_color(session, pet, user, key)
    await _style_screen(cb, session, pet, notice,
                        slots_page=open_slot_of(cb.data) or 0)
    from app.utils.fx import apply_effect
    if "✨" in notice:
        await apply_effect(cb, "equip", toast_override=notice.split("\n")[0][:200] or None)
    else:
        await cb.answer(notice[:120], show_alert="🪙" in notice)

@router.callback_query(F.data.startswith("style:wear:"))
async def style_wear(cb: CallbackQuery, session: AsyncSession) -> None:
    pet = await _pet_or_alert(cb, session)
    if pet is None:
        return
    users = UserRepository(session)
    user = await users.get(cb.from_user.id)
    if user is None:
        await cb.answer("Сначала /start", show_alert=True)
        return
    parts = cb.data.split(":")
    try:
        emoji = parts[3]
        slots_page = int(parts[-2])
        item_page = int(parts[-1])
    except (IndexError, ValueError):
        emoji, slots_page, item_page = (parts[2] if len(parts) > 2 else ""), 0, 0
    notice = await TamagotchiService(session).buy_accessory(session, pet, user, emoji)
    await _style_screen(cb, session, pet, notice,
                        slots_page=slots_page, item_page=item_page)
    from app.utils.fx import apply_effect
    if "надел" in notice or "достал из шкафа" in notice:
        await apply_effect(cb, "equip", toast_override=notice.split("\n")[0][:200] or None)
    elif "Активных наборов" in notice or "снял" in notice:
        await apply_effect(cb, "unequip", toast_override=notice.split("\n")[0][:200] or None)
    else:
        await cb.answer(notice[:120], show_alert=len(notice) > 120)

