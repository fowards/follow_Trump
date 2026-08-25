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

  # 가격·추적 수익률만 매일 갱신 (PDF 불필요)
  python3 scripts/build_data.py --refresh-prices --out data.json

  # sources.json의 목록/자동탐색으로 새 공시까지 반영 (GitHub Actions용)
  python3 scripts/build_data.py --from-sources --refresh-prices --out data.json

  # 네트워크 없이 파서/분류/포맷 로직만 검증
  python3 scripts/build_data.py --self-test

주의(라이선스): 정부 원본을 광고 수익 목적으로 재배포하는 것은 소스별 약관에
따라 제한될 수 있습니다. 상업화 전 각 소스의 이용약관을 확인하세요.
"""

import argparse
import csv
import html
import io
import json
import re
import sys
import urllib.parse
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


# 채권의 결정적 신호. 개별 주식에는 만기와 표면금리가 없다.
# 실측 예: "5% DUE 09/01/38", "5.00 % Duo Jun 15 2026", "YIELD TO MATURITY"
BOND_RE = re.compile(
    r"(\d[\d.,]*\s*%\s*(due|duo|d0e))"      # 5% DUE / 5.00 % Duo
    r"|(\bdue\s+\w+\s*\d)"                  # DUE Jun 15
    r"|(yield\s+to\s+maturity)"
    r"|(\bcallable\b)|(\bcoupon\b)|(\bmaturity\b)",
    re.I)

# 지방채·기관채에 반복해서 나타나는 약어(실측 공시에서 수집).
MUNI_TOKENS = [
    "cnty", "county", "mun", "muni", "sch dist", "indpt sch", "unif sch",
    "rev", "rfdg", "rfog", "rf□g", "ser a", "ser b", "ser c", "b/e", "8/e",
    "be/r/", "bie", "ctf oblig", "auth", "pollt", "wtr", "swr", "hwys",
    "trans commn", "st rd", "gen oblig", "go bds", "putbnd", "varate",
    "cr enh", "st intrcpt", "appropriation", "approp", "tax", "brd regts",
]


def is_individual_stock(asset_name: str) -> bool:
    """자산명이 개별 상장 주식이면 True.

    ETF·펀드·머니마켓뿐 아니라 지방채/기관채도 걸러낸다.
    실측 결과 트럼프 공시의 대부분이 지방채였다.
    """
    low = asset_name.lower()
    if BOND_RE.search(asset_name):
        return False
    if any(kw in low for kw in NON_STOCK_KEYWORDS):
        return False
    if any(tok in low for tok in MUNI_TOKENS):
        return False
    return True


# 티커로 오인하기 쉬운 대문자 약어(회사명이 아님).
TICKER_STOPWORDS = {
    "INC", "CORP", "CO", "LLC", "LP", "LTD", "PLC", "SA", "NV", "AG", "USD",
    "THE", "AND", "FOR", "NEW", "ST", "SER", "DUE", "REV", "BE", "BIE", "CAB",
    "CNTY", "MUN", "AUTH", "GO", "II", "III", "IV", "PJS", "FC", "OTO", "YES",
    "NO", "VOS", "VES", "DB", "NA", "US", "USA", "ETF", "REIT", "TX", "NY",
    "CA", "FL", "PA", "WI", "MN", "NC", "OK", "AL", "IN", "MI", "WA", "MO",
}


def extract_ticker(asset_name: str) -> str | None:
    """자산명에서 티커를 뽑는다.

    1) '(MRNA)' 괄호 표기  2) 회사명 매핑
    3) 단독 대문자 토큰 — 공시 OCR이 회사명을 뭉개도(Moderna→Modorna)
       티커 자체는 대문자라 비교적 온전히 남는다.
    """
    m = re.search(r"\(([A-Z]{1,5})\)", asset_name)
    if m:
        return m.group(1)
    low = asset_name.lower()
    for name, tk in NAME_TO_TICKER.items():
        if name in low:
            return tk
    for tok in re.findall(r"\b([A-Z]{2,5})\b", asset_name):
        if tok not in TICKER_STOPWORDS:
            return tok
    return None


# ---------------------------------------------------------------------------
# 2) 278-T PDF 파싱
# ---------------------------------------------------------------------------

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"}

# 공시 PDF는 백악관이 스캔 후 저품질 OCR을 거친 텍스트를 담고 있다.
# 실측 예: "purchase"→"lourchoso"/"ourchoso", "sale"→"salo", "YES"→"VOS".
# 그래서 정확한 단어가 아니라 뭉개진 형태까지 잡는 패턴을 쓴다.
TYPE_PATTERNS = [
    (re.compile(r"(purch|ourch|urcho|urcha|pu·ch)", re.I), "buy"),
    (re.compile(r"(\bsal[eo0]\b|\bsold\b|\bsalq\b)", re.I), "sell"),
    (re.compile(r"exchan", re.I), "exchange"),
]

# 금액 구간. 구분자가 하이픈이 아니라 불릿(•)인 경우가 많고, OCR 탓에
# 콤마가 공백이나 마침표로 깨지기도 한다.
AMOUNT_RE = re.compile(
    r"[\$sS]\s?([\d][\d,.\s]{2,15}?)\s*[-–—•·‧∙]\s*[\$sS]?\s?([\d][\d,.\s]{2,15})")

# 거래일. MM/DD/YYYY 와 M/D/YY 를 모두 받는다.
DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")

# 문서 전체의 공시일(행마다 있지 않고 헤더에 한 번 나온다).
RECEIVED_RE = re.compile(r"OGE\s+RECEIVED[:\s]+(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I)


def _norm_amount(x):
    """'250,001' / '500 000' / '1.000.001' → 250001 형태의 정수. 실패 시 None."""
    d = re.sub(r"[^\d]", "", x)
    if not d or len(d) > 12:
        return None
    return int(d)


def parse_amount(text: str):
    for m in AMOUNT_RE.finditer(text):
        lo, hi = _norm_amount(m.group(1)), _norm_amount(m.group(2))
        if lo is not None and hi is not None and 0 < lo < hi:
            return [lo, hi]
    return None


def parse_date(text: str, year_hint=None):
    m = DATE_RE.search(text)
    if not m:
        return None
    mm, dd, yy = m.groups()
    y = int(yy)
    if y < 100:
        y += 2000
    try:
        return datetime(y, int(mm), int(dd)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_received_date(text: str):
    """문서 헤더의 'OGE RECEIVED: M/D/YYYY' → 공시일."""
    m = RECEIVED_RE.search(text)
    if not m:
        return None
    mm, dd, yy = m.groups()
    y = int(yy)
    if y < 100:
        y += 2000
    try:
        return datetime(y, int(mm), int(dd)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_ptr_text(text: str, disclosure_date=None):
    """278-T 텍스트에서 거래 행을 추출한다.

    실측한 실제 행 구조:
      <번호> <자산 설명> <거래유형> <거래일> <30일초과 여부> <금액 구간>
    예) 3 WASHINGTON ST HEALT 5% DUE 09/01/38 ourchoso 11/26/2025 VOS $1,000,001 -$5,000,000

    공시일은 행이 아니라 문서 헤더(OGE RECEIVED)에 한 번만 나오므로
    호출부에서 넘겨주거나 이 함수가 텍스트에서 찾아 쓴다.
    """
    if disclosure_date is None:
        disclosure_date = parse_received_date(text)

    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) < 20:
            continue
        amount = parse_amount(line)
        if not amount:
            continue
        action = None
        for pat, kind in TYPE_PATTERNS:
            if pat.search(line):
                action = kind
                break
        if not action:
            continue
        txn = parse_date(line)
        if not txn:
            continue
        # 자산 설명 = 금액·날짜·행번호를 걷어낸 앞부분
        name = AMOUNT_RE.sub(" ", line)
        name = DATE_RE.sub(" ", name)
        name = re.sub(r"^\s*\d{1,3}\s+", "", name)
        for pat, _ in TYPE_PATTERNS:
            name = pat.sub(" ", name)
        name = re.sub(r"\b(VOS|YES|NO|ves|yes|no)\b", " ", name)
        name = re.sub(r"\s{2,}", " ", name).strip(" .-|·•")
        rows.append({
            "asset": name,
            "action": action,
            "transactionDate": txn,
            "disclosureDate": disclosure_date,
            "amountRange": amount,
        })
    return rows


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def encode_url(url: str) -> str:
    """URL의 비ASCII 문자를 퍼센트 인코딩한다.

    실제 공시 파일명에 en-dash(–)가 섞여 있어 urllib이
    UnicodeEncodeError로 죽는 사례가 있었다(ASCII만 허용).
    이미 인코딩된 %XX는 건드리지 않는다.
    """
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((
        parts.scheme,
        parts.netloc.encode("idna").decode("ascii") if parts.netloc else "",
        urllib.parse.quote(parts.path, safe="/%$+(),!~*'-._:@&="),
        urllib.parse.quote(parts.query, safe="=&%+/:,$"),
        "",
    ))


def fetch_bytes(source: str) -> bytes:
    if source.startswith("http://") or source.startswith("https://"):
        req = urllib.request.Request(encode_url(source), headers=UA)
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.read()
    with open(source, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 3) 가격 보강 (Stooq, 무료·키 불필요)
# ---------------------------------------------------------------------------

def _fetch_yahoo_daily(ticker: str):
    """Yahoo Finance 차트 API — 무료·키 불필요·JSON. 1순위."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(ticker)}?range=5y&interval=1d")
    body = _get_text(url, timeout=45)
    doc = json.loads(body)
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("빈 응답")
    r = res[0]
    stamps = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    out = []
    for ts, c in zip(stamps, closes):
        if c is None:
            continue
        out.append((datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"), float(c)))
    if not out:
        raise ValueError("종가 없음")
    return out


def _fetch_stooq_daily(ticker: str):
    """Stooq CSV — 2순위.

    주의: 데이터센터 IP에서는 JS 브라우저 검증 페이지를 돌려주며 막는다
    (실측 확인). 그래서 1순위로 쓰지 않는다.
    """
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    body = _get_text(url, timeout=45)
    if not body.lstrip().lower().startswith("date"):
        raise ValueError("CSV가 아님(봇 차단 페이지로 추정)")
    out = []
    for row in csv.DictReader(io.StringIO(body)):
        try:
            out.append((row["Date"], float(row["Close"])))
        except (KeyError, ValueError):
            continue
    if not out:
        raise ValueError("행 없음")
    return out


PRICE_PROVIDERS = (("yahoo", _fetch_yahoo_daily), ("stooq", _fetch_stooq_daily))


def fetch_stooq_daily(ticker: str):
    """[(date 'YYYY-MM-DD', close)] 오름차순. 제공처를 순서대로 시도, 전부 실패 시 []."""
    for name, fn in PRICE_PROVIDERS:
        try:
            series = fn(ticker)
            print(f"  · 시세 {ticker}: {name} OK ({len(series)}일, 최신 {series[-1][0]})",
                  file=sys.stderr)
            return series
        except Exception as e:  # noqa: BLE001
            print(f"  · 시세 {ticker}: {name} 실패 — {type(e).__name__}: {e}", file=sys.stderr)
    print(f"  ! 시세 조회 전부 실패: {ticker}", file=sys.stderr)
    return []


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


def merge_records(existing, fresh):
    """새로 파싱한 레코드를 기존과 병합.

    자동 파싱이 채우지 못하는 수동 보완 필드(촉매·메모·한글명 등)는 기존 값을 지킨다.
    새 PTR에 없는 과거 거래도 버리지 않는다.
    """
    keep = ("catalyst", "note", "companyKo", "sector", "closed", "closeDate")
    by_id = {r.get("id"): r for r in existing if r.get("id")}
    out = []
    for r in fresh:
        old = by_id.pop(r.get("id"), None)
        if old:
            for k in keep:
                if old.get(k):
                    r[k] = old[k]
        out.append(r)
    out.extend(by_id.values())
    out.sort(key=lambda x: x.get("disclosureDate") or "", reverse=True)
    return out


def refresh_prices(path):
    """거래 내역은 그대로 두고 가격·추적 수익률만 최신화한다.

    새 공시가 없어도 매일 돌릴 수 있는 경로. 티커를 이미 알고 있으므로
    PDF 없이 Stooq 시세만으로 '공시 후 지금까지' 수익률이 갱신된다.
    """
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    trades = doc.get("trades", [])
    cache, updated, failed = {}, 0, []

    for t in trades:
        tk = t.get("ticker")
        if not tk:
            continue
        if tk not in cache:
            cache[tk] = fetch_stooq_daily(tk)
        series = cache[tk]
        if not series:
            failed.append(tk)
            continue
        disc = t.get("disclosureDate") or t.get("transactionDate")
        px_tx = close_on_or_after(series, t.get("transactionDate") or disc)
        px_disc = close_on_or_after(series, disc)
        if px_tx:
            t["priceAtTransaction"] = px_tx
        if px_disc:
            t["priceAtDisclosure"] = px_disc
        t["priceAfter2mFromDisclosure"] = close_on_or_after(series, add_days(disc, 60))
        t["priceLatest"] = series[-1][1]
        t["priceLatestDate"] = series[-1][0]
        if px_disc and t["priceLatest"]:
            t["trackingReturnPct"] = round(
                ((t["priceLatest"] - px_disc) / px_disc) * 100, 1)
        t["priceHistory"] = history_since(series, disc)
        # 실제 시세로 덮였으므로 '예시 값' 표기를 지운다.
        t.pop("priceValues", None)
        updated += 1

    meta = doc.setdefault("meta", {})
    # 한 건도 못 받았으면 '갱신했다'고 기록하지 않는다(거짓 표기 방지).
    if updated:
        meta["lastUpdated"] = datetime.utcnow().strftime("%Y-%m-%d")
        meta["pricesRefreshedAt"] = datetime.utcnow().strftime("%Y-%m-%d")
        meta["priceSource"] = "stooq"
    if failed:
        meta["priceFetchFailed"] = sorted(set(failed))
    else:
        meta.pop("priceFetchFailed", None)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"✓ 가격 갱신 {updated}건" + (f" / 실패 {failed}" if failed else ""), file=sys.stderr)
    return doc.get("trades", [])


def load_sources(path="scripts/sources.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"ptrUrls": [], "discover": []}


OGE_VIEW = "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index"


def _get_text(url, timeout=60):
    req = urllib.request.Request(encode_url(url), headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _strip_tags(x):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x)).strip()


def oge_view_entries(base=OGE_VIEW, verbose=False):
    """PAS Index 뷰에서 (문서 UNID, 표시 텍스트) 목록을 얻는다.

    OGE는 Lotus Domino라 ?ReadViewEntries 가 XML을 준다(파싱하기 가장 안정적).
    실패하면 일반 HTML 뷰로 폴백한다.
    """
    attempts = [
        base + "?ReadViewEntries&Count=2000",
        base + "?ReadViewEntries",
        base + "?OpenView&Count=2000",
        base + "?OpenView",
    ]
    for url in attempts:
        try:
            body = _get_text(url)
        except Exception as e:  # noqa: BLE001
            print(f"  · 뷰 실패 {url}: {e}", file=sys.stderr)
            continue
        out = []
        if "<viewentry" in body:
            for m in re.finditer(
                r'<viewentry[^>]*unid="([0-9A-Fa-f]{32})"(.*?)</viewentry>', body, re.S):
                texts = " ".join(re.findall(r"<text>(.*?)</text>", m.group(2), re.S))
                out.append((m.group(1), html.unescape(_strip_tags(texts))))
        if not out:
            for m in re.finditer(
                r'href="[^"]*?([0-9A-Fa-f]{32})\?OpenDocument"[^>]*>(.*?)</a>', body, re.S | re.I):
                out.append((m.group(1), html.unescape(_strip_tags(m.group(2)))))
        if out:
            print(f"  · 뷰 OK {url} — 항목 {len(out)}건", file=sys.stderr)
            if verbose:
                for u, l in out[:5]:
                    print(f"      예시: {u} | {l[:80]}", file=sys.stderr)
            return out
        print(f"  · 뷰 응답은 받았으나 항목 미검출 {url} (본문 {len(body)}자)", file=sys.stderr)
        if verbose:
            print("      본문 앞부분:", body[:400].replace("\n", " "), file=sys.stderr)
    return []


def oge_doc_pdfs(unid, base=OGE_VIEW, verbose=False):
    """문서 하나를 열어 첨부된 PDF의 $FILE 링크를 뽑는다."""
    url = f"{base}/{unid}?OpenDocument"
    try:
        body = _get_text(url)
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"      문서 실패 {unid}: {e}", file=sys.stderr)
        return []
    links = re.findall(r'href="([^"]*\$[Ff][Ii][Ll][Ee][^"]*\.pdf)"', body)
    return [urllib.parse.urljoin(url, l) for l in links]


