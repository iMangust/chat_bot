"""Стражи состояний «сон / прогулка»: запрет действий по таблице state_deny."""
import asyncio
from datetime import timedelta

from app.db.models import Pet
from app.services.tamagotchi import TamagotchiService
from app.utils.local_time import now as local_now


def _mk_pet(**kw):
    """Pet вне ORM: атрибуты задаём явно (дефолты SQLAlchemy не применяются)."""
    p = Pet()
    p.id, p.user_id, p.name, p.species = 1, 1, "Тест", "cat"
    p.last_update = local_now()
    p.level, p.xp = 1, 0
    p.hunger = p.happiness = p.energy = p.hygiene = 80.0
    p.health = 100.0
    p.is_sleeping = False
    p.sick_since = None
    p.settings_extra = {}
    p.walk_until = None
    p.walk_start_at = None
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_sleep_denies_all_but_wake():
    svc = TamagotchiService(None)
    pet = _mk_pet(is_sleeping=True)
    # Сон = нет взаимодействия: запрещены ВСЕ активные действия.
    for a in ("feed", "wash", "play", "train", "heal", "medicine", "toy",
              "walk", "duel", "game"):
        assert svc.state_deny(pet, a), f"во сне запрещено: {a}"
    # Разрешено: разбудить и нейтральные просмотры (карточка, стиль…)
    assert svc.state_deny(pet, "wake") is None
    assert svc.state_deny(pet, "card") is None
    assert svc.state_deny(pet, "style") is None


def test_walk_denies_home_actions():
    svc = TamagotchiService(None)
    now = local_now()
    pet = _mk_pet(walk_until=now + timedelta(hours=1), walk_start_at=now)
    for a in ("feed", "wash", "sleep", "train", "heal", "medicine", "toy", "game", "play", "duel"):
        assert svc.state_deny(pet, a), f"на прогулке запрещено: {a}"
    assert svc.state_deny(pet, "end_walk") is None
    assert svc.state_deny(pet, "where") is None


def test_active_pet_no_deny():
    svc = TamagotchiService(None)
    pet = _mk_pet()
    for a in ("feed", "wash", "sleep", "train", "heal", "play", "game", "walk", "duel"):
        assert svc.state_deny(pet, a) is None


def test_expired_walk_not_blocking():
    svc = TamagotchiService(None)
    now = local_now()
    pet = _mk_pet(walk_until=now - timedelta(minutes=5), walk_start_at=now - timedelta(hours=2))
    assert svc.state_deny(pet, "wash") is None  # прогулка истекла — питомец «дома»


def test_feed_blocked_while_sleeping():
    """Сквозная проверка: feed() возвращает текст отказа, статы не меняются."""
    async def run():
        svc = TamagotchiService(None)
        pet = _mk_pet(is_sleeping=True, hunger=50.0, created_at=local_now())
        msg = await svc.feed(pet, {"hunger": 20})
        assert "😴" in msg or "спит" in msg.lower()
        assert abs(pet.hunger - 50.0) < 0.5  # страж до начисления: только естественный спад, не +20
    asyncio.run(run())


def test_play_blocked_on_walk():
    async def run():
        svc = TamagotchiService(None)
        now = local_now()
        pet = _mk_pet(walk_until=now + timedelta(hours=1), walk_start_at=now,
                      energy=90.0, happiness=50.0, created_at=now)
        msg = await svc.play(pet, won=True)
        assert "🚶" in msg or "гуляет" in msg.lower()
        assert pet.happiness < 51.0  # без награды за игру — только естественный спад
    asyncio.run(run())


# ── UI: кнопки состояний НЕ прячутся, а всегда видны отдельной строкой ──
def test_hub_kb_shows_state_buttons_on_every_tab():
    """Гуляющему питомцу кнопка «Вернуть с прогулки» видна на ВСЕХ вкладках,
    и ни одна обычная кнопка не исчезает (запрет — только alert при тапе)."""
    from app.handlers.tamagotchi import _hub_kb
    from app.keyboards.inline import PET_PAGES
    svc = TamagotchiService(None)
    pet = _mk_pet(walk_until=local_now() + timedelta(hours=2),
                  walk_start_at=local_now())
    assert svc.on_walk(pet)
    for page in range(len(PET_PAGES)):
        kb = _hub_kb(svc, pet, chat_id=1, page=page)
        rows = kb.model_dump()['inline_keyboard']
        data = [b.get('callback_data') for r in rows for b in r if b.get('callback_data')]
        texts = [b['text'] for r in rows for b in r]
        assert 'pet:end_walk' in data, f"вкладка {page}: пропала кнопка возврата!"
        # ничего не прячем: все кнопки вкладки на месте
        for lbl, cb in PET_PAGES[page][1]:
            assert cb in data, f"вкладка {page}: спрятана кнопка {lbl}"
    # спящий: «Разбудить» виден, обычные кнопки тоже
    pet2 = _mk_pet(is_sleeping=True)
    kb = _hub_kb(svc, pet2, chat_id=1, page=0)
    data = [b.get('callback_data') for r in kb.model_dump()['inline_keyboard'] for b in r]
    assert 'pet:wake' in data and 'pet:feed' in data
    # без состояний — лишних строк нет
    pet3 = _mk_pet()
    kb = _hub_kb(svc, pet3, chat_id=1, page=0)
    data = [b.get('callback_data') for r in kb.model_dump()['inline_keyboard'] for b in r]
    assert 'pet:wake' not in data and 'pet:end_walk' not in data
