"""База и экономика: репозитории и базовые сервисы на in-memory sqlite."""
from __future__ import annotations

from _helpers import pytest

from app.db.repositories import UserRepository

# ---------------------------------------------------------------------------
# 4. База/экономика
# ---------------------------------------------------------------------------
class TestCore:
    @pytest.mark.asyncio
    async def test_user_get_or_create(self, session):
        repo = UserRepository(session)
        u = await repo.get_or_create(1001, "Ваня", "vanya")
        u2 = await repo.get_or_create(1001, "Ваня", "vanya")
        assert u.tg_id == u2.tg_id == 1001

    @pytest.mark.asyncio
    async def test_referrer_link(self, session):
        repo = UserRepository(session)
        await repo.get_or_create(2001, "A", None)
        await repo.get_or_create(2002, "B", None)
        assert await repo.set_referrer(2002, 2001) is True
        assert await repo.set_referrer(2002, 2001) is False  # однократно

    def test_version(self):
        from app.config import __version__
        assert __version__ == "1.5.22"

    def test_env_reading_windows_encodings(self, tmp_path, monkeypatch):
        """v1.5.13/1.5.14: .env в cp1251 / с BOM / битый — импорт не падает,
        короткие ключи API_ID/API_HASH/PHONE подхватываются (регресс Windows)."""
        import subprocess
        import sys
        from pathlib import Path
        env_dir = tmp_path / "bot"
        env_dir.mkdir()
        # cp1251-файл с кириллическим комментарием (как в Notepad на Windows)
        (env_dir / ".env").write_bytes(
            "# Привет, это конфиг бота\nAPI_ID=12345678\n"
            "API_HASH=0123456789abcdef0123456789abcdef\n".encode("cp1251"))
        code = (
            "import sys, os\n"
            "sys.path.insert(0, %r)\n"
            "from app.config import _read_env_values\n"
            "vals = _read_env_values(%r)\n"
            "assert vals['API_ID'] == '12345678', vals\n"
            "os.environ['API_ID'] = vals['API_ID']\n"
            "from app.config import _alias_short_mtproto_keys\n"
            "_alias_short_mtproto_keys()\n"
            "assert os.environ.get('TELEGRAM_API_ID') == '12345678'\n"
            "print('OK')"
        ) % (str(Path(__file__).resolve().parents[1]), str(env_dir / ".env"))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env={k: v for k, v in __import__("os").environ.items()
                                if k not in ("API_ID", "TELEGRAM_API_ID")})
        assert "OK" in r.stdout, (r.stdout, r.stderr)
        # BOM + utf-8
        (env_dir / ".env_bom").write_bytes(
            "\ufeffAPI_ID=999\n".encode("utf-8"))
        code2 = (
            "import sys; sys.path.insert(0, %r)\n"
            "from app.config import _read_env_values\n"
            "assert _read_env_values(%r)['API_ID'] == '999'\n"
            "print('OK')"
        ) % (str(Path(__file__).resolve().parents[1]), str(env_dir / ".env_bom"))
        r2 = subprocess.run([sys.executable, "-c", code2], capture_output=True, text=True)
        assert "OK" in r2.stdout, (r2.stdout, r2.stderr)
        #完全不 читаемый как текст мусор — не падает (latin-1 fallback)
        (env_dir / ".env_bin").write_bytes(b"\xff\xfe\x00garbage\xff")
        code3 = (
            "import sys; sys.path.insert(0, %r)\n"
            "from app.config import _read_env_values\n"
            "_read_env_values(%r)\n"
            "print('OK')"
        ) % (str(Path(__file__).resolve().parents[1]), str(env_dir / ".env_bin"))
        r3 = subprocess.run([sys.executable, "-c", code3], capture_output=True, text=True)
        assert "OK" in r3.stdout, (r3.stdout, r3.stderr)