def discover_oge(cfg, verbose=False):
    """OGE PAS Index에서 대상 인물의 278-T PDF 링크를 자동 수집한다."""
    cfg = cfg or {}
    base = cfg.get("viewUrl", OGE_VIEW)
    filer = re.compile(cfg.get("filerPattern", "trump"), re.I)
    form = re.compile(cfg.get("formPattern", r"278-?T"), re.I)
    limit = int(cfg.get("maxDocs", 60))

    entries = oge_view_entries(base, verbose=verbose)
    if not entries:
        print("  ! OGE 뷰에서 목록을 얻지 못했습니다.", file=sys.stderr)
        return []

    hits = [(u, l) for u, l in entries if filer.search(l)]
    print(f"  · 대상 인물 매칭 {len(hits)}건 / 전체 {len(entries)}건", file=sys.stderr)
    if verbose:
        for u, l in hits[:10]:
            print(f"      매칭: {u} | {l[:90]}", file=sys.stderr)

    pdfs = []
    for unid, label in hits[:limit]:
        for link in oge_doc_pdfs(unid, base, verbose=verbose):
            # 연간보고서(278e/ANNUAL)가 아니라 거래보고서(278-T)만 받는다.
            if form.search(link) or form.search(label):
                if link not in pdfs:
                    pdfs.append(link)
                    if verbose:
                        print(f"      PDF: {link}", file=sys.stderr)
    print(f"· OGE 자동 탐색 결과 278-T PDF {len(pdfs)}건", file=sys.stderr)
    return pdfs


