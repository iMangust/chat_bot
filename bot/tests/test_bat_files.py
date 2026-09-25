"""Тесты корректности .bat файлов для cmd.exe на Windows.

cmd.exe читает batch-файлы в OEM-кодовой странице консоли (для русской
локали это cp866). Если файл сохранён в UTF-8 без BOM, кириллица
интерпретируется как мусор и строки echo/exec-команд распадаются на
«команды» ('ый' is not recognized...). Плюс cmd требует CRLF.

Эти тесты гарантируют, что bat-файлы:
  * декодируются как cp866 (т.е. не содержат байтов вне этой страницы);
  * не содержат UTF-8 BOM;
  * используют только CRLF-переводы строк;
  * переключают кодовую страницу командой `chcp 866`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # корень проекта (/workspace)
BAT_FILES = sorted(ROOT.glob("*.bat"))


@pytest.mark.parametrize(
    "bat", BAT_FILES, ids=lambda p: p.name
)
def test_bat_is_cp866_clean(bat: Path) -> None:
    data = bat.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), f"{bat.name}: UTF-8 BOM недопустим в .bat"
    try:
        text = data.decode("cp866")
    except UnicodeDecodeError as exc:  # pragma: no cover - fail path
        pytest.fail(f"{bat.name}: файл не является валидным cp866 — {exc}")
    assert "chcp 866" in text, f"{bat.name}: отсутствует 'chcp 866 >nul'"


def test_bat_line_endings_are_crlf() -> None:
    for bat in BAT_FILES:
        data = bat.read_bytes()
        stripped = data.replace(b"\r\n", b"")
        assert b"\n" not in stripped, f"{bat.name}: найдены LF-переводы строк (cmd требует CRLF)"


def test_bat_no_emoji_outside_cp866() -> None:
    """Эмодзи/стрелки не представимы в cp866 — их быть не должно."""
    for bat in BAT_FILES:
        text = bat.read_bytes().decode("cp866")
        for ch in text:
            assert ord(ch) < 0x2500 or ch in "─│", (
                f"{bat.name}: символ {ch!r} не поддерживается консолью cp866"
            )


def test_all_three_launchers_present() -> None:
    names = {p.name for p in BAT_FILES}
    assert {"install.bat", "run.bat", "console.bat"} <= names


def test_bat_no_double_ampersand_after_if() -> None:
    """cmd.exe не понимает `if <cond> cmd && cmd` — «Непредвиденное появление: ..».

    Конструкция вида `where py >nul 2>&1 && set X=...` в интерпретаторе cmd
    разбирается так, что после редиректа `2>&1` идёт `&&`, и при определённых
    условиях (вложенность в if-блок / обработка `&`) cmd выдаёт ошибку
    «Непредвиденное появление». Надёжный паттерн — отдельные строки с
    проверкой `%errorlevel%`. Тест запрещает любые `&&` в bat-скриптах.
    """
    for bat in BAT_FILES:
        text = bat.read_bytes().decode("cp866")
        for num, line in enumerate(text.split("\r\n"), 1):
            stripped = line.strip()
            if stripped.lower().startswith("rem"):
                continue
            assert "&&" not in line, (
                f"{bat.name}:{num}: найден '&&' — недопустимо в cmd.exe, "
                f"используйте отдельные строки с if %errorlevel%==0: {stripped!r}"
            )
