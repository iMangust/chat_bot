"""Регрессия v1.5.18: никаких мутаций frozen callback-объектов; секретов в коде нет."""
from __future__ import annotations


class TestFrozenCallbackRegression:
    """v1.5.18: ValidationError при покупке — shop.buy_item мутировал cb.data,
    но объекты aiogram 3.x frozen (pydantic). Мутаций callback-объектов быть
    не должно ни в одном хендлере."""

    def test_no_callback_mutation_anywhere(self):
        import pathlib
        import re
        bad = []
        for p in pathlib.Path("app").rglob("*.py"):
            src = p.read_text(encoding="utf-8")
            for i, line in enumerate(src.splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"\b(cb|event|query|callback)\.data\s*=(?!=)", code):
                    bad.append(f"{p}:{i}: {line.strip()}")
        assert not bad, "мутация frozen CallbackQuery.data: " + "; ".join(bad)

    def test_shop_screen_accepts_explicit_page(self):
        import inspect
        from app.handlers.shop import shop_screen
        sig = inspect.signature(shop_screen)
        assert "page" in sig.parameters, \
            "после покупки страница возврата передаётся аргументом (frozen cb)"

    def test_sync_skips_mtproto_account_self(self):
        """v1.5.18: аккаунт MTProto (он же участник канала) не должен попадать
        в welcome-очередь — иначе бот шлёт приветствие «сам себе»."""
        import pathlib
        src_path = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "mtproto_sync.py"
        src = src_path.read_text(encoding="utf-8")
        assert "self_ids" in src and "holder.me" in src