def discover_ptr_urls(cfg):
    """설정에 따라 새 PTR PDF를 탐색. 실패해도 예외를 던지지 않는다."""
    found = []
    if cfg.get("oge"):
        try:
            found += discover_oge(cfg["oge"])
        except Exception as e:  # noqa: BLE001
            print(f"  ! OGE 탐색 중 오류: {e}", file=sys.stderr)
    # 목록 페이지에서 링크 수집
    for c in cfg.get("discover") or []:
        url = c.get("listUrl")
        if not url:
            continue
        try:
            body = _get_text(url)
        except Exception as e:  # noqa: BLE001
            print(f"  ! 목록 조회 실패 {url}: {e}", file=sys.stderr)
            continue
        raw = re.findall(c.get("linkPattern", r'href="([^"]+\.pdf)"'), body, flags=re.I)
        # 이 목록에는 백악관 직원 전체의 공시가 섞여 있다. 대상 인물만 남긴다.
        keep = re.compile(c.get("filerPattern", "trump"), re.I)
        picked = 0
        for m in raw:
            link = m if m.startswith("http") else urllib.parse.urljoin(url, m)
            if not keep.search(urllib.parse.unquote(link)):
                continue
            if link not in found:
                found.append(link)
                picked += 1
        print(f"  · 목록 {url} — 링크 {len(raw)}건 중 대상 {picked}건", file=sys.stderr)
    return found


