#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
트럼프 팔로우 — 데이터 수집 파이프라인
=======================================

트럼프의 OGE Form 278-T(정기 거래 보고서) PDF를 읽어서, 웹사이트가 쓰는
data.json 스키마로 변환합니다. 핵심 차별화 로직 두 가지가 여기에 들어 있습니다.

  1) ETF·머니마켓·채권 등 '노이즈'를 걷어내고 개별 종목만 남긴다.
  2) 내부자 매수가가 아니라 '공시 시점가'를 기준으로 수익률을 계산한다.

무료 데이터 소스
  - 1차 원본(무료): whitehouse.gov / OGE 에 올라오는 278-T PDF
      예: https://www.whitehouse.gov/wp-content/uploads/2025/11/
          President-Donald-J.-Trump-Periodic-Transaction-Report-11.14.25.pdf
  - 가격(무료, 키 불필요): Stooq 일별 CSV  https://stooq.com/q/d/l/?s=<ticker>.us&i=d
  - (선택) Quiver Trump API: 유료(Commercial). --quiver-token 로 사용.

사용법
  # 실제 PDF에서 생성 (로컬에서 실행 — 회사/샌드박스 egress 막히면 안 됨)
  python3 scripts/build_data.py --ptr <PDF-URL-또는-경로> --out data.json

  # 여러 PDF를 합쳐서 생성
  python3 scripts/build_data.py --ptr a.pdf --ptr b.pdf --out data.json

  # 네트워크 없이 파서/분류/포맷 로직만 검증
  python3 scripts/build_data.py --self-test

