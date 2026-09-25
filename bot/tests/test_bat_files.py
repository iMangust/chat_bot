"""Тесты корректности .bat файлов для cmd.exe на Windows.

cmd.exe читает batch-файлы в кодовой странице консоли. Мы используем
cp1251 (ANSI Russian) + `chcp 1251`: это нативная страница русской
Windows-локали, поэтому кириллица отображается корректно и при запуске
из cmd, и при просмотре файла в Блокноте/Notepad++ (тоже cp1251 по
умолчанию). Если файл сохранить в UTF-8 без BOM, cmd интерпретирует
кириллицу как мусор и строки распадаются на «команды»
('ый' is not recognized...). Плюс cmd требует CRLF.

Эти тесты гарантируют, что bat-файлы:
  * декодируются как cp1251 (т.е. не содержат байтов вне этой страницы);
  * не содержат UTF-8 BOM;
  * используют только CRLF-переводы строк;
  * переключают кодовую страницу командой `chcp 1251`;
  * не содержат символов вне cp1251 (эмодзи и т.п.).
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # корень проекта (/workspace)
BAT_FILES = sorted(ROOT.glob("*.bat"))


@pytest.mark.parametrize(
    "bat", BAT_FILES, ids=lambda p: p.name
)
def test_bat_is_cp1251_clean(bat: Path) -> None:
    data = bat.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), f"{bat.name}: UTF-8 BOM недопустим в .bat"
    try:
        text = data.decode("cp1251")
    except UnicodeDecodeError as exc:  # pragma: no cover - fail path
        pytest.fail(f"{bat.name}: файл не является валидным cp1251 — {exc}")
    assert "chcp 1251" in text, f"{bat.name}: отсутствует 'chcp 1251 >nul'"
    # двусторонняя проверка: перекодировка даёт побайтовый оригинал
    assert text.encode("cp1251") == data, f"{bat.name}: неоднозначная кодировка"


def test_bat_line_endings_are_crlf() -> None:
    for bat in BAT_FILES:
        data = bat.read_bytes()
        stripped = data.replace(b"\r\n", b"")
        assert b"\n" not in stripped, f"{bat.name}: найдены LF-переводы строк (cmd требует CRLF)"


def test_bat_no_emoji_outside_cp1251() -> None:
    """Эмодзи не представимы в cp1251 — их быть не должно."""
    for bat in BAT_FILES:
        text = bat.read_bytes().decode("cp1251")
        for ch in text:
            assert ord(ch) < 0x2000, (
                f"{bat.name}: символ {ch!r} не поддерживается консолью cp1251"
            )


def test_run_and_console_force_python_utf8_output() -> None:
    """Логи Python идут в utf-8 (PYTHONIOENCODING), а echo в bat — в cp1251.

    Без этих переменных python.exe может включить UTF-8 режим консоли
    (PYTHONUTF8=1 наследуется с некоторых установок) и вывести русские
    логи «иероглифами» в окне chcp 1251.
    """
    for name in ("run.bat", "console.bat"):
        bat = ROOT / name
        text = bat.read_bytes().decode("cp1251")
        assert "set PYTHONUTF8=0" in text, f"{name}: нет 'set PYTHONUTF8=0'"
        assert "set PYTHONIOENCODING=utf-8" in text, f"{name}: нет PYTHONIOENCODING"


def test_all_three_launchers_present() -> None:
    names = {p.name for p in BAT_FILES}
    assert {"install.bat", "run.bat", "console.bat"} <= names