def write_sitemap(records, base_url, out_path="sitemap.xml"):
    """홈·용어집·종목 상세 URL로 sitemap.xml 생성(SEO)."""
    base = base_url.rstrip("/")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    urls = [
        f"{base}/",
        f"{base}/about.html",
        f"{base}/guide.html",
        f"{base}/glossary.html",
        f"{base}/privacy.html",
    ]
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

# 아래는 실제 공시 PDF(2026-01-14 접수분)에서 추출된 텍스트를 그대로 가져온
# 것이다. 백악관 스캔 OCR 특유의 오인식(purchase→ourchoso, YES→VOS,
# 구분자가 하이픈이 아닌 불릿)이 그대로 들어 있어 회귀 시험에 적합하다.
SAMPLE_PTR_TEXT = """OGE Form 278-T (Updated February 2024)
U.S. Ollico of Govommont Ethics; 5 C.F.R. part 2634
Flier"s Nome I Pnnn
Donfld J Trump I D->?o? nf7
OGE RECEIVED:  1/14/2026
Oa■crlDtlon 1'vDe Data Daya Ago Amount
1 MIAMI-DADE CNTY Fl WTR & SR B fN BE/R/ 5 DUE 100133 OTO 120425 FC 040126 2.610% YIELD TO MATURITY lourchoso 11/14/2025 VOS $250,001 • $500,000
2 NEW YORK NY CITY MUN WT RV BE/R/ 2,7 061543 OTO 111413 CALLABLE VARATE PUTBND salo 11/17/2025 ves $250,001 • $500 000
3 WASHINGTON ST HEALT 5% DUE 09/01/38 ourchoso 11/26/2025 VOS $1,000,001 -$5,000,000
7 MISSOURI ST HWYS & TRANS COMMN ST RD REV APPROP MEGA PJS SER A B/E 5.00 % Duo Moy 1, 2026 lpurchaso 12/10/25 VOS $100,001 -$250,000
30 Modorna Inc MRNA ourchoso 3/2/2026 VOS $15,001 • $50,000
31 Comcast Corp CMCSA lourchoso 2/10/2026 VOS $1,001 • $15,000
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
    check("지방채는 제외", not is_individual_stock("MIAMI-DADE CNTY Fl WTR & SR B DUE 100133"))
    check("Money Market는 제외", not is_individual_stock("Fidelity Money Market Fund"))

    # 티커 추출
    check("괄호 티커 추출", extract_ticker("Comcast Corporation (CMCSA)") == "CMCSA")
    check("이름맵 티커 추출", extract_ticker("Moderna, Inc.") == "MRNA")

    # 실제 OCR 텍스트 파싱
    rows = parse_ptr_text(SAMPLE_PTR_TEXT)
    check(f"거래행 6개 파싱 (실제 {len(rows)})", len(rows) == 6)

    check("문서 헤더에서 공시일 추출", parse_received_date(SAMPLE_PTR_TEXT) == "2026-01-14")
    if rows:
        check("모든 행에 공시일 부여", all(r["disclosureDate"] == "2026-01-14" for r in rows))

    muni = [r for r in rows if "MIAMI" in r["asset"]]
    check("불릿(•) 구분 금액 파싱", bool(muni) and muni[0]["amountRange"] == [250001, 500000])
    check("불릿 행의 거래일", bool(muni) and muni[0]["transactionDate"] == "2025-11-14")
    check("깨진 'lourchoso'를 매수로 인식", bool(muni) and muni[0]["action"] == "buy")

    sale = [r for r in rows if "NEW YORK" in r["asset"]]
    check("깨진 'salo'를 매도로 인식", bool(sale) and sale[0]["action"] == "sell")
    check("콤마 없는 금액 파싱", bool(sale) and sale[0]["amountRange"] == [250001, 500000])

    two = [r for r in rows if "MISSOURI" in r["asset"]]
    check("두자리 연도(12/10/25) 파싱", bool(two) and two[0]["transactionDate"] == "2025-12-10")

    mrna = [r for r in rows if "MRNA" in r["asset"] or "odorna" in r["asset"]]
    check("개별 종목 행 인식", bool(mrna))
    if mrna:
        check("개별 종목 거래일", mrna[0]["transactionDate"] == "2026-03-02")

    # 노이즈 필터: 지방채를 걸러내고 개별 종목만 남겨야 한다
    kept = [r for r in rows if is_individual_stock(r["asset"]) and extract_ticker(r["asset"])]
    check(f"개별종목만 2건 남김 (실제 {len(kept)})", len(kept) == 2)

    # 가격 헬퍼
    series = [("2026-05-11", 26.0), ("2026-05-12", 26.4), ("2026-07-11", 34.1)]
    check("공시일 종가 26.4", close_on_or_after(series, "2026-05-12") == 26.4)
    check("+60일 종가 34.1", close_on_or_after(series, add_days("2026-05-12", 60)) == 34.1)

    hist = history_since(series, "2026-05-12")
    check(f"추적 이력 2점 (실제 {len(hist)})", len(hist) == 2)
    check("추적 이력 시작 = 공시일", hist[0][0] == "2026-05-12")

    # 병합: 자동 파싱이 못 채우는 수동 필드를 덮어쓰지 않아야 한다
    existing = [
        {"id": "a", "ticker": "AAA", "catalyst": "수동 촉매", "companyKo": "에이"},
        {"id": "old", "ticker": "OLD", "disclosureDate": "2020-01-01"},
    ]
    fresh = [{"id": "a", "ticker": "AAA", "catalyst": "", "companyKo": "AAA",
              "disclosureDate": "2026-01-01"}]
    merged = merge_records(existing, fresh)
    got = [x for x in merged if x["id"] == "a"][0]
    check("병합 시 수동 catalyst 보존", got["catalyst"] == "수동 촉매")
    check("병합 시 수동 한글명 보존", got["companyKo"] == "에이")
    check("병합 시 기존 거래 유지", any(x["id"] == "old" for x in merged))

    # 소스 목록 로딩(파일 없어도 죽지 않아야 함)
    cfg = load_sources("scripts/does-not-exist.json")
    check("소스 파일 없으면 빈 설정 반환", cfg.get("ptrUrls") == [])

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
    ap.add_argument("--refresh-prices", action="store_true",
                    help="거래 내역은 두고 가격·추적 수익률만 최신화 (PDF 불필요, 매일 실행용)")
    ap.add_argument("--from-sources", action="store_true",
                    help="scripts/sources.json의 PTR 목록/자동탐색으로 갱신")
    ap.add_argument("--sources", default="scripts/sources.json", help="소스 목록 경로")
    ap.add_argument("--probe-oge", action="store_true",
                    help="OGE 자동 탐색만 실행해 진단 출력(데이터 변경 없음)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.probe_oge:
        cfg = load_sources(args.sources)
        print("=== OGE 자동 탐색 진단 ===", file=sys.stderr)
        urls = discover_oge(cfg.get("oge") or {}, verbose=True)
        print(f"\n=== 최종 {len(urls)}건 ===", file=sys.stderr)
        for u in urls:
            print(u)
        return 0 if urls else 2

    # 1) 새 공시 파싱 (선택) — 기존 수동 보완 내용은 병합으로 보존
    ptrs = list(args.ptr)
    if args.from_sources:
        cfg = load_sources(args.sources)
        ptrs += [u for u in cfg.get("ptrUrls", []) if u not in ptrs]
        ptrs += [u for u in discover_ptr_urls(cfg.get("discover")) if u not in ptrs]

    if ptrs:
        fresh, stats = build_from_ptrs(ptrs, enrich=not args.no_price)
        existing = []
        try:
            with open(args.out, encoding="utf-8") as f:
                existing = json.load(f).get("trades", [])
        except (FileNotFoundError, ValueError):
            pass
        merged = merge_records(existing, fresh)
        write_data_json(merged, stats, args.out)

    # 2) 가격 갱신 (선택) — 새 공시가 없어도 매일 돌릴 수 있는 경로
    if args.refresh_prices:
        refresh_prices(args.out)

    if not ptrs and not args.refresh_prices:
        ap.error("--ptr / --from-sources / --refresh-prices / --self-test 중 하나가 필요합니다.")

    with open(args.out, encoding="utf-8") as f:
        records = json.load(f).get("trades", [])
    write_sitemap(records, args.base_url, args.sitemap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
