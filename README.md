# 트럼프 팔로우 (Follow Trump)

도널드 트럼프의 주식 매매 공시를 **한국어**로 추적하는 정적 웹사이트입니다.
기존 트래커와의 차별점은 세 가지입니다.

1. **공시 지연(블랙박스) 시각화** — 실제 거래일과 공시일 사이의 “당신이 알 수 없던 기간”을 타임라인에 빗금으로 표시합니다.
2. **정직한 수익률** — 내부자 매수가가 아니라, 실제로 따라 살 수 있던 **공시 시점가**를 기준으로 “2개월 보유” 수익률을 계산합니다.
3. **정직한 통계** — 대박 사례 하나가 아니라 전체 거래의 승률·평균·최악까지 그대로 보여줍니다.

대상 범위는 **트럼프 단독**입니다.

## 구성

| 파일 | 역할 |
|------|------|
| `index.html` | 홈 — 매매 카드·정직한 통계·정직한 전제 배너 |
| `stock.html` + `stock.js` | **개별 종목 상세** (`stock.html?ticker=MRNA`) — 종목별 이력 + 공시 후 추적 차트 |
| `glossary.html` | **한국어 용어 풀이** (278-T·블라인드 트러스트·STOCK법 등 9개 항목) |
| `about.html` | **소개·방법론** — 데이터 출처, 공시 시점가 기준, ETF 필터, 전체통계 공개 원칙 |
| `guide.html` | **따라하기 가이드** — 검증된 사실 기반 구조적 함정 5가지 (모더나 사례 포함) |
| `privacy.html` | **개인정보처리방침·이용약관** (AdSense 요건: 쿠키·DoubleClick·옵트아웃·면책) |
| `common.js` | 홈/상세 공유 유틸 + SVG 라인차트 |
| `app.js` | 홈 렌더링 (카드→상세 링크, D+n 추적 배지) |
| `styles.css` | 다크 테마, 한국식 색(상승=빨강/하락=파랑) |
| `data.json` | 매매 데이터 (**현재 예시 데이터**) |
| `sitemap.xml`, `robots.txt` | 검색엔진 색인용 (파이프라인이 sitemap 자동 생성) |

### 지속 추적 (공시일 공개 + 이후 수익률 갱신)
- 각 거래는 `disclosureDate`에 공개되고, 최근 14일 내 공시는 홈에서 **NEW** 배지가 붙습니다.
- 공시 이후 주가를 계속 따라가며 두 가지 수익률을 보여줍니다:
  **① 2개월 규칙**(공시 후 2개월 보유) vs **② 공시 후 지금까지**(계속 보유, `trackingReturnPct`).
- 상세 페이지의 라인차트는 `priceHistory`(공시일→현재 종가열)로 그립니다.
- **운영**: GitHub Actions가 매일 자동 갱신합니다(아래 참조).

## 자동 갱신 (로컬 PC에서 하루 1회)

정기 실행은 **내 PC**가 담당합니다. `run_local.bat` + Windows 작업 스케줄러.
설치·설정은 **[scripts/setup_local.md](scripts/setup_local.md)** 참고.

```cmd
run_local.bat
```

한 번 실행하면 아래를 순서대로 처리하고, 바뀐 게 있을 때만 커밋·푸시합니다(→ Pages 자동 재배포).

| 단계 | 하는 일 |
|------|---------|
| ① 자체 검증 | 파서 회귀 시험 37건. 실패하면 중단해 잘못된 데이터가 배포되지 않게 함 |
| ② 공시 수집 | whitehouse.gov 목록에서 트럼프 278-T만 선별해 다운로드 |
| ③ **OCR** | 텍스트 레이어가 없는 스캔본은 300dpi로 렌더링 후 Tesseract로 판독 |
| ④ 파싱·필터 | 지방채·회사채·ETF를 걷어내고 개별 종목만 추출 |
| ⑤ 시세 갱신 | Yahoo Finance로 공시 후 추적 수익률 재계산 |

### 왜 OCR이 필요한가
백악관은 공시를 **종이 출력물을 스캔한 PDF**로 올립니다. 실측 결과 16건 중 6건이
텍스트 레이어가 아예 없었고(32MB·34쪽인데 추출 텍스트 33자), 나머지도 저품질 OCR이
섞여 있었습니다(`GOLDMAN`→`GOLOM.AN`, `purchase`→`ourchoso`).
개별 주식 거래는 주로 이 스캔본 쪽에 있어, OCR 없이는 정작 중요한 거래를 놓칩니다.

OCR 결과는 PDF 해시 기준으로 `.cache/ocr/`에 저장되어 **같은 문서를 두 번 읽지 않습니다.**
첫 실행만 오래 걸리고(문서당 수 분) 이후에는 빠릅니다.

