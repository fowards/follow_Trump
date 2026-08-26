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
import hashlib
import html
import io
import os
import json
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

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
    r"(\d[\d.,]*\s*%)"                       # 표면금리: 5% / 02.850% / 5.0000%
    r"|(\bdue\b|\bduo\b)"                    # DUE '30 / Duo Jun 15
    r"|(yield\s+to\s+maturity)"
    r"|(\bcallable\b)|(\bcoupon\b)|(\bmaturity\b)"
    r"|(\bnts\b|\bnotes?\b|\bbds?\b|\bbnd\b)"   # NTS / NOTES / BDS
    r"|(\breg[\s.]?s\b)|(pursuant)"                         # REG S (해외발행 채권)
    r"|(\b(dt0|otd|om|oto)\d{4,6}\b)"          # 발행일 코드(OCR 변형 포함)
    r"|(\b\d{6}\b\s*$)",                      # 끝의 6자리 만기 코드
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


# ---------------------------------------------------------------------------
# 종목명 → 티커 해석 (SEC company_tickers.json)
# ---------------------------------------------------------------------------
# 278-T에는 티커가 없고 회사명만 있다("ADOBE INC"). 수백 개 종목의 티커를
# 손으로 넣을 수 없으니, SEC가 무료로 공개하는 공식 목록으로 해석한다.
#   https://www.sec.gov/files/company_tickers.json
# 형식: {"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."}, ...}

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_TICKERS_FILE = os.environ.get("FT_SEC_TICKERS", "scripts/company_tickers.json")
_SEC_INDEX = None          # 정규화된 회사명 → 티커

# 회사명에서 떼어낼 접미사·주식종류 표기. 정규화 때 양쪽에서 똑같이 지워
# "ADOBE INC" 와 SEC의 "Adobe Inc." 가 같아지게 한다.
_NAME_NOISE = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "COS",
    "LLC", "LP", "LLP", "LTD", "PLC", "SA", "NV", "AG", "SE", "AB",
    "HLDG", "HLDGS", "HOLDING", "HOLDINGS", "GROUP", "GRP", "THE",
    "COM", "COMMON", "STOCK", "STK", "CAP", "SHS", "SH", "SHARES",
    "CL", "CLA", "CLB", "CLC", "CLASS", "A", "B", "C", "NEW", "ADR", "ADS",
    "SER", "REIT", "TR", "TRUST", "FUND", "ORD", "NPV", "PAR",
}


def _normalize_company(name: str) -> str:
    """회사명을 비교용으로 정규화한다(대문자·부호제거·접미사제거)."""
    name = re.sub(r"[^A-Za-z0-9& ]", " ", name.upper())
    name = name.replace("&", " AND ")
    toks = [t for t in name.split() if t and t not in _NAME_NOISE]
    return " ".join(toks)


