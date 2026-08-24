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
| `index.html` | 페이지 골격 (한국어, 모바일 대응) |
| `styles.css` | 다크 테마, 한국식 색(상승=빨강/하락=파랑) |
| `app.js` | `data.json`을 읽어 카드·타임라인·통계 렌더링 |
| `data.json` | 매매 데이터 (**현재 예시 데이터**) |

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

## 실제 데이터 연결 (다음 단계)

현재 `data.json`은 구조 확인용 **예시 데이터**입니다. 실제 서비스 시 공시 API로 교체합니다.

- 공시 원본: House Clerk / SEC (PDF 기반, 파싱 필요)
- 정제 API 후보: EODHD, CongressInvests 등 (거래일·공시일·금액 구간·지연 일수 필드 제공)

`data.json`의 스키마만 유지하면 프론트엔드는 그대로 재사용됩니다. 필수 필드:

```
ticker, companyKo, companyEn, sector, action(buy|sell),
amountRange[min,max], transactionDate, disclosureDate,
priceAtTransaction, priceAtDisclosure, priceAfter2mFromDisclosure,
priceLatest, catalyst, note
```

> ⚠️ 라이선스 주의: 정부 원본 데이터를 **광고 수익 목적으로 재배포**하는 것은 데이터 소스별 이용약관에 따라 제한될 수 있습니다(예: House Clerk 약관). 상업화 전 각 소스의 라이선스를 반드시 확인하세요.

## AdSense

- 승인 후 `index.html` 상단 주석의 `<script ... client=ca-pub-XXXX>`를 활성화하고, `.ad-placeholder` 위치에 광고 유닛을 넣습니다.
- 사이트 루트에 `ads.txt`를 두어야 합니다 (승인 시 발급되는 pub-id로 교체).

## 면책

본 사이트는 공개 공시를 정리한 **정보 제공** 서비스이며, 특정 종목의 매수·매도를 권유하지 않습니다.
투자 자문·매매 신호가 아니며 모든 판단과 책임은 이용자 본인에게 있습니다.
