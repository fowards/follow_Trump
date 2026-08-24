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

## 자동 갱신 (GitHub Actions)

`.github/workflows/update-data.yml` 이 **매일 06:00 KST**(21:00 UTC)에 실행됩니다.
러너는 외부 네트워크가 열려 있어 whitehouse.gov·Stooq에 직접 접근합니다. public 저장소라 Actions는 무료입니다.

갱신은 성격이 다른 두 단계로 나뉩니다.

| 단계 | 하는 일 | 안정성 |
|------|---------|--------|
| ① 새 공시(PTR) 확인 | `--from-sources` — `scripts/sources.json`의 URL 목록 + 목록 페이지 자동 탐색 | 원본 사이트 구조에 의존 → **실패 가능**. `continue-on-error`로 감싸 ②를 막지 않음 |
| ② 가격·추적 갱신 | `--refresh-prices` — 티커를 이미 아니까 PDF 없이 시세만 갱신 | 안정적. 매일 '공시 후 지금까지' 수익률이 최신화됨 |

data.json / sitemap.xml 이 **실제로 바뀐 경우에만** 커밋하며, 커밋이 나가면 Pages가 자동 재배포합니다.

```bash
# 로컬에서 같은 동작 확인
python3 scripts/build_data.py --refresh-prices --out data.json          # 가격만
python3 scripts/build_data.py --from-sources --refresh-prices --out data.json  # 공시 탐색 + 가격
```

### 새 공시를 확실히 반영하는 방법

자동 탐색은 원본 사이트가 바뀌면 놓칠 수 있습니다. 확실한 경로는 두 가지입니다.

1. **URL을 목록에 추가** — `scripts/sources.json`의 `ptrUrls`에 PDF 주소를 넣고 커밋하면 다음 실행부터 반영됩니다.
2. **수동 실행** — 저장소 Actions 탭 → "데이터 자동 갱신" → *Run workflow* → `ptr_url` 칸에 PDF 주소를 넣고 실행.

> 병합은 `id` 기준이며, 자동 파싱이 채우지 못하는 **수동 보완 필드(`catalyst`·`note`·`companyKo`·`sector`)는 덮어쓰지 않습니다.**
> 따라서 손으로 다듬은 해설이 자동 갱신으로 사라지지 않습니다.


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
