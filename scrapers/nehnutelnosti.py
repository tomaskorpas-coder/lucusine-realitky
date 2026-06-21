"""
scrapers/nehnutelnosti.py — Scraper skeleton for Nehnutelnosti.sk

NOTE: This is a structural skeleton with realistic selectors based on the site's
HTML structure. In production you may need to adjust CSS selectors if the site
updates its markup. Enable via --scraper nehnutelnosti in the CLI runner.
"""

from __future__ import annotations

import logging
import re
from typing import Generator

from .base import BaseScraper, NITRA_DISTRICT_CITIES

# Import RawListing from the engine
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils.engine import RawListing

logger = logging.getLogger(__name__)

PROPERTY_TYPE_MAP = {
    "byty": "byt",
    "domy": "dom",
    "pozemky": "pozemok",
    "komerčné": "komercia",
    "ostatné": "iny",
}


class NehnutelnostiScraper(BaseScraper):
    """
    Scraper for https://www.nehnutelnosti.sk
    Targets the Nitriansky kraj region, all property types.
    """

    source_name = "nehnutelnosti.sk"
    base_url = "https://www.nehnutelnosti.sk"

    # Region slug for Nitriansky kraj on nehnutelnosti.sk
    REGION_SLUG = "nitriansky-kraj"
    SEARCH_TYPES = ["byty", "domy", "pozemky"]

    def iter_listings(self) -> Generator[RawListing, None, None]:
        for ptype_slug in self.SEARCH_TYPES:
            yield from self._scrape_type(ptype_slug)

    def _scrape_type(self, ptype_slug: str) -> Generator[RawListing, None, None]:
        property_type = PROPERTY_TYPE_MAP.get(ptype_slug, "iny")
        page = 1

        while True:
            url = (
                f"{self.base_url}/{ptype_slug}/predaj/"
                f"{self.REGION_SLUG}/?page={page}"
            )
            logger.info("[nehnutelnosti.sk] Fetching page %d — %s", page, url)
            soup = self._fetch(url)

            if soup is None:
                logger.warning("[nehnutelnosti.sk] Empty response, stopping pagination")
                break

            # ── Listing cards ─────────────────────────────────────────────
            cards = soup.select("div.advertisement-item, article.property-card")
            if not cards:
                logger.info("[nehnutelnosti.sk] No cards found on page %d, done.", page)
                break

            for card in cards:
                raw = self._parse_card(card, property_type)
                if raw:
                    yield raw

            # ── Pagination ─────────────────────────────────────────────────
            next_btn = soup.select_one("a.pagination__next, a[rel='next']")
            if not next_btn:
                break
            page += 1

    def _parse_card(self, card, property_type: str) -> RawListing | None:
        try:
            # Title / link
            title_el = card.select_one("a.title, h2.advertisement-title a, a.property-title")
            if not title_el:
                return None
            listing_url = title_el.get("href", "")
            if listing_url and not listing_url.startswith("http"):
                listing_url = self.base_url + listing_url
            title_text = title_el.get_text(strip=True)

            # City extraction — try dedicated location element first
            city_el = card.select_one(
                "span.location, span.address, div.advertisement-location, p.city"
            )
            city_raw = city_el.get_text(strip=True) if city_el else title_text
            city = self._extract_city(city_raw)
            if not city:
                return None

            # Price
            price_el = card.select_one(
                "span.price, strong.price, div.advertisement-price, span.property-price"
            )
            if not price_el:
                return None
            price = self.parse_price(price_el.get_text())
            if not price or price < 1000:
                return None

            # Area
            area_el = card.select_one(
                "span.area, span.parameters, li.area, span.property-area"
            )
            if not area_el:
                # Try extracting from title e.g. "3-izbový byt 72 m²"
                area_match = re.search(r"(\d+)\s*m[²2]", title_text)
                area = float(area_match.group(1)) if area_match else None
            else:
                area = self.parse_area(area_el.get_text())

            if not area or area < 5:
                return None

            # Subtype from title
            subtype = self._extract_subtype(title_text)

            return RawListing(
                source_url=listing_url,
                location_city=city,
                property_type=property_type,
                area_sqm=area,
                absolute_price=price,
                subtype=subtype,
            )

        except Exception as exc:
            logger.debug("[nehnutelnosti.sk] Card parse error: %s", exc)
            return None

    @staticmethod
    def _extract_city(text: str) -> str | None:
        """Pull the city name from address text like 'Nitra - Chrenová' → 'Nitra'."""
        # Take the part before a dash or comma
        city = re.split(r"[-,–]", text)[0].strip()
        return city if len(city) > 1 else None

    @staticmethod
    def _extract_subtype(title: str) -> str | None:
        patterns = [
            r"\d+-izbov[ýáé]\s+\w+",   # 3-izbový byt
            r"rodinný dom",
            r"chata|chalupa",
            r"pozemok\s+\w+",
        ]
        for pat in patterns:
            m = re.search(pat, title, re.IGNORECASE)
            if m:
                return m.group(0)
        return None
