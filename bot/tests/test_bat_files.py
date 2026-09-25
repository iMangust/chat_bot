"""Тесты корректности .bat файлов для cmd.exe на Windows.

cmd.exe читает batch-файлы в кодовой странице консоли. Начиная с v1.3
используется cp1251 (ANSI Windows Cyrillic): скрипты сохраняются в cp1251
и выполняют `chcp 1251`, поэтому и парсер cmd, и вывод echo показывают
корректную кириллицу. (Ранее был cp866/OEM — при просмотре файлов в ANSI
или запуске из среды с другой страницей возникал mojibake «иероглифы».)
Плюс cmd требует CRLF.

Эти тесты гарантируют, что bat-файлы:
  * декодируются как cp1251 (т.е. не содержат байтов вне этой страницы);
  * не содержат UTF-8 BOM;
  * используют только CRLF-переводы строк;
  * переключают кодовую страницу командой `chcp 1251`;
  * задают PYTHONIOENCODING=cp1251 для вывода Python в той же странице.
"""

from __future__ import annotations

import re

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
    assert "PYTHONIOENCODING=cp1251" in text, f"{bat.name}: нет PYTHONIOENCODING=cp1251"


def test_bat_line_endings_are_crlf() -> None:
    for bat in BAT_FILES:
        data = bat.read_bytes()
        stripped = data.replace(b"\r\n", b"")
        assert b"\n" not in stripped, f"{bat.name}: найдены LF-переводы строк (cmd требует CRLF)"


def test_bat_no_emoji_outside_cp1251() -> None:
    """Эмодзи/стрелки не представимы в cp1251 — их быть не должно."""
    for bat in BAT_FILES:
        text = bat.read_bytes().decode("cp1251")
        for ch in text:
            assert ord(ch) < 0x2500 or ch in "─│", (
                f"{bat.name}: символ {ch!r} не поддерживается консолью cp1251"
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
        text = bat.read_bytes().decode("cp1251")
        for num, line in enumerate(text.split("\r\n"), 1):
            stripped = line.strip()
            if stripped.lower().startswith("rem"):
                continue
            assert "&&" not in line, (
                f"{bat.name}:{num}: найден '&&' — недопустимо в cmd.exe, "
                f"используйте отдельные строки с if %errorlevel%==0: {stripped!r}"
            )


def test_bat_no_errorlevel_inside_parenthesized_if_blocks() -> None:
    """Регресс v1.2.5: вложенный `if ( ... if %errorlevel%==0 ... )` ломает разбор.

    На Windows Server 2019 (cmd 10.0.17763) конструкция вида::

        if not defined PYCMD (
            where python >nul 2>nul
            if %errorlevel%==0 set PYCMD=python
        )

    приводила к «Непредвиденное появление: ..» сразу после первой echo-строки,
    т.к. cmd раскрывает %errorlevel% при парсинге всего блока. Надёжный
    паттерн — плоские строки + goto-метки. Тест запрещает `%errorlevel%==`
    внутри многострочных if-блоков (строки блока не должны содержать
    подстановку errorlevel с оператором сравнения).
    """
    for bat in BAT_FILES:
        text = bat.read_bytes().decode("cp1251")
        inside_block = False
        depth = 0
        for num, line in enumerate(text.split("\r\n"), 1):
            stripped = line.strip()
            if not stripped or stripped.lower().startswith(("rem", "::")):
                continue
            if "%errorlevel%==" in stripped.lower() and depth > 0:
                pytest.fail(
                    f"{bat.name}:{num}: %errorlevel%== внутри if-блока "
                    f"(ломает разбор на cmd 10.0.17763) — перепишите плоскими "
                    f"строками с goto-меткой: {stripped!r}"
                )
            depth += stripped.count("(") - stripped.count(")")
            if stripped.startswith("if ") and stripped.endswith("("):
                inside_block = True
            elif stripped == ")" and inside_block:
                inside_block = False
            depth = max(depth, 0)


def test_bat_no_parenthesized_if_blocks() -> None:
    """Регресс v1.2.5: bat-скрипты полностью в goto-стиле, без многострочных блоков `if ... ( )`.

    На cmd.exe 10.0.17763 (Windows Server 2019) многострочные блоки вида::

        if not exist .env (
            echo ...
            pause
        )

    при определённом содержимом строк (кавычки/скобки/кириллица после редиректов)
    вызывают «Непредвиденное появление: ..». Все скрипты переписаны плоскими
    строками с goto-метками — тест запрещает открывающие `(` в конце if-строк.
    """
    for bat in BAT_FILES:
        text = bat.read_bytes().decode("cp1251")
        for num, line in enumerate(text.split("\r\n"), 1):
            stripped = line.strip()
            if stripped.lower().startswith(("rem", "::")) or not stripped:
                continue
            assert not re.match(r"^if\s+.*\($", stripped, re.I), (
                f"{bat.name}:{num}: найден многострочный if-блок — используйте "
                f"goto-стиль: {stripped!r}"
            )


def test_bat_python_invocations_are_quoted() -> None:
    """Команды запуска Python должны быть в кавычках: "%PY%" / "%PYCMD%".

    Без кавычки путь `C:\\Program Files\\...\\python.exe` с пробелом разбирается
    cmd как два аргумента и падает с непонятными ошибками.
    """
    import re

    pat_unquoted = re.compile(r"^%(PY|PYCMD)%\s", re.M)
    for bat in BAT_FILES:
        text = bat.read_bytes().decode("cp1251")
        m = pat_unquoted.search(text)
        if m is not None:
            pytest.fail(
                f"{bat.name}: незакавыщенное обращение к переменной Python "
                f"({m.group(0).strip()!r}) — используйте \"%PY%\" ..."
            )
