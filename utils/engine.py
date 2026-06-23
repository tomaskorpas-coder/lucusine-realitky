"""
utils/engine.py — Deduplication + Mathematical Analytics Engine

Implements:
  - Intelligent listing deduplication (location + type + area ±2% + price ±2%)
  - IQR-based outlier removal before median calculation
  - Hot Deal detection: P_sqm ≤ 0.80 × median_segment
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

# Import models relative to project root
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from models import Listing, PriceHistory

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
AREA_TOLERANCE = 0.02        # ±2 % tolerance for area match
PRICE_TOLERANCE = 0.02       # ±2 % tolerance for absolute price match
HOT_DEAL_THRESHOLD = 0.80    # listing must be ≤ 80 % of segment median
IQR_MULTIPLIER = 1.5         # Tukey fences multiplier
MIN_SEGMENT_SIZE = 3         # minimum listings in a segment for median to be meaningful


# ── Dataclass for raw scraped input ──────────────────────────────────────────
@dataclass
class RawListing:
    source_url: str
    location_city: str
    property_type: str          # byt | dom | pozemok | komercia | iny
    area_sqm: float
    absolute_price: float
    location_district: Optional[str] = None
    subtype: Optional[str] = None
    condition: Optional[str] = None

    def __post_init__(self):
        if self.area_sqm <= 0:
            raise ValueError(f"area_sqm must be positive, got {self.area_sqm}")
        if self.absolute_price <= 0:
            raise ValueError(f"absolute_price must be positive, got {self.absolute_price}")
        self.price_per_sqm = round(self.absolute_price / self.area_sqm, 2)


# ── Deduplication ─────────────────────────────────────────────────────────────
def _within(a: float, b: float, tol: float) -> bool:
    """Return True if |a-b| / mean(a,b) ≤ tol."""
    if a == 0 and b == 0:
        return True
    mean = (a + b) / 2
    return abs(a - b) / mean <= tol


def find_duplicate(db: Session, raw: RawListing) -> Optional[Listing]:
    """
    Search for an existing active listing that matches raw on:
      - location_city (exact, case-insensitive)
      - property_type (exact)
      - area_sqm within ±AREA_TOLERANCE
      - absolute_price within ±PRICE_TOLERANCE
    Returns the first matching record or None.
    """
    candidates = (
        db.query(Listing)
        .filter(
            Listing.location_city.ilike(raw.location_city),
            Listing.property_type == raw.property_type,
        )
        .all()
    )
    for c in candidates:
        if _within(c.area_sqm, raw.area_sqm, AREA_TOLERANCE) and \
           _within(c.absolute_price, raw.absolute_price, PRICE_TOLERANCE):
            return c
    return None


def upsert_listing(db: Session, raw: RawListing) -> tuple[Listing, bool]:
    """
    Insert new listing or merge into existing duplicate.
    Returns (listing, created: bool).
    Records a PriceHistory entry on price change.
    """
    duplicate = find_duplicate(db, raw)

    if duplicate:
        # ── Merge: add URL, update price if changed ───────────────────────
        duplicate.add_source_url(raw.source_url)
        duplicate.status = "active"

        if abs(duplicate.absolute_price - raw.absolute_price) > 1:
            # Record historical change
            history = PriceHistory(
                listing_id=duplicate.id,
                old_price=duplicate.absolute_price,
                new_price=raw.absolute_price,
                old_price_per_sqm=duplicate.price_per_sqm,
                new_price_per_sqm=raw.price_per_sqm,
                note="price updated during dedup merge",
            )
            db.add(history)
            duplicate.absolute_price = raw.absolute_price
            duplicate.price_per_sqm = raw.price_per_sqm

        duplicate.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(duplicate)
        logger.info("Merged duplicate → id=%d", duplicate.id)
        return duplicate, False

    else:
        # ── Insert new ────────────────────────────────────────────────────
        listing = Listing(
            location_city=raw.location_city,
            location_district=raw.location_district,
            property_type=raw.property_type,
            subtype=raw.subtype,
            condition=raw.condition,
            area_sqm=raw.area_sqm,
            absolute_price=raw.absolute_price,
            price_per_sqm=raw.price_per_sqm,
            status="active",
            source_urls="[]",
        )
        listing.set_source_urls([raw.source_url])
        db.add(listing)
        db.flush()   # get id before history insert

        # First price log
        history = PriceHistory(
            listing_id=listing.id,
            old_price=None,
            new_price=raw.absolute_price,
            old_price_per_sqm=None,
            new_price_per_sqm=raw.price_per_sqm,
            note="initial insert",
        )
        db.add(history)
        db.commit()
        db.refresh(listing)
        logger.info("Inserted new listing id=%d", listing.id)
        return listing, True


# ── Mathematical Analytics Engine ─────────────────────────────────────────────
@dataclass
class SegmentStats:
    segment_key: str
    n_total: int
    n_after_iqr: int
    lower_fence: float
    upper_fence: float
    clean_median_price_per_sqm: float
    hot_deal_threshold: float        # = 0.80 × clean_median


def _remove_outliers_iqr(values: np.ndarray) -> np.ndarray:
    """
    Apply Tukey fences (IQR method) to remove outliers.
    Returns cleaned array. If fewer than 4 values, returns original (not enough data).
    """
    if len(values) < 4:
        return values
    q1, q3 = np.percentile(values, [25, 75])
    iqr = q3 - q1
    lower = q1 - IQR_MULTIPLIER * iqr
    upper = q3 + IQR_MULTIPLIER * iqr
    return values[(values >= lower) & (values <= upper)]


def compute_segment_stats(df_segment: pd.DataFrame, segment_key: str) -> Optional[SegmentStats]:
    """
    Given a DataFrame slice of a micro-segment, compute clean median and fences.
    Returns None if segment is too small to be meaningful.
    """
    prices = df_segment["price_per_sqm"].dropna().values.astype(float)
    n_total = len(prices)

    if n_total < MIN_SEGMENT_SIZE:
        return None

    # IQR fences for reporting
    if n_total >= 4:
        q1, q3 = np.percentile(prices, [25, 75])
        iqr = q3 - q1
        lower_fence = q1 - IQR_MULTIPLIER * iqr
        upper_fence = q3 + IQR_MULTIPLIER * iqr
    else:
        lower_fence = prices.min()
        upper_fence = prices.max()

    clean_prices = _remove_outliers_iqr(prices)
    n_after = len(clean_prices)

    if n_after == 0:
        return None  # edge case: entire segment was outliers

    clean_median = float(np.median(clean_prices))

    return SegmentStats(
        segment_key=segment_key,
        n_total=n_total,
        n_after_iqr=n_after,
        lower_fence=round(lower_fence, 2),
        upper_fence=round(upper_fence, 2),
        clean_median_price_per_sqm=round(clean_median, 2),
        hot_deal_threshold=round(HOT_DEAL_THRESHOLD * clean_median, 2),
    )


def get_all_listings_df(db: Session) -> pd.DataFrame:
    """Load all active listings into a DataFrame."""
    rows = db.query(Listing).filter(Listing.status == "active").all()
    if not rows:
        return pd.DataFrame()

    records = []
    for r in rows:
        records.append({
            "id": r.id,
            "source_urls": r.get_source_urls(),
            "location_city": r.location_city,
            "location_district": r.location_district,
            "property_type": r.property_type,
            "subtype": r.subtype,
            "condition": r.condition,
            "area_sqm": r.area_sqm,
            "absolute_price": r.absolute_price,
            "price_per_sqm": r.price_per_sqm,
            "status": r.status,
            "internal_notes": r.internal_notes or "",
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        })
    return pd.DataFrame(records)


def mark_stale_listings(db: Session, source_domain: str, seen_urls: set[str]) -> int:
    """
    After a scraper run, mark as inactive any listing whose source URLs
    all belong to source_domain but NONE were found in this run.

    Only marks inactive listings that originated exclusively from this portal.
    Listings merged from multiple portals are only marked inactive when
    ALL their portal URLs have gone stale.

    Returns count of listings marked inactive.
    """
    if not seen_urls:
        logger.warning(
            "mark_stale_listings called with empty seen_urls for %s — skipping to avoid false positives.",
            source_domain,
        )
        return 0

    domain_pattern = source_domain.replace("www.", "")
    active_listings = db.query(Listing).filter(Listing.status == "active").all()
    marked = 0

    for listing in active_listings:
        urls = listing.get_source_urls()
        if not urls:
            continue

        # URLs from THIS portal
        portal_urls = [u for u in urls if domain_pattern in u]
        if not portal_urls:
            continue  # listing doesn't belong to this portal

        # URLs from OTHER portals
        other_urls = [u for u in urls if domain_pattern not in u]

        # Any of this portal's URLs seen in current run?
        any_seen = any(u in seen_urls for u in portal_urls)
        if any_seen:
            continue  # still active on this portal

        # If listing exists on other portals too, don't mark inactive yet
        if other_urls:
            continue

        # Exclusively on this portal and not found → mark inactive
        history = PriceHistory(
            listing_id=listing.id,
            old_price=listing.absolute_price,
            new_price=listing.absolute_price,
            old_price_per_sqm=listing.price_per_sqm,
            new_price_per_sqm=listing.price_per_sqm,
            note=f"marked inactive — not found on {source_domain}",
        )
        db.add(history)
        listing.status = "inactive"
        listing.updated_at = datetime.utcnow()
        marked += 1

    if marked:
        db.commit()
        logger.info("Marked %d listings as inactive (not found on %s)", marked, source_domain)
    return marked


def detect_hot_deals(db: Session) -> pd.DataFrame:
    """
    Full pipeline:
      1. Load active listings
      2. Group by micro-segment (city + property_type)
      3. For each segment: IQR-clean → compute median → threshold
      4. Flag listings where P_sqm ≤ 0.80 × clean_median
    Returns DataFrame with hot deal listings plus analytical columns.
    """
    df = get_all_listings_df(db)
    if df.empty:
        return pd.DataFrame()

    results = []
    groups = df.groupby(["location_city", "property_type"], sort=False)

    for (city, ptype), group in groups:
        segment_key = f"{city} | {ptype}"
        stats = compute_segment_stats(group, segment_key)
        if stats is None:
            continue

        hot_deals = group[group["price_per_sqm"] <= stats.hot_deal_threshold].copy()
        if hot_deals.empty:
            continue

        hot_deals["segment_key"] = segment_key
        hot_deals["segment_n_total"] = stats.n_total
        hot_deals["segment_n_clean"] = stats.n_after_iqr
        hot_deals["clean_median_sqm"] = stats.clean_median_price_per_sqm
        hot_deals["hot_deal_threshold_sqm"] = stats.hot_deal_threshold
        hot_deals["discount_pct"] = (
            (stats.clean_median_price_per_sqm - hot_deals["price_per_sqm"])
            / stats.clean_median_price_per_sqm * 100
        ).round(1)
        results.append(hot_deals)

    if not results:
        return pd.DataFrame()
    return pd.concat(results, ignore_index=True)
