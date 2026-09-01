#!/usr/bin/env python3
"""Save Truth Social login cookies for automated scraping.

Usage:
    python scripts/save_truth_social_cookies.py

This script will:
1. Open a browser to Truth Social login page
2. Wait for you to log in manually
3. Navigate to Trump's profile to verify login
4. Save cookies and localStorage to config/cookies/
"""

import json
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


def main():
    print("=" * 60)
    print("Truth Social Cookie Saver")
    print("=" * 60)
    print()

    # Create browser (non-headless for login)
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,900")

    print("브라우저를 여는 중...")
    driver = webdriver.Chrome(options=options)
    driver.get("https://truthsocial.com/login")

    print()
    print("=" * 60)
    print("브라우저가 열렸습니다!")
    print()
    print("1. Truth Social에 로그인해주세요")
    print("2. 로그인 완료 후 홈 피드가 보일 때까지 기다려주세요")
    print("3. 이 터미널로 돌아와서 Enter를 눌러주세요")
    print("=" * 60)
    print()

    input("로그인 완료 후 Enter 키를 누르세요... ")

    # Navigate to Trump's profile to ensure full authentication
    print("\n트럼프 프로필 페이지로 이동 중...")
    driver.get("https://truthsocial.com/@realDonaldTrump")
    time.sleep(5)

    # Check if we can see posts
    page_source = driver.page_source
    if "No Truths" in page_source or "log in" in page_source.lower():
        print("\n경고: 로그인이 제대로 되지 않은 것 같습니다.")
        print("다시 로그인을 시도해주세요.")
        input("로그인 후 다시 Enter를 눌러주세요... ")
        driver.get("https://truthsocial.com/@realDonaldTrump")
        time.sleep(5)

    # Save cookies
    cookies = driver.get_cookies()

    cookie_dir = Path(__file__).parent.parent / "config" / "cookies"
    cookie_dir.mkdir(parents=True, exist_ok=True)
    cookie_path = cookie_dir / "truth_social.json"

    with open(cookie_path, "w") as f:
        json.dump(cookies, f, indent=2)

    # Also save localStorage (contains auth tokens)
    local_storage = driver.execute_script(
        "var ls = {}; for (var i = 0; i < localStorage.length; i++) { "
        "var key = localStorage.key(i); ls[key] = localStorage.getItem(key); } return ls;"
    )

    local_storage_path = cookie_dir / "truth_social_localstorage.json"
    with open(local_storage_path, "w") as f:
        json.dump(local_storage, f, indent=2)

    print()
    print("=" * 60)
    print(f"쿠키 저장 완료!")
    print(f"쿠키 파일: {cookie_path} ({len(cookies)}개)")
    print(f"LocalStorage 파일: {local_storage_path} ({len(local_storage)}개)")
    print()
    print("저장된 쿠키 목록:")
    for c in cookies:
        print(f"  - {c['name']}: {c['value'][:30]}...")
    print("=" * 60)

    driver.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
