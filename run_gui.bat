@echo off
chcp 65001 >nul
title TamaBot - Панель управления
setlocal EnableExtensions
cd /d "%~dp0bot"

if exist .env goto env_ok
echo.
echo  ^[!^] Не найден файл bot\.env
echo       Сначала запустите install.bat, он создаст .env.
echo       Заполните BOT_TOKEN и DATABASE_URL, затем повторите запуск.
echo.
pause
exit /b 1
:env_ok

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if exist venv\Scripts\python.exe (
    set "PY=venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" -m app.web
set "EXITCODE=%ERRORLEVEL%"

if "%EXITCODE%"=="0" goto ok_exit
title TamaBot - Ошибка %EXITCODE%
echo.
echo  ^[!^] Бот завершился с кодом %EXITCODE%. Подробности в папке bot\logs
echo       Если не найдены модули fastapi, uvicorn, websockets -
echo       переустановите зависимости через install.bat
echo.
pause
:ok_exit
endlocal
exit /b %EXITCODE%