### GitHub Actions
`.github/workflows/update-data.yml`은 **정기 실행을 껐고 수동 실행만** 남겼습니다.
로컬과 동시에 돌면 같은 파일을 커밋해 git 충돌이 나기 때문입니다.
로컬 PC를 못 쓸 때 Actions 탭에서 수동으로 돌릴 수 있습니다.

### 새 공시를 확실히 반영하는 방법
자동 탐색은 원본 사이트 구조가 바뀌면 놓칠 수 있습니다.
`scripts/sources.json`의 `ptrUrls`에 PDF 주소를 직접 추가하면 다음 실행부터 반영됩니다.

> 병합은 `id` 기준이며, 자동 파싱이 채우지 못하는 **수동 보완 필드(`catalyst`·`note`·`companyKo`·`sector`)는 덮어쓰지 않습니다.**

## 로컬 실행

`file://`로 직접 열면 `fetch("data.json")`가 막힙니다. 간단한 서버로 실행하세요.

```bash
python3 -m http.server 8000
# 브라우저에서 http://localhost:8000
```

## 배포 (GitHub Pages)

1. GitHub 저장소 → Settings → Pages
2. Source: `Deploy from a branch`, Branch: `main` (또는 기본 브랜치), 폴더 `/ (root)`
3. 몇 분 뒤 `https://<user>.github.io/follow_Trump/` 에서 확인

## 실제 데이터 연결 (`scripts/build_data.py`)

트럼프 **개인** 매매는 무료 의회 API(House/Senate Stock Watcher)에 **없습니다**(트럼프는 의원이 아님).
데이터 소스는 사실상 아래 세 등급입니다.

| 소스 | 비용 | 형태 | 비고 |
|------|------|------|------|
| **OGE Form 278-T (whitehouse.gov / OGE)** | 무료 | PDF | 1차 원본. 분기당 수천 건, 대부분 ETF·머니마켓·채권 |
| **Stooq 일별 시세** | 무료·키 불필요 | CSV | 가격 보강용 (`?s=<ticker>.us&i=d`) |
| Quiver *Trump Stock Trades API* | 유료 (Hobbyist $30/월~, **상업용은 별도 견적**) | JSON | 정제돼 있으나 광고 사이트는 Commercial 티어 필요 |

`build_data.py`는 무료 경로(278-T PDF + Stooq)로 `data.json`을 생성합니다.
**차별화 로직 두 가지가 여기 들어 있습니다:** ① ETF·채권 노이즈를 걷어내고 **개별 종목만** 남기고,
② 내부자 매수가가 아니라 **공시 시점가** 기준으로 수익률을 계산합니다.

```bash
pip install -r scripts/requirements.txt

# 로직 검증 (네트워크 불필요)
python3 scripts/build_data.py --self-test

# 실제 PDF에서 생성 (egress 제한 없는 로컬/서버에서)
python3 scripts/build_data.py \
  --ptr "https://www.whitehouse.gov/wp-content/uploads/2025/11/President-Donald-J.-Trump-Periodic-Transaction-Report-11.14.25.pdf" \
  --out data.json
```

> 278-T 서식은 판마다 조금씩 달라, 실제 배포 전 대상 PDF 몇 개로 파싱 결과를 한 번 검증·보정하세요.
> `catalyst`(호재/악재)·매도 연결 같은 맥락은 자동 파싱으로 다 채워지지 않아 수동 보완을 권장합니다.

`data.json` 스키마(필수 필드):

```
ticker, companyKo, companyEn, sector, instrumentType, action(buy|sell),
amountRange[min,max], transactionDate, disclosureDate,
priceAtTransaction, priceAtDisclosure, priceAfter2mFromDisclosure,
priceLatest, catalyst, note
```

> ⚠️ **라이선스 주의:** 정부 원본을 **광고 수익 목적으로 재배포**하는 것은 소스별 약관에 따라 제한될 수 있습니다.
> 상업화 전 각 소스(OGE, Stooq, Quiver)의 이용약관을 반드시 확인하세요.
>
> ⚠️ **블라인드 트러스트:** 백악관은 트럼프 자산이 블라인드 트러스트로 관리되어 본인은 개별 종목을 모른다고 주장합니다.
> 사이트는 이 전제를 상단에 명시합니다.

## AdSense

- 승인 후 `index.html` 상단 주석의 `<script ... client=ca-pub-XXXX>`를 활성화하고, `.ad-placeholder` 위치에 광고 유닛을 넣습니다.
- 사이트 루트에 `ads.txt`를 두어야 합니다 (승인 시 발급되는 pub-id로 교체).

## 면책

본 사이트는 공개 공시를 정리한 **정보 제공** 서비스이며, 특정 종목의 매수·매도를 권유하지 않습니다.
투자 자문·매매 신호가 아니며 모든 판단과 책임은 이용자 본인에게 있습니다.
