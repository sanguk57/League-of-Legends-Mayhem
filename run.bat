@echo off
setlocal
cd /d "%~dp0"
set "SELF=%~f0"
set PYTHONUTF8=1
rem 이 파일은 CP949(EUC-KR) 인코딩, CRLF 줄바꿈으로 저장해야 cmd가 한글을 제대로 읽는다

rem Python 3.13 자동 설치에 쓰는 설치 파일 (winget이 없을 때)
set "PY_VERSION=3.13.12"

call :find_python
if not defined PY (
    echo Python이 설치되어 있지 않아 자동으로 설치합니다. 몇 분 걸릴 수 있습니다...
    call :install_python
    call :find_python
)
if not defined PY (
    echo.
    echo [오류] Python을 자동으로 설치하지 못했습니다.
    echo https://www.python.org/downloads/ 에서 직접 설치하고, 설치 화면에서 "Add python.exe to PATH"를 체크하세요.
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
echo  1. 게임 중 자동 분석 + 웹페이지 (http://localhost:8765) + 증강 선택 화면 자동 읽기
echo  2. 1번 + 음성 안내
echo  3. 샘플 데이터로 미리 보기 (게임 없이 테스트)
echo  4. 증강 선택지 비교 (게임 중, 선택 화면의 증강 3개 입력)
echo  5. 게임 기록과 추천을 따랐을 때의 승률
echo  6. 아이템 골드 효율표
echo  7. 단위 테스트 실행 (tests)
echo  8. Windows 로그인 시 자동 실행 등록 (1번을 최소화 창으로 실행)
echo  9. 자동 실행 해제
echo  0. 종료 (Enter만 눌러도 종료)
echo.
set "SEL="
set /p SEL=번호 선택:

if "%SEL%"=="1" %PY% -m aram_helper live --watch 2 --screen --serve 8765
if "%SEL%"=="2" %PY% -m aram_helper live --watch 2 --screen --speak --serve 8765
if "%SEL%"=="3" %PY% -m aram_helper live --file samples/sample_allgamedata.json
if "%SEL%"=="4" call :pick
if "%SEL%"=="5" %PY% -m aram_helper history
if "%SEL%"=="6" %PY% -m aram_helper efficiency
if "%SEL%"=="7" %PY% -m unittest discover -s tests -t .
if "%SEL%"=="8" call :startup_on
if "%SEL%"=="9" call :startup_off
if "%SEL%"=="0" exit /b
if "%SEL%"=="" exit /b
goto menu


:pick
set "AUGS="
set /p AUGS=증강 이름을 쉼표로 구분해 입력 (예: 둠, 핀볼, 남작의 도움):
if defined AUGS %PY% -m aram_helper pick "%AUGS%"
exit /b


:startup_on
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\aram_helper.bat"
> "%STARTUP%" echo @echo off
>> "%STARTUP%" echo start "aram_helper" /min "%SELF%" live --watch 2 --screen --serve 8765
echo 등록했습니다. 다음 로그인부터 최소화된 창으로 자동 실행되어 게임을 기다립니다.
echo (등록 파일: %STARTUP%)
exit /b


:startup_off
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\aram_helper.bat"
if exist "%STARTUP%" (
    del "%STARTUP%"
    echo 자동 실행을 해제했습니다.
) else (
    echo 등록된 자동 실행이 없습니다.
)
exit /b


:find_python
rem py 런처 → python → 기본 설치 위치 순서로 찾는다 (설치 직후에는 PATH가 반영되지 않으므로)
rem Python 3.8 미만은 없는 것으로 본다
set "PY="
set "PYCHECK=import sys; sys.exit(sys.version_info < (3, 8))"
where py >nul 2>nul && py -3 -c "%PYCHECK%" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "%PYCHECK%" >nul 2>nul && set "PY=python"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3 -c "%PYCHECK%" >nul 2>nul && set "PY="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do "%%D\python.exe" -c "%PYCHECK%" >nul 2>nul && set "PY="%%D\python.exe""
exit /b


:install_python
rem 1) winget (Windows 10 최신 업데이트, Windows 11에 기본 포함). 사용자 계정에만 설치하므로 관리자 권한 불필요
where winget >nul 2>nul
if not errorlevel 1 (
    winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
    call :find_python
)
if defined PY exit /b
rem 2) python.org 설치 파일을 받아 조용히 설치
echo python.org에서 Python %PY_VERSION% 설치 파일을 내려받습니다...
set "PY_INSTALLER=%TEMP%\python-%PY_VERSION%-amd64.exe"
curl -L -s -o "%PY_INSTALLER%" "https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"
if not exist "%PY_INSTALLER%" powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe' -OutFile '%PY_INSTALLER%'"
if not exist "%PY_INSTALLER%" (
    echo [오류] 설치 파일을 내려받지 못했습니다. 인터넷 연결을 확인하세요.
    exit /b
)
echo 설치 중...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 InstallLauncherAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
del "%PY_INSTALLER%" >nul 2>nul
exit /b
