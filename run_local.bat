@echo off
REM 콘솔을 UTF-8로 전환하지 않으면 한글이 깨져 보인다(cmd 기본은 cp949).
chcp 65001 >nul
REM ===================================================================
REM  트럼프 팔로우 — 로컬 자동 갱신
REM  새 공시 수집 → OCR → 파싱 → 시세 갱신 → 배포
REM ===================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM --- Tesseract 자동 탐색 ---------------------------------------
REM 설치는 했는데 PATH에 안 잡히는 경우가 흔해서 흔한 위치를 직접 뒤진다.
if not defined TESSERACT_CMD (
  where tesseract >nul 2>&1
  if !errorlevel! equ 0 (
    for /f "delims=" %%i in ('where tesseract') do set "TESSERACT_CMD=%%i"
  )
)
if not defined TESSERACT_CMD (
  for %%p in (
    "C:\Program Files\Tesseract-OCR\tesseract.exe"
    "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"
    "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"
    "%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"
  ) do (
    if exist "%%~p" if not defined TESSERACT_CMD set "TESSERACT_CMD=%%~p"
  )
)

if defined TESSERACT_CMD (
  echo [OCR] Tesseract: !TESSERACT_CMD!
) else (
  echo [OCR] 경고: Tesseract를 찾지 못했습니다. 스캔 공시는 건너뜁니다.
  echo       진단: python scripts\build_data.py --doctor
)
set "LOG=%~dp0run_log.txt"
echo [LOG] 실행 기록: %LOG%
echo. > "%LOG%"
echo.

echo [1/4] 파이프라인 자체 검증
call :run python scripts\build_data.py --self-test
if errorlevel 1 (
  echo    검증 실패 - 중단합니다.
  goto :end
)

echo.
echo [2/4] 새 공시 수집 + OCR + 파싱
call :run python scripts\build_data.py --from-sources --out data.json
if errorlevel 1 echo    (공시 단계 실패 - 시세 갱신은 계속합니다)

echo.
echo [3/4] 시세 / 추적 수익률 갱신
call :run python scripts\build_data.py --refresh-prices --out data.json
if errorlevel 1 (
  echo    시세 갱신 실패 - 중단합니다.
  goto :end
)

echo.
echo [4/4] 변경분 배포
git diff --quiet -- data.json sitemap.xml
if errorlevel 1 (
  git add data.json sitemap.xml
  git commit -m "data: 로컬 자동 갱신 %date%"
  git push
  echo    배포 완료 - 1~2분 뒤 사이트에 반영됩니다.
) else (
  echo    변경 없음 - 배포 생략
)

:end
echo.
echo ================================================================
echo  실행 기록이 아래 파일에 저장됐습니다. 문제가 있으면 이 파일을 보내주세요:
echo    %LOG%
echo ================================================================
pause
endlocal
exit /b

REM --- 명령을 실행하면서 화면과 로그 파일에 동시에 남긴다 -----------
:run
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "& { %* 2>&1 | Tee-Object -FilePath '%LOG%' -Append }"
exit /b %errorlevel%
