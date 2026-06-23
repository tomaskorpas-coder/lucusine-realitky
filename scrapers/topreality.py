"""
scrapers/topreality.py — Scraper for Topreality.sk

URL pattern: https://www.topreality.sk/{type}/predaj/nitriansky-kraj/{page}/
Topreality uses server-side rendered HTML, making it well-suited for BeautifulSoup.
"""

from __future__ import annotations

import logging
import re
from typing import Generator

from .base import BaseScraper, is_nitra_district

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils.engine import RawListing

logger = logging.getLogger(__name__)

PROPERTY_TYPE_MAP = {
    "byty": "byt",
    "domy": "dom",
    "pozemky": "pozemok",
}


class TopRealityScraper(BaseScraper):
    """
    Scraper for https://www.topreality.sk
    Targets Nitriansky kraj, sale listings.
    """

    source_name = "topreality.sk"
    base_url = "https://www.topreality.sk"
    REGION_SLUG = "nitriansky-kraj"
    SEARCH_TYPES = list(PROPERTY_TYPE_MAP.keys())

    def __init__(self):
        super().__init__(delay_range=(2.0, 4.5))
        self._seen_urls: set[str] = set()

    @property
    def seen_urls(self) -> set[str]:
        return self._seen_urls

    def iter_listings(self) -> Generator[RawListing, None, None]:
        for ptype_slug in self.SEARCH_TYPES:
            yield from self._scrape_type(ptype_slug)

    def _scrape_type(self, ptype_slug: str) -> Generator[RawListing, None, None]:
        property_type = PROPERTY_TYPE_MAP[ptype_slug]
        page = 1
        consecutive_empty = 0

        while consecutive_empty < 2:
            # Topreality URL formats (try both patterns):
            # /byty/predaj/nitriansky-kraj/
            # /byty/predaj/nitriansky-kraj/strana-2/
            if page == 1:
                url = f"{self.base_url}/{ptype_slug}/predaj/{self.REGION_SLUG}/"
            else:
                url = f"{self.base_url}/{ptype_slug}/predaj/{self.REGION_SLUG}/strana-{page}/"

            logger.info("[topreality.sk] Fetching page %d — %s", page, url)
            soup = self._fetch(url)

            if soup is None:
                logger.warning("[topreality.sk] No response on page %d.", page)
                break

            # Topreality listing cards — multiple selector attempts
            cards = (
                soup.select("article.item")
                or soup.select("div.item")
                or soup.select("li.item")
                or soup.select("div.property-item")
                or soup.select("article.property")
                or soup.select("div.listing-card")
                or soup.select("[class*='item'][class*='nehnutelnost']")
            )

            if not cards:
                consecutive_empty += 1
                logger.info(
                    "[topreality.sk] No cards on page %d (consecutive empty: %d).",
                    page, consecutive_empty,
                )
                page += 1
                continue

            consecutive_empty = 0
            parsed = 0
            for card in cards:
                raw = self._parse_card(card, property_type)
                if raw:
                    self._seen_urls.add(raw.source_url)
                    parsed += 1
                    yield raw

            logger.debug("[topreality.sk] Page %d: parsed %d/%d cards", page, parsed, len(cards))

            # Check for next page
            has_next = bool(
                soup.select_one("a[rel='next']")
                or soup.select_one("a.next")
                or soup.select_one("li.next a")
                or soup.select_one(f"a[href*='strana-{page + 1}']")
            )
            if not has_next:
                logger.info("[topreality.sk] No next page after page %d.", page)
                break
            page += 1

    def _parse_card(self, card, property_type: str) -> RawListing | None:
        try:
            # Link — Topreality links go to /nehnutelnost/xxxxx.html
            link_el = (
                card.select_one("a.title")
                or card.select_one("h2 a")
                or card.select_one("h3 a")
                or card.select_one("a[href*='/nehnutelnost/']")
                or card.select_one("a[href*='.html']")
                or card.find("a", href=re.compile(r"\.html$"))
            )
            if not link_el:
                return None

            listing_url = link_el.get("href", "")
            if not listing_url.startswith("http"):
                listing_url = self.base_url + listing_url
            title_text = link_el.get_text(strip=True)

            # Location — Topreality typically shows "Nitra, Chrenová" or "Nitra - Zobor"
            loc_el = (
                card.select_one("span.location")
                or card.select_one("span.address")
                or card.select_one("div.location")
                or card.select_one("p.location")
                or card.select_one("span.mesto")
                or card.select_one("[class*='location']")
                or card.select_one("[class*='address']")
            )
            city_raw = loc_el.get_text(strip=True) if loc_el else title_text
            city = self._extract_city(city_raw) or self._extract_city(title_text)
            if not city or not is_nitra_district(city):
                return None

            # Price
            price_el = (
                card.select_one("span.price")
                or card.select_one("div.price")
                or card.select_one("strong.price")
                or card.select_one("p.price")
                or card.select_one("[class*='price']")
            )
            if not price_el:
                return None
            price_text = price_el.get_text()
            if "dohod" in price_text.lower():
                return None
            price = self.parse_price(price_text)
            if not price or price < 1_000:
                return None

            # Area
            area_el = (
                card.select_one("span.area")
                or card.select_one("div.area")
                or card.select_one("[class*='area']")
                or card.select_one("[class*='plocha']")
            )
            if area_el:
                area = self.parse_area(area_el.get_text())
            else:
                m = re.search(r"(\d+[\.,]?\d*)\s*m[²2]", title_text)
                area = float(m.group(1).replace(",", ".")) if m else None

            if not area or area < 5:
                return None

            return RawListing(
                source_url=listing_url,
                location_city=city,
                property_type=property_type,
                area_sqm=area,
                absolute_price=price,
                subtype=self._extract_subtype(title_text),
            )

        except Exception as exc:
            logger.debug("[topreality.sk] Card parse error: %s", exc)
            return None

    @staticmethod
    def _extract_city(text: str) -> str | None:
        """Extract city from 'Nitra - Chrenová' or 'Nitra, Zobor'."""
        city = re.split(r"[-,–\|]", text)[0].strip()
        return city if len(city) > 1 else None

    @staticmethod
    def _extract_subtype(title: str) -> str | None:
        m = re.search(r"(\d+)-izbov[ýáé]", title, re.IGNORECASE)
        if m:
            return f"{m.group(1)}-izbový byt"
        if re.search(r"rodinný dom", title, re.IGNORECASE):
            return "rodinný dom"
        if re.search(r"chata|chalupa", title, re.IGNORECASE):
            return "chata/chalupa"
        return None
