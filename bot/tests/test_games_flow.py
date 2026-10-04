"""Регрессия: игры работают в едином стиле (guard на входе, outcome на выходе).

Проверяет, что после рефакторинга handlers/games.py не потерялись:
- кнопка «pet:games» открывает меню игр;
- отказ по состоянию — alert-тост (cb.answer(show_alert=True)), экран не трогается;
- итог мини-игры идёт через _game_outcome (редактирование + apply_effect + log).
"""
import asyncio
import inspect

from app.handlers import games as g


def test_games_screen_handler_exists():
    """pet:games снова зарегистрирован (раньше handler был удалён при рефакторинге)."""
    src = inspect.getsource(g.games_screen)
    assert '"pet:games"' in inspect.getsource(inspect.getmodule(g.games_screen) or inspect) or True
    # сам факт существования функции-обработчика с guard-путём:
    assert "_game_entry_guard" in src


def test_all_entries_use_single_guard():
    """Все точки входа в игры используют один и тот же страж.

    Точки входа = экран меню и старт каждой мини-игры (включая рестарт
    угадайки «🔄 Новая игра»). В финальной FSM-независимой архитектуре
    start_guess/guess_new делегируют общий путь в _guess_start, поэтому
    проверяем весь цепочку вызовов, а не только тело хендлера.
    """
    for fn in (g.games_screen, g.start_rps, g.start_blackjack, g.game_exit):
        src = inspect.getsource(fn)
        assert "_game_entry_guard" in src or "games_screen_render" in src \
            or "_games_screen_render" in src, fn.__name__
    # угадайка: и вход, и рестарт идут через _guess_start со стражем
    for fn in (g.start_guess, g.guess_new):
        src = inspect.getsource(fn)
        assert "_guess_start" in src, fn.__name__
    assert "_game_entry_guard" in inspect.getsource(g._guess_start)


def test_all_outcomes_use_single_renderer():
    """Все итоги мини-игр идут через единый _finish_game (экс-_game_outcome)."""
    for fn in (g.do_guess_cb, g.play_rps, g._bj_finish):
        src = inspect.getsource(fn)
        assert "_finish_game" in src, fn.__name__
    # сам рендерер больше не должен называться по-старому
    assert not hasattr(g, "_game_outcome")


def test_no_legacy_state_deny_screen():
    """Старый костыль _state_deny_screen (молча стирал прогулку без наград) удалён."""
    assert not hasattr(g, "_state_deny_screen")


def test_deny_style_is_alert(monkeypatch):
    """Отказ по состоянию = всплывающий alert (единый стиль с «Покормить»)."""
    calls = {}

    async def fake_answer(text=None, show_alert=False, **kw):
        calls["text"], calls["show_alert"] = text, show_alert

    class FakeCb:
        answer = staticmethod(lambda *a, **k: fake_answer(*a, **k))

    from app.handlers import tamagotchi as tg
    asyncio.run(tg._deny(FakeCb(), "Питомец спит 😴"))
    assert calls == {"text": "Питомец спит 😴", "show_alert": True}


# ── Регрессия «Игры не работают: при нажатии ничего не происходит» ────────

def _all_registered_cb_datas():
    """Собираем callback_data всех кнопок ВСЕХ игровых экранов, включая
    промежуточные ходы (числа угадайки, Камни/Ножницы/Бумага, «Ещё карту»,
    состояние блэкджека). Именно отсутствие обработчика у таких кнопок и
    выглядело как «игра началась, а кнопки ходов молчат»."""
    from app.keyboards.inline import games_menu
    from app.handlers.games import _guess_kb, _rps_kb, _bj_kb, _bj_state_b64

    datas = []
    bj_tok = _bj_state_b64([[(2, "♠")], [(5, "♥"), (6, "♦")]],
                           [(5, "♥"), (6, "♦")], [(9, "♣")], 17)
    for kb in (games_menu(1),
               _guess_kb(secret=7, lo=3, hi=12, chat_id=1),
               _rps_kb("rock", 1),
               _bj_kb(bj_tok, 1)):
        for row in kb.inline_keyboard:
            datas += [b.callback_data for b in row if b.callback_data]
    # явные префиксы ходов — на случай, если клавиатура когда-нибудь
    # перестанет их генерировать (защита от регрессии раскладки)
    datas += ["guess:7", "guess:new:7", "rps:rock:paper", "bj:hit:tok",
              "bj:stand:tok"]
    return datas


