"""
scrapers/bazos.py — Scraper skeleton for Bazos.sk (Reality section)

Bazos uses simple paginated HTML, easier to parse than JS-heavy portals.
"""

from __future__ import annotations

import logging
import re
from typing import Generator

from .base import BaseScraper

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils.engine import RawListing

logger = logging.getLogger(__name__)


class BazosScraper(BaseScraper):
    """
    Scraper for https://reality.bazos.sk
    Filters for Nitriansky kraj listings.
    """

    source_name = "bazos.sk"
    base_url = "https://reality.bazos.sk"

    # Bazos category IDs for reality
    CATEGORIES = {
        "byty-predaj": "byt",
        "domy-predaj": "dom",
        "pozemky-predaj": "pozemok",
    }

    # Bazos uses a region parameter for Nitriansky kraj
    REGION_PARAM = "Nitriansky kraj"

    def iter_listings(self) -> Generator[RawListing, None, None]:
        for category, ptype in self.CATEGORIES.items():
            yield from self._scrape_category(category, ptype)

    def _scrape_category(self, category: str, property_type: str) -> Generator[RawListing, None, None]:
        offset = 0
        page_size = 20  # bazos shows 20 per page

        while True:
            # Bazos search URL pattern
            if offset == 0:
                url = f"{self.base_url}/{category}/"
            else:
                url = f"{self.base_url}/{category}/{offset}/"

            logger.info("[bazos.sk] Fetching %s offset=%d", category, offset)
            soup = self._fetch(url)

            if soup is None:
                break

            # ── Listing rows ───────────────────────────────────────────────
            items = soup.select("div.inzeraty div.inzerat, article.listing-item")
            if not items:
                logger.info("[bazos.sk] No items at offset %d, done.", offset)
                break

            parsed_count = 0
            for item in items:
                raw = self._parse_item(item, property_type)
                if raw:
                    parsed_count += 1
                    yield raw

            # ── Check for next page ────────────────────────────────────────
            next_link = soup.select_one("a.next, span.next a, a[rel='next']")
            if not next_link and parsed_count == 0:
                break
            offset += page_size

    def _parse_item(self, item, property_type: str) -> RawListing | None:
        try:
            # Title + URL
            title_el = item.select_one("h2.nadpis a, h3 a, a.title")
            if not title_el:
                return None
            listing_url = title_el.get("href", "")
            if listing_url and not listing_url.startswith("http"):
                listing_url = "https://reality.bazos.sk" + listing_url
            title = title_el.get_text(strip=True)

            # Description — Bazos puts city info in description or meta
            desc_el = item.select_one("div.popis, p.description, span.location")
            description = desc_el.get_text(" ", strip=True) if desc_el else title

            # City extraction
            # Bazos typically formats: "Predaj, Nitra" or "Nitra, Zobor"
            city = self._extract_city_bazos(description, title)
            if not city:
                return None

            # Price — Bazos format: "125 000 €"
            price_el = item.select_one("span.cena, div.cena, strong.price")
            if not price_el:
                return None
            price_text = price_el.get_text()
            if "dohod" in price_text.lower():  # "cena dohodou" = negotiable, skip
                return None
            price = self.parse_price(price_text)
            if not price or price < 500:
                return None

            # Area from title or description
            area_match = re.search(r"(\d+[\.,]?\d*)\s*m[²2]", title + " " + description)
            if not area_match:
                return None
            area = float(area_match.group(1).replace(",", "."))
            if area < 5:
                return None

            subtype = self._guess_subtype(title, property_type)

            return RawListing(
                source_url=listing_url,
                location_city=city,
                property_type=property_type,
                area_sqm=area,
                absolute_price=price,
                subtype=subtype,
            )

        except Exception as exc:
            logger.debug("[bazos.sk] Parse error: %s", exc)
            return None

    @staticmethod
    def _extract_city_bazos(description: str, title: str) -> str | None:
        """
        Bazos puts location info in varied places.
        Common patterns:
          - "Predám 3-izbový byt v Nitre na Zobore"
          - "Nitra - Zobor, 3 izbový byt"
          - "Lokalita: Nitra"
        """
        # Try "v Nitre/v Zlatých Moravciach" (locative case)
        locative = re.search(r"\bv\s+([A-ZÁČĎÉÍĹĽŇÓŔŠŤÚÝŽ][a-záčďéíĺľňóŕšťúýž]+(?:\s+\w+)?)", description)
        if locative:
            return locative.group(1).strip()

        # Try "City - " prefix
        prefix = re.match(r"^([A-ZÁČĎÉÍĹĽŇÓŔŠŤÚÝŽ][a-záčďéíĺľňóŕšťúýž]+)\s*[-–]", description)
        if prefix:
            return prefix.group(1).strip()

        # Try "Lokalita: City"
        lokalita = re.search(r"[Ll]ocalit[ay]:\s*([A-ZÁČĎÉÍĹĽŇÓŔŠŤÚÝŽ]\w+)", description)
        if lokalita:
            return lokalita.group(1).strip()

        return None

    @staticmethod
    def _guess_subtype(title: str, property_type: str) -> str | None:
        if property_type == "byt":
            m = re.search(r"(\d+)\s*[-–]\s*izb", title, re.IGNORECASE)
            if m:
                return f"{m.group(1)}-izbový byt"
        elif property_type == "dom":
            if re.search(r"rodinný", title, re.IGNORECASE):
                return "rodinný dom"
            if re.search(r"chata|chalupa", title, re.IGNORECASE):
                return "chata/chalupa"
        return None
