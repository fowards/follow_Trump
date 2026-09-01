# 로컬 자동 갱신 설정 (Windows)

하루 한 번 내 PC에서 돌려 사이트를 갱신합니다. GitHub Actions는 쓰지 않습니다.

## 1. 설치할 것 (한 번만)

### Python 3.10 이상
https://www.python.org/downloads/ — 설치 시 **"Add Python to PATH"** 반드시 체크

### Tesseract OCR ★필수★
공시 절반이 스캔 이미지라 OCR이 없으면 그 문서들을 읽지 못합니다.

- 다운로드: https://github.com/UB-Mannheim/tesseract/wiki
- 파일명 예: `tesseract-ocr-w64-setup-5.x.x.exe`
- 기본 경로(`C:\Program Files\Tesseract-OCR`)에 설치하면 `run_local.bat`이 자동으로 찾습니다.
- 다른 곳에 설치했다면 `run_local.bat`의 `TESSERACT_CMD` 경로를 수정하세요.
- 영어만 쓰므로 추가 언어팩은 필요 없습니다.

### 파이썬 패키지
```cmd
cd C:\경로\follow_Trump
pip install -r scripts\requirements.txt
```

## 2. 동작 확인 — 문제가 있으면 여기서 다 알려줍니다

```cmd
python scripts\build_data.py --doctor
```

파이썬 버전, 패키지 4종, Tesseract 위치, 프로젝트 파일, 네트워크를 순서대로 점검하고
빠진 항목마다 **해결 방법을 함께** 출력합니다.

Tesseract를 설치했는데도 못 찾는다면(PATH에 안 잡히는 흔한 경우), doctor가 실제
설치 경로를 찾아 아래처럼 알려줍니다:

```
set TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

이상이 없으면 로직 검증도 돌려보세요:

```cmd
python scripts\build_data.py --self-test
```
`전체 통과 ✅` 가 나오면 준비 완료입니다.

## 3. 수동 실행

```cmd
run_local.bat
```

`run_local.bat`은 실행할 때마다 맨 먼저 `git pull`로 최신 코드를 받아옵니다
(Claude가 원격에서 고친 내용이 내 PC에 반영되도록). **지금 쓰는 `run_local.bat`이
이 pull 단계가 생기기 전 버전이라면** 뉴스 기능처럼 나중에 추가된 파일이
아예 없을 수 있습니다 — 한 번만 아래로 직접 최신화하세요:

```cmd
cd C:\경로\follow_Trump
git pull origin claude/funny-darwin-b1k2s1
```

이후로는 `run_local.bat`이 매번 알아서 받아옵니다.

처음 실행은 오래 걸립니다(스캔 PDF OCR, 문서당 수 분).
한 번 읽은 PDF는 `.cache/ocr`에 저장되어 다음부터는 건너뜁니다.

## 4. 매일 자동 실행 (작업 스케줄러)

관리자 권한 명령 프롬프트에서:

```cmd
schtasks /create /tn "TrumpFollow_Daily" /tr "C:\경로\follow_Trump\run_local.bat" /sc daily /st 08:00
```

- `/st 08:00` — 매일 오전 8시 (원하는 시각으로 변경)
- PC가 꺼져 있으면 실행되지 않습니다. 켜져 있는 시간대로 잡으세요.

확인·삭제:
```cmd
schtasks /query /tn "TrumpFollow_Daily"
schtasks /delete /tn "TrumpFollow_Daily" /f
```

## 5. 문제가 생기면

| 증상 | 원인/해결 |
|------|-----------|
| `OCR 의존성 없음` | `pip install -r scripts\requirements.txt` 재실행 |
| OCR 시험 "건너뜀" | Tesseract 미설치 또는 경로 오류 → `TESSERACT_CMD` 확인 |
| `시세 조회 전부 실패` | 네트워크 확인. Nasdaq이 막히면 Stooq로 자동 폴백 (Yahoo는 429 차단이 잦아 사용하지 않음) |
| OCR이 너무 느림 | `--ocr-dpi 200` 으로 낮추기(정확도는 조금 떨어짐) |
| git push 실패 | `git config --global user.name/user.email` 설정, 자격증명 확인 |

## 참고: OCR 캐시

`.cache/ocr/` 에 PDF 해시별 OCR 결과가 쌓입니다.
다시 읽고 싶으면 해당 파일을 지우면 됩니다. (git에는 올라가지 않습니다)
