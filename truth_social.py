"""Truth Social scraper using Selenium."""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# Use undetected-chromedriver to bypass bot detection
try:
    import undetected_chromedriver as uc
    USE_UNDETECTED = True
except ImportError:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    USE_UNDETECTED = False

logger = logging.getLogger(__name__)


class TruthSocialScraper:
    """Scraper for Truth Social posts.

    Collects posts from a Truth Social profile using Selenium.
    Uses saved cookies for authenticated access to get full content.
    """

    BASE_URL = "https://truthsocial.com"
    DEFAULT_USERNAME = "realDonaldTrump"
    COOKIE_FILE = Path(__file__).parent.parent.parent / "config" / "cookies" / "truth_social.json"
    LOCALSTORAGE_FILE = Path(__file__).parent.parent.parent / "config" / "cookies" / "truth_social_localstorage.json"

    def __init__(self, username: str = None):
        """Initialize Truth Social scraper.

        Args:
            username: Truth Social username to scrape (without @)
        """
        self.username = username or self.DEFAULT_USERNAME
        self.profile_url = f"{self.BASE_URL}/@{self.username}"
        self.cookies = self._load_cookies()
        self.local_storage = self._load_local_storage()

    def _load_cookies(self) -> List[Dict[str, Any]]:
        """Load saved cookies from file.

        Returns:
            List of cookie dictionaries or empty list if not found
        """
        if self.COOKIE_FILE.exists():
            try:
                with open(self.COOKIE_FILE, "r") as f:
                    cookies = json.load(f)
                logger.info(f"Loaded {len(cookies)} cookies from {self.COOKIE_FILE}")
                return cookies
            except Exception as e:
                logger.warning(f"Failed to load cookies: {e}")
        else:
            logger.warning(f"Cookie file not found: {self.COOKIE_FILE}")
            logger.warning("Run 'python scripts/save_truth_social_cookies.py' to save login cookies")
        return []

    def _load_local_storage(self) -> Dict[str, str]:
        """Load saved localStorage from file.

        Returns:
            Dict of localStorage key-value pairs or empty dict if not found
        """
        if self.LOCALSTORAGE_FILE.exists():
            try:
                with open(self.LOCALSTORAGE_FILE, "r") as f:
                    local_storage = json.load(f)
                logger.info(f"Loaded {len(local_storage)} localStorage items from {self.LOCALSTORAGE_FILE}")
                return local_storage
            except Exception as e:
                logger.warning(f"Failed to load localStorage: {e}")
        return {}

    def _apply_cookies(self, driver) -> None:
        """Apply saved cookies and localStorage to browser session.

        Args:
            driver: Selenium WebDriver instance
        """
        if not self.cookies and not self.local_storage:
            logger.warning("No cookies or localStorage to apply - scraping may have limited access")
            return

        # First navigate to the domain to set cookies/localStorage
        driver.get(self.BASE_URL)
        time.sleep(2)

        # Apply localStorage first (contains auth tokens)
        if self.local_storage:
            for key, value in self.local_storage.items():
                try:
                    # Escape special characters in value
                    escaped_value = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
                    driver.execute_script(f"localStorage.setItem('{key}', '{escaped_value}');")
                except Exception as e:
                    logger.debug(f"Could not set localStorage {key}: {e}")
            logger.info(f"Applied {len(self.local_storage)} localStorage items")

        # Add each cookie
        cookies_added = 0
        for cookie in self.cookies:
            try:
                # Remove problematic keys that Selenium doesn't accept
                cookie_copy = {k: v for k, v in cookie.items()
                              if k not in ['sameSite', 'expiry'] or k == 'expiry'}

                # Handle expiry format
                if 'expiry' in cookie_copy:
                    # Selenium expects expiry as int timestamp
                    if isinstance(cookie_copy['expiry'], float):
                        cookie_copy['expiry'] = int(cookie_copy['expiry'])

                driver.add_cookie(cookie_copy)
                cookies_added += 1
            except Exception as e:
                logger.debug(f"Could not add cookie {cookie.get('name')}: {e}")

        logger.info(f"Applied {cookies_added}/{len(self.cookies)} cookies")

        # Refresh to apply cookies and localStorage
        driver.refresh()
        time.sleep(3)

    def _create_driver(self, headless: bool = True):
        """Create Chrome driver with bot detection bypass.

        Args:
            headless: Run browser in headless mode

        Returns:
            Chrome WebDriver instance
        """
        if USE_UNDETECTED:
            # Use undetected-chromedriver for better bot detection bypass
            options = uc.ChromeOptions()

            if headless:
                options.add_argument("--headless=new")

            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")

            driver = uc.Chrome(options=options, use_subprocess=True)
            logger.info("Using undetected-chromedriver")
            return driver
        else:
            # Fallback to regular selenium
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            options = Options()

            if headless:
                options.add_argument("--headless=new")

            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")

            options.add_argument(
                "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)

            driver = webdriver.Chrome(options=options)

            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            logger.info("Using regular selenium webdriver")
            return driver

    def scrape_posts(
        self,
        limit: int = 50,
        hours_back: int = 48,
        headless: bool = True,
        existing_post_ids: set = None,
    ) -> List[Dict[str, Any]]:
        """Scrape posts from Truth Social profile by clicking each post.

        This method collects post links from the profile page, then visits
        each post individually to get full content. Stops when:
        - Post is older than hours_back
        - Post was already collected (in existing_post_ids)
        - Limit is reached

        Args:
            limit: Maximum number of posts to collect
            hours_back: Only collect posts from the last N hours (default 48)
            headless: Run browser in headless mode
            existing_post_ids: Set of already collected post IDs to skip

        Returns:
            List of post dictionaries
        """
        logger.info(f"Scraping Truth Social posts from @{self.username}")
        logger.info(f"Settings: limit={limit}, hours_back={hours_back}, headless={headless}")

        driver = self._create_driver(headless=headless)
        posts = []
        existing_post_ids = existing_post_ids or set()
        cutoff_time = datetime.now() - timedelta(hours=hours_back)

        try:
            # Apply saved cookies and localStorage for authenticated access
            if self.cookies or self.local_storage:
                logger.info("Applying saved auth for authenticated access...")
                self._apply_cookies(driver)

            # Navigate to profile
            logger.info(f"Loading profile: {self.profile_url}")
            driver.get(self.profile_url)
            time.sleep(5)

            # Handle popups
            self._dismiss_cookie_popup(driver)
            self._dismiss_modal_popups(driver)

            # Wait for posts to appear
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/posts/']"))
                )
            except TimeoutException:
                logger.warning("Timeout waiting for posts")

            # Collect post URLs by scrolling SLOWLY
            post_urls = []
            collected_ids = set()
            scroll_attempts = 0
            max_scroll_attempts = 50  # More attempts since we scroll smaller amounts
            stop_collecting = False
            last_height = 0
            no_new_posts_count = 0

            while len(post_urls) < limit * 2 and scroll_attempts < max_scroll_attempts and not stop_collecting:
                # Find all post links currently visible
                links = driver.find_elements(By.CSS_SELECTOR, f"a[href*='/@{self.username}/posts/']")
                new_posts_found = 0

                for link in links:
                    href = link.get_attribute("href")
                    if href:
                        match = re.search(r'/posts/(\d+)', href)
                        if match:
                            post_id = match.group(1)
                            if post_id not in collected_ids:
                                # Check if already in DB
                                if post_id in existing_post_ids:
                                    logger.info(f"Post {post_id} already in DB, stopping collection")
                                    stop_collecting = True
                                    break

                                collected_ids.add(post_id)
                                post_urls.append(href)
                                new_posts_found += 1
                                logger.info(f"Found post URL #{len(post_urls)}: {href}")

                if stop_collecting:
                    break

                # Track if we're finding new posts
                if new_posts_found == 0:
                    no_new_posts_count += 1
                    if no_new_posts_count >= 5:
                        logger.info("No new posts found after 5 scroll attempts, stopping")
                        break
                else:
                    no_new_posts_count = 0

                # Scroll down by a smaller amount (500px) instead of jumping to bottom
                current_scroll = driver.execute_script("return window.pageYOffset;")
                driver.execute_script(f"window.scrollTo(0, {current_scroll + 800});")
                time.sleep(1.5)  # Wait for lazy-loaded content

                # Check if we've reached the bottom
                new_height = driver.execute_script("return document.body.scrollHeight")
                if new_height == last_height and no_new_posts_count >= 3:
                    logger.info("Reached end of page")
                    break
                last_height = new_height

                scroll_attempts += 1
                logger.debug(f"Scroll attempt {scroll_attempts}, found {len(post_urls)} URLs so far")

            logger.info(f"Found {len(post_urls)} post URLs to visit")

            # Visit each post page to get full content
            for i, url in enumerate(post_urls):
                if len(posts) >= limit:
                    break

                try:
                    post_id = re.search(r'/posts/(\d+)', url).group(1)

                    # Skip if already in DB
                    if post_id in existing_post_ids:
                        logger.info(f"Skipping {post_id} - already in DB")
                        continue

                    logger.info(f"[{i+1}/{len(post_urls)}] Visiting post: {url}")
                    driver.get(url)
                    time.sleep(3)

                    # Extract post data from individual page
                    post_data = self._extract_post_from_page(driver, post_id, url)

                    if post_data:
                        # Check date
                        post_time = self._parse_post_date(post_data.get("created_at"))
                        if post_time and post_time < cutoff_time:
                            logger.info(f"Post {post_id} is older than {hours_back} hours, stopping")
                            break

                        posts.append(post_data)
                        logger.info(f"Collected post {post_id}: {post_data.get('content', '')[:60]}...")
                    else:
                        logger.warning(f"Could not extract data from {url}")

                except Exception as e:
                    logger.warning(f"Error processing {url}: {e}")
                    continue

            logger.info(f"Scraped {len(posts)} posts from @{self.username}")
            return posts

        except Exception as e:
            logger.error(f"Error scraping Truth Social: {e}")
            return posts

        finally:
            driver.quit()

    def _extract_post_from_page(self, driver, post_id: str, url: str) -> Optional[Dict[str, Any]]:
        """Extract post data from an individual post page.

        Args:
            driver: Selenium WebDriver instance
            post_id: The post ID
            url: The post URL

        Returns:
            Post data dictionary or None
        """
        try:
            post_data = {
                "post_id": post_id,
                "content": None,
                "created_at": None,
                "url": url,
                "likes_count": 0,
                "reposts_count": 0,
                "replies_count": 0,
                "author_username": self.username,
            }

            # Wait for content to load
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".status__content, [data-testid='status-content'], article"))
                )
            except TimeoutException:
                logger.warning("Timeout waiting for post content")

            # Extract content - use data-testid="markup" which contains the actual post text
            content_selectors = [
                "[data-testid='markup']",  # Primary: actual post text
                "[data-testid='status-content'] p",
                "div.status__content__text",
                ".status__content p",
            ]

            for selector in content_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    for elem in elements:
                        text = elem.text.strip()
                        # Skip if it's just the username or very short
                        if text and len(text) > 5 and text != "Donald J. Trump" and text != self.username:
                            post_data["content"] = text
                            break
                    if post_data["content"]:
                        break
                except Exception:
                    continue

            # If no content found, try data-testid="status-content" container
            if not post_data["content"]:
                try:
                    content_div = driver.find_element(By.CSS_SELECTOR, "[data-testid='status-content']")
                    text = content_div.text.strip()
                    if text and text != "Donald J. Trump":
                        post_data["content"] = text
                except Exception:
                    pass

            # Extract timestamp - try multiple approaches
            time_found = False

            # Method 1: time element with datetime attribute
            try:
                time_elems = driver.find_elements(By.CSS_SELECTOR, "time[datetime]")
                for time_elem in time_elems:
                    datetime_attr = time_elem.get_attribute("datetime")
                    title_attr = time_elem.get_attribute("title")
                    text_content = time_elem.text.strip()

                    if datetime_attr:
                        post_data["created_at"] = datetime_attr
                        time_found = True
                        logger.debug(f"Found time via datetime attr: {datetime_attr}")
                        break
                    elif title_attr:
                        post_data["created_at"] = title_attr
                        time_found = True
                        logger.debug(f"Found time via title attr: {title_attr}")
                        break
                    elif text_content:
                        post_data["created_at"] = text_content
                        time_found = True
                        logger.debug(f"Found time via text: {text_content}")
                        break
            except Exception as e:
                logger.debug(f"Method 1 failed: {e}")

            # Method 2: Look for time in a link that contains the post URL
            if not time_found:
                try:
                    post_links = driver.find_elements(By.CSS_SELECTOR, f"a[href*='/posts/{post_id}']")
                    for link in post_links:
                        # Check if link contains time element
                        try:
                            time_in_link = link.find_element(By.CSS_SELECTOR, "time")
                            datetime_attr = time_in_link.get_attribute("datetime")
                            title_attr = time_in_link.get_attribute("title")
                            if datetime_attr:
                                post_data["created_at"] = datetime_attr
                                time_found = True
                                break
                            elif title_attr:
                                post_data["created_at"] = title_attr
                                time_found = True
                                break
                        except NoSuchElementException:
                            pass

                        # Check link's own text for date pattern
                        link_text = link.text.strip()
                        if re.match(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+', link_text):
                            post_data["created_at"] = link_text
                            time_found = True
                            break
                except Exception as e:
                    logger.debug(f"Method 2 failed: {e}")

            # Method 3: Search for date patterns in page text
            if not time_found:
                try:
                    # Look for date-like text near the post
                    page_text = driver.find_element(By.TAG_NAME, "body").text
                    # Pattern: "Jan 07, 2026, 7:28 AM" or "Jan 07, 2026"
                    date_patterns = [
                        r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}\s+(?:AM|PM))',
                        r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4})',
                    ]
                    for pattern in date_patterns:
                        match = re.search(pattern, page_text)
                        if match:
                            post_data["created_at"] = match.group(1)
                            time_found = True
                            logger.debug(f"Found time via regex: {match.group(1)}")
                            break
                except Exception as e:
                    logger.debug(f"Method 3 failed: {e}")

            # Method 4: Look for relative time text and convert
            if not time_found:
                try:
                    relative_selectors = [
                        ".relative-timestamp",
                        "[class*='time']",
                        "span[class*='ago']",
                    ]
                    for sel in relative_selectors:
                        try:
                            elem = driver.find_element(By.CSS_SELECTOR, sel)
                            text = elem.text.strip().lower()
                            if 'ago' in text or 'hour' in text or 'minute' in text or 'day' in text:
                                post_data["created_at"] = text
                                time_found = True
                                break
                        except NoSuchElementException:
                            continue
                except Exception as e:
                    logger.debug(f"Method 4 failed: {e}")

            if not time_found:
                logger.warning(f"Could not find timestamp for post {post_id}")

            # Extract engagement metrics from detailed post page
            # Format on detail page: <button><span>2.79k</span> ReTruths</button>
            try:
                # Find all buttons with engagement counts
                buttons = driver.find_elements(By.CSS_SELECTOR, "button")
                for button in buttons:
                    btn_text = button.text.strip()

                    # ReTruths: "2.79k ReTruths" or "2.79k\nReTruths"
                    if "ReTruth" in btn_text:
                        # Extract number from text like "2.79k ReTruths"
                        match = re.search(r'([\d.]+[kKmM]?)', btn_text)
                        if match:
                            post_data["reposts_count"] = self._parse_count(match.group(1))
                            logger.debug(f"Found ReTruths: {match.group(1)}")

                    # Likes: "10.2k Likes" or "10.2k\nLikes"
                    elif "Like" in btn_text and "Unlike" not in btn_text:
                        match = re.search(r'([\d.]+[kKmM]?)', btn_text)
                        if match:
                            post_data["likes_count"] = self._parse_count(match.group(1))
                            logger.debug(f"Found Likes: {match.group(1)}")

                    # Replies: Check for reply button with count
                    elif "Repl" in btn_text:
                        match = re.search(r'([\d.]+[kKmM]?)', btn_text)
                        if match:
                            post_data["replies_count"] = self._parse_count(match.group(1))
                            logger.debug(f"Found Replies: {match.group(1)}")

            except Exception as e:
                logger.debug(f"Could not extract engagement metrics: {e}")

            # Only return if we have content
            if post_data["content"]:
                return post_data

            return None

        except Exception as e:
            logger.error(f"Error extracting post from page: {e}")
            return None

    def _parse_count(self, count_str: str) -> int:
        """Parse count string like '5.52k' or '29.3k' to integer.

        Args:
            count_str: Count string (e.g., "5.52k", "1.2M", "123")

        Returns:
            Integer count
        """
        if not count_str:
            return 0

        count_str = count_str.strip().lower()

        try:
            # Handle k (thousands)
            if 'k' in count_str:
                return int(float(count_str.replace('k', '')) * 1000)
            # Handle m (millions)
            elif 'm' in count_str:
                return int(float(count_str.replace('m', '')) * 1000000)
            # Plain number
            else:
                return int(float(count_str))
        except ValueError:
            return 0

    def _parse_post_date(self, date_str: str) -> Optional[datetime]:
        """Parse various date formats from Truth Social.

        Args:
            date_str: Date string in various formats

        Returns:
            datetime object or None
        """
        if not date_str:
            return None

        # Try ISO format first
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass

        # Try "Jan 07, 2026, 7:28 AM" format
        try:
            return datetime.strptime(date_str, "%b %d, %Y, %I:%M %p")
        except ValueError:
            pass

        # Try "Jan 07, 2026" format
        try:
            return datetime.strptime(date_str, "%b %d, %Y")
        except ValueError:
            pass

        return None

    def _dismiss_cookie_popup(self, driver) -> None:
        """Dismiss cookie consent popup if present.

        Args:
            driver: Selenium WebDriver instance
        """
        cookie_selectors = [
            # Truth Social specific - exact match for their cookie modal
            "button.ch2-btn.ch2-allow-all-btn",
            "button[class*='ch2-allow-all']",
            # Common cookie consent button patterns
            "button[id*='accept'], button[id*='Accept']",
            "button[class*='accept'], button[class*='Accept']",
            "[data-testid*='cookie'] button",
            ".cookie-consent button",
            "button[aria-label*='accept'], button[aria-label*='Accept']",
            # Generic "Allow all" type buttons
            "button[id*='allow'], button[class*='allow']",
            # Truth Social specific (if any)
            ".cookie-banner button",
            "#cookie-consent button",
        ]

        for selector in cookie_selectors:
            try:
                buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                for button in buttons:
                    button_text = button.text.lower()
                    if any(word in button_text for word in ['accept', 'allow', 'agree', 'ok', '동의', '허용']):
                        logger.info(f"Dismissing cookie popup: {button.text}")
                        button.click()
                        time.sleep(1)
                        return
            except Exception:
                continue

        # Also try by XPath for text content
        try:
            xpath_patterns = [
                "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'accept')]",
                "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'allow')]",
                "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'agree')]",
            ]
            for xpath in xpath_patterns:
                buttons = driver.find_elements(By.XPATH, xpath)
                for button in buttons:
                    if button.is_displayed():
                        logger.info(f"Dismissing cookie popup (xpath): {button.text}")
                        button.click()
                        time.sleep(1)
                        return
        except Exception:
            pass

        logger.debug("No cookie popup found or already dismissed")

    def _dismiss_modal_popups(self, driver) -> None:
        """Dismiss any modal/video popups that might appear.

        Args:
            driver: Selenium WebDriver instance
        """
        close_selectors = [
            # Common close button patterns
            "button[aria-label='Close'], button[aria-label='close']",
            "button[aria-label='닫기']",
            ".modal-close, .close-modal",
            "[data-testid='close'], [data-testid='modal-close']",
            "button.close, button.dismiss",
            # X button patterns
            "button[class*='close'], button[class*='Close']",
            ".modal button[type='button']",
            "div[role='dialog'] button[aria-label*='close']",
            "div[role='dialog'] button[aria-label*='Close']",
            # Video player close
            ".video-modal button.close",
            ".player-close",
            # Generic modal backdrop click (sometimes clicking outside closes it)
            ".modal-backdrop",
        ]

        for selector in close_selectors:
            try:
                buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                for button in buttons:
                    if button.is_displayed():
                        logger.info(f"Dismissing modal popup: {selector}")
                        button.click()
                        time.sleep(1)
                        return
            except Exception:
                continue

        # Try pressing Escape key to close any modal
        try:
            from selenium.webdriver.common.keys import Keys
            body = driver.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.ESCAPE)
            time.sleep(0.5)
        except Exception:
            pass

        logger.debug("No modal popups found or already dismissed")

    def _find_post_elements(self, driver) -> List:
        """Find post elements on the page.

        Args:
            driver: Selenium WebDriver instance

        Returns:
            List of post WebElements
        """
        # Truth Social is a Mastodon fork, try various selectors
        selectors = [
            "[data-testid='status']",
            "article[data-id]",
            ".status",
            "div[class*='status__wrapper']",
            "article",
        ]

        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    return elements
            except Exception:
                continue

        return []

    def _extract_post_data(self, element, driver) -> Optional[Dict[str, Any]]:
        """Extract post data from a post element.

        Args:
            element: Post WebElement
            driver: Selenium WebDriver instance

        Returns:
            Post data dictionary or None
        """
        try:
            post_data = {
                "post_id": None,
                "content": None,
                "created_at": None,
                "url": None,
                "likes_count": 0,
                "reposts_count": 0,
                "replies_count": 0,
                "author_username": self.username,
            }

            # Extract post ID from element or link
            try:
                # Try to get ID from element attribute
                post_id = element.get_attribute("data-id") or element.get_attribute("data-status-id")

                if not post_id:
                    # Try to find link with post ID
                    links = element.find_elements(By.CSS_SELECTOR, "a[href*='/posts/'], a[href*='/statuses/']")
                    for link in links:
                        href = link.get_attribute("href")
                        if href:
                            # Extract ID from URL like /posts/123456 or /@user/posts/123456
                            match = re.search(r'/(?:posts|statuses)/(\d+)', href)
                            if match:
                                post_id = match.group(1)
                                post_data["url"] = href
                                break

                post_data["post_id"] = post_id

            except Exception as e:
                logger.debug(f"Could not extract post ID: {e}")

            # Extract content text
            try:
                content_selectors = [
                    ".status__content",
                    "[data-testid='status-content']",
                    ".status-content",
                    "div[class*='status__content']",
                    "p",
                ]

                for selector in content_selectors:
                    try:
                        content_element = element.find_element(By.CSS_SELECTOR, selector)
                        if content_element:
                            post_data["content"] = content_element.text.strip()
                            if post_data["content"]:
                                break
                    except NoSuchElementException:
                        continue

                # If still no content, try getting all text
                if not post_data["content"]:
                    post_data["content"] = element.text.strip()

            except Exception as e:
                logger.debug(f"Could not extract content: {e}")

            # Extract timestamp
            try:
                time_selectors = [
                    "time[datetime]",
                    ".relative-time",
                    "[data-testid='relative-time']",
                    "a[href*='/posts/'] time",
                ]

                for selector in time_selectors:
                    try:
                        time_element = element.find_element(By.CSS_SELECTOR, selector)
                        if time_element:
                            datetime_attr = time_element.get_attribute("datetime")
                            if datetime_attr:
                                post_data["created_at"] = datetime_attr
                                break
                            # Try title attribute as fallback
                            title_attr = time_element.get_attribute("title")
                            if title_attr:
                                post_data["created_at"] = title_attr
                                break
                    except NoSuchElementException:
                        continue

            except Exception as e:
                logger.debug(f"Could not extract timestamp: {e}")

            # Extract engagement metrics
            try:
                # Likes
                like_selectors = [
                    "[aria-label*='like'], [aria-label*='Like']",
                    "button[title*='like'], button[title*='Like']",
                    ".status__action-bar button:nth-child(3)",
                ]
                for selector in like_selectors:
                    try:
                        like_element = element.find_element(By.CSS_SELECTOR, selector)
                        like_text = like_element.text.strip()
                        if like_text and like_text.isdigit():
                            post_data["likes_count"] = int(like_text)
                            break
                    except (NoSuchElementException, ValueError):
                        continue

                # Reposts/Reblogs
                repost_selectors = [
                    "[aria-label*='repost'], [aria-label*='Repost'], [aria-label*='boost'], [aria-label*='Boost']",
                    "button[title*='repost'], button[title*='boost']",
                    ".status__action-bar button:nth-child(2)",
                ]
                for selector in repost_selectors:
                    try:
                        repost_element = element.find_element(By.CSS_SELECTOR, selector)
                        repost_text = repost_element.text.strip()
                        if repost_text and repost_text.isdigit():
                            post_data["reposts_count"] = int(repost_text)
                            break
                    except (NoSuchElementException, ValueError):
                        continue

                # Replies
                reply_selectors = [
                    "[aria-label*='reply'], [aria-label*='Reply']",
                    "button[title*='reply']",
                    ".status__action-bar button:nth-child(1)",
                ]
                for selector in reply_selectors:
                    try:
                        reply_element = element.find_element(By.CSS_SELECTOR, selector)
                        reply_text = reply_element.text.strip()
                        if reply_text and reply_text.isdigit():
                            post_data["replies_count"] = int(reply_text)
                            break
                    except (NoSuchElementException, ValueError):
                        continue

            except Exception as e:
                logger.debug(f"Could not extract engagement metrics: {e}")

            # Build URL if not already set
            if not post_data["url"] and post_data["post_id"]:
                post_data["url"] = f"{self.BASE_URL}/@{self.username}/posts/{post_data['post_id']}"

            # Only return if we have meaningful content
            if post_data["content"] and len(post_data["content"]) > 0:
                return post_data

            return None

        except Exception as e:
            logger.error(f"Error extracting post data: {e}")
            return None

    def save_posts_to_file(
        self,
        posts: List[Dict[str, Any]],
        output_path: Path,
    ) -> None:
        """Save scraped posts to a JSON file.

        Args:
            posts: List of post dictionaries
            output_path: Path to output JSON file
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(posts, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"Saved {len(posts)} posts to {output_path}")
