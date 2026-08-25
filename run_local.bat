@echo off
REM ===================================================================
REM  트럼프 팔로우 — 로컬 자동 갱신
REM  하루 한 번 실행하면: 새 공시 수집 → OCR → 파싱 → 시세 갱신 → 배포
REM ===================================================================
setlocal
cd /d "%~dp0"

REM Tesseract 설치 경로 (기본 위치가 아니면 여기를 수정하세요)
if not defined TESSERACT_CMD (
  set "TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe"
)

echo [1/4] 파이프라인 자체 검증
python scripts\build_data.py --self-test
if errorlevel 1 (
  echo    검증 실패 - 중단합니다.
  exit /b 1
)

echo.
echo [2/4] 새 공시 수집 + OCR + 파싱
python scripts\build_data.py --from-sources --out data.json
if errorlevel 1 echo    (공시 단계 실패 - 시세 갱신은 계속합니다)

echo.
echo [3/4] 시세 / 추적 수익률 갱신
python scripts\build_data.py --refresh-prices --out data.json
if errorlevel 1 (
  echo    시세 갱신 실패 - 중단합니다.
  exit /b 1
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

endlocal