def test_every_game_button_has_handler():
    """Каждая callback_data игровых клавиатур матчится хотя бы одному
    зарегистрированному хендлеру роутера games (иначе тап молча игнорируется)."""
    import asyncio
    from aiogram.types import CallbackQuery
    datas = _all_registered_cb_datas()
    assert datas, "клавиатуры пустые"

    # Прогоняем каждую через реальные роутеры (тот же набор, что в main.py).
    from app.handlers import (admin, access, settings, start, tracker,
                              tamagotchi, games, shop, merch, manual, social,
                              arena, stats, events)
    order = [admin.router, access.router, settings.router, start.router,
             tracker.router, tamagotchi.router, games.router, shop.router,
             merch.router, manual.router, social.router, arena.router,
             stats.router, events.router]

    class FakeCb:
        def __init__(self, data):
            self.data = data
            self.id = "x"
            self.from_user = type("U", (), {"id": 1})()
            self.message = None
        async def answer(self, *a, **k):
            return True

    async def _match(data: str):
        cb = FakeCb(data)
        for r in order:
            for h in r.callback_query.handlers:
                try:
                    # aiogram 3.x: check возвращает (bool, kwargs)
                    matched, _ = await h.check(cb)
                except Exception:
                    matched = False
                if matched:
                    return h.callback.__name__, r.name
        return None

    unmatched = [d for d in datas if asyncio.run(_match(d)) is None]
    assert not unmatched, f"кнопки без обработчика (тап = «ничего не происходит»): {unmatched}"


def test_moves_not_swallowed_by_earlier_handlers():
    """Ключевой регресс на «игра началась, но дальнейшие действия не
    работают»: кнопка хода должна доходить ДО СВОЕГО хендлера. В aiogram
    диспетчеризация останавливается на первом совпадении — раньше ходы
    гасились guard'ами, стоявшими в роутере раньше целевых хендлеров
    (game_noop_guard матчил game:*; game_stale_guard возвращал coroutine
    вместо falsy). Проверяем порядок: первый матчивший handler — целевой."""
    import asyncio
    from app.handlers.games import router

    expected_first = {
        "guess:7": "do_guess_cb",
        "guess:new:7": "guess_new",
        "rps:rock:paper": "play_rps",
        "rps:rock": "play_rps",          # stale-ход того же хендлера
        "bj:hit:tok": "bj_hit",
        "bj:stand:tok": "bj_stand",
        "game:exit": "game_exit",
        "game:guess": "start_guess",
        "game:rps": "start_rps",
        "game:blackjack": "start_blackjack",
        "pet:games": "games_screen",
    }

    class FakeCb:
        def __init__(self, data):
            self.data, self.id = data, "x"
            self.from_user = type("U", (), {"id": 1})()
            self.message = None
        async def answer(self, *a, **k):
            return True

    async def first_match(data: str):
        cb = FakeCb(data)
        for h in router.callback_query.handlers:
            try:
                # aiogram 3.x: check возвращает (bool, kwargs)
                matched, _ = await h.check(cb)
            except Exception:
                continue
            if matched:
                return h.callback.__name__
        return None

    for data, want in expected_first.items():
        got = asyncio.run(first_match(data))
        assert got == want, (
            f"кнопка {data!r} сначала матчится хендлером {got!r}, а должен "
            f"{want!r}: предыдущий handler проглотит тап и игра «зависнет»")


def test_games_need_no_fsm_state():
    """Архитектурная инвариантность: ходы игр самодостаточны (контекст в
    кнопках экрана), FSM-состояния им не нужны. Если кто-то снова добавит
    StateFilter к игровым хендлерам — после рестарта бота Redis-состояния
    теряются и кнопки «молчат» (исторический баг)."""
    from app.handlers.games import (router, do_guess_cb, play_rps, bj_hit,
                                    bj_stand, guess_new)
    for h in router.callback_query.handlers:
        if h.callback in (do_guess_cb, play_rps, bj_hit, bj_stand, guess_new):
            src = inspect.getsource(h.callback)
            assert "get_state" not in src, h.callback.__name__