주의(라이선스): 정부 원본을 광고 수익 목적으로 재배포하는 것은 소스별 약관에
따라 제한될 수 있습니다. 상업화 전 각 소스의 이용약관을 확인하세요.
"""

import argparse
import csv
import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# 1) 개별 종목 vs 노이즈(ETF/펀드/채권) 분류
# ---------------------------------------------------------------------------

# 자산명에 이 키워드가 들어 있으면 개별 종목이 아니라고 본다.
NON_STOCK_KEYWORDS = [
    "etf", "fund", "index", "trust fund", "money market", "treasury",
    "t-bill", "bill", "bond", "note", "municipal", "muni", "cd ",
    "certificate of deposit", "ishares", "vanguard", "spdr", "invesco",
    "proshares", "schwab", "fidelity", "ubs", "pimco", "select sector",
    "s&p 500", "nasdaq-100", "russell", "total market", "money mkt",
]

# 티커가 자산명에 안 붙어 나오는 경우를 위한 최소 이름→티커 맵(필요 시 확장).
NAME_TO_TICKER = {
    "moderna": "MRNA",
    "comcast": "CMCSA",
    "ptc inc": "PTC",
    "ptc": "PTC",
    "nvidia": "NVDA",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "merck": "MRK",
}

# 섹터 라벨(표시용). 없으면 '기타'.
TICKER_SECTOR = {
    "MRNA": "바이오/제약", "MRK": "바이오/제약",
    "CMCSA": "미디어/통신",
    "PTC": "소프트웨어", "MSFT": "소프트웨어",
    "NVDA": "반도체/AI",
    "AAPL": "빅테크", "AMZN": "빅테크",
}

TICKER_NAME_KO = {
    "MRNA": "모더나", "MRK": "머크", "CMCSA": "컴캐스트", "PTC": "PTC",
    "NVDA": "엔비디아", "AAPL": "애플", "MSFT": "마이크로소프트", "AMZN": "아마존",
}


def is_individual_stock(asset_name: str) -> bool:
    """자산명이 개별 상장 주식이면 True, ETF/펀드/채권류면 False."""
    low = asset_name.lower()
    return not any(kw in low for kw in NON_STOCK_KEYWORDS)


def extract_ticker(asset_name: str) -> str | None:
    """자산명에서 티커를 뽑는다. '(MRNA)' 형태 우선, 없으면 이름 맵."""
    m = re.search(r"\(([A-Z]{1,5})\)", asset_name)
    if m:
        return m.group(1)
    low = asset_name.lower()
    for name, tk in NAME_TO_TICKER.items():
        if name in low:
            return tk
    return None


# ---------------------------------------------------------------------------
# 2) 278-T PDF 파싱
# ---------------------------------------------------------------------------

TXN_TYPE = {"P": "buy", "S": "sell", "S (partial)": "sell", "E": "exchange"}

# 금액 구간 문자열 → [min, max]
AMOUNT_RE = re.compile(r"\$([\d,]+)\s*[-–]\s*\$([\d,]+)")
DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def parse_amount(text: str):
    m = AMOUNT_RE.search(text)
    if not m:
        return None
    lo = int(m.group(1).replace(",", ""))
    hi = int(m.group(2).replace(",", ""))
    return [lo, hi]


def parse_date(text: str):
    m = DATE_RE.search(text)
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


def parse_ptr_text(text: str):
    """278-T 텍스트에서 거래 행을 추출한다(휴리스틱).

    보고서 서식은 판마다 조금씩 달라서, 한 '행'을 다음 신호로 인식한다.
      - 거래유형 토큰 (P/S/E) 이 있고
      - MM/DD/YYYY 날짜가 최소 1개(거래일), 보통 2개(거래일, 통지/공시일)
      - $범위 금액
    실제 배포용으로 쓸 땐 대상 PDF 몇 개로 한 번 검증/보정하는 것을 권장.
    """
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        dates = DATE_RE.findall(line)
        amount = parse_amount(line)
        # 거래유형: 단독 대문자 토큰 P/S/E (앞뒤 공백)
        ttype = None
        tm = re.search(r"(?<![A-Za-z])([PSE])(?![A-Za-z])", line)
        if tm:
            ttype = TXN_TYPE.get(tm.group(1))
        if ttype and dates and amount:
            # 자산명 = 금액/날짜/유형 토큰을 걷어낸 앞부분
            name = AMOUNT_RE.sub("", line)
            name = DATE_RE.sub("", name).strip(" .-\t")
            txn_date = f"{dates[0][2]}-{dates[0][0]}-{dates[0][1]}"
            disc_date = None
            if len(dates) >= 2:
                disc_date = f"{dates[1][2]}-{dates[1][0]}-{dates[1][1]}"
            rows.append({
                "asset": name,
                "action": ttype,
                "transactionDate": txn_date,
                "disclosureDate": disc_date,
                "amountRange": amount,
            })
    return rows


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def fetch_bytes(source: str) -> bytes:
    if source.startswith("http://") or source.startswith("https://"):
        req = urllib.request.Request(source, headers={"User-Agent": "follow-trump/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    with open(source, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 3) 가격 보강 (Stooq, 무료·키 불필요)
# ---------------------------------------------------------------------------

def fetch_stooq_daily(ticker: str):
    """[(date 'YYYY-MM-DD', close float)] 오름차순. 실패 시 []"""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "follow-trump/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print(f"  ! Stooq 가격 조회 실패 {ticker}: {e}", file=sys.stderr)
        return []
    out = []
    for row in csv.DictReader(io.StringIO(body)):
        try:
            out.append((row["Date"], float(row["Close"])))
        except (KeyError, ValueError):
            continue
    return out


def close_on_or_after(series, date_str: str):
    """date_str 당일 또는 그 이후 첫 거래일의 종가."""
    for d, c in series:
        if d >= date_str:
            return c
    return None


def add_days(date_str: str, days: int) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 4) 레코드 조립
# ---------------------------------------------------------------------------

def history_since(series, start_date, max_points=30):
    """공시일 이후 (date, close) 이력을 추려서 최대 max_points개로 다운샘플."""
    pts = [[d, c] for d, c in series if d >= start_date]
    if len(pts) <= max_points:
        return pts
    step = len(pts) / float(max_points)
    out = [pts[int(i * step)] for i in range(max_points)]
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def build_record(row, series):
    ticker = row["ticker"]
    disc = row["disclosureDate"] or row["transactionDate"]
    price_tx = close_on_or_after(series, row["transactionDate"]) if series else None
    price_disc = close_on_or_after(series, disc) if series else None
    price_2m = close_on_or_after(series, add_days(disc, 60)) if series else None
    price_latest = series[-1][1] if series else None
    price_latest_date = series[-1][0] if series else None
    # 공시 후 지금까지 계속 추적한 누적 수익률 + 추적 차트용 이력
    tracking = None
    if price_disc and price_latest:
        tracking = round(((price_latest - price_disc) / price_disc) * 100, 1)
    price_history = history_since(series, disc) if series else []
    return {
        "id": f"{ticker.lower()}-{row['transactionDate']}",
        "ticker": ticker,
        "companyKo": TICKER_NAME_KO.get(ticker, ticker),
        "companyEn": row["asset"][:60],
        "sector": TICKER_SECTOR.get(ticker, "기타"),
        "instrumentType": "stock",
        "action": row["action"],
        "amountRange": row["amountRange"],
        "transactionDate": row["transactionDate"],
        "disclosureDate": disc,
        "priceAtTransaction": price_tx,
        "priceAtDisclosure": price_disc,
        "priceAfter2mFromDisclosure": price_2m,
        "priceLatest": price_latest,
        "priceLatestDate": price_latest_date,
        "trackingReturnPct": tracking,
        "priceHistory": price_history,
        "catalyst": "",
        "closed": False,
        "closeDate": None,
        "note": "OGE 278-T 공시 자동 파싱. 촉매/매도 정보는 수동 보완 권장.",
    }


def build_from_ptrs(sources, enrich=True):
    raw_rows = []
    for src in sources:
        print(f"· PTR 로드: {src}", file=sys.stderr)
        text = extract_pdf_text(fetch_bytes(src))
        parsed = parse_ptr_text(text)
        print(f"  → 원시 거래행 {len(parsed)}건", file=sys.stderr)
        raw_rows.extend(parsed)

    # 개별 종목만 남기고 티커 부여
    kept = []
    dropped = 0
    for r in raw_rows:
        if not is_individual_stock(r["asset"]):
            dropped += 1
            continue
        tk = extract_ticker(r["asset"])
        if not tk:
            dropped += 1
            continue
        r["ticker"] = tk
        kept.append(r)
    print(f"· 필터: 개별종목 {len(kept)}건 / 제외(ETF·채권·미식별) {dropped}건", file=sys.stderr)

    # 가격 보강 (티커별 1회 조회 캐시)
    cache = {}
    records = []
    for r in kept:
        if enrich:
            if r["ticker"] not in cache:
                cache[r["ticker"]] = fetch_stooq_daily(r["ticker"])
            series = cache[r["ticker"]]
        else:
            series = []
        records.append(build_record(r, series))

    records.sort(key=lambda x: x["disclosureDate"], reverse=True)
    return records, {"kept": len(kept), "dropped": dropped, "raw": len(raw_rows)}


def write_data_json(records, stats, out_path):
    doc = {
        "meta": {
            "subject": "Donald J. Trump",
            "subjectKo": "도널드 트럼프",
            "dataSource": "oge-278t",
            "generatedBy": "scripts/build_data.py",
            "note": f"OGE 278-T 자동 파싱 결과. 원시 {stats['raw']}건 중 개별종목 {stats['kept']}건만 표시(ETF·채권 등 {stats['dropped']}건 제외).",
            "caveats": [
                "백악관은 트럼프 자산이 블라인드 트러스트로 관리된다고 주장합니다.",
                "공시 원본 대부분은 ETF·머니마켓·채권입니다. 이 사이트는 개별 종목만 보여줍니다.",
                "공시는 거래 후 상당한 지연을 두고 공개됩니다.",
            ],
            "lastUpdated": datetime.utcnow().strftime("%Y-%m-%d"),
        },
        "trades": records,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"✓ {out_path} 작성 완료 — 개별종목 {len(records)}건", file=sys.stderr)


def write_sitemap(records, base_url, out_path="sitemap.xml"):
    """홈·용어집·종목 상세 URL로 sitemap.xml 생성(SEO)."""
    base = base_url.rstrip("/")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    urls = [f"{base}/", f"{base}/glossary.html", f"{base}/privacy.html"]
    for tk in sorted({r["ticker"] for r in records}):
        urls.append(f"{base}/stock.html?ticker={tk}")
    body = "\n".join(
        f"  <url><loc>{u}</loc><lastmod>{today}</lastmod></url>" for u in urls
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           f"{body}\n</urlset>\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)
    print(f"✓ {out_path} 작성 완료 — URL {len(urls)}개", file=sys.stderr)


# ---------------------------------------------------------------------------
# 5) 셀프 테스트 (네트워크 불필요)
# ---------------------------------------------------------------------------

SAMPLE_PTR_TEXT = """
Asset Trans Date Notified Amount
Moderna, Inc. (MRNA) P 03/02/2026 05/12/2026 $15,001 - $50,000
Comcast Corporation (CMCSA) P 02/10/2026 05/12/2026 $1,001 - $15,000
Vanguard S&P 500 ETF (VOO) P 02/01/2026 05/12/2026 $50,001 - $100,000
U.S. Treasury Bill S 02/15/2026 05/12/2026 $250,001 - $500,000
Fidelity Money Market Fund P 02/20/2026 05/12/2026 $100,001 - $250,000
PTC Inc. (PTC) P 02/24/2026 05/12/2026 $1,001 - $15,000
Moderna, Inc. (MRNA) S 05/18/2026 07/01/2026 $1,001 - $15,000
"""


def self_test():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + name)
        ok = ok and cond

    # 분류
    check("MRNA는 개별종목", is_individual_stock("Moderna, Inc. (MRNA)"))
    check("VOO는 ETF로 제외", not is_individual_stock("Vanguard S&P 500 ETF (VOO)"))
    check("Treasury는 제외", not is_individual_stock("U.S. Treasury Bill"))
    check("Money Market는 제외", not is_individual_stock("Fidelity Money Market Fund"))

    # 티커 추출
    check("괄호 티커 추출", extract_ticker("Comcast Corporation (CMCSA)") == "CMCSA")
    check("이름맵 티커 추출", extract_ticker("Moderna, Inc.") == "MRNA")

    # 파싱
    rows = parse_ptr_text(SAMPLE_PTR_TEXT)
    check(f"거래행 7개 파싱 (실제 {len(rows)})", len(rows) == 7)
    mrna = [r for r in rows if "MRNA" in r["asset"]][0]
    check("거래일 파싱 2026-03-02", mrna["transactionDate"] == "2026-03-02")
    check("공시일 파싱 2026-05-12", mrna["disclosureDate"] == "2026-05-12")
    check("금액 파싱 [15001,50000]", mrna["amountRange"] == [15001, 50000])
    check("매수/매도 구분", mrna["action"] == "buy")

    # 필터 통합 (가격 조회 없이)
    kept = []
    for r in rows:
        if is_individual_stock(r["asset"]) and extract_ticker(r["asset"]):
            kept.append(r)
    check(f"개별종목만 4건 남김 (실제 {len(kept)})", len(kept) == 4)

    # 가격 헬퍼
    series = [("2026-05-11", 26.0), ("2026-05-12", 26.4), ("2026-07-11", 34.1)]
    check("공시일 종가 26.4", close_on_or_after(series, "2026-05-12") == 26.4)
    check("+60일 종가 34.1", close_on_or_after(series, add_days("2026-05-12", 60)) == 34.1)

    # 추적 이력: 공시일 이후만 남김 (05-11은 제외, 05-12/07-11만)
    hist = history_since(series, "2026-05-12")
    check(f"추적 이력 2점 (실제 {len(hist)})", len(hist) == 2)
    check("추적 이력 시작 = 공시일", hist[0][0] == "2026-05-12")

    print("\n" + ("전체 통과 ✅" if ok else "실패 있음 ❌"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="트럼프 278-T → data.json 파이프라인")
    ap.add_argument("--ptr", action="append", default=[], help="278-T PDF URL 또는 로컬 경로 (반복 가능)")
    ap.add_argument("--out", default="data.json", help="출력 경로 (기본 data.json)")
    ap.add_argument("--no-price", action="store_true", help="가격 보강 생략(구조만)")
    ap.add_argument("--base-url", default="https://fowards.github.io/follow_Trump",
                    help="sitemap.xml 생성용 사이트 기본 URL")
    ap.add_argument("--sitemap", default="sitemap.xml", help="sitemap 출력 경로")
    ap.add_argument("--self-test", action="store_true", help="네트워크 없이 로직 검증")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not args.ptr:
        ap.error("--ptr 를 최소 1개 주거나 --self-test 를 사용하세요.")

    records, stats = build_from_ptrs(args.ptr, enrich=not args.no_price)
    write_data_json(records, stats, args.out)
    write_sitemap(records, args.base_url, args.sitemap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
