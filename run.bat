@echo off
rem ============================================================
rem  TamaBot — запуск бота с живыми логами в реальном времени
rem  Двойной клик => окно консоли с цветными логами, статусом
rem  и uptime в заголовке. Ctrl+C или закрытие окна = остановка.
rem  Файл должен лежать рядом с папкой bot\ (в корне проекта).
rem ============================================================
chcp 65001 >nul
title TamaBot - запуск...
cd /d "%~dp0bot"

if not exist .env (
    echo.
    echo  [ОШИБКА] Не найден файл .env
    echo  Скопируйте .env.example в .env и заполните BOT_TOKEN и DATABASE_URL.
    echo.
    pause
    exit /b 1
)

if exist venv\Scripts\python.exe (
    set PY=venv\Scripts\python.exe
) else (
    set PY=python
)

%PY% -m app.console.livelog --no-hold
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    title TamaBot - ОСТАНОВЛЕН с ошибкой %EXITCODE%
    echo.
    echo  ^>^> Бот завершился с кодом %EXITCODE%. Полный лог: bot\logs\
    echo.
    pause
)
exit /b %EXITCODE%
