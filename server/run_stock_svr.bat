@echo off
REM ============================================================
REM  stock_svr 실행 스크립트 (Windows)
REM    run_stock_svr.bat            : GUI 실행 (엔진 자동 시작)
REM    run_stock_svr.bat --check    : 읽기 전용 점검
REM    run_stock_svr.bat --smoke 20 : GUI 를 20초만 띄우고 종료
REM ============================================================
setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PY=C:\Users\UT\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "%SCRIPT_DIR%config\config.local.ini" (
  echo [오류] config\config.local.ini 가 없습니다. config\config.example.ini 를 복사해 만드세요.
  pause
  exit /b 1
)

"%PY%" -m stock_svr %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo stock_svr 가 코드 %RC% 로 종료되었습니다.
  pause
)
endlocal & exit /b %RC%
