@echo off
rem ============================================================
rem  TamaBot - первичная установка: Python-venv + зависимости.
rem  Запустить один раз после копирования проекта на сервер.
rem  Требуется установленный Python 3.11+ (в PATH как "py").
rem ============================================================
chcp 866 >nul
title TamaBot - установка
if not exist "%~dp0bot\requirements.txt" (
    echo  [ОШИБКА] Не найдена папка bot с исходниками проекта.
    echo  Положите install.bat в корень проекта, рядом с папкой bot\.
    echo  Текущая папка: %CD%
    pause
    exit /b 1
)
cd /d "%~dp0bot"

echo  [*] Проверка Python...
rem --- Python: сначала py-launcher, затем python из PATH
set PYCMD=
where py >nul 2>nul
if %errorlevel%==0 set PYCMD=py -3
if not defined PYCMD (
    where python >nul 2>nul
    if %errorlevel%==0 set PYCMD=python
)
if not defined PYCMD (
    echo  [ОШИБКА] Python не найден. Установите Python 3.11+ с python.org
    echo           (обязательно включите "Add to PATH" при установке).
    pause
    exit /b 1
)
%PYCMD% --version

if not exist venv (
    echo  [*] Создание виртуального окружения venv...
    %PYCMD% -m venv venv
    if errorlevel 1 (echo  [ОШИБКА] venv не создан & pause & exit /b 1)
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
