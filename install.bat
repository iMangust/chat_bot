@echo off
chcp 65001 >nul
title TamaBot - Установка
setlocal EnableExtensions
cd /d "%~dp0bot"

echo.
echo  [*] Проверка Python...
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    echo  [!] Python не найден. Установите Python 3.11+ с python.org
    echo      и обязательно отметьте "Add Python to PATH".
    pause
    exit /b 1
)
%PY% --version

if not exist venv (
    echo  [*] Создание виртуального окружения venv...
    %PY% -m venv venv
    if errorlevel 1 (
        echo  [!] Не удалось создать venv.
        pause
        exit /b 1
    )
)

set "VENV_PY=venv\Scripts\python.exe"

echo  [*] Обновление pip...
"%VENV_PY%" -m pip install --upgrade pip

echo  [*] Установка зависимостей из requirements.txt (может занять 2-5 минут)...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo  [!] Ошибка установки зависимостей. Проверьте подключение к интернету
    echo      и повторите запуск install.bat
    pause
    exit /b 1
)

if not exist .env.example (
    > .env.example (
        echo # Скопируйте этот файл в .env и заполните значения
        echo BOT_TOKEN=
        echo TRACKED_CHAT_IDS=[]
        echo ADMIN_IDS=[]
        echo DATABASE_URL=mysql+aiomysql://tamabot:tamabot@127.0.0.1:3306/tamabot?charset=utf8mb4
        echo REDIS_URL=redis://127.0.0.1:6379/0
        echo # MTProto для синхронизации подписчиков канала ^(необязательно^):
        echo API_ID=
        echo API_HASH=
        echo PHONE=
    )
)

if not exist .env (
    copy .env.example .env >nul
    echo  [*] Создан bot\.env - заполните BOT_TOKEN, DATABASE_URL,
    echo      TRACKED_CHAT_IDS, ADMIN_IDS ^(откроется в Блокноте^).
    start "" notepad .env
)

echo.
echo  [OK] Установка завершена успешно.
echo      Запуск панели управления: двойной клик на run_gui.bat
echo      Файл настроек: bot\.env
echo.
pause
endlocal
exit /b 0
