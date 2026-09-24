@echo off
rem ============================================================
rem  TamaConsole - полноэкранный интерфейс управления:
rem  вкладки Логи / Дашборд / БД / Настройки / Пользователи,
rem  кнопки Запустить / Остановить / Перезапустить.
rem  Горячие клавиши: r - пуск/стоп, l - логи, d - БД, q - выход.
rem ============================================================
chcp 866 >nul
title TamaConsole - интерфейс управления
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

%PY% -m app.console
if errorlevel 1 (
    echo.
    echo  Оболочка завершилась с ошибкой. Установлены ли зависимости? ^(install.bat^)
    echo.
    pause
)
