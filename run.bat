@echo off
rem ============================================================
rem  TamaBot - запуск бота с живыми логами в реальном времени
rem  Двойной клик => окно консоли с цветными логами, статусом
rem  и uptime в заголовке. Ctrl+C или закрытие окна = остановка.
rem  Файл должен лежать рядом с папкой bot\ (в корне проекта).
rem ============================================================
chcp 866 >nul
title TamaBot - запуск...
if exist "%~dp0bot\app\main.py" goto :srcok
echo  [ОШИБКА] Не найдена папка bot с исходниками проекта.
echo  Положите run.bat в корень проекта, рядом с папкой bot\.
pause
exit /b 1
:srcok
cd /d "%~dp0bot"

if exist .env goto :envok
echo.
echo  [ОШИБКА] Не найден файл .env
echo  Скопируйте .env.example в .env и заполните BOT_TOKEN и DATABASE_URL.
echo.
pause
exit /b 1
:envok

set PY=python
if exist venv\Scripts\python.exe set PY=venv\Scripts\python.exe

"%PY%" -m app.console.livelog --no-hold
set EXITCODE=%ERRORLEVEL%

if "%EXITCODE%"=="0" goto :finalline
title TamaBot - ОСТАНОВЛЕН с ошибкой %EXITCODE%
echo.
echo  >> Бот завершился с кодом %EXITCODE%. Полный лог: bot\logs\
echo.
pause
:finalline
exit /b %EXITCODE%