def load_ticker_index(path=None, fetch=True, quiet=False):
    """SEC 티커 목록을 읽어 정규화 인덱스를 만든다.

    로컬 파일이 있으면 그걸 쓰고, 없으면(그리고 fetch=True면) SEC에서 받아
    캐시한다. 파일도 네트워크도 없으면 인덱스 없이(=기존 방식) 넘어간다.
    """
    global _SEC_INDEX
    path = path or SEC_TICKERS_FILE
    data = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    elif fetch:
        try:
            if not quiet:
                print(f"· SEC 티커 목록 다운로드: {SEC_TICKERS_URL}", file=sys.stderr)
            # SEC는 브라우저 UA를 403으로 막고, 연락처가 든 UA를 요구한다
            # (공정접근 정책). fetch_bytes(브라우저 UA)를 쓰지 않고 직접 받는다.
            req = urllib.request.Request(
                SEC_TICKERS_URL,
                headers={"User-Agent": "follow-trump-tracker contact@follow-trump.app",
                         "Accept-Encoding": "gzip, deflate"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
            data = json.loads(raw)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(raw)
        except Exception as e:  # noqa: BLE001
            if not quiet:
                print(f"  ! SEC 목록을 받지 못했습니다({e}).", file=sys.stderr)
                print(f"    수동 다운로드: {SEC_TICKERS_URL}", file=sys.stderr)
                print(f"    → {path} 로 저장 후 다시 실행하세요.", file=sys.stderr)
            _SEC_INDEX = {}
            return _SEC_INDEX

    index = {}
    if data:
        rows = data.values() if isinstance(data, dict) else data
        for row in rows:
            tk = (row.get("ticker") or "").strip().upper()
            title = row.get("title") or ""
            if not tk or not title:
                continue
            key = _normalize_company(title)
            if not key:
                continue
            # 같은 정규화명이 여럿이면(예: Alphabet GOOGL/GOOG) 짧은 티커를
            # 우선한다. 대개 대표 클래스(보통주)에 가깝다.
            if key not in index or len(tk) < len(index[key]):
                index[key] = tk
    _SEC_INDEX = index
    if not quiet:
        print(f"· SEC 티커 인덱스 {len(index):,}개 회사", file=sys.stderr)
    return index


def _find_companies(tokens):
    """토큰 열에서 SEC 회사명과 일치하는 구간을 모두 찾는다.

    OCR이 앞뒤에 잡음을 붙이므로("Donald J Trump AMGEN INC ar") 회사명이
    맨 앞에 있으리라 가정하지 않고, 가장 긴 것부터 훑는다.
    반환: [(티커, 시작index, 토큰수), ...]
    """
    spans = []
    i, n = 0, len(tokens)
    while i < n:
        hit = None
        for k in range(min(6, n - i), 0, -1):
            tk = _SEC_INDEX.get(" ".join(tokens[i:i + k]))
            if tk:
                hit = (tk, k)
                break
        if hit:
            spans.append((hit[0], i, hit[1]))
            i += hit[1]
        else:
            i += 1
    return spans


def _resolve_sec_ticker(asset_name: str):
    """SEC 목록으로 티커를 해석하되, OCR 잡음·행 오염에 견디게 한다.

    두 가지 관문을 둔다(정직성 우선 — 틀린 데이터를 내느니 버린다):
      · 서로 다른 회사가 둘 이상이면 한 줄에 두 종목이 섞인 것 → 버린다.
      · 회사명이 행의 '의미 있는 글자'의 절반 이상을 차지해야 한다.
        그러지 않으면 OCR 잡음 속에서 짧은 티커가 우연히 걸린 것이다
        (실측: "us ee eranoconwormon dee…"에서 ADM, 잡음의 "aes"에서 AES).
    """
    toks = _normalize_company(asset_name).split()
    spans = _find_companies(toks)
    distinct = {t for t, _, _ in spans}
    if len(distinct) != 1:
        return None
    matched_idx = {j for _, s, k in spans for j in range(s, s + k)}
    matched_chars = sum(len(toks[j]) for j in matched_idx)
    leftover_chars = sum(
        len(t) for j, t in enumerate(toks)
        if j not in matched_idx and not t.isdigit()
        and len(t) >= 2 and t not in _NAME_NOISE)
    if matched_chars >= leftover_chars:
        return next(iter(distinct))
    return None


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
    # SEC 공식 목록으로 회사명 → 티커 해석 (인덱스가 로드된 경우에만)
    if _SEC_INDEX:
        return _resolve_sec_ticker(asset_name)
    # 마지막 수단: 이름 안에 티커가 그대로 박혀 있는 경우(일부 278-T 형식).
    # 단 SEC 인덱스가 로드됐다면, 못 찾은 이름을 대문자 조각으로 '추측'하지
    # 않는다 — 그러면 ADOBE→"ADOBE"처럼 가짜 티커가 생겨 잘못된 데이터가 된다.
    if not _SEC_INDEX:
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


# 278-T 금액 구간은 정해진 값만 쓴다. OCR이 끝자리를 틀리면($15,003) 가장
# 가까운 표준 구간으로 스냅해 교정한다. (하한, 상한) 쌍.
AMOUNT_BRACKETS = [
    (1, 1000), (1001, 15000), (15001, 50000), (50001, 100000),
    (100001, 250000), (250001, 500000), (500001, 1000000),
    (1000001, 5000000), (5000001, 25000000),
    (25000001, 50000000), (50000001, 100000000),
]


def snap_amount_bracket(lo, hi):
    """OCR로 흐트러진 (하한,상한)을 표준 278-T 구간으로 스냅한다.

    각 표준 구간과의 상대오차 합이 가장 작은 것을 고른다. 오차가 너무 크면
    (표준에 없는 값) 원래 값을 그대로 둔다.
    """
    best, berr = None, None
    for blo, bhi in AMOUNT_BRACKETS:
        err = abs(lo - blo) / blo + abs(hi - bhi) / bhi
        if berr is None or err < berr:
            best, berr = (blo, bhi), err
    # 상대오차 합 0.15 이내일 때만 스냅(끝자리 몇 개 오차 수준)
    if best and berr is not None and berr <= 0.15:
        return [best[0], best[1]]
    return [lo, hi]


def parse_amount(text: str):
    for m in AMOUNT_RE.finditer(text):
        lo, hi = _norm_amount(m.group(1)), _norm_amount(m.group(2))
        if lo is not None and hi is not None and 0 < lo < hi:
            return snap_amount_bracket(lo, hi)
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
    """문서 헤더의 'OGE RECEIVED: M/D/YYYY' → 공시일.

    OCR이 숫자를 틀리면(실측: 2026 → "2626") 이 값이 전체 파싱을 망가뜨린다.
    공시일은 거래일의 상한선으로 쓰이기 때문에, 말이 안 되는 연도가 들어오면
    아예 None을 돌려 '상한선 없음(오늘 기준)'으로 떨어지게 한다.
    """
    m = RECEIVED_RE.search(text)
    if not m:
        return None
    mm, dd, yy = m.groups()
    y = int(yy)
    if y < 100:
        y += 2000
    try:
        d = datetime(y, int(mm), int(dd))
    except ValueError:
        return None
    # 278-T 전자공시는 2012년 이후. 미래 날짜도 있을 수 없다(접수 여유 며칠만 허용).
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if d.year < 2012 or d > now + timedelta(days=7):
        return None
    return d.strftime("%Y-%m-%d")


def pick_transaction_date(line: str, disclosure_date=None):
    """행에서 '거래일'을 고른다.

    채권 행에는 만기일(2040년, 2065년 등)이 함께 나오기 때문에, 단순히
    첫 날짜를 쓰면 만기일을 거래일로 오인한다(실측에서 확인).
    거래일은 공시일보다 뒤일 수 없고 지나치게 과거일 수도 없다.
    """
    limit = disclosure_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    earliest = (datetime.strptime(limit, "%Y-%m-%d") - timedelta(days=730)).strftime("%Y-%m-%d")
    best = None
    for m in DATE_RE.finditer(line):
        mm, dd, yy = m.groups()
        y = int(yy)
        if y < 100:
            y += 2000
        try:
            d = datetime(y, int(mm), int(dd)).strftime("%Y-%m-%d")
        except ValueError:
            continue
        if earliest <= d <= limit and (best is None or d > best):
            best = d
    return best


def _line_has(line):
    """한 줄이 가진 신호: (금액, 날짜있음, 거래유형)"""
    amount = parse_amount(line)
    has_date = bool(DATE_RE.search(line))
    action = None
    for pat, kind in TYPE_PATTERNS:
        if pat.search(line):
            action = kind
            break
    return amount, has_date, action


def parse_ptr_text(text: str, disclosure_date=None, window=8):
    """278-T 텍스트에서 거래 행을 추출한다.

    핵심 착안: 278-T 표의 모든 거래 행은 **금액 구간으로 끝난다**
    ($100,001 - $250,000 꼴). 그래서 금액이 나타나는 줄을 '행의 끝'으로 보고,
    직전까지 쌓인 줄에서 종목명·날짜·거래유형을 거둬들인다.

    실측(트럼프 2026 Q1·Q2 스캔본)에서 확인한 두 가지:
      · 종목명·날짜·금액이 서로 다른 줄에 흩어진다(스캔 OCR).
      · 거래유형(purchase/sale) 칸이 OCR에서 자주 통째로 사라진다.
        수백 건이 유형 없이 이름+날짜+금액만 남는다. 그래서 유형을
        '필수'로 두면 대부분을 놓친다 — 유형은 선택으로 두고, 없으면
        매수로 추정하되 actionInferred 플래그를 남겨 정직하게 표시한다.
    """
    if disclosure_date is None:
        disclosure_date = parse_received_date(text)

    rows = []
    buf = []          # 아직 금액을 만나지 못한, 쌓이는 줄들

    # 헤더·안내문·페이지 표시는 어떤 거래 행에도 속하지 않는다. 만나면 버퍼를 비운다.
    # OCR이 단어를 뭉개므로(RECEIVED→RECELVED) 앞부분만 느슨하게 잡는다.
    boundary_re = re.compile(
        r"(OGE\s+REC|OGE\s+Form|Filer.{0,3}s\s+Name|Periodic\s+Trans"
        r"|Received\s+Over|Do\s+not\s+include|This\s+is\s+a?\s*publ"
        r"|Page\s?\d|Paged\d|of\s?\d\d|Note\s?[:\.]"
        # 매 쪽 반복되는 머리글/열이름. 종목명에 섞이면 티커 해석을 망친다.
        r"|Donald\s?J|D\.?\s?Trump|Transactions?\b|Notification"
        r"|Description|Recelved)", re.I)

    def flush(amount):
        joined = " ".join(buf)
        # 거래일: 버퍼 전체에서 공시일 이전의 가장 그럴듯한 날짜(없으면 None)
        txn = pick_transaction_date(joined, disclosure_date)
        # 거래유형: 감지되면 쓰고, 없으면 매수로 추정(플래그 남김)
        action, inferred = None, False
        for pat, kind in TYPE_PATTERNS:
            if pat.search(joined):
                action = kind
                break
        if action is None:
            action, inferred = "buy", True

        # 종목명 정리: 금액·날짜·유형어·표 부호·안내 토큰을 걷어낸다
        name = AMOUNT_RE.sub(" ", joined)
        name = DATE_RE.sub(" ", name)
        name = re.sub(r"^\s*\d{1,3}\s+", "", name)          # 앞 행번호
        # 주의: 쿠폰금리(7.1000%)·만기 같은 숫자는 지우지 않는다. 채권 판별
        # (BOND_RE)이 이 표식으로 채권을 걸러내기 때문. 지우면 채권이 주식으로
        # 새어 들어온다(실측: ALLY FINL PERP NT 7.1000% 가 주식으로 분류됨).
        name = " ".join(tok for tok in name.split()
                        if not any(p.search(tok) for p, _ in TYPE_PATTERNS))
        name = re.sub(r"\b(VOS|VES|YES|NO|Yos|Yes)\b", " ", name)
        # 슬래시가 뭉개진 날짜 잔해(412712026 등) 제거. 5자리 이상 연속 숫자만
        # 지우므로 쿠폰금리(7.1000)·만기연도(2049)는 건드리지 않는다.
        name = re.sub(r"\b\d{5,}\b", " ", name)
        name = re.sub(r"[|_}{~•·\[\]!]+", " ", name)
        name = re.sub(r"\s{2,}", " ", name).strip(" .-—|·•,")

        # 이름에 알파벳 대문자가 최소 두 글자는 있어야 종목으로 본다(잡음 배제)
        if len(re.findall(r"[A-Z]", name)) < 2 or len(name) < 3:
            return False
        rows.append({
            "asset": name,
            "action": action,
            "actionInferred": inferred,
            "transactionDate": txn,
            "disclosureDate": disclosure_date,
            "amountRange": amount,
        })
        return True

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if boundary_re.search(line):
            buf.clear()
            continue
        buf.append(line)
        buf[:] = buf[-window:]
        amount = parse_amount(line)      # 금액이 있으면 이 줄이 행의 끝
        if amount:
            flush(amount)
            buf.clear()

    return rows


# 공시 PDF의 절반가량은 종이 출력물을 스캔한 이미지라 텍스트 레이어가 없다
# (실측: 32MB·34쪽인데 추출 텍스트 33자). 그런 문서는 직접 OCR해야 한다.
MIN_CHARS_PER_PAGE = 200
OCR_CACHE_DIR = os.environ.get("FT_OCR_CACHE", ".cache/ocr")


def _text_layer(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = [(p.extract_text() or "") for p in reader.pages]
    return "\n".join(pages), len(pages)


def has_text_layer(text: str, pages: int) -> bool:
    return pages > 0 and (len(text) / pages) >= MIN_CHARS_PER_PAGE


# 기본값. --ocr-tune 으로 실측해 고른 값을 여기에 반영한다.
OCR_DPI = int(os.environ.get("FT_OCR_DPI", "300"))
OCR_PSM = os.environ.get("FT_OCR_PSM", "4")
OCR_PREP = os.environ.get("FT_OCR_PREP", "sharp")


def _ocr_cache_path(data: bytes, dpi: int, psm="6", prep="none") -> str:
    """OCR 설정이 다르면 결과도 다르므로 캐시 키에 넣는다.

    기본 설정(psm 6 / 전처리 없음)은 예전 파일명을 그대로 써서, 이미 몇 시간
    걸려 만들어 둔 캐시를 버리지 않는다.
    """
    key = hashlib.sha256(data).hexdigest()[:32]
    if psm == "6" and prep == "none":
        return os.path.join(OCR_CACHE_DIR, f"{key}-{dpi}.txt")
    return os.path.join(OCR_CACHE_DIR, f"{key}-{dpi}-p{psm}-{prep}.txt")


def _render_pages(data: bytes, dpi: int, pages=None):
    """PDF를 페이지 이미지로 렌더링한다.

    PyMuPDF를 먼저 쓰되, 최신 파이썬에서 휠이 없어 설치가 안 되는 경우가 있어
    pypdfium2로도 동작하게 해 둔다. 둘 중 하나만 있으면 된다.
    """
    from PIL import Image

    try:
        import pymupdf
    except ImportError:
        pymupdf = None
    if pymupdf is not None:
        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            mat = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
            for i, page in enumerate(doc):
                if pages is not None and i not in pages:
                    continue
                pix = page.get_pixmap(matrix=mat, colorspace=pymupdf.csGRAY)
                yield Image.frombytes("L", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
        return

    try:
        import pypdfium2
    except ImportError:
        raise RuntimeError(
            "PDF 렌더러가 없습니다. 아래 중 하나를 설치하세요:\n"
            "    pip install pymupdf\n"
            "    pip install pypdfium2   (파이썬이 너무 최신이라 pymupdf가 안 깔릴 때)")

    pdf = pypdfium2.PdfDocument(data)
    try:
        for i, page in enumerate(pdf):
            if pages is not None and i not in pages:
                continue
            yield page.render(scale=dpi / 72.0, grayscale=True).to_pil()
    finally:
        pdf.close()


# 실측: 300dpi + --psm 6 으로 읽은 백악관 스캔본은 숫자가 통째로 글자가 된다.
#   "$500,001 - $1,000,000" → "ssongon-sonnsnn"
#   "$50,000 - $100,000"    → "ssenco-stconcoo"
# 금액 칸이 하나도 안 읽히니 거래를 뽑을 수가 없다. 원인은 파서가 아니라
# 해상도·전처리다. 아래 전처리들을 실제로 재보고 고르기 위한 장치.

PREP_MODES = ("none", "sharp", "binary", "binary2x", "binary3x")

# 확대 후 이미지가 지나치게 커지면 Tesseract가 사실상 멈춘다.
# 600dpi 페이지를 3배로 키우면 3억 픽셀이라 감당이 안 된다.
MAX_OCR_PIXELS_SIDE = 6000


def preprocess(img, mode: str):
    """OCR 전 이미지 보정. mode별 차이를 --ocr-tune 으로 실측해 고른다."""
    if mode == "none":
        return img
    from PIL import Image, ImageOps, ImageFilter
    g = img.convert("L")
    if mode == "sharp":
        # 대비만 펴고 살짝 선명하게. 원본 해상도 유지.
        return ImageOps.autocontrast(g).filter(ImageFilter.SHARPEN)
    if mode.startswith("binary"):
        factor = {"binary": 1, "binary2x": 2, "binary3x": 3}[mode]
        if factor > 1:
            # 작은 숫자는 픽셀이 모자라 뭉개진다. 키운 뒤 이진화한다.
            # 다만 상한을 둬서 거대한 이미지로 멈추는 일이 없게 한다.
            cap = MAX_OCR_PIXELS_SIDE / max(g.width, g.height)
            factor = max(1.0, min(float(factor), cap))
            if factor > 1.01:
                g = g.resize((int(g.width * factor), int(g.height * factor)),
                             Image.LANCZOS)
        g = ImageOps.autocontrast(g)
        thr = _otsu(g)
        return g.point(lambda v: 255 if v > thr else 0, mode="L")
    raise ValueError(f"알 수 없는 전처리: {mode}")


def _otsu(img) -> int:
    """Otsu 임계값. 스캔 품질이 문서마다 달라 고정값을 쓰면 안 된다."""
    hist = img.histogram()[:256]
    total = sum(hist) or 1
    sum_all = sum(i * h for i, h in enumerate(hist))
    w_b = 0.0
    sum_b = 0.0
    best, thr = -1.0, 128
    for i, h in enumerate(hist):
        w_b += h
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * h
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best:
            best, thr = var, i
    return thr


def count_pdf_pages(data: bytes) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001
        return 0


def ocr_pdf(data: bytes, dpi=None, lang="eng", progress=True,
            psm=None, prep=None, pages=None) -> str:
    """스캔 PDF를 페이지 이미지로 렌더링해 Tesseract로 읽는다.

    느리기 때문에(쪽당 수 초) 결과를 PDF 해시 기준으로 캐시한다.
    같은 공시를 다시 돌려도 두 번 OCR하지 않는다.
    """
    dpi = dpi or OCR_DPI
    psm = psm or OCR_PSM
    prep = prep or OCR_PREP
    cache = _ocr_cache_path(data, dpi, psm, prep) if pages is None else None
    if cache and os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            txt = f.read()
        print(f"    (OCR 캐시 사용: {len(txt):,}자)", file=sys.stderr)
        return txt

    try:
        import pytesseract
    except ImportError:
        print("  ! pytesseract 없음 — pip install -r scripts/requirements.txt", file=sys.stderr)
        return ""

    cmd = os.environ.get("TESSERACT_CMD") or (find_tesseract()[0] or "")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd

    total = count_pdf_pages(data)
    out = []
    try:
        # --oem 1: LSTM 엔진만 사용. 구형 엔진이 섞이면 숫자 오독이 늘어난다.
        cfg = f"--oem 1 --psm {psm} -c preserve_interword_spaces=1"
        for i, img in enumerate(_render_pages(data, dpi, pages), 1):
            out.append(pytesseract.image_to_string(
                preprocess(img, prep), lang=lang, config=cfg))
            if progress:
                print(f"    OCR {i}/{total or '?'}쪽", end="\r", file=sys.stderr)
    except RuntimeError as e:
        print(f"  ! {e}", file=sys.stderr)
        return ""

    text = "\n".join(out)
    if progress:
        print(f"    OCR 완료 {len(out)}쪽 → {len(text):,}자          ", file=sys.stderr)

    if cache:
        os.makedirs(OCR_CACHE_DIR, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            f.write(text)
    return text


def extract_pdf_text(data: bytes, use_ocr=True, dpi=None) -> str:
    """텍스트 레이어를 우선 쓰고, 없으면(스캔본) OCR로 넘어간다."""
    text, pages = _text_layer(data)
    if has_text_layer(text, pages):
        return text
    if not use_ocr:
        return text
    print(f"    텍스트 레이어 없음({len(text)}자/{pages}쪽) → OCR 시작", file=sys.stderr)
    return ocr_pdf(data, dpi=dpi) or text


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


PDF_CACHE_DIR = os.environ.get("FT_PDF_CACHE", ".cache/pdf")


def fetch_bytes(source: str) -> bytes:
    """받은 PDF는 디스크에 남긴다.

    공시 한 건이 32MB나 되는 게 있어서, OCR 설정을 바꿔가며 실험할 때마다
    다시 받으면 시간도 대역폭도 낭비다. 원본은 확정 문서라 변하지 않는다.
    """
    if not (source.startswith("http://") or source.startswith("https://")):
        with open(source, "rb") as f:
            return f.read()

    key = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    cached = os.path.join(PDF_CACHE_DIR, f"{key}.pdf")
    if os.path.exists(cached):
        with open(cached, "rb") as f:
            return f.read()

    req = urllib.request.Request(encode_url(source), headers=UA)
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
    os.makedirs(PDF_CACHE_DIR, exist_ok=True)
    with open(cached, "wb") as f:
        f.write(data)
    # 어느 캐시가 어느 공시인지 나중에 알아볼 수 있게 남긴다.
    with open(os.path.join(PDF_CACHE_DIR, "index.txt"), "a", encoding="utf-8") as f:
        f.write(f"{key}\t{source}\n")
    return data


# ---------------------------------------------------------------------------
# 3) 가격 보강 (Stooq, 무료·키 불필요)
# ---------------------------------------------------------------------------

# Yahoo는 쿠키 없는 요청을 429(Too Many Requests)로 막는다(실측).
# 세션 쿠키를 한 번 받아두면 통과한다.
_YAHOO_OPENER = None


def _yahoo_opener():
    global _YAHOO_OPENER
    if _YAHOO_OPENER is not None:
        return _YAHOO_OPENER
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = list(UA.items()) + [
        ("Accept", "text/html,application/json,*/*"),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]
    for seed in ("https://fc.yahoo.com/", "https://finance.yahoo.com/"):
        try:
            op.open(seed, timeout=20).read(2048)
        except Exception:  # noqa: BLE001
            pass  # 쿠키만 얻으면 되므로 실패해도 계속
    _YAHOO_OPENER = op
    return op


def _parse_yahoo_chart(body):
    doc = json.loads(body)
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        err = (doc.get("chart") or {}).get("error")
        raise ValueError(f"빈 응답{f' ({err})' if err else ''}")
    r = res[0]
    stamps = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    out = []
    for ts, c in zip(stamps, closes):
        if c is None:
            continue
        out.append((datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"), float(c)))
    if not out:
        raise ValueError("종가 없음")
    return out


def _fetch_yahoo(host):
    """Yahoo 차트 API. 쿠키 세션을 쓰고, 429면 잠시 쉬었다 다시 시도한다."""
    def fn(ticker):
        url = encode_url(f"https://{host}/v8/finance/chart/"
                         f"{urllib.parse.quote(ticker)}?range=5y&interval=1d")
        op = _yahoo_opener()
        last = None
        for attempt in range(3):
            try:
                with op.open(url, timeout=45) as r:
                    return _parse_yahoo_chart(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code} {e.reason}"
                if e.code == 429 and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(last) from None
        raise RuntimeError(last or "실패")
    return fn


def _fetch_nasdaq_daily(ticker: str):
    """Nasdaq 공개 API — 키 불필요. Yahoo가 막힐 때의 대안."""
    today = datetime.now(timezone.utc)
    frm = (today - timedelta(days=5 * 365)).strftime("%Y-%m-%d")
    url = (f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(ticker)}/historical"
           f"?assetclass=stocks&fromdate={frm}&todate={today.strftime('%Y-%m-%d')}&limit=9999")
    body = _get_text(url, timeout=45, extra_headers={
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nasdaq.com/",
        "Origin": "https://www.nasdaq.com",
    })
    return _parse_nasdaq(body)


def _parse_nasdaq(body):
    doc = json.loads(body)
    rows = (((doc.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    if not rows:
        raise ValueError(f"행 없음 ({(doc.get('status') or {}).get('bCodeMessage')})")
    out = []
    for r in rows:
        d, c = r.get("date"), (r.get("close") or "").replace("$", "").replace(",", "")
        if not d or not c:
            continue
        try:
            iso = datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")
            out.append((iso, float(c)))
        except ValueError:
            continue
    if not out:
        raise ValueError("종가 파싱 실패")
    out.sort()  # Nasdaq은 최신순으로 준다
    return out


def _fetch_stooq_daily(ticker: str):
    """Stooq CSV. 데이터센터/일부 IP에서는 JS 검증 페이지로 막힌다(실측)."""
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


PRICE_PROVIDERS = (
    ("yahoo(query1)", _fetch_yahoo("query1.finance.yahoo.com")),
    ("yahoo(query2)", _fetch_yahoo("query2.finance.yahoo.com")),
    ("nasdaq", _fetch_nasdaq_daily),
    ("stooq", _fetch_stooq_daily),
)


def fetch_stooq_daily(ticker: str):
    """[(date, close)] 오름차순. 제공처를 순서대로 시도, 전부 실패 시 []."""
    for name, fn in PRICE_PROVIDERS:
        try:
            series = fn(ticker)
            print(f"  · 시세 {ticker}: {name} OK ({len(series)}일, 최신 {series[-1][0]})",
                  file=sys.stderr)
            return series
        except Exception as e:  # noqa: BLE001
            print(f"  · 시세 {ticker}: {name} 실패 — {e}", file=sys.stderr)
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
        "id": f"{ticker.lower()}-{row.get('transactionDate') or disc}",
        "ticker": ticker,
        "companyKo": TICKER_NAME_KO.get(ticker, ticker),
        "companyEn": row["asset"][:60],
        "sector": TICKER_SECTOR.get(ticker, "기타"),
        "instrumentType": "stock",
        "action": row["action"],
        # OCR이 매수/매도 칸을 못 읽어 매수로 추정한 건은 표시해 둔다(정직성).
        "actionInferred": bool(row.get("actionInferred")),
        # 뉴스·원문으로 매수/매도를 확인한 경우의 출처(있으면).
        "verifiedSource": row.get("verifiedSource"),
        "amountRange": row["amountRange"],
        "transactionDate": row["transactionDate"],
        # 거래일이 OCR로 깨져 공시일로 대체한 경우.
        "transactionDateApprox": bool(row.get("transactionDateApprox")),
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
        "note": "OGE 278-T 공시 자동 파싱(스캔본 OCR). 촉매/매도 정보는 수동 보완 권장.",
    }


VERIFIED_TYPES_FILE = os.environ.get("FT_VERIFIED", "scripts/verified_types.json")


def load_verified_types(path=None):
    """뉴스·원문으로 확인한 매수/매도 정보를 읽는다.

    형식(둘 다 허용):
      "TICKER|YYYY-MM-DD": {"action":"buy"|"sell", "source":"URL 또는 메모"}
      "TICKER":            {"action":"buy"|"sell", "source":"..."}   # 날짜 무관
    날짜가 붙은 키가 우선한다. 이 파일은 사장님이 채우는 '검증 레이어'이며,
    OCR이 읽지 못한 거래유형을 사실로 덮어써 '추정' 딱지를 없앤다.
    """
    path = path or VERIFIED_TYPES_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("action")}


def apply_verified_type(row, verified):
    """검증 파일에 있으면 거래유형을 확정하고 출처를 남긴다."""
    if not verified:
        return
    tk = row.get("ticker")
    hit = verified.get(f"{tk}|{row.get('transactionDate')}") or verified.get(tk)
    if not hit:
        return
    act = str(hit.get("action", "")).lower()
    if act in ("buy", "sell", "exchange"):
        row["action"] = act
        row["actionInferred"] = False
        if hit.get("source"):
            row["verifiedSource"] = hit["source"]


def build_from_ptrs(sources, enrich=True, use_ocr=True, dpi=300):
    load_ticker_index()          # 회사명 → 티커 해석 준비(SEC 목록)
    verified = load_verified_types()   # 뉴스·원문 검증 레이어
    raw_rows = []
    n_ocr = n_text = n_empty = 0
    for src in sources:
        print(f"· PTR 로드: {src.rsplit('/', 1)[-1]}", file=sys.stderr)
        data = fetch_bytes(src)
        layer, pages = _text_layer(data)
        had_text = has_text_layer(layer, pages)
        text = extract_pdf_text(data, use_ocr=use_ocr, dpi=dpi)
        used_ocr = (not had_text) and len(text) > len(layer)
        if used_ocr:
            n_ocr += 1
        elif had_text:
            n_text += 1
        else:
            n_empty += 1
        parsed = parse_ptr_text(text)
        how = "OCR" if used_ocr else ("텍스트" if had_text else "판독실패")
        print(f"  → [{how}] {pages}쪽 / 텍스트 {len(text):,}자 / 거래행 {len(parsed)}건",
              file=sys.stderr)
        raw_rows.extend(parsed)
    print(f"· 문서 {len(sources)}건 — 텍스트 {n_text} / OCR {n_ocr} / 판독실패 {n_empty}",
          file=sys.stderr)

    # 개별 종목만 남기고 티커 부여
    kept = []
    dropped = 0
    seen = set()
    for r in raw_rows:
        if not is_individual_stock(r["asset"]):
            dropped += 1
            continue
        tk = extract_ticker(r["asset"])
        if not tk:
            dropped += 1
            continue
        # 거래일이 OCR로 깨져 없으면 공시일로 대체하고 근사임을 표시한다.
        if not r.get("transactionDate"):
            r["transactionDate"] = r["disclosureDate"]
            r["transactionDateApprox"] = True
        r["ticker"] = tk
        # 뉴스·원문으로 확인한 매수/매도가 있으면 그 값을 우선한다(출처 기록).
        apply_verified_type(r, verified)
        # 중복 제거: 같은 종목·거래일·유형·금액은 한 건으로.
        key = (tk, r["transactionDate"], r["action"], tuple(r["amountRange"]))
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(r)
    n_ver = sum(1 for r in kept if r.get("verifiedSource"))
    print(f"· 필터: 개별종목 {len(kept)}건 / 제외·중복 {dropped}건"
          f" (뉴스·원문 검증 {n_ver}건)", file=sys.stderr)

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

    records.sort(key=lambda x: x["disclosureDate"] or "", reverse=True)
    return records, {"kept": len(kept), "dropped": dropped, "raw": len(raw_rows),
                     "docs": len(sources), "ocr": n_ocr, "text": n_text, "unreadable": n_empty}


def write_data_json(records, stats, out_path, parsed_ids=None):
    """data.json 저장.

    주의: 예전에는 meta를 통째로 새로 써서, 자동 파싱이 0건이어도
    '자동 파싱 결과'라고 표기해 사실과 어긋났다. 이제는 기존 meta를 보존하고
    이번 실행에서 확인된 사실만 갱신한다.
    """
    doc = {}
    try:
        with open(out_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (FileNotFoundError, ValueError):
        pass

    meta = doc.get("meta") or {}
    meta.setdefault("subject", "Donald J. Trump")
    meta.setdefault("subjectKo", "도널드 트럼프")
    meta.setdefault("caveats", [
        "백악관은 트럼프의 보유 자산이 블라인드 트러스트로 관리되며 본인은 개별 종목을 모른다고 주장합니다.",
        "공시 원본에는 분기당 수천 건의 거래가 있고 대부분 ETF·머니마켓·채권입니다. 이 사이트는 그 노이즈를 걷어내고 개별 종목만 보여줍니다.",
        "공시는 거래 후 최대 45일(실제로는 그 이상 지연되기도 함) 뒤에 공개됩니다.",
    ])

    n_parsed = len(parsed_ids or [])
    n_manual = len(records) - n_parsed

    # 자동 추출(OCR) 데이터에는 그에 맞는 한계를 정직하게 명시한다.
    if n_parsed:
        n_inferred = sum(1 for r in records if r.get("actionInferred"))
        n_approx = sum(1 for r in records if r.get("transactionDateApprox"))
        meta["caveats"] = [
            "백악관은 트럼프의 보유 자산이 블라인드 트러스트로 관리되며 본인은 개별 종목을 모른다고 주장합니다.",
            "공시 원본에는 분기당 수천 건의 거래가 있고 대부분 지방채·회사채·ETF입니다. 이 사이트는 그 노이즈를 걷어내고 개별 주식만 보여줍니다.",
            "공시는 거래 후 최대 45일(실제로는 그 이상 지연되기도 함) 뒤에 공개됩니다.",
            "원본 상당수가 저해상도 스캔본이라 OCR로 읽었습니다. 회사명은 SEC 공식 목록과 정확히 일치할 때만 채택하고(불확실하면 버림), 금액은 278-T 표준 구간으로 보정했습니다. 그래도 누락·오차가 있을 수 있습니다.",
            f"매수/매도 대부분은 공시 원문에서 직접 읽었습니다. 원문에서 그 칸이 훼손된 {n_inferred}건은, 해당 분기 공시가 언론 보도상 '수백 종목 대량 매수'였다는 사실에 근거해 매수로 표기했습니다(개별 확인이 필요한 건은 검증 후 출처를 답니다).",
            f"거래일이 스캔에서 훼손된 {n_approx}건은 공시일로 대체(근사)했습니다.",
        ]
    meta["parseStats"] = {
        "documents": stats.get("docs"),
        "readByText": stats.get("text"),
        "readByOcr": stats.get("ocr"),
        "unreadable": stats.get("unreadable"),
        "rawRows": stats.get("raw"),
        "individualStocks": stats.get("kept"),
        "filteredOut": stats.get("dropped"),
        "ranAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    # 표시 중인 거래가 어디서 왔는지 정확히 적는다.
    if n_parsed and n_manual:
        meta["dataSource"] = "oge-278t+manual"
        meta["note"] = (f"공시 {stats.get('docs')}건에서 원시 {stats.get('raw')}건을 읽어 "
                        f"개별 종목 {n_parsed}건을 자동 추출했고, 수동 입력 {n_manual}건을 함께 표시합니다. "
                        f"가격은 실제 시세입니다.")
    elif n_parsed:
        meta["dataSource"] = "oge-278t"
        meta["note"] = (f"공시 {stats.get('docs')}건에서 원시 {stats.get('raw')}건을 읽어 "
                        f"개별 종목 {n_parsed}건을 자동 추출했습니다(ETF·채권 등 "
                        f"{stats.get('dropped')}건 제외). 가격은 실제 시세입니다.")
    else:
        # 자동 추출이 0건이면 '자동 파싱 결과'라고 쓰면 안 된다.
        meta["dataSource"] = "manual-trades+live-prices"
        meta["note"] = (f"이번 자동 파싱에서는 개별 종목을 찾지 못했습니다"
                        f"(공시 {stats.get('docs')}건, 원시 {stats.get('raw')}건, "
                        f"OCR {stats.get('ocr')}건, 판독실패 {stats.get('unreadable')}건). "
                        f"현재 표시된 {n_manual}건은 공시·보도를 근거로 손으로 입력한 것이며, "
                        f"가격은 실제 시세입니다.")

    doc["meta"] = meta
    doc["trades"] = records
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"✓ {out_path} — 표시 {len(records)}건 (자동 {n_parsed} / 수동·유지 {n_manual})",
          file=sys.stderr)


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
        meta["lastUpdated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        meta["pricesRefreshedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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


def _get_text(url, timeout=60, extra_headers=None):
    headers = dict(UA)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(encode_url(url), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 상태 코드와 본문 앞부분을 남겨야 원인(차단/한도/경로오류)을 구분할 수 있다.
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {e.code} {e.reason}" + (f" | {body}" if body else "")) from None


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
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
30 Modorna Inc MRNA ourchoso 12/2/2025 VOS $15,001 • $50,000
31 Comcast Corp CMCSA lourchoso 12/10/2025 VOS $1,001 • $15,000
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

    # SEC 목록 기반 티커 해석(잡음·오염 견디기). 임시 인덱스로 검증 후 복원.
    global _SEC_INDEX
    _saved_idx = _SEC_INDEX
    try:
        _mini = {"Microsoft Corp": "MSFT", "Amgen Inc": "AMGN",
                 "Bank of America Corp": "BAC", "Goldman Sachs Group Inc": "GS",
                 "Republic Services Inc": "RSG", "Chipotle Mexican Grill Inc": "CMG",
                 "Deere & Co": "DE", "Packaging Corp of America": "PKG"}
        _SEC_INDEX = {" ".join(_normalize_company(k).split()): v
                      for k, v in _mini.items()}
        check("SEC 해석: 깨끗한 이름", extract_ticker("MICROSOFT CORP") == "MSFT")
        check("SEC 해석: 뒤 잡음 무시", extract_ticker("AMGEN INC ar") == "AMGN")
        check("SEC 해석: 앞 잡음 무시(중간의 회사명)",
              extract_ticker("a a wlio ton Goldman Sachs Group Inc No") == "GS")
        check("SEC 해석: 잡음이 회사명보다 많으면 버림(짧은 티커 오탐 방지)",
              extract_ticker("us ee us eranoconwormon dee armel amgen rl") is None)
        check("SEC 해석: 두 회사 섞인 행은 버림",
              extract_ticker("REPUBLIC SERVICES INC 71 CHIPOTLE MEXICAN GRILL INC")
              is None)
        check("SEC 해석: 미등록 회사는 버림(추측 안 함)",
              extract_ticker("Space Expl Technologies Corp") is None)
    finally:
        _SEC_INDEX = _saved_idx

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
        check("개별 종목 거래일", mrna[0]["transactionDate"] == "2025-12-02")

    # OCR이 연도를 틀리면(2026→2626) 공시일이 거래일 상한선으로 쓰여 전체가 0건이 된다
    check("말도 안 되는 연도의 공시일은 무시",
          parse_received_date("OGE RECEIVED 5/12/2626") is None)
    check("2012년 이전 공시일은 무시",
          parse_received_date("OGE RECEIVED 5/12/1998") is None)
    check("정상 공시일은 그대로",
          parse_received_date("OGE RECEIVED 5/12/2026") == "2026-05-12")
    check("공시일이 깨져도 거래는 파싱된다",
          len(parse_ptr_text(
              "OGE RECEIVED 5/12/2626\n"
              "1 MODERNA INC MRNA purchase 3/02/2026 $15,001 - $50,000\n")) == 1)

    # OCR이 표의 한 행을 여러 줄로 쪼갠 경우 (우리가 직접 OCR한 스캔본의 실제 모습).
    # 특히 종목명은 행의 첫 줄에 오는데, 예전 파서는 버퍼가 비어 있으면 그 줄을
    # 버려서 이름이 통째로 사라졌다("purchase"의 잔해인 "ase Yes"만 남았다).
    SPLIT = """OGE Form 278-T (Periodic Transaction Report)
OGE RECEIVED:  5/12/2026
Filer's Name: Donald J. Trump

2  MODERNA INC
   MRNA
   purchase
   3/02/2026   Yes
   $15,001 - $50,000

6  PENNSYLVANIA ST TURNPIKE COMMN
   5.25% DUE 11/01/40
   purchase
   3/20/2026   Yes
   $500,001 - $1,000,000

11 COINBASE GLOBAL INC
   COIN  Class A
   salo
   4/07/2026   Yes
   $1,001 - $15,000
"""
    srows = parse_ptr_text(SPLIT)
    check(f"쪼개진 행 3건 파싱 (실제 {len(srows)})", len(srows) == 3)
    sstk = [r for r in srows if is_individual_stock(r["asset"])]
    check(f"그중 개별종목 2건 (실제 {len(sstk)})", len(sstk) == 2)
    check("쪼개진 행에서 종목명 보존",
          bool(sstk) and "MODERNA" in sstk[0]["asset"])
    check("종목명에 'ase' 같은 거래유형 잔해 없음",
          all("ase" not in r["asset"].split() for r in srows))
    check("쪼개진 행 티커 MRNA",
          bool(sstk) and extract_ticker(sstk[0]["asset"]) == "MRNA")
    check("쪼개진 행 거래일(헤더 접수일 아님)",
          bool(sstk) and sstk[0]["transactionDate"] == "2026-03-02")
    check("쪼개진 행 매도 인식",
          len(sstk) > 1 and sstk[1]["action"] == "sell")
    check("쪼개진 행 중 채권은 제외",
          all("PENNSYLVANIA" not in r["asset"] for r in sstk))

    # 뉴스·원문 검증 레이어: OCR 추정을 사실로 덮어쓰고 출처를 남긴다
    _vrow = {"ticker": "ADBE", "transactionDate": "2026-04-17", "action": "buy",
             "actionInferred": True, "amountRange": [1001, 15000],
             "disclosureDate": "2026-05-14"}
    apply_verified_type(_vrow, {"ADBE|2026-04-17": {"action": "sell", "source": "뉴스X"}})
    check("검증 파일이 거래유형을 덮어쓴다", _vrow["action"] == "sell")
    check("검증되면 추정 플래그 해제", _vrow.get("actionInferred") is False)
    check("검증 출처를 남긴다", _vrow.get("verifiedSource") == "뉴스X")
    _vrow2 = {"ticker": "ZZZZ", "transactionDate": "2026-01-01", "action": "buy",
              "actionInferred": True}
    apply_verified_type(_vrow2, {"ADBE": {"action": "sell", "source": "x"}})
    check("검증에 없는 종목은 그대로", _vrow2["action"] == "buy" and _vrow2["actionInferred"] is True)

    # 실측: 2026 Q2 스캔본(199dpi, psm4+sharp). 표의 거래유형(purchase) 칸이
    # OCR에서 대부분 사라지고, 종목명·날짜·금액이 여러 줄에 흩어진다.
    # 유형 없어도 금액을 기준으로 행을 잡아내야 한다(수백 건이 이 형태).
    Q2 = """OGE Form 278-T (Updated February 2024)
Note: This is a public form. Do not include account numbers.
| Paged2of44 | 02 of 44
ADOBE INC                                             4/17/2026
Yes} $1,000,001 - $5,000,000
AGILENT TECHNOLOGIES INC              4/17/2026    Yes |$100,001 - $250,000
ALLY FINL INC PERP -D NT 7.1000% 12/31/49    412712026   Yos| $1,001 - $15,000
BERKSHIRE HATHAWAY INC-CL B           4/17/2026
Yes! $1,000,001 - $5,000,000
"""
    q2 = parse_ptr_text(Q2, disclosure_date="2026-05-14")
    q2_stk = [r for r in q2 if is_individual_stock(r["asset"])]
    check(f"유형칸 없는 스캔본에서 행 추출 (실제 {len(q2)})", len(q2) == 4)
    check("금액만으로 종목 인식 (ADOBE/AGILENT/BERKSHIRE)",
          any("ADOBE" in r["asset"] for r in q2_stk)
          and any("BERKSHIRE" in r["asset"] for r in q2_stk))
    check("유형 없으면 매수로 추정하고 플래그를 남긴다",
          all(r.get("actionInferred") for r in q2_stk if "ADOBE" in r["asset"]))
    check("유형 있으면 명시로 두고 추정 안 함",
          not any(r.get("actionInferred") for r in srows if r["action"] == "sell"))
    check("쿠폰 붙은 채권(PERP NT 7.1000%)은 여기서도 제외",
          not any("ALLY" in r["asset"] for r in q2_stk))
    check("종목명에 뭉개진 날짜숫자(412712026) 잔해 없음",
          all(not re.search(r"\\d{5,}", r["asset"]) for r in q2))

    # 회사채는 회사 이름이 붙어 있어도 주식이 아니다(실측에서 대량 오검출)
    check("회사채(NTS) 제외", not is_individual_stock("PAYPAL HOLDINGS INC NTS 02.850% 100129"))
    check("회사채(DUE) 제외", not is_individual_stock("MACYS RETAIL HOLDINGS LLC REGS DUE '30 05.875"))
    check("지방채(GO BDS) 제외", not is_individual_stock("NEW YORK NY GO BOS FISCAL 5.0000%"))

    # 만기일을 거래일로 오인하면 안 된다(2040년 등 미래 날짜가 나왔던 실측 버그)
    mat = pick_transaction_date(
        "NORTH EASTTEX REGLS% DUE 01/01/2040 ourchoso 12/9/2025", "2026-01-14")
    check("만기일 대신 거래일 선택", mat == "2025-12-09")
    check("공시일 이후 날짜는 거래일 아님",
          pick_transaction_date("x 05/01/2065 y", "2026-01-14") is None)

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

    # meta 정직성: 자동 추출이 0건인데 '자동 파싱 결과'라고 쓰면 안 된다(실제 발생했던 문제)
    # tempfile은 상단에서 import
    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "d.json")
        manual = [{"id": "m1", "ticker": "AAA", "disclosureDate": "2026-01-01"}]
        st = {"docs": 16, "raw": 280, "kept": 0, "dropped": 280,
              "ocr": 6, "text": 10, "unreadable": 0}
        write_data_json(manual, st, fp, parsed_ids=set())
        got = json.load(open(fp, encoding="utf-8"))
        check("자동 0건이면 dataSource가 자동파싱이 아님",
              got["meta"]["dataSource"] == "manual-trades+live-prices")
        check("자동 0건이면 note에 '찾지 못했습니다' 명시",
              "찾지 못했" in got["meta"]["note"])
        check("OCR 통계 기록", got["meta"]["parseStats"]["readByOcr"] == 6)

        # 기존 meta의 수동 정정(caveats)이 덮이지 않아야 한다
        got["meta"]["caveats"] = ["손으로 쓴 주의문구"]
        json.dump(got, open(fp, "w", encoding="utf-8"), ensure_ascii=False)
        write_data_json(manual, st, fp, parsed_ids=set())
        got2 = json.load(open(fp, encoding="utf-8"))
        check("기존 caveats 보존", got2["meta"]["caveats"] == ["손으로 쓴 주의문구"])

        # 자동 추출이 있으면 그 사실을 반영
        parsed = [{"id": "p1", "ticker": "BBB", "disclosureDate": "2026-02-01"}]
        st2 = dict(st, kept=1)
        write_data_json(parsed, st2, fp, parsed_ids={"p1"})
        got3 = json.load(open(fp, encoding="utf-8"))
        check("자동 추출 시 dataSource=oge-278t", got3["meta"]["dataSource"] == "oge-278t")

    # 시세 응답 파서 (네트워크 없이 합성 응답으로 검증)
    yser = _parse_yahoo_chart(json.dumps({"chart": {"result": [{
        "timestamp": [1767225600, 1767312000],
        "indicators": {"quote": [{"close": [26.4, 174.38]}]}}]}}))
    check(f"Yahoo 응답 파싱 2일 (실제 {len(yser)})", len(yser) == 2)
    check("Yahoo 종가", yser[-1][1] == 174.38)
    try:
        _parse_yahoo_chart(json.dumps({"chart": {"result": []}}))
        check("Yahoo 빈 응답은 오류로 처리", False)
    except ValueError:
        check("Yahoo 빈 응답은 오류로 처리", True)

    nas = _parse_nasdaq(json.dumps({"data": {"tradesTable": {"rows": [
        {"date": "08/19/2026", "close": "$174.38"},
        {"date": "05/12/2026", "close": "$26.40"},
    ]}}}))
    check(f"Nasdaq 응답 파싱 2일 (실제 {len(nas)})", len(nas) == 2)
    check("Nasdaq 오름차순 정렬", nas[0][0] == "2026-05-12" and nas[-1][0] == "2026-08-19")
    check("Nasdaq 달러기호 제거", nas[-1][1] == 174.38)

    # OCR 배관 검증 — 텍스트 레이어가 없는 PDF를 만들어 실제로 읽어본다.
    have_render = False
    for _m in ("pymupdf", "pypdfium2"):
        try:
            __import__(_m)
            have_render = True
            break
        except ImportError:
            pass
    try:
        import pytesseract  # noqa: F401
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        have_ocr = have_render and (find_tesseract()[0] is not None)
    except ImportError:
        have_ocr = False

    if not have_ocr:
        print("  · OCR 의존성 없음 — OCR 시험 건너뜀 (로컬에서 확인 필요)")
    else:
        from PIL import Image, ImageDraw, ImageFont
        lines = [
            "OGE RECEIVED:  5/12/2026",
            "2  MODERNA INC MRNA           purchase  3/02/2026  Yes  $15,001 - $50,000",
            "6  PENNSYLVANIA ST 5.25% DUE 11/01/39  purchase  3/20/2026  Yes  $500,001 - $1,000,000",
        ]
        img = Image.new("L", (2000, 300), 255)
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 30)
        except Exception:  # noqa: BLE001
            font = ImageFont.load_default()
        for i, ln in enumerate(lines):
            d.text((40, 40 + i * 60), ln, fill=20, font=font)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PDF")   # 텍스트 레이어 없는 PDF
        pdf = buf.getvalue()

        raw, pages = _text_layer(pdf)
        check("스캔본은 텍스트 레이어 없음으로 판정", not has_text_layer(raw, pages))
        # 시험용 OCR 결과가 실제 캐시에 섞이면 안 된다. 실측에서 이 픽스처가
        # .cache/ocr 에 쌓여 --dump-ocr 진단을 오염시켰다(가짜 MRNA 3건).
        global OCR_CACHE_DIR
        _real_cache = OCR_CACHE_DIR
        with tempfile.TemporaryDirectory() as _tmp:
            OCR_CACHE_DIR = _tmp
            try:
                text = extract_pdf_text(pdf)
            finally:
                OCR_CACHE_DIR = _real_cache
        check("OCR로 텍스트 복원", len(text) > 50)
        check("OCR 텍스트에서 공시일 추출", parse_received_date(text) == "2026-05-12")
        orows = parse_ptr_text(text)
        ostocks = [r for r in orows
                   if is_individual_stock(r["asset"]) and extract_ticker(r["asset"])]
        check(f"OCR 후 개별종목 1건 (실제 {len(ostocks)})", len(ostocks) == 1)
        if ostocks:
            check("OCR 티커 MRNA", extract_ticker(ostocks[0]["asset"]) == "MRNA")
            check("OCR 거래일", ostocks[0]["transactionDate"] == "2026-03-02")

    print("\n" + ("전체 통과 ✅" if ok else "실패 있음 ❌"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 전체 진단 (--diagnose) — 한 번에 모든 문서를 훑어 어디가 막혔는지 보여준다
# ---------------------------------------------------------------------------

def list_sources(sources_path="scripts/sources.json"):
    """공시를 받지 않고 목록과 날짜만 즉시 보여준다.

    '지금 우리가 몇 월 공시부터 보고 있나'에 대한 답. 파일명 날짜만 파싱하므로
    네트워크는 목록 페이지 한 번만 두드리고 PDF는 하나도 받지 않는다.
    """
    cfg = load_sources(sources_path)
    urls = list(cfg.get("ptrUrls") or [])
    try:
        for u in discover_ptr_urls(cfg):
            if u not in urls:
                urls.append(u)
    except Exception as e:  # noqa: BLE001
        print(f"  ! 자동 탐색 오류: {e}", file=sys.stderr)

    if not urls:
        print("공시를 찾지 못했습니다.", file=sys.stderr)
        return 2

    rows = []
    for u in urls:
        name = urllib.parse.unquote(u.rsplit("/", 1)[-1])
        rows.append((_date_from_name(name), name, urllib.parse.urlsplit(u).netloc))
    rows.sort(key=lambda r: r[0] or "0000")

    print("=" * 70)
    print(f" 현재 잡히는 공시 {len(rows)}건 (다운로드 없이 목록만)")
    print("=" * 70)
    for d, name, host in rows:
        print(f"  {d or '날짜?':<12}  {name[:52]}")

    dated = [d for d, _, _ in rows if d]
    print("-" * 70)
    if dated:
        print(f" 기간: {min(dated)} ~ {max(dated)}  (총 {len(dated)}건 날짜 확인)")
        print(f" ⚠ 이 범위 밖의 트럼프 거래는 지금 목록에 없습니다.")
        print("   더 옛날 공시가 필요하면 sources.json의 ptrUrls에 직접 추가하세요.")
    else:
        print(" 파일명에서 날짜를 못 읽었습니다.")
    return 0


def diagnose(sources_path="scripts/sources.json"):
    """모든 공시를 실제로 받아 파싱해 보고, 문서별로 무엇이 걸러졌는지 보여준다.

    지금까지는 스캔본 6건만 --dump-ocr로 봤다. 하지만 텍스트 레이어로 깨끗이
    읽힌 11건이 뭘로 파싱됐는지는 본 적이 없다. 이 도구는 셋을 한꺼번에 답한다.
      1) 문제가 OCR 화질인가, 필터 과다인가, 아니면 진짜 채권뿐인가
      2) 스캔본은 해상도를 올리면 나아질 여지가 있는가(원본 dpi 측정)
      3) 걸러낸 것 중에 진짜 개별 주식이 섞여 있는가(눈검사용 전체 목록)
    PDF·OCR 캐시를 쓰므로 두 번째 실행부터는 빠르다. 딱 한 번만 돌리면 된다.
    """
    cfg = load_sources(sources_path)
    load_ticker_index()          # 회사명 → 티커 해석 준비(SEC 목록)
    print("=" * 70)
    print(" 전체 진단 — 공시 목록 수집 중")
    print("=" * 70)

    urls = []
    for u in (cfg.get("ptrUrls") or []):
        if u not in urls:
            urls.append(u)
    try:
        for u in discover_ptr_urls(cfg):
            if u not in urls:
                urls.append(u)
    except Exception as e:  # noqa: BLE001
        print(f"  ! 자동 탐색 중 오류(계속 진행): {e}", file=sys.stderr)

    if not urls:
        print("공시를 하나도 찾지 못했습니다. 네트워크나 sources.json을 확인하세요.",
              file=sys.stderr)
        return 2

    print(f"\n대상 공시 {len(urls)}건. 하나씩 받아 파싱합니다"
          " (캐시가 있으면 빠릅니다)…\n")

    docs = []            # 문서별 결과
    all_stocks = []      # 전체 개별 종목
    all_dropped = []     # 전체 걸러낸 행 (문서 출처 포함)

    for i, url in enumerate(urls, 1):
        name = urllib.parse.unquote(url.rsplit("/", 1)[-1])[:48]
        host = urllib.parse.urlsplit(url).netloc
        try:
            data = fetch_bytes(url)
        except Exception as e:  # noqa: BLE001
            print(f"  {i:>2}. [받기실패] {name}  ({e})")
            docs.append({"name": name, "host": host, "fail": str(e)})
            continue

        layer, pages = _text_layer(data)
        had_text = has_text_layer(layer, pages)
        text = extract_pdf_text(data, use_ocr=True)
        used_ocr = (not had_text) and len(text) > len(layer)
        how = "OCR" if used_ocr else ("텍스트" if had_text else "판독실패")

        rows = parse_ptr_text(text)
        disc = parse_received_date(text)
        txn_dates = sorted(r["transactionDate"] for r in rows if r.get("transactionDate"))
        stocks, dropped = [], []
        for r in rows:
            if is_individual_stock(r["asset"]) and extract_ticker(r["asset"]):
                r["ticker"] = extract_ticker(r["asset"])
                stocks.append(r)
                all_stocks.append(r)
            else:
                dropped.append(r)
                all_dropped.append((name, r))

        nat = ""
        if not had_text:                       # 스캔본만 해상도 측정
            info = None
            try:
                info = native_scan_dpi(data, _first_image_page(data, pages))
            except Exception:  # noqa: BLE001
                pass
            if info:
                nat = f"  원본 {info[0]:.0f}dpi"

        name_date = _date_from_name(name)
        docs.append({
            "name": name, "host": host, "how": how, "pages": pages,
            "chars": len(text), "rows": len(rows), "stocks": len(stocks),
            "scan_dpi": (info[0] if (not had_text and nat) else None),
            "disclosure": disc, "name_date": name_date,
            "txn_min": txn_dates[0] if txn_dates else None,
            "txn_max": txn_dates[-1] if txn_dates else None,
        })
        # 이 공시가 '언제 것'인지: 파일명 날짜 → 없으면 헤더 공시일
        when = name_date or disc or "날짜?"
        span = f"{txn_dates[0][:7]}~{txn_dates[-1][:7]}" if txn_dates else "거래없음"
        print(f"  {i:>2}. {when}  [{how:^5}] {pages:>2}쪽  거래행 {len(rows):>3}  "
              f"개별 {len(stocks):>2}  거래일 {span}{nat}")

    # ---- 요약 -----------------------------------------------------------
    n_text = sum(1 for d in docs if d.get("how") == "텍스트")
    n_ocr = sum(1 for d in docs if d.get("how") == "OCR")
    n_fail = sum(1 for d in docs if d.get("how") == "판독실패" or d.get("fail"))
    print("\n" + "=" * 70)
    print(f" 문서 {len(docs)}건 — 텍스트 {n_text} / OCR {n_ocr} / 판독실패 {n_fail}")
    print(f" 개별 종목 {len(all_stocks)}건 / 걸러낸 행 {len(all_dropped)}건")

    # 공시가 커버하는 기간 — '몇 월부터'에 대한 답
    filed = sorted(d["name_date"] or d.get("disclosure") for d in docs
                   if d.get("name_date") or d.get("disclosure"))
    txns = sorted(d["txn_min"] for d in docs if d.get("txn_min"))
    txns_max = sorted((d["txn_max"] for d in docs if d.get("txn_max")), reverse=True)
    if filed:
        print(f" 공시 파일 날짜:  {filed[0]} ~ {filed[-1]}")
    if txns:
        print(f" 읽어낸 거래일:   {txns[0]} ~ {txns_max[0]}  "
              f"(이 범위 밖의 거래는 우리 데이터에 없음)")
    print("=" * 70)

    # ---- 개별 종목 (있으면) ---------------------------------------------
    if all_stocks:
        print("\n[✓ 개별 종목으로 판정된 거래]  ※ 채권 오탐이 없는지 확인하세요")
        seen = set()
        for r in all_stocks:
            key = (r["ticker"], r["transactionDate"], tuple(r["amountRange"]))
            if key in seen:
                continue
            seen.add(key)
            lo, hi = r["amountRange"]
            print(f"  {r['transactionDate']}  {r['action']:<8} {r['ticker']:<6} "
                  f"${lo:,}~${hi:,}  | {r['asset'][:46]}")

    # ---- 걸러낸 행 전체 (핵심: 여기 진짜 주식이 있나) --------------------
    if all_dropped:
        print(f"\n[✗ 채권·ETF·미식별로 걸러낸 행 {len(all_dropped)}건]"
              "  ※ 여기 진짜 개별 주식이 보이면 알려주세요")
        for src_name, r in all_dropped[:80]:
            why = _drop_reason(r["asset"])
            print(f"  [{why:<6}] {r['asset'][:60]}")
        if len(all_dropped) > 80:
            print(f"  ... 외 {len(all_dropped) - 80}건")

    # ---- 판정 -----------------------------------------------------------
    print("\n" + "=" * 70)
    print(" 판정")
    print("=" * 70)
    _diagnose_verdict(docs, all_stocks, all_dropped)
    return 0


def _first_image_page(data: bytes, pages: int) -> int:
    """스캔 이미지가 실제로 들어 있는 첫 쪽(1부터). 없으면 1."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        for i, pg in enumerate(reader.pages, 1):
            try:
                if any(True for _ in pg.images):
                    return i
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return 1


def _drop_reason(asset: str) -> str:
    """왜 걸러졌는지 한 단어로. 눈검사 때 채권/ETF/미식별을 구분해 준다."""
    if not is_individual_stock(asset):
        low = asset.lower()
        if any(t in low for t in MUNI_TOKENS):
            return "지방채"
        if BOND_RE.search(asset):
            return "채권"
        if re.search(r"\betf\b|fund|index|trust|money\s*market", low):
            return "펀드"
        return "비종목"
    if not extract_ticker(asset):
        return "티커없음"
    return "기타"


def _diagnose_verdict(docs, stocks, dropped):
    """숫자를 사람 말로 옮긴다. 다음에 뭘 할지가 여기서 갈린다."""
    scanned = [d for d in docs if d.get("how") == "OCR" or d.get("how") == "판독실패"]
    low_res = [d for d in scanned if d.get("scan_dpi") and d["scan_dpi"] < 250]
    ticketless = [name for name, r in dropped if _drop_reason(r["asset"]) == "티커없음"]

    if stocks:
        print(f"• 개별 종목 {len(stocks)}건을 찾았습니다. 위 목록에 채권이 섞이지 않았다면")
        print("  run_local.bat 으로 data.json에 반영할 수 있습니다.")
    else:
        print("• 개별 종목을 한 건도 찾지 못했습니다. 아래로 원인을 좁힙니다:")

    # 텍스트 레이어 문서가 주식을 못 낸 경우 — 필터냐 진짜 채권이냐
    text_docs = [d for d in docs if d.get("how") == "텍스트"]
    if text_docs and not stocks:
        print(f"\n  ① 깨끗한 텍스트 문서 {len(text_docs)}건도 개별 종목 0건입니다.")
        if ticketless:
            print(f"     그런데 '티커없음'으로 걸러진 게 {len(ticketless)}건 있습니다 —")
            print("     이름은 주식 같은데 티커 매핑이 없을 수 있습니다. 위 [티커없음]")
            print("     줄을 보세요. 진짜 주식이면 이름→티커 매핑만 추가하면 됩니다.")
        else:
            print("     걸러진 행이 전부 채권·지방채·펀드라면, 이 문서들엔 실제로")
            print("     개별 주식이 없는 것입니다(트럼프 공시의 정상적 특성).")

    # 스캔본 화질 문제
    if scanned:
        print(f"\n  ② 스캔본 {len(scanned)}건 중 개별 종목을 낸 건 "
              f"{sum(1 for d in scanned if d.get('stocks'))}건입니다.")
        if low_res:
            print(f"     이 중 {len(low_res)}건은 원본이 250dpi 미만이라 해상도를 올려도")
            print("     소용없습니다(없는 정보는 못 만듭니다). OCR로는 한계입니다.")
        hi_res = [d for d in scanned if d.get("scan_dpi") and d["scan_dpi"] >= 250
                  and not d.get("stocks")]
        if hi_res:
            print(f"     반면 {len(hi_res)}건은 원본 해상도가 충분한데도 0건입니다 —")
            print("     python scripts/build_data.py --ocr-tune 로 전처리를 바꾸면")
            print("     나아질 여지가 있습니다.")

    if not stocks:
        print("\n  → 요약: 위 [걸러낸 행] 목록을 붙여 주시면, 진짜 주식이 필터에")
        print("     걸린 것인지(고칠 수 있음) 실제로 채권뿐인지(정상) 제가 판단합니다.")


# ---------------------------------------------------------------------------
# OCR 결과 진단 (--dump-ocr)
# ---------------------------------------------------------------------------

def dump_ocr(lines=60):
    """캐시된 OCR 텍스트를 파서에 그대로 통과시켜, 어디서 걸러지는지 보여준다.

    단계는 셋이다.
      ① parse_ptr_text 가 거래 행을 몇 건 뽑는가
      ② 그중 개별 종목으로 남는 건 몇 건인가 (채권·지방채·ETF 제외)
      ③ 남은 건에서 티커가 뽑히는가
    0이 되는 지점이 진짜 원인이다. 각 단계별로 실제 예시를 함께 찍는다.
    """
    if not os.path.isdir(OCR_CACHE_DIR):
        print(f"OCR 캐시 폴더가 없습니다: {OCR_CACHE_DIR}", file=sys.stderr)
        print("run_local.bat 을 먼저 실행하세요.", file=sys.stderr)
        return 2

    files = [os.path.join(OCR_CACHE_DIR, f)
             for f in os.listdir(OCR_CACHE_DIR) if f.endswith(".txt")]
    if not files:
        print(f"OCR 캐시가 비어 있습니다: {OCR_CACHE_DIR}", file=sys.stderr)
        return 2

    files.sort(key=os.path.getsize, reverse=True)
    print("=" * 62)
    print(f" OCR 캐시 {len(files)}건 — 파일별 파싱 결과")
    print("=" * 62)

    total_rows, total_stocks = 0, 0
    kept, dropped = [], []
    per_file = []
    fixtures = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            t = fh.read()
        # 예전 버전의 --self-test가 시험용 픽스처를 실제 캐시에 써 놓았다.
        # 그대로 두면 가짜 MRNA 거래가 진단에 잡힌다.
        if len(t) < 1000 and "MODERNA" in t.upper() and "PENNSYLVANIA" in t.upper():
            fixtures.append(f)
            continue
        rws = parse_ptr_text(t)
        stk = [r for r in rws if is_individual_stock(r["asset"])]
        total_rows += len(rws)
        total_stocks += len(stk)
        kept.extend(stk)
        dropped.extend(r for r in rws if r not in stk)
        per_file.append((f, t, rws, stk))
        print(f"  {os.path.getsize(f):>9,}B  거래행 {len(rws):>4}  개별종목 {len(stk):>3}"
              f"   {os.path.basename(f)[:28]}")

    if fixtures:
        print(f"\n  ⚠ 시험용 픽스처 {len(fixtures)}건을 건너뛰었습니다(가짜 데이터).")
        print("    지우려면:")
        for f in fixtures:
            print(f"      del \"{os.path.abspath(f)}\"")

    print("\n" + "-" * 62)
    print(f" ① 파서가 뽑은 거래 행      : {total_rows:>5}")
    print(f" ② 개별 종목으로 남은 건    : {total_stocks:>5}")
    tickered = [r for r in kept if extract_ticker(r["asset"])]
    print(f" ③ 티커까지 뽑힌 건         : {len(tickered):>5}")
    print("-" * 62)

    if kept:
        print("\n[개별 종목으로 판정된 건]  ※ 채권 오탐이 없는지 눈으로 확인하세요")
        for r in kept[:25]:
            tk = extract_ticker(r["asset"]) or "티커?"
            lo, hi = r["amountRange"]
            print(f"  {r['transactionDate']}  {r['action']:<8} {tk:<6} "
                  f"${lo:,}~${hi:,}  | {r['asset'][:52]}")
        if len(kept) > 25:
            print(f"  ... 외 {len(kept)-25}건")

    if dropped:
        print(f"\n[채권·ETF로 걸러낸 건 {len(dropped)}건 중 앞 15건]"
              "  ※ 여기 진짜 주식이 섞였으면 필터가 과했다는 뜻")
        for r in dropped[:15]:
            print(f"  {r['transactionDate']}  {r['action']:<8} | {r['asset'][:60]}")

    # 원인 지목: 0이 되는 첫 단계를 짚는다.
    print()
    if not per_file:
        print("[진단] 읽을 수 있는 OCR 캐시가 없습니다(전부 시험용 픽스처).")
        print("       위 del 명령으로 지운 뒤 run_local.bat 을 다시 돌리세요.")
        return 0
    if total_rows == 0:
        print("[진단] 파서가 거래 행을 한 건도 못 뽑았습니다.")
        target, text, _, _ = per_file[0]
        all_lines = [l.strip() for l in text.splitlines() if l.strip()]
        n_amount = sum(1 for l in all_lines if parse_amount(l))
        n_date = sum(1 for l in all_lines if DATE_RE.search(l))
        n_type = sum(1 for l in all_lines
                     if any(p.search(l) for p, _ in TYPE_PATTERNS))
        print(f"       금액 {n_amount}줄 / 날짜 {n_date}줄 / 매수·매도 표현 {n_type}줄")
        if n_amount == 0:
            print("       → 금액 구간($1,001 - $15,000 꼴)을 하나도 못 찾았습니다.")
            print("         OCR 품질 문제이거나 금액 표기 형식이 다릅니다.")
        elif n_type == 0:
            print("       → 매수/매도 표현을 못 찾았습니다. OCR이 단어를 심하게")
            print("         뭉갰을 수 있습니다(purchase→ourchoso 같은 사례).")
        elif n_date == 0:
            print("       → 날짜를 못 찾았습니다.")
        else:
            print("       → 신호는 다 있는데 6줄 창 안에 모이지 않았거나,")
            print("         거래일이 공시일 이후/2년 초과라 버려졌습니다.")
            print(f"         공시일 판독값: {parse_received_date(text)}")
        print(f"\n===== {os.path.basename(target)} 앞 {lines}줄 =====")
        for l in all_lines[:lines]:
            print(l)
    elif total_stocks == 0:
        print("[진단] 거래 행은 뽑혔지만 전부 채권·지방채·ETF로 걸러졌습니다.")
        print("       위 '걸러낸 건' 목록에 진짜 개별 주식이 보이면 알려주세요.")
        print("       실제로 트럼프 공시는 대부분 채권이라 이게 정상일 수 있습니다.")
    elif not tickered:
        print("[진단] 개별 종목은 찾았는데 티커를 못 뽑았습니다.")
        print("       위 목록의 종목명을 보고 티커 매핑을 추가하면 됩니다.")
    else:
        print(f"[정상] 개별 종목 {total_stocks}건, 티커 {len(tickered)}건 추출됨.")
        print("       run_local.bat 으로 전체 실행하면 data.json에 반영됩니다.")
    return 0


# ---------------------------------------------------------------------------
# OCR 설정 실측 (--ocr-tune)
# ---------------------------------------------------------------------------

def _date_from_name(name: str):
    """공시 파일명에 박힌 날짜(MM.DD.YY / MM.DD.YYYY / MM-DD-YY)를 뽑는다.

    whitehouse.gov 파일명이 '...Report-11.14.25.pdf' 꼴이라 여기서
    이 공시가 '언제 것'인지 바로 알 수 있다.
    """
    m = re.search(r"(\d{1,2})[.\-](\d{1,2})[.\-](\d{2,4})", name)
    if not m:
        return None
    mm, dd, yy = m.groups()
    y = int(yy)
    if y < 100:
        y += 2000
    try:
        d = datetime(y, int(mm), int(dd))
    except ValueError:
        return None
    if d.year < 2012 or d > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=7):
        return None
    return d.strftime("%Y-%m-%d")


def native_scan_dpi(data: bytes, page: int):
    """스캔 이미지의 실제 해상도(dpi)를 재본다.

    이게 결정적이다. PDF에 200dpi로 스캔된 이미지가 박혀 있으면, 600dpi로
    렌더링해도 없는 정보가 생기지는 않는다(보간일 뿐). 그런 문서는
    해상도를 올려도 소용없고 다른 수를 찾아야 한다.
    반대로 원본이 300dpi 이상인데 못 읽는 거라면 전처리로 개선 여지가 있다.
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pg = reader.pages[page - 1]
        box = pg.mediabox
        pt_w = float(box.width) or 612.0
        pt_h = float(box.height) or 792.0
        best = None
        for im in pg.images:
            w, h = im.image.size
            dpi = max(w / (pt_w / 72.0), h / (pt_h / 72.0))
            if best is None or dpi > best[0]:
                best = (dpi, w, h, im.image.mode)
        return best
    except Exception:  # noqa: BLE001
        return None


def _score_ocr(text):
    """OCR 결과가 '거래표로서' 쓸만한지 점수화한다.

    핵심은 금액 칸이다. 실측에서 종목명은 대충 읽히는데 금액이 통째로
    글자가 돼버려("$500,001" → "ssongon") 거래를 하나도 못 뽑았다.
    그래서 금액 인식 줄 수를 가장 무겁게 본다.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    n_amount = sum(1 for l in lines if parse_amount(l))
    n_date = sum(1 for l in lines if DATE_RE.search(l))
    n_type = sum(1 for l in lines if any(p.search(l) for p, _ in TYPE_PATTERNS))
    rows = parse_ptr_text(text)
    digits = sum(c.isdigit() for c in text)
    return {
        "amount": n_amount, "date": n_date, "type": n_type,
        "rows": len(rows), "digits": digits, "chars": len(text),
        # 최종 산출물은 거래 행이다. 금액줄이 많아도 행으로 안 묶이면 소용없다.
        "score": len(rows) * 20 + n_amount * 10 + n_date * 3 + n_type,
    }


def ocr_tune(pdf=None, page=None, dpis=(300, 400, 600),
             preps=("none", "sharp", "binary2x", "binary3x"), psms=("6", "4")):
    """한 페이지를 여러 설정으로 OCR해 보고 어느 조합이 제일 나은지 표로 보여준다.

    추측 대신 실측으로 고르기 위한 도구다. 페이지 하나만 쓰기 때문에
    조합당 수 초~수십 초면 끝난다.
    """
    if pdf is None:
        cands = []
        if os.path.isdir(PDF_CACHE_DIR):
            cands = [os.path.join(PDF_CACHE_DIR, f)
                     for f in os.listdir(PDF_CACHE_DIR) if f.endswith(".pdf")]
        if cands:
            pdf = max(cands, key=os.path.getsize)   # 큰 문서 = 스캔본일 확률이 높다
        else:
            # 캐시가 비었으면 목록에서 가장 큰 공시 하나만 받아 온다.
            # 전체를 받을 필요는 없다. 실험은 한 문서 한 쪽이면 충분하다.
            print("PDF 캐시가 비어 있어 공시 목록에서 하나만 받아옵니다.")
            pdf = _pick_largest_ptr()
            if pdf is None:
                print("공시를 찾지 못했습니다. 주소를 직접 지정하세요:", file=sys.stderr)
                print("  python scripts/build_data.py --ocr-tune --pdf <주소나 파일경로>",
                      file=sys.stderr)
                return 2

    print(f"대상 PDF: {pdf}")
    data = fetch_bytes(pdf)
    total = count_pdf_pages(data)
    print(f"  {len(data):,} bytes / {total}쪽")

    if page is None:
        page = _find_table_page(data, total)
        if page is None:
            print("  거래표가 있는 쪽을 못 찾아 3쪽으로 진행합니다.", file=sys.stderr)
            page = 3
    print(f"  실험 대상: {page}쪽 (1부터 셈)")

    nat = native_scan_dpi(data, page)
    if nat:
        ndpi, w, h, mode = nat
        print(f"  원본 스캔 해상도: 약 {ndpi:.0f}dpi ({w}x{h}, {mode})")
        if ndpi < 250:
            print("  ⚠ 원본이 250dpi 미만입니다. 렌더링 dpi를 올려도 없는 정보는")
            print("    생기지 않습니다(보간일 뿐). 전처리 쪽에서 답이 나와야 합니다.")
        # 원본의 3배를 넘겨 렌더링하는 건 계산만 늘고 얻는 게 없다.
        # 다만 Tesseract는 어느 정도의 확대는 좋아하므로 여유를 둔다.
        cap = max(ndpi * 3, min(dpis))
        usable = [d for d in dpis if d <= cap]
        if len(usable) < 2:
            usable = sorted(dpis)[:2]
        if len(usable) < len(dpis):
            dpis = tuple(usable)
            print(f"  → 시험할 dpi를 {dpis}로 줄입니다(원본의 3배까지).")
    else:
        print("  원본 스캔 해상도: 판독 실패")
    print()

    idx = {page - 1}
    print(f"{'dpi':>5} {'psm':>4} {'전처리':<10} "
          f"{'금액줄':>6} {'날짜줄':>6} {'매매줄':>6} {'거래행':>6} {'숫자수':>7} {'점수':>6}")
    print("-" * 70)
    results = []
    for dpi in dpis:
        for psm in psms:
            for prep in preps:
                try:
                    txt = ocr_pdf(data, dpi=dpi, psm=psm, prep=prep,
                                  pages=idx, progress=False)
                except RuntimeError as e:
                    print(f"  ! {e}", file=sys.stderr)
                    return 2
                sc = _score_ocr(txt)
                results.append((sc["score"], dpi, psm, prep, sc, txt))
                print(f"{dpi:>5} {psm:>4} {prep:<10} "
                      f"{sc['amount']:>6} {sc['date']:>6} {sc['type']:>6} "
                      f"{sc['rows']:>6} {sc['digits']:>7} {sc['score']:>6}")

    results.sort(key=lambda r: r[0], reverse=True)
    best = results[0]
    print("\n" + "=" * 70)
    if best[0] == 0:
        print("[결과] 어떤 설정으로도 금액·날짜를 읽지 못했습니다.")
        print("       이 스캔본은 OCR로 자동 처리하기 어렵습니다.")
        print("       --page 로 다른 쪽을 지정해 보고(표지·서명 쪽일 수 있음),")
        print("       그래도 0이면 수동 입력이나 유료 API를 고려해야 합니다.")
    else:
        _, dpi, psm, prep, sc, txt = best
        print(f"[최적] dpi={dpi}  psm={psm}  전처리={prep}"
              f"   (금액 {sc['amount']}줄 / 거래행 {sc['rows']}건)")
        print("\n적용하려면 run_local.bat 실행 전에:")
        print(f"    set FT_OCR_DPI={dpi}")
        print(f"    set FT_OCR_PSM={psm}")
        print(f"    set FT_OCR_PREP={prep}")
        print("\n===== 최적 설정의 OCR 결과 앞 25줄 =====")
        for l in [l for l in txt.splitlines() if l.strip()][:25]:
            print(l)
    return 0


def _pick_largest_ptr():
    """공시 목록에서 가장 큰 PDF 주소를 고른다.

    큰 문서 = 스캔본(이미지)일 확률이 높고, 개별 주식 거래도 거기 들어 있다.
    크기는 HEAD 요청으로만 확인하므로 실제로 받는 건 한 건뿐이다.
    """
    urls = discover_ptr_urls(load_sources())
    if not urls:
        return None
    print(f"  공시 {len(urls)}건 발견 — 크기 확인 중")
    best, best_n = None, -1
    for u in urls:
        try:
            req = urllib.request.Request(encode_url(u), headers=UA, method="HEAD")
            with urllib.request.urlopen(req, timeout=30) as r:
                n = int(r.headers.get("Content-Length") or 0)
        except Exception:  # noqa: BLE001
            continue
        if n > best_n:
            best, best_n = u, n
    if best:
        print(f"  가장 큰 공시: {best_n:,} bytes")
    return best


def _find_table_page(data: bytes, total: int):
    """거래표가 있는 쪽을 찾는다.

    앞쪽은 표지·서명 페이지라 거래가 없다. 저해상도로 빠르게 훑어
    'Received Over 30' 같은 표 머리글이나 금액 비슷한 문자열이 있는 쪽을 고른다.
    """
    try:
        import pytesseract
    except ImportError:
        return None
    cmd = os.environ.get("TESSERACT_CMD") or (find_tesseract()[0] or "")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd

    scan = range(min(total, 12))
    best, best_n = None, 0
    for i in scan:
        try:
            img = next(iter(_render_pages(data, 150, {i})))
        except (StopIteration, RuntimeError):
            break
        t = pytesseract.image_to_string(img, config="--oem 1 --psm 6")
        n = len(re.findall(r"[\$sS]\s?\d", t)) + t.lower().count("received over")
        print(f"    쪽 탐색 {i+1}/{min(total,12)} (신호 {n})", end="\r", file=sys.stderr)
        if n > best_n:
            best, best_n = i + 1, n
    print(" " * 40, end="\r", file=sys.stderr)
    return best

# ---------------------------------------------------------------------------
# 환경 진단 (--doctor)
# ---------------------------------------------------------------------------

# Windows에서 Tesseract가 흔히 설치되는 위치들.
# 설치는 했는데 PATH에 안 잡히는 경우가 많아 직접 뒤져본다.
TESSERACT_GUESSES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%USERPROFILE%\AppData\Local\Tesseract-OCR\tesseract.exe"),
    "/usr/bin/tesseract", "/usr/local/bin/tesseract", "/opt/homebrew/bin/tesseract",
]


def find_tesseract():
    """PATH → 환경변수 → 흔한 설치 경로 순으로 tesseract 실행파일을 찾는다."""
    import shutil
    env = os.environ.get("TESSERACT_CMD")
    if env and os.path.exists(env):
        return env, "TESSERACT_CMD 환경변수"
    found = shutil.which("tesseract")
    if found:
        return found, "PATH"
    for g in TESSERACT_GUESSES:
        if g and os.path.exists(g):
            return g, "설치 경로 자동 탐색"
    return None, None


def doctor():
    """무엇이 빠졌는지 짚어주는 진단. 각 항목마다 해결 방법을 함께 출력한다."""
    problems = []

    def ok(msg):
        print(f"  [OK]   {msg}")

    def bad(msg, fix):
        print(f"  [문제] {msg}")
        print(f"         → {fix}")
        problems.append(msg)

    print("=" * 62)
    print(" 트럼프 팔로우 — 환경 진단")
    print("=" * 62)

    print("\n[1] 파이썬")
    v = sys.version_info
    if v >= (3, 10):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        bad(f"Python {v.major}.{v.minor} (3.10 이상 필요)",
            "https://www.python.org/downloads/ 에서 최신 버전 설치")

    print("\n[2] 파이썬 패키지")
    for mod, pkg, why in [
        ("pypdf", "pypdf", "PDF 텍스트 추출"),
        ("pytesseract", "pytesseract", "Tesseract 연결"),
        ("PIL", "pillow", "이미지 처리"),
    ]:
        try:
            __import__(mod)
            ok(f"{pkg} ({why})")
        except ImportError:
            bad(f"{pkg} 없음 ({why})",
                "pip install -r scripts\\requirements.txt")

    renderers = []
    for mod in ("pymupdf", "pypdfium2"):
        try:
            __import__(mod)
            renderers.append(mod)
        except ImportError:
            pass
    if renderers:
        ok(f"PDF 렌더러: {', '.join(renderers)} (하나만 있으면 충분)")
    else:
        bad("PDF 렌더러 없음 (스캔 공시를 읽을 수 없음)",
            "pip install pypdfium2      (권장, 휠 지원 범위 넓음)\n"
            "         또는 pip install pymupdf   (더 빠르지만 최신 파이썬에선 설치 실패 가능)")

    print("\n[3] Tesseract OCR 엔진")
    path, how = find_tesseract()
    if path:
        ok(f"{path}  ({how})")
        try:
            import subprocess
            out = subprocess.run([path, "--version"], capture_output=True,
                                 text=True, timeout=20)
            ver = (out.stdout or out.stderr).splitlines()[0]
            ok(f"버전: {ver}")
        except Exception as e:  # noqa: BLE001
            bad(f"실행 실패: {e}", "설치가 손상되었을 수 있습니다. 재설치를 권합니다")
        if how == "설치 경로 자동 탐색":
            print(f"         ※ PATH에는 없습니다. run_local.bat이 쓰도록 하려면:")
            print(f'            set TESSERACT_CMD={path}')
    else:
        bad("Tesseract를 찾을 수 없음",
            "https://github.com/UB-Mannheim/tesseract/wiki 에서 설치\n"
            "         → 이미 설치했다면: set TESSERACT_CMD=설치경로\\tesseract.exe")

    print("\n[4] 프로젝트 파일")
    for f in ["data.json", "scripts/sources.json", "index.html"]:
        if os.path.exists(f):
            ok(f)
        else:
            bad(f"{f} 없음",
                "저장소 최상위 폴더에서 실행하세요 (cd follow_Trump)")

    print("\n[5] SEC 티커 목록 (회사명 → 티커 해석)")
    if os.path.exists(SEC_TICKERS_FILE):
        try:
            n = len(load_ticker_index(quiet=True))
            ok(f"{SEC_TICKERS_FILE} — 회사 {n:,}개")
        except Exception as e:  # noqa: BLE001
            bad(f"{SEC_TICKERS_FILE} 읽기 실패: {e}",
                "파일이 깨졌을 수 있습니다. 지우고 다시 받으세요.")
    else:
        bad(f"{SEC_TICKERS_FILE} 없음 (없으면 실행 중 자동 다운로드 시도)",
            f"수동으로 받으려면: {SEC_TICKERS_URL} → scripts\\company_tickers.json 로 저장")

    print("\n[6] 네트워크")
    try:
        body = _get_text("https://www.whitehouse.gov/disclosures/", timeout=25)
        ok(f"공시 목록 (whitehouse.gov) — 응답 {len(body):,}자")
    except Exception as e:  # noqa: BLE001
        bad(f"공시 목록 접속 실패: {e}", "인터넷 연결 또는 방화벽/백신 확인")

    print("\n[7] 시세 제공처 (하나만 되면 충분)")
    got = None
    for name, fn in PRICE_PROVIDERS:
        try:
            series = fn("AAPL")
            ok(f"{name} — {len(series)}일, 최신 {series[-1][0]} ${series[-1][1]:.2f}")
            got = got or name
        except Exception as e:  # noqa: BLE001
            print(f"  [실패] {name} — {e}")
    if got:
        ok(f"사용 가능: {got}")
    else:
        bad("시세 제공처 전부 실패",
            "방화벽/백신이 파이썬의 HTTPS를 막는지 확인하거나,\n"
            "         회사망이면 프록시 설정(HTTPS_PROXY 환경변수)이 필요할 수 있습니다")

    print("\n" + "=" * 62)
    if problems:
        print(f" 문제 {len(problems)}건 — 위의 → 표시를 따라 조치하세요.")
    else:
        print(" 이상 없음. run_local.bat 을 실행하면 됩니다.")
    print("=" * 62)
    return 1 if problems else 0


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
    ap.add_argument("--no-ocr", action="store_true",
                    help="스캔 PDF OCR 생략(텍스트 레이어가 있는 것만 처리)")
    ap.add_argument("--ocr-dpi", type=int, default=300,
                    help="OCR 렌더링 해상도(기본 300). 낮추면 빠르고 부정확")
    ap.add_argument("--list-sources", action="store_true",
                    help="다운로드 없이 현재 잡히는 공시 목록과 기간만 즉시 출력")
    ap.add_argument("--diagnose", action="store_true",
                    help="모든 공시를 한 번에 받아 파싱·필터·해상도를 통째로 진단")
    ap.add_argument("--dump-ocr", action="store_true",
                    help="캐시된 OCR 텍스트와 파서 실패 지점을 진단")
    ap.add_argument("--ocr-tune", action="store_true",
                    help="OCR 설정(해상도·전처리)을 한 쪽으로 실측 비교")
    ap.add_argument("--pdf", help="--ocr-tune 대상 PDF (주소 또는 파일경로)")
    ap.add_argument("--page", type=int, help="--ocr-tune 대상 쪽 번호 (1부터)")
    ap.add_argument("--doctor", action="store_true",
                    help="무엇이 빠졌는지 진단(설치 확인용)")
    ap.add_argument("--probe-oge", action="store_true",
                    help="OGE 자동 탐색만 실행해 진단 출력(데이터 변경 없음)")
    args = ap.parse_args()

    if args.list_sources:
        return list_sources(args.sources)

    if args.diagnose:
        return diagnose(args.sources)

    if args.dump_ocr:
        return dump_ocr()

    if args.ocr_tune:
        return ocr_tune(pdf=args.pdf, page=args.page)

    if args.doctor:
        return doctor()

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
        ptrs += [u for u in discover_ptr_urls(cfg) if u not in ptrs]

    if ptrs:
        fresh, stats = build_from_ptrs(ptrs, enrich=not args.no_price,
                                       use_ocr=not args.no_ocr, dpi=args.ocr_dpi)
        existing = []
        try:
            with open(args.out, encoding="utf-8") as f:
                existing = json.load(f).get("trades", [])
        except (FileNotFoundError, ValueError):
            pass
        merged = merge_records(existing, fresh)
        write_data_json(merged, stats, args.out,
                        parsed_ids={r.get("id") for r in fresh})

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