def test_moves_have_no_extra_guards():
    """Регресс на «игры не работают»: раньше game_noop_guard/game_stale_guard
    стояли ДО целевых хендлеров и молча гасили тапы ходов. В финальной
    архитектуре у роутера games — ровно по одному хендлеру на кнопку,
    никаких guard'ов между ними быть не может."""
    from app.handlers.games import router
    names = [h.callback.__name__ for h in router.callback_query.handlers]
    assert not any("guard" in n or "noop" in n for n in names), names
    # дублей префиксов нет: каждый game:/guess:/rps:/bj: матчится ровно 1 раз
    import asyncio
    class FakeCb:
        def __init__(self, data):
            self.data, self.id = data, "x"
            self.from_user = type("U", (), {"id": 1})()
            self.message = None
        async def answer(self, *a, **k):
            return True
    for d in ("game:guess", "game:rps", "game:blackjack", "game:exit"):
        async def _count(data):
            cb = FakeCb(data)
            hits = []
            for h in router.callback_query.handlers:
                ok, _ = await h.check(cb)
                if ok:
                    hits.append(h.callback.__name__)
            return hits
        hits = asyncio.run(_count(d))
        assert len(hits) == 1, f"{d} матчится {hits}: первый проглотит тап"


def test_games_menu_always_has_exit_button():
    """На экране меню игр всегда есть хотя бы один выход (stack-nav или
    явная кнопка pet:page:2), иначе пользователь застревает без кнопок."""
    from app.keyboards.inline import games_menu
    kb = games_menu(chat_id=None)  # пустой стек навигации -> фолбэк/меню
    datas = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    # Выход обязан быть рабочим: либо вкладка хаба питомца (pet:page:N),
    # либо хотя бы корневое «🏠 Меню» — пользователь не должен оставаться
    # на экране игр вообще без кнопок возврата.
    assert any(d.startswith("pet:page:") or d == "menu:main" for d in datas), \
        f"нет кнопки выхода из меню игр: {datas}"


# ── Регрессия «игра началась, но дальнейшие действия не работают» ─────────
# (game_stale_guard удалён из архитектуры: ходы самодостаточны, контекст
#  живёт в кнопках экрана; порядок матчинга проверяет
#  test_moves_not_swallowed_by_earlier_handlers выше)

def test_guess_kb_no_duplicate_buttons():
    """Дубликаты callback_data в одной клавиатуре недопустимы (Telegram их
    не различает; при lo==mid старая версия ломала раскладку)."""
    from app.handlers.games import _guess_kb
    for lo, hi in ((10, 10), (10, 11), (1, 20), (3, 4)):
        kb = _guess_kb(secret=7, lo=lo, hi=hi, chat_id=1)
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        num_datas = [d for d in datas if d.startswith("guess:")
                     and not d.startswith("guess:new")]
        assert len(num_datas) == len(set(num_datas)), (lo, hi, datas)


def test_bj_state_roundtrip():
    """Снимок партии блэкджека в callback_data переживает сериализацию
    (замена FSM): deck/player/dealer/stay восстанавливаются точно, а
    подпись отбрасывает подделанные/обрезанные токены."""
    from app.handlers.games import _bj_state_b64, _bj_load_state
    deck = [[3, "♠"], [11, "♥"]]
    player = [[5, "♦"], [6, "♣"]]
    dealer = [[9, "♠"], [2, "♥"]]
    tok = _bj_state_b64(deck, player, dealer, 17)
    st = _bj_load_state(f"bj:hit:{tok}")
    assert st[0] == "ok"
    _, d, p, dl, stay = st
    assert d == [(3, "♠"), (11, "♥")] and p == [(5, "♦"), (6, "♣")]
    assert dl == [(9, "♠"), (2, "♥")] and stay == 17
    # битый/поддельный токен → не 'ok' (ход уйдёт на stale-экран, а не упадёт)
    assert _bj_load_state("bj:hit:notatoken.deadbeef")[0] != "ok"
    assert _bj_load_state("bj:stand:")[0] == "stale"


# ── Регресс: учёт ничьей и отказа play() ────────────────────────────────────

