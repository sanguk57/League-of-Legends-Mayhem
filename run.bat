@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

rem Windows용 Python 찾기 (py 런처 우선)
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" (
    set "PY="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3"
)
if not defined PY (
    echo [오류] Python을 찾을 수 없습니다.
    echo https://www.python.org/downloads/ 에서 설치하고, 설치 화면에서 "Add python.exe to PATH"를 체크하세요.
    pause
    exit /b 1
)

rem 인자를 주면 그대로 전달: run.bat live --watch 2 --role tank
if not "%~1"=="" (
    %PY% -m aram_helper %*
    pause
    exit /b
)

:menu
echo.
echo ===== 칼바람 아이템·증강 추천 =====
echo  1. 게임 중 자동 분석
echo  2. 게임 중 자동 분석 + 음성 안내 + 웹페이지 (http://localhost:8765)
echo  3. 샘플 데이터로 미리 보기 (게임 없이 테스트)
echo  4. 아이템 골드 효율표
echo  5. 단위 테스트 실행 (tests)
echo  0. 종료 (Enter만 눌러도 종료)
echo.
set "SEL="
set /p SEL=번호 선택:

if "%SEL%"=="1" %PY% -m aram_helper live --watch 2
if "%SEL%"=="2" %PY% -m aram_helper live --watch 2 --speak --serve 8765
if "%SEL%"=="3" %PY% -m aram_helper live --file samples/sample_allgamedata.json
if "%SEL%"=="4" %PY% -m aram_helper efficiency
if "%SEL%"=="5" %PY% -m unittest discover -s tests -t .
if "%SEL%"=="0" exit /b
if "%SEL%"=="" exit /b
goto menu
