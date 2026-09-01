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
        print(f"  - [{it['source']}] {it['title']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
