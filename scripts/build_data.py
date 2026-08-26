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


def parse_ptr_text(text: str, disclosure_date=None, window=6):
    """278-T 텍스트에서 거래 행을 추출한다.

    표의 한 행이 한 줄에 담기는 경우(텍스트 레이어 문서)와, 여러 줄로
    쪼개지는 경우(우리가 직접 OCR한 스캔 문서)를 모두 처리한다.
    실측: OCR 결과에서는 금액·날짜·거래유형이 서로 다른 줄에 흩어져
    한 줄 기준으로만 찾으면 한 건도 못 뽑는다.

    방식: 줄을 누적하다가 금액·날짜·거래유형이 모두 모이면 한 건으로
    확정하고 버퍼를 비운다. 한 줄에 다 있으면 즉시 확정되므로
    기존 동작과 동일하고, 흩어져 있으면 최대 window줄까지 묶는다.
    """
    if disclosure_date is None:
        disclosure_date = parse_received_date(text)

    rows = []
    buf = []          # [(line, amount, has_date, action), ...]

    def flush():
        """버퍼가 한 건을 이루면 레코드로 만든다."""
        if not buf:
            return False
        amount = next((b[1] for b in buf if b[1]), None)
        action = next((b[3] for b in buf if b[3]), None)
        joined = " ".join(b[0] for b in buf)
        if not amount or not action:
            return False
        # 금액이 적힌 줄에 거래일이 함께 있는 경우가 많다. 그 줄을 먼저 보고,
        # 없을 때만 버퍼 전체에서 찾는다(헤더 날짜 오염 방지).
        amount_line = next((b[0] for b in buf if b[1]), "")
        txn = (pick_transaction_date(amount_line, disclosure_date)
               or pick_transaction_date(joined, disclosure_date))
        if not txn:
            return False

        name = AMOUNT_RE.sub(" ", joined)
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
        return True

    header_re = re.compile(
        r"(OGE\s+RECEIVED|OGE\s+Form|Filer.{0,3}s\s+Name|Periodic\s+Transaction)", re.I)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if header_re.search(line):
            buf.clear()          # 헤더는 어떤 거래 행에도 속하지 않는다
            continue
        amount, has_date, action = _line_has(line)
        if not (amount or has_date or action):
            # 아무 신호도 없는 줄은 버퍼를 끊지 않되 이름 조각으로 남겨둔다.
            if buf:
                buf.append((line, None, False, None))
                buf[:] = buf[-window:]
            continue

        buf.append((line, amount, has_date, action))
        buf[:] = buf[-window:]

        have_amount = any(b[1] for b in buf)
        have_date = any(b[2] for b in buf)
        have_action = any(b[3] for b in buf)
        if have_amount and have_date and have_action:
            if flush():
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


def _ocr_cache_path(data: bytes, dpi: int) -> str:
    key = hashlib.sha256(data).hexdigest()[:32]
    return os.path.join(OCR_CACHE_DIR, f"{key}-{dpi}.txt")


def _render_pages(data: bytes, dpi: int):
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
            for page in doc:
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
        for page in pdf:
            yield page.render(scale=dpi / 72.0, grayscale=True).to_pil()
    finally:
        pdf.close()


def count_pdf_pages(data: bytes) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001
        return 0


