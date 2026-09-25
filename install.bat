@echo off
rem ============================================================
rem  TamaBot - первичная установка: Python-venv + зависимости.
rem  Запустить один раз после копирования проекта на сервер.
rem  Требуется установленный Python 3.11+ (в PATH как "py").
rem ============================================================
chcp 866 >nul
title TamaBot - установка
if exist "%~dp0bot\requirements.txt" goto :srcok
echo  [ОШИБКА] Не найдена папка bot с исходниками проекта.
echo  Положите install.bat в корень проекта, рядом с папкой bot\.
echo  Текущая папка: %CD%
pause
exit /b 1
:srcok
cd /d "%~dp0bot"

echo  [*] Проверка Python...
rem --- Python: ищем в PATH через "where", без вложенных if-блоков
rem     (вложенные скобки + errorlevel ломают разбор на некоторых сборках cmd)
set PYCMD=
where py.exe >nul 2>nul
if not errorlevel 1 set PYCMD=py
if defined PYCMD goto :pyfound
where python.exe >nul 2>nul
if not errorlevel 1 set PYCMD=python
:pyfound
if defined PYCMD goto :pyok
echo  [ОШИБКА] Python не найден. Установите Python 3.11+ с python.org
echo           Обязательно включите "Add to PATH" при установке.
pause
exit /b 1
:pyok
echo  [*] Найден интерпретатор: %PYCMD%
"%PYCMD%" --version
if not errorlevel 1 goto :verok
echo  [ОШИБКА] Команда проверки версии Python завершилась с ошибкой.
pause
exit /b 1
:verok

if exist venv goto :venvok
echo  [*] Создание виртуального окружения venv...
"%PYCMD%" -m venv venv
if not errorlevel 1 goto :venvcreated
echo  [ОШИБКА] venv не создан.
pause
exit /b 1
:venvcreated
:venvok

echo  [*] Установка зависимостей (первый запуск - 2-5 минут)...
venv\Scripts\python.exe -m pip install --upgrade pip >nul
venv\Scripts\python.exe -m pip install -r requirements.txt
if not errorlevel 1 goto :pipok
echo  [ОШИБКА] pip install завершился с ошибкой - проверьте интернет/антивирус.
pause
exit /b 1
:pipok

if exist .env goto :envok
copy .env.example .env >nul
echo  [*] Создан .env из примера - ЗАПОЛНИТЕ его перед запуском:
echo      BOT_TOKEN, DATABASE_URL, TRACKED_CHAT_IDS, ADMIN_IDS
notepad .env
:envok

echo.
echo  [OK] Установка завершена.
echo     Дальше: отредактируйте bot\.env и запустите run.bat
echo     (или console.bat - если хотите интерфейс с вкладками).
echo.
pause
