@echo off
rem ============================================================
rem  TamaBot - веб-панель управления (современный интерфейс).
rem  Бот стартует автоматически, браузер открывается сам.
rem  Логи, настройки (.env) и управление запуском/остановкой -
rem  прямо в браузере. Закройте это окно или нажмите Ctrl+C,
rem  чтобы остановить бота и панель.
rem ============================================================
chcp 1251 >nul
title TamaBot - веб-панель
cd /d "%~dp0bot"

if not exist .env (
    echo.
    echo  [!] Не найден файл .env
    echo  Скопируйте .env.example в .env и заполните BOT_TOKEN и DATABASE_URL,
    echo  либо запустите сначала install.bat.
    echo.
    pause
    exit /b 1
)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if exist venv\Scripts\python.exe (
    set PY=venv\Scripts\python.exe
) else (
    set PY=python
)

%PY% -m app.web
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    title TamaBot - ошибка %EXITCODE%
    echo.
    echo  [!] Панель завершилась с кодом %EXITCODE%. Подробности: bot\logs\
    echo      (частая причина: не установлены зависимости веб-оболочки -
    echo       выполните install.bat повторно или "pip install fastapi uvicorn websockets")
    echo.
    pause
)
exit /b %EXITCODE%
