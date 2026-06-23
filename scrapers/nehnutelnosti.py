"""
scrapers/nehnutelnosti.py — Scraper for Nehnutelnosti.sk

URL pattern: https://www.nehnutelnosti.sk/{type}/predaj/nitriansky-kraj/?page={n}
The site partially renders via JS. We rely on structured data in <script> tags
as fallback when BeautifulSoup selectors return nothing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Generator

from .base import BaseScraper, NITRA_DISTRICT_CITIES, is_nitra_district

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils.engine import RawListing

logger = logging.getLogger(__name__)

PROPERTY_TYPE_MAP = {
    "byty": "byt",
    "domy": "dom",
    "pozemky": "pozemok",
}


class NehnutelnostiScraper(BaseScraper):
    """
    Scraper for https://www.nehnutelnosti.sk
    Targets Nitriansky kraj, all property types.
    """

    source_name = "nehnutelnosti.sk"
    base_url = "https://www.nehnutelnosti.sk"
    REGION_SLUG = "nitriansky-kraj"
    SEARCH_TYPES = list(PROPERTY_TYPE_MAP.keys())

    def __init__(self):
        super().__init__(delay_range=(2.5, 5.0))
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
            url = f"{self.base_url}/{ptype_slug}/predaj/{self.REGION_SLUG}/?page={page}"
            logger.info("[nehnutelnosti.sk] Fetching page %d — %s", page, url)
            soup = self._fetch(url)

            if soup is None:
                logger.warning("[nehnutelnosti.sk] Empty response on page %d, stopping.", page)
                break

            # Try JSON-LD structured data first (more reliable than scraping HTML)
            json_listings = list(self._parse_json_ld(soup, property_type))
            if json_listings:
                for raw in json_listings:
                    self._seen_urls.add(raw.source_url)
                    yield raw
                consecutive_empty = 0
                page += 1
                if not soup.select_one("a[rel='next'], a.pagination-next, li.next a"):
                    break
                continue

            # Fallback: HTML scraping with multiple selector attempts
            cards = (
                soup.select("article.advertisement-item")
                or soup.select("div.advertisement-item")
                or soup.select("article.property-card")
                or soup.select("div.property-card")
                or soup.select("li.item")
                or soup.select("div.listing-item")
            )

            if not cards:
                consecutive_empty += 1
                logger.info(
                    "[nehnutelnosti.sk] No cards on page %d (empty=%d). "
                    "Site may require JS rendering.",
                    page, consecutive_empty,
                )
                page += 1
                continue

            consecutive_empty = 0
            for card in cards:
                raw = self._parse_card(card, property_type)
                if raw:
                    self._seen_urls.add(raw.source_url)
                    yield raw

            # Pagination
            has_next = bool(
                soup.select_one("a[rel='next']")
                or soup.select_one("a.pagination__next")
                or soup.select_one("li.next a")
                or soup.select_one("a.next-page")
            )
            if not has_next:
                break
            page += 1

    def _parse_json_ld(self, soup, property_type: str) -> Generator[RawListing, None, None]:
        """
        Extract listings from JSON-LD <script> blocks.
        Many modern sites embed structured data even when HTML is JS-rendered.
        """
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("itemListElement", [data])

                for item in items:
                    raw = self._parse_json_ld_item(item, property_type)
                    if raw:
                        yield raw
            except (json.JSONDecodeError, AttributeError):
                continue

    def _parse_json_ld_item(self, item: dict, property_type: str) -> RawListing | None:
        try:
            if item.get("@type") not in ("RealEstateListing", "Product", "ListItem", "Apartment", "House"):
                return None

            # Handle ListItem wrapper
            if item.get("@type") == "ListItem" and "item" in item:
                item = item["item"]

            name = item.get("name", "")
            url = item.get("url", "") or item.get("@id", "")
            if not url:
                return None

            # Price
            offer = item.get("offers", {})
            if isinstance(offer, list):
                offer = offer[0] if offer else {}
            price_raw = offer.get("price") or item.get("price")
            if not price_raw:
                return None
            price = float(str(price_raw).replace(" ", "").replace(",", "."))
            if price < 500:
                return None

            # Area
            area = None
            floor_size = item.get("floorSize", {})
            if isinstance(floor_size, dict):
                area = float(floor_size.get("value", 0) or 0)
            if not area:
                m = re.search(r"(\d+[\.,]?\d*)\s*m[²2]", name)
                if m:
                    area = float(m.group(1).replace(",", "."))
            if not area or area < 5:
                return None

            # City
            address = item.get("address", {})
            city = (
                address.get("addressLocality")
                or address.get("addressRegion")
                or self._extract_city(name)
            )
            if not city or not is_nitra_district(city):
                return None

            return RawListing(
                source_url=url if url.startswith("http") else self.base_url + url,
                location_city=city,
                property_type=property_type,
                area_sqm=area,
                absolute_price=price,
                subtype=self._extract_subtype(name),
            )
        except (ValueError, TypeError, KeyError):
            return None

    def _parse_card(self, card, property_type: str) -> RawListing | None:
        try:
            # Link + title
            link_el = (
                card.select_one("a.title")
                or card.select_one("h2 a")
                or card.select_one("h3 a")
                or card.select_one("a.advertisement-title")
                or card.select_one("a[href*='/detail/']")
                or card.select_one("a[href]")
            )
            if not link_el:
                return None
            listing_url = link_el.get("href", "")
            if not listing_url.startswith("http"):
                listing_url = self.base_url + listing_url
            title_text = link_el.get_text(strip=True)

            # City / location
            loc_el = (
                card.select_one("span.location")
                or card.select_one("span.address")
                or card.select_one("div.advertisement-location")
                or card.select_one("p.location")
                or card.select_one("span.locality")
            )
            city_raw = loc_el.get_text(strip=True) if loc_el else title_text
            city = self._extract_city(city_raw)
            if not city or not is_nitra_district(city):
                return None

            # Price
            price_el = (
                card.select_one("span.price")
                or card.select_one("strong.price")
                or card.select_one("div.advertisement-price")
                or card.select_one("span.advertisement-price")
                or card.select_one("p.price")
            )
            if not price_el:
                return None
            price = self.parse_price(price_el.get_text())
            if not price or price < 1_000:
                return None

            # Area
            area_el = (
                card.select_one("span.area")
                or card.select_one("li.area")
                or card.select_one("span.property-area")
                or card.select_one("span.parameter-area")
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
            logger.debug("[nehnutelnosti.sk] Card parse error: %s", exc)
            return None

    @staticmethod
    def _extract_city(text: str) -> str | None:
        """Pull city from 'Nitra - Chrenová' or 'Nitra, Zobor' → 'Nitra'."""
        city = re.split(r"[-,–]", text)[0].strip()
        return city if len(city) > 1 else None

    @staticmethod
    def _extract_subtype(title: str) -> str | None:
        patterns = [
            (r"(\d+)-izbov[ýáé]\s+\w+", lambda m: f"{m.group(1)}-izbový byt"),
            (r"rodinný dom", lambda m: "rodinný dom"),
            (r"chata|chalupa", lambda m: "chata/chalupa"),
            (r"stavebný pozemok", lambda m: "stavebný pozemok"),
        ]
        for pat, fmt in patterns:
            m = re.search(pat, title, re.IGNORECASE)
            if m:
                return fmt(m)
        return None
