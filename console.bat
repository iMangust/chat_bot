@echo off
rem ============================================================
rem  TamaConsole - полноэкранный интерфейс управления:
rem  вкладки Логи / Дашборд / БД / Настройки / Пользователи,
rem  кнопки Запустить / Остановить / Перезапустить.
rem  Горячие клавиши: r - пуск/стоп, l - логи, d - БД, q - выход.
rem ============================================================
chcp 866 >nul
title TamaConsole - интерфейс управления
if exist "%~dp0bot\app\main.py" goto :srcok
echo  [ОШИБКА] Не найдена папка bot с исходниками проекта.
echo  Положите console.bat в корень проекта, рядом с папкой bot\.
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

"%PY%" -m app.console
if not errorlevel 1 goto :conok
echo.
echo  Оболочка завершилась с ошибкой. Запустите сначала install.bat
echo.
pause
:conok
