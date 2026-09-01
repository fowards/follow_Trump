"""Trump Truth Social post collector from shorts_db."""

import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any

from channels.base.collector import BaseCollector
from core.database import get_shorts_db

logger = logging.getLogger(__name__)


def parse_truth_social_date(date_str: str) -> datetime:
    """Parse Truth Social date format like 'Jan 07, 2026, 7:28 AM'.

    Args:
        date_str: Date string from Truth Social

    Returns:
        Parsed datetime object or None if parsing fails
    """
    if not date_str:
        return None

    try:
        # Format: "Jan 07, 2026, 7:28 AM" or "Jan 07, 2026, 12:30 PM"
        return datetime.strptime(date_str, "%b %d, %Y, %I:%M %p")
    except ValueError:
        try:
            # Try without time
            return datetime.strptime(date_str.split(",")[0] + date_str.split(",")[1], "%b %d %Y")
        except ValueError:
            logger.warning(f"Could not parse date: {date_str}")
            return None


class TrumpPostCollector(BaseCollector):
    """Collects Trump Truth Social posts from shorts database.

    Unlike ElonTweetCollector which reads from trace_alpha_db,
    this collector reads from shorts_db where posts were saved
    by the Truth Social scraper (scripts/collect_truth_social.py).
    """

    SOURCE_TYPE = "truth_social"
    AUTHOR_USERNAME = "realdonaldtrump"

    def __init__(self):
        super().__init__(channel_code="trump_today")
        self.shorts_db = get_shorts_db()

    async def collect(
        self,
        hours_back: int = 24,
        limit: int = 50,
        target_date: str = None,
    ) -> List[Dict[str, Any]]:
        """Fetch recent Trump posts from shorts database.

        Posts are collected by the scraper and stored in content_sources table.
        This method reads those collected posts.

        Args:
            hours_back: How many hours back to look (used when target_date is None)
            limit: Maximum posts to fetch
            target_date: Specific date to get posts for (YYYY-MM-DD format)

        Returns:
            List of post dictionaries
        """
        if target_date:
            self.logger.info(f"Collecting Trump posts from {target_date}...")
        else:
            self.logger.info(f"Collecting Trump posts from last {hours_back} hours...")

        try:
            # Get channel ID
            channel = self.shorts_db.get_channel("trump_today")
            if not channel:
                self.logger.error("Channel 'trump_today' not found")
                return []

            channel_id = channel["id"]

            # Query posts from content_sources
            # Note: Use collected_at for filtering since created_at is in human-readable format
            if target_date:
                # Get posts collected on a specific date
                posts = self.shorts_db.execute_query(
                    """
                    SELECT id, source_id, content_text, source_url, content_metadata, collected_at
                    FROM content_sources
                    WHERE channel_id = %s
                      AND source_type = %s
                      AND collected_at::date = %s::date
                    ORDER BY collected_at DESC
                    LIMIT %s
                    """,
                    (channel_id, self.SOURCE_TYPE, target_date, limit),
                )
            else:
                # Get posts collected in last N hours
                posts = self.shorts_db.execute_query(
                    """
                    SELECT id, source_id, content_text, source_url, content_metadata, collected_at
                    FROM content_sources
                    WHERE channel_id = %s
                      AND source_type = %s
                      AND collected_at >= NOW() - INTERVAL '%s hours'
                    ORDER BY collected_at DESC
                    LIMIT %s
                    """,
                    (channel_id, self.SOURCE_TYPE, hours_back, limit),
                )

            self.logger.info(f"Found {len(posts)} posts in database")

            # Transform to standard format
            result = []
            now = datetime.now()
            cutoff_time = now - timedelta(hours=hours_back)

            for post in posts:
                metadata = post.get("content_metadata") or {}
                created_at_str = metadata.get("created_at")

                # Filter by actual post creation date (not collection date)
                if created_at_str:
                    post_date = parse_truth_social_date(created_at_str)
                    if post_date and post_date < cutoff_time:
                        # Skip posts older than hours_back from when they were POSTED
                        continue

                result.append({
                    "id": post["id"],  # Database ID for reference
                    "source_id": post["source_id"],
                    "content": post["content_text"],
                    "content_text": post["content_text"],  # Include both keys for compatibility
                    "created_at": created_at_str,
                    "likes_count": metadata.get("likes_count", 0),
                    "reposts_count": metadata.get("reposts_count", 0),
                    "replies_count": metadata.get("replies_count", 0),
                    "author_username": metadata.get("author_username", self.AUTHOR_USERNAME),
                    "url": post.get("source_url") or f"https://truthsocial.com/@{self.AUTHOR_USERNAME}/posts/{post['source_id']}",
                })

            self.logger.info(f"After date filtering: {len(result)} posts within {hours_back} hours")
            return result

        except Exception as e:
            self.logger.error(f"Failed to collect posts: {e}")
            return []

    async def collect_and_save(
        self,
        hours_back: int = 24,
        limit: int = 50,
    ) -> int:
        """For Trump channel, posts are already saved by the scraper.

        This method just returns the count of available posts.
        The actual saving is done by scripts/collect_truth_social.py.

        Args:
            hours_back: How many hours back to look
            limit: Maximum posts to fetch

        Returns:
            Number of available posts
        """
        posts = await self.collect(hours_back=hours_back, limit=limit)
        return len(posts)

    async def get_collected_posts(
        self,
        limit: int = 50,
        hours_back: int = 48,
    ) -> List[Dict[str, Any]]:
        """Get previously collected posts from shorts database.

        Args:
            limit: Maximum posts to return
            hours_back: How many hours back to look

        Returns:
            List of collected post records
        """
        return await self.collect(hours_back=hours_back, limit=limit)
