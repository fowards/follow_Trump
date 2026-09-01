#!/usr/bin/env python3
"""트럼프 관련 뉴스 헤드라인을 하루 1~2건만 가져온다.

방식: 본문을 긁지 않는다. Google News RSS(제목·출처·링크·날짜만 담긴
공개 피드 — 애초에 기계가 읽어가라고 만든 것)에서 헤드라인만 받아
news.json에 쌓는다. 사이트에는 제목+출처+날짜와 원문 링크만 보여주고,
클릭하면 언론사 원문으로 이동한다(본문 재게시 아님 — 저작권·AdSense
정책 문제 없음).

사용법:
    python scripts/fetch_news.py                # 최신 2건만 추가
    python scripts/fetch_news.py --limit 5
"""

import argparse
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

NEWS_JSON = os.environ.get("FT_NEWS_JSON", "news.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"}

# 개별 종목 매매와 관련된 기사 위주로 좁힌다("Trump" 단독 검색은 정치
# 일반 뉴스가 너무 많이 섞인다).
QUERY = os.environ.get(
    "FT_NEWS_QUERY",
    '"Trump" (stock OR shares OR trade OR portfolio OR disclosure OR "periodic transaction")',
)
RSS_URL = ("https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en")


def fetch_rss(query: str) -> bytes:
    url = RSS_URL.format(q=urllib.parse.quote(query))
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _clean_title(title: str, source: str) -> str:
    """Google News가 제목 끝에 ' - 출처명'을 붙이는데, source 태그로 이미
    아니까 중복이면 떼어낸다."""
    if source and title.endswith(" - " + source):
        return title[: -(len(source) + 3)].strip()
    return title.strip()


def parse_rss(body: bytes):
    root = ET.fromstring(body)
    items = []
    for item in root.findall("./channel/item"):
        title_raw = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        title = _clean_title(title_raw, source)
        if not title or not link:
            continue
        try:
            dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
        except ValueError:
            dt = datetime.now(timezone.utc)
        items.append({
            "title": title,
            "source": source or "출처 미상",
            "link": link,
            "publishedAt": dt.strftime("%Y-%m-%d"),
            "_sort": dt,
        })
    items.sort(key=lambda x: x["_sort"], reverse=True)
    for it in items:
        del it["_sort"]
    return items


MYMEMORY_URL = "https://api.mymemory.translated.net/get"


def translate_ko(text: str) -> str:
    """제목만 한국어로 옮긴다(본문은 다루지 않음).

    번역도 저작권 대상이지만, 미국 저작권청 규정(37 CFR 202.1)은
    "제목·짧은 문구"를 보호 대상에서 제외한다 — 독창적 표현으로 보기엔
    너무 짧다는 것. 그래서 헤드라인 번역은 안전하고, 본문을 문장째
    옮기는 것과는 다르다. MyMemory는 번역 용도로 공개된 무료 API라
    스크래핑이 아니다(공식 문서화된 엔드포인트, 키 불필요).

    실패하면(네트워크 문제·일일 한도 등) None을 돌려주고, 호출부가
    영어 원문을 그대로 쓰게 한다 — 번역 실패가 전체 수집을 막지 않는다.
    """
    if not text:
        return None
    try:
        url = (MYMEMORY_URL + "?q=" + urllib.parse.quote(text) + "&langpair=en|ko")
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        translated = (data.get("responseData") or {}).get("translatedText")
        # MyMemory는 실패해도 200을 주고 원문을 그대로 돌려줄 때가 있다 —
        # 그러면 "번역됨"으로 잘못 표시하지 않게 원문과 같으면 버린다.
        if translated and translated.strip().lower() != text.strip().lower():
            return translated.strip()
    except Exception as e:  # noqa: BLE001
        print(f"  ! 번역 실패({text[:30]}...): {e}", file=sys.stderr)
    return None


def load_existing(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc.get("items", [])
    except (FileNotFoundError, ValueError):
        return []


def main():
    ap = argparse.ArgumentParser(description="트럼프 관련 뉴스 헤드라인만 수집(본문 없음)")
    ap.add_argument("--limit", type=int, default=2,
                    help="이번 실행에서 새로 추가할 최대 건수(기본 2)")
    ap.add_argument("--out", default=NEWS_JSON)
    ap.add_argument("--keep", type=int, default=60,
                    help="news.json에 보관할 최대 총 건수(기본 60, 오래된 것부터 정리)")
    ap.add_argument("--no-translate", action="store_true",
                    help="제목 한국어 번역 생략(영어 원문만)")
    args = ap.parse_args()

    try:
        body = fetch_rss(QUERY)
        fresh = parse_rss(body)
    except Exception as e:  # noqa: BLE001
        print(f"! 뉴스 수집 실패: {e}", file=sys.stderr)
        return 1

    existing = load_existing(args.out)
    seen_links = {it["link"] for it in existing}

    added = []
    for it in fresh:
        if it["link"] in seen_links:
            continue
        if not args.no_translate:
            it["titleKo"] = translate_ko(it["title"])
        added.append(it)
        seen_links.add(it["link"])
        if len(added) >= args.limit:
            break

    merged = added + existing
    merged.sort(key=lambda x: x.get("publishedAt", ""), reverse=True)
    merged = merged[: args.keep]

    doc = {
        "meta": {
            "source": "Google News RSS (제목·출처·링크만 — 본문 재게시 없음)",
            "query": QUERY,
            "lastFetched": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        },
        "items": merged,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    print(f"· 새 헤드라인 {len(added)}건 추가 (전체 {len(merged)}건 보관)", file=sys.stderr)
    for it in added:
        shown = it.get("titleKo") or it["title"]
        print(f"  - [{it['source']}] {shown}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
