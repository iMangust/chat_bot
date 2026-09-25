@echo off
rem ============================================================
rem  TamaBot - первичная установка: Python-venv + зависимости.
rem  Запустить один раз после копирования проекта на сервер.
rem  Требуется установленный Python 3.11+ (в PATH как "py").
rem ============================================================
chcp 1251 >nul
title TamaBot - установка
cd /d "%~dp0bot"

echo  [*] Проверка Python...
py -3 --version >nul 2>&1
if errorlevel 1 (
    echo  [ОШИБКА] Python не найден. Установите Python 3.11+ с python.org
    echo           (обязательно включите "Add to PATH" при установке).
    pause
    exit /b 1
)

if not exist venv (
    echo  [*] Создание виртуального окружения venv...
    py -3 -m venv venv || (echo  [ОШИБКА] venv не создан & pause & exit /b 1)
)

echo  [*] Установка зависимостей (первый запуск - 2-5 минут)...
venv\Scripts\python.exe -m pip install --upgrade pip >nul
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo  [ОШИБКА] pip install завершился с ошибкой - проверьте интернет/антивирус.
    pause
    exit /b 1
)

if not exist .env (
    copy .env.example .env >nul
    echo  [*] Создан .env из примера - ЗАПОЛНИТЕ его перед запуском:
    echo      BOT_TOKEN, DATABASE_URL, TRACKED_CHAT_IDS, ADMIN_IDS
    notepad .env
)

echo.
echo  [OK] Установка завершена.
echo     Дальше: отредактируйте bot\.env и запустите run.bat
echo     (или console.bat - если хотите интерфейс с вкладками).
echo.
pause