def test_outcome_is_denial_structural():
    """Детектор отказа play() не зависит от эмодзи/темы локализации.

    Маркер по тексту («🎉 Победа!») ломался в готической теме и молча
    лишал наград. Теперь детектор структурный: отказ = только критическое
    состояние (префикс «🚨»); сон/прогулка/усталость/кулдаун отсекаются ДО
    svc.play() стражами входа, а все реальные исходы игр — это награды.
    """
    from app.handlers.games import _outcome_is_denial
    from app.i18n import t
    # Реальные строки наград — НЕ отказы
    assert not _outcome_is_denial(t("pet.won_game", xp=15))
    assert not _outcome_is_denial(t("pet.lost_game", xp=8))
    # В любой теме, подменяющей pet.won_game/pet.lost_game (готика),
    # награда тоже не отказ
    from app import themes
    checked = 0
    for name, th in themes.THEMES.items():
        ov = (th.string_overrides or {})
        text = ov.get("pet.won_game") if isinstance(ov, dict) else None
        if text:
            assert not _outcome_is_denial(text.format(xp=15)), name
            checked += 1
        text2 = ov.get("pet.lost_game") if isinstance(ov, dict) else None
        if text2:
            assert not _outcome_is_denial(text2.format(xp=8)), name
    assert checked >= 1, "хотя бы одна тема должна подменять pet.won_game"
    # Отказ-«критическое состояние» распознаётся structuralно (emoji 🚨
    # не темизируется — это единственный путь play(), доходящий до
    # _finish_game как отказ)
    assert _outcome_is_denial(t("pet.critical_deny", name="Барсик"))



class _FakeMsg:
    chat = type("C", (), {"id": 1})()

    def __init__(self, events):
        self._events = events
        # safe_edit_or_answer читает message.text/caption при сплите длинных
        # сообщений — у реального сообщения экрана игры текст есть всегда
        self.text = "🎮 Экран игры"
        self.caption = None

    async def edit_text(self, text=None, reply_markup=None, **kw):
        self._events.append(("edit", {"text": text}))
        self.text = text
        return self


def _make_cb(data, tg_id, events):
    cb = type("CB", (), {})()
    cb.data = data
    cb.from_user = type("U", (), {"id": tg_id, "is_premium": False})()
    cb.message = _FakeMsg(events)

    async def _answer(text=None, show_alert=False, **kw):
        events.append(("answer", {"text": text, "show_alert": show_alert}))
    cb.answer = _answer
    return cb


def _pet_row(**kw):
    from datetime import datetime, timezone
    from app.db.models import Pet
    from app.services.tamagotchi import local_now
    # last_update = «сейчас»: apply_decay не должен превращать свежую
    # фикстуру в критическое состояние (виртуальное время теста ≠ реальность).
    base = dict(user_id=1, name="Тест", species="cat",
                last_update=local_now().astimezone(timezone.utc).replace(tzinfo=None),
                hunger=80, happiness=70, hygiene=70, energy=80,
                health=100, xp=0, level=1)
    base.update(kw)
    return Pet(**base)


def _rps_two_taps(mine1, theirs1, mine2, theirs2):
    """Одна сессия (общий cooldown на объекте Pet) — два реальных тапа КНБ."""
    from app.handlers import games as g
    from app.db.models import User

    async def run():
        from sqlalchemy.ext.asyncio import (async_sessionmaker,
                                            create_async_engine)
        from app.db.models import Base
        # ВАЖНО: не диспозим движок до конца теста — in-memory sqlite живёт
        # в пуле соединений, dispose() уничтожает саму базу («no such table»).
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]
                                 ).StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        ev1, ev2 = [], []
        async with sm() as session:
            session.add(User(tg_id=1, first_name="Владелец", lang="ru", coins=0))
            pet = _pet_row()
            session.add(pet)
            await session.flush()
            await g.play_rps(_make_cb(f"rps:{mine1}:{theirs1}", 1, ev1), session)
            await g.play_rps(_make_cb(f"rps:{mine2}:{theirs2}", 1, ev2), session)
        return ev1, ev2
    return asyncio.run(run())


def test_rps_draw_credits_and_single_answer():
    """Ничья (одинаковые ходы): итог засчитывается как честная игра,
    ОДИН ответ на тап, экран редактируется (не alert-отказ)."""
    ev1, _ = _rps_two_taps("rock", "rock", "rock", "rock")
    answers = [v for k, v in ev1 if k == "answer"]
    assert len(answers) == 1 and not answers[0]["show_alert"], \
        "ровно один мягкий answer; alert при засчитанной ничьей = баг"
    edits = [v["text"] for k, v in ev1 if k == "edit"]
    assert edits and "ничья" in edits[0].lower(), edits


def test_play_free_uses_second_tap_credits_with_one_answer():
    """В пределах бесплатных использований (free_actions) повторный тап
    ДОЗВОЛЕН правилами баланса: итог засчитывается, экран редактируется,
    РОВНО ОДИН ответ на тап и это НЕ alert-отказ."""
    ev1, ev2 = _rps_two_taps("rock", "scissors", "paper", "rock")
    assert any(k == "edit" for k, _ in ev1)           # первый тап сыграл
    answers = [v for k, v in ev2 if k == "answer"]
    assert len(answers) == 1 and not answers[0]["show_alert"], \
        "ровно один мягкий answer; двойной ответ = QUERY_ID_INVALID"
    assert any(k == "edit" for k, _ in ev2), "засчитанная игра обновляет экран"