def ocr_pdf(data: bytes, dpi=300, lang="eng", progress=True) -> str:
    """스캔 PDF를 페이지 이미지로 렌더링해 Tesseract로 읽는다.

    느리기 때문에(쪽당 수 초) 결과를 PDF 해시 기준으로 캐시한다.
    같은 공시를 다시 돌려도 두 번 OCR하지 않는다.
    """
    cache = _ocr_cache_path(data, dpi)
    if os.path.exists(cache):
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
        for i, img in enumerate(_render_pages(data, dpi), 1):
            # --psm 6: 표 형태 문서를 한 덩어리 텍스트로 읽기
            out.append(pytesseract.image_to_string(img, lang=lang, config="--psm 6"))
            if progress:
                print(f"    OCR {i}/{total or '?'}쪽", end="\r", file=sys.stderr)
    except RuntimeError as e:
        print(f"  ! {e}", file=sys.stderr)
        return ""

    text = "\n".join(out)
    if progress:
        print(f"    OCR 완료 {len(out)}쪽 → {len(text):,}자          ", file=sys.stderr)

    os.makedirs(OCR_CACHE_DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def extract_pdf_text(data: bytes, use_ocr=True, dpi=300) -> str:
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


def build_from_ptrs(sources, enrich=True, use_ocr=True, dpi=300):
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
    import tempfile
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
        text = extract_pdf_text(pdf)
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
# OCR 결과 진단 (--dump-ocr)
# ---------------------------------------------------------------------------

def dump_ocr(lines=60):
    """캐시된 OCR 텍스트를 보여주고, 파서가 왜 못 읽는지 짚어준다.

    OCR은 성공했는데 거래행이 0건인 경우, 원인은 대개
    '한 줄에 금액·날짜·거래유형이 모두 있어야 한다'는 파서 가정이
    OCR의 줄 나눔과 맞지 않아서다. 어느 조건이 몇 줄에서 걸리는지 센다.
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
    print(f" OCR 캐시 {len(files)}건 (큰 순)")
    print("=" * 62)
    for f in files:
        print(f"  {os.path.getsize(f):>9,} bytes  {os.path.basename(f)}")

    target = files[0]
    with open(target, encoding="utf-8") as fh:
        text = fh.read()

    rows = parse_ptr_text(text)
    all_lines = text.splitlines()
    n_amount = sum(1 for l in all_lines if parse_amount(l))
    n_date = sum(1 for l in all_lines if DATE_RE.search(l))
    n_type = sum(1 for l in all_lines
                 if any(p.search(l) for p, _ in TYPE_PATTERNS))
    n_all3 = sum(1 for l in all_lines
                 if parse_amount(l) and DATE_RE.search(l)
                 and any(p.search(l) for p, _ in TYPE_PATTERNS))

    print(f"\n가장 큰 파일 분석: {os.path.basename(target)}")
    print(f"  전체 {len(text):,}자 / {len(all_lines):,}줄")
    print(f"  공시일(OGE RECEIVED) 추출: {parse_received_date(text)}")
    print("\n  파서 조건별로 걸리는 줄 수:")
    print(f"    금액 구간이 있는 줄      : {n_amount:>5}")
    print(f"    날짜가 있는 줄           : {n_date:>5}")
    print(f"    매수/매도 표현이 있는 줄 : {n_type:>5}")
    print(f"    → 셋 다 있는 줄(=거래행) : {n_all3:>5}   ★ 이게 0이면 줄 나눔 문제")
    print(f"  parse_ptr_text 결과: {len(rows)}건")

    print(f"\n===== 앞 {lines}줄 (실제 OCR 원문) =====")
    for l in all_lines[:lines]:
        print(l)

    if n_all3 == 0 and (n_amount or n_date):
        print("\n[진단] 금액·날짜는 있는데 한 줄에 모이지 않았습니다.")
        print("       OCR이 표의 한 행을 여러 줄로 쪼갠 것으로 보입니다.")
    return 0


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

    print("\n[5] 네트워크")
    try:
        body = _get_text("https://www.whitehouse.gov/disclosures/", timeout=25)
        ok(f"공시 목록 (whitehouse.gov) — 응답 {len(body):,}자")
    except Exception as e:  # noqa: BLE001
        bad(f"공시 목록 접속 실패: {e}", "인터넷 연결 또는 방화벽/백신 확인")

    print("\n[6] 시세 제공처 (하나만 되면 충분)")
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
    ap.add_argument("--dump-ocr", action="store_true",
                    help="캐시된 OCR 텍스트와 파서 실패 지점을 진단")
    ap.add_argument("--doctor", action="store_true",
                    help="무엇이 빠졌는지 진단(설치 확인용)")
    ap.add_argument("--probe-oge", action="store_true",
                    help="OGE 자동 탐색만 실행해 진단 출력(데이터 변경 없음)")
    args = ap.parse_args()

    if args.dump_ocr:
        return dump_ocr()

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
