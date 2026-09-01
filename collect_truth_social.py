#!/usr/bin/env python3
"""Collect Truth Social posts and save to database.

Usage:
    python scripts/collect_truth_social.py
    python scripts/collect_truth_social.py --dry-run
    python scripts/collect_truth_social.py --limit 20 --hours 48
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.scraper import TruthSocialScraper
from core.database import get_shorts_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def collect_truth_social(
    limit: int = 50,
    hours_back: int = 24,
    dry_run: bool = False,
    headless: bool = True,
) -> dict:
    """Collect Truth Social posts and save to database.

    Args:
        limit: Maximum number of posts to collect
        hours_back: Only collect posts from the last N hours
        dry_run: If True, don't save to database
        headless: Run browser in headless mode

    Returns:
        Collection result dictionary
    """
    result = {
        "success": False,
        "posts_scraped": 0,
        "posts_saved": 0,
        "posts_updated": 0,
        "errors": [],
    }

    try:
        # Get existing post IDs from database to avoid duplicates
        existing_post_ids = set()
        if not dry_run:
            try:
                db = get_shorts_db()
                channel = db.get_channel("trump_today")
                if channel:
                    existing_posts = db.execute_query(
                        """
                        SELECT source_id FROM content_sources
                        WHERE channel_id = %s AND source_type = 'truth_social'
                        """,
                        (channel["id"],),
                    )
                    existing_post_ids = {p["source_id"] for p in existing_posts}
                    logger.info(f"Found {len(existing_post_ids)} existing posts in DB")
            except Exception as e:
                logger.warning(f"Could not fetch existing posts: {e}")

        # Initialize scraper
        scraper = TruthSocialScraper(username="realDonaldTrump")

        # Scrape posts
        logger.info(f"Scraping Truth Social (limit={limit}, hours_back={hours_back})")
        posts = scraper.scrape_posts(
            limit=limit,
            hours_back=hours_back,
            headless=headless,
            existing_post_ids=existing_post_ids,
        )

        result["posts_scraped"] = len(posts)
        logger.info(f"Scraped {len(posts)} posts")

        if not posts:
            logger.warning("No posts collected")
            result["success"] = True  # Not an error, just no new posts
            return result

        # Save to file for debugging
        if dry_run:
            output_dir = Path("temp") / "truth_social"
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = output_dir / f"posts_{timestamp}.json"
            scraper.save_posts_to_file(posts, output_file)
            logger.info(f"Dry run: saved to {output_file}")
            result["success"] = True
            return result

        # Get database and channel
        db = get_shorts_db()
        channel = db.get_channel("trump_today")

        if not channel:
            logger.error("Channel 'trump_today' not found in database")
            result["errors"].append("Channel not found")
            return result

        channel_id = channel["id"]

        # Save posts to database
        for post in posts:
            try:
                if not post.get("post_id"):
                    logger.warning(f"Skipping post without ID: {post.get('content', '')[:50]}")
                    continue

                # Prepare metadata
                metadata = {
                    "likes_count": post.get("likes_count", 0),
                    "reposts_count": post.get("reposts_count", 0),
                    "replies_count": post.get("replies_count", 0),
                    "author_username": post.get("author_username", "realDonaldTrump"),
                    "created_at": post.get("created_at"),
                    "scraped_at": datetime.now().isoformat(),
                }

                # Save to content_sources
                saved = db.save_content_source(
                    channel_id=channel_id,
                    source_type="truth_social",
                    source_id=str(post["post_id"]),
                    content_text=post.get("content", ""),
                    source_url=post.get("url"),
                    content_metadata=metadata,
                )

                if saved:
                    result["posts_saved"] += 1
                    logger.debug(f"Saved post {post['post_id']}")

            except Exception as e:
                logger.error(f"Error saving post {post.get('post_id')}: {e}")
                result["errors"].append(str(e))

        result["success"] = True
        logger.info(f"Collection complete: {result['posts_saved']} posts saved")

    except Exception as e:
        logger.error(f"Collection failed: {e}")
        result["errors"].append(str(e))

    return result


def main():
    parser = argparse.ArgumentParser(description="Collect Truth Social posts")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of posts to collect (default: 50)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Hours to look back (default: 24)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't save to database, just save to file",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Show browser window (for debugging)",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("TRUTH SOCIAL COLLECTION")
    logger.info(f"Limit: {args.limit}")
    logger.info(f"Hours back: {args.hours}")
    logger.info(f"Dry run: {args.dry_run}")
    logger.info(f"Headless: {not args.no_headless}")
    logger.info("=" * 60)

    result = collect_truth_social(
        limit=args.limit,
        hours_back=args.hours,
        dry_run=args.dry_run,
        headless=not args.no_headless,
    )

    logger.info("=" * 60)
    logger.info("COLLECTION RESULT")
    logger.info(f"Success: {result['success']}")
    logger.info(f"Posts scraped: {result['posts_scraped']}")
    logger.info(f"Posts saved: {result['posts_saved']}")
    if result["errors"]:
        logger.info(f"Errors: {len(result['errors'])}")
        for err in result["errors"]:
            logger.error(f"  - {err}")
    logger.info("=" * 60)

    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