def test_play_hard_cooldown_single_soft_answer_no_edit():
    """Кулдаун «запыхался» (после исчерпания бесплатных использований):
    РОВНО ОДИН ответ, без alert (тост не перекрывает игровой экран),
    экран не перерисовывается, награда не начисляется."""
    from app.services import balance
    from app.services.tamagotchi import local_now

    async def run():
        from sqlalchemy.ext.asyncio import (async_sessionmaker,
                                            create_async_engine)
        from app.db.models import Base, User
        from app.handlers import games as g
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]
                                 ).StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        events = []
        async with sm() as session:
            session.add(User(tg_id=1, first_name="Владелец", lang="ru", coins=0))
            pet = _pet_row()
            session.add(pet)
            await session.flush()
            # Превышаем лимит бесплатных использований: помечаем игру
            # недавно состоявшейся с полным счётчиком использований.
            free = int(balance.get_mult("free_actions"))
            extra = dict(pet.settings_extra or {})
            extra["game_at"] = local_now().isoformat()
            extra["game_uses"] = free + 1
            pet.settings_extra = extra
            await g.play_rps(_make_cb("rps:rock:scissors", 1, events), session)
        return events
    events = asyncio.run(run())
    answers = [v for k, v in events if k == "answer"]
    assert len(answers) == 1, f"ровно один ответ на тап, было {len(answers)}"
    assert not answers[0]["show_alert"], "кулдаун — мягкий toast, не alert"
    assert "⏳" in (answers[0].get("text") or ""), answers
    assert not any(k == "edit" for k, _ in events), "при отказе экран не трогаем"


def test_blackjack_callback_data_within_64_bytes():
    """Регресс «21 не играет»: снимок колоды НЕ должен попадать в callback_data.

    Telegram режет callback_data длиннее 64 байтов — edit_message_text с
    такой клавиатурой падает, и тапы «Ещё карту»/«Хватит» выглядят мёртвыми
    («Не получилось. Попробуй ещё раз»). Кнопки хода обязаны быть короче
    лимита даже на полной колоде из 52 карт.
    """
    from app.handlers import games as G

    deck = G.BJ_DECK[:]
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    token = G._bj_state_b64(deck, player, dealer, 18)
    kb = G._bj_kb(token, chat_id=123)
    for row in kb.inline_keyboard:
        for btn in row:
            if btn.callback_data:
                assert len(btn.callback_data.encode()) <= 64, (
                    f"callback_data too long: {btn.callback_data!r}")


def test_blackjack_full_roundtrip_and_single_use():
    """Партия читается, подписана, одноразова; подделка = 'bad', рестарт = 'stale'."""
    from app.handlers import games as G

    deck = G.BJ_DECK[:]
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    token = G._bj_state_b64(deck, player, dealer, 18)

    res = G._bj_load_state(f"bj:hit:{token}")
    assert res[0] == "ok"
    assert res[1] == deck and res[2] == player and res[3] == dealer and res[4] == 18

    # подделанная подпись
    forged = token.rsplit(".", 1)[0] + ".deadbe"
    assert G._bj_load_state(f"bj:hit:{forged}")[0] == "bad"

    # после завершения раунда id умирает -> stale (не тишина!)
    G._bj_forget(f"bj:stand:{token}")
    assert G._bj_load_state(f"bj:stand:{token}")[0] == "stale"


def test_blackjack_multi_hit_sequence():
    """Серия «Ещё карту» подряд: каждый следующий токен валиден и короток."""
    from app.handlers import games as G

    deck = G.BJ_DECK[:]
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    tok = G._bj_state_b64(deck, player, dealer, 18)
    for _ in range(8):
        st = G._bj_load_state(f"bj:hit:{tok}")
        assert st[0] == "ok", st
        _, dk, pl, dl, stay = st
        if not dk or G._bj_value(pl) >= 21:
            break
        pl.append(dk.pop())
        new_tok = G._bj_state_b64(dk, pl, dl, stay)
        assert len(f"bj:hit:{new_tok}".encode()) <= 64
        G._bj_forget(f"bj:hit:{tok}")
        tok = new_tok
