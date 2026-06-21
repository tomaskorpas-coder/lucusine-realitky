"""
run_scrapers.py — CLI runner for all scrapers.

Usage:
    python run_scrapers.py                    # run all scrapers
    python run_scrapers.py --scraper bazos    # run only Bazos
    python run_scrapers.py --scraper nehnutelnosti
    python run_scrapers.py --dry-run          # parse but don't save to DB
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import argparse
import logging

from models import init_db, SessionLocal
from utils.engine import upsert_listing
from scrapers import ALL_SCRAPERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("runner")


def run(scraper_filter: str | None = None, dry_run: bool = False) -> None:
    init_db()
    db = SessionLocal()

    total_inserted = 0
    total_merged = 0
    total_errors = 0

    scrapers_to_run = [
        S for S in ALL_SCRAPERS
        if scraper_filter is None or scraper_filter.lower() in S.source_name.lower()
    ]

    if not scrapers_to_run:
        logger.error("No scrapers matched filter: %r", scraper_filter)
        sys.exit(1)

    for ScraperClass in scrapers_to_run:
        scraper = ScraperClass()
        logger.info("▶ Starting scraper: %s", scraper.source_name)

        try:
            for raw in scraper.iter_listings():
                if dry_run:
                    logger.info("[DRY-RUN] Would upsert: %s %s %.0fm² %.0f€",
                                raw.location_city, raw.property_type,
                                raw.area_sqm, raw.absolute_price)
                    continue
                try:
                    _, created = upsert_listing(db, raw)
                    if created:
                        total_inserted += 1
                    else:
                        total_merged += 1
                except Exception as exc:
                    total_errors += 1
                    logger.warning("Upsert error for %s: %s", raw.source_url, exc)
        except KeyboardInterrupt:
            logger.info("Interrupted by user.")
            break
        except Exception as exc:
            logger.error("Scraper %s crashed: %s", scraper.source_name, exc, exc_info=True)

    db.close()
    logger.info(
        "Done. Inserted=%d | Merged=%d | Errors=%d",
        total_inserted, total_merged, total_errors,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Real Estate Scraper Runner")
    parser.add_argument("--scraper", type=str, default=None,
                        help="Filter by scraper name (bazos, nehnutelnosti)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse listings without saving to DB")
    args = parser.parse_args()
    run(scraper_filter=args.scraper, dry_run=args.dry_run)
