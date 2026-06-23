"""
scrapers/bazos.py — Scraper for reality.bazos.sk

Bazos uses straightforward paginated HTML without JS rendering.
URL structure: https://reality.bazos.sk/{category}/{offset}/
"""

from __future__ import annotations

import logging
import re
from typing import Generator

from .base import BaseScraper, NITRA_DISTRICT_CITIES, is_nitra_district

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils.engine import RawListing

logger = logging.getLogger(__name__)

# Locative → nominative mapping for common Nitra district cities
LOCATIVE_MAP = {
    "nitre": "Nitra",
    "šuranoch": "Šurany",
    "zlatých moravciach": "Zlaté Moravce",
    "vrábľoch": "Vráble",
    "mojmírovciach": "Mojmírovce",
    "lužiankach": "Lužianky",
    "pohranici": "Pohranice",
    "jarku": "Jarok",
    "zbehy": "Zbehy",
    "žiranoch": "Žirany",
    "golianove": "Golianovo",
    "hruboňove": "Hruboňovo",
    "jelenči": "Jelenec",
    "telinci": "Telince",
    "novosadoch": "Novosady",
    "čechynciach": "Čechynce",
}


class BazosScraper(BaseScraper):
    """
    Scraper for https://reality.bazos.sk
    Filters results for Nitriansky okres.
    """

    source_name = "bazos.sk"
    base_url = "https://reality.bazos.sk"

    CATEGORIES = {
        "byty-predaj": "byt",
        "domy-predaj": "dom",
        "pozemky-predaj": "pozemok",
    }

    def __init__(self):
        super().__init__(delay_range=(2.0, 4.0))
        self._seen_urls: set[str] = set()

    @property
    def seen_urls(self) -> set[str]:
        return self._seen_urls

    def iter_listings(self) -> Generator[RawListing, None, None]:
        for category, ptype in self.CATEGORIES.items():
            yield from self._scrape_category(category, ptype)

    def _scrape_category(self, category: str, property_type: str) -> Generator[RawListing, None, None]:
        offset = 0
        page_size = 20
        empty_pages = 0

        while empty_pages < 2:
            url = (
                f"{self.base_url}/{category}/"
                if offset == 0
                else f"{self.base_url}/{category}/{offset}/"
            )
            logger.info("[bazos.sk] Fetching %s offset=%d", category, offset)
            soup = self._fetch(url)

            if soup is None:
                logger.warning("[bazos.sk] No response at offset=%d, stopping.", offset)
                break

            # Bazos listing items: div.inzeraty contains multiple div.inzerat
            items = soup.select("div.inzerat")
            if not items:
                # Fallback: try article-based layout
                items = soup.select("article.item, div.item-row")

            if not items:
                empty_pages += 1
                logger.info("[bazos.sk] No items at offset=%d (empty page %d).", offset, empty_pages)
                if empty_pages >= 2:
                    break
                offset += page_size
                continue

            empty_pages = 0
            parsed_in_page = 0
            for item in items:
                raw = self._parse_item(item, property_type)
                if raw:
                    self._seen_urls.add(raw.source_url)
                    parsed_in_page += 1
                    yield raw

            logger.debug("[bazos.sk] %s offset=%d: parsed %d/%d items", category, offset, parsed_in_page, len(items))
            offset += page_size

    def _parse_item(self, item, property_type: str) -> RawListing | None:
        try:
            # Title + URL — multiple selector fallbacks
            title_el = (
                item.select_one("h2.nadpis a")
                or item.select_one("h2 a")
                or item.select_one("h3 a")
                or item.select_one("a.title")
                or item.select_one("a[href*='/inzerat/']")
            )
            if not title_el:
                return None

            listing_url = title_el.get("href", "")
            if listing_url and not listing_url.startswith("http"):
                listing_url = "https://reality.bazos.sk" + listing_url
            title = title_el.get_text(strip=True)

            # Description text — multiple fallbacks
            desc_el = (
                item.select_one("div.popis")
                or item.select_one("p.popis")
                or item.select_one("div.description")
                or item.select_one("p.description")
            )
            description = desc_el.get_text(" ", strip=True) if desc_el else ""

            # Location element (Bazos often has a separate location span)
            loc_el = (
                item.select_one("span.lokace")
                or item.select_one("div.lokace")
                or item.select_one("span.location")
                or item.select_one("span.mesto")
            )
            location_text = loc_el.get_text(strip=True) if loc_el else ""

            # City extraction — try location element first, then description, then title
            city = (
                self._extract_city_from_location(location_text)
                or self._extract_city_bazos(description)
                or self._extract_city_bazos(title)
            )
            if not city:
                return None

            # Filter to Nitriansky okres only
            if not is_nitra_district(city):
                return None

            # Price — multiple fallbacks
            price_el = (
                item.select_one("span.cena strong")
                or item.select_one("span.cena")
                or item.select_one("div.cena")
                or item.select_one("strong.cena")
                or item.select_one("b.cena")
            )
            if not price_el:
                return None
            price_text = price_el.get_text()
            if "dohod" in price_text.lower() or "dohodou" in price_text.lower():
                return None
            price = self.parse_price(price_text)
            if not price or price < 500:
                return None

            # Area from title + description combined
            full_text = title + " " + description
            area_match = re.search(r"(\d+[\.,]?\d*)\s*m[²2]", full_text)
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
    def _extract_city_from_location(location_text: str) -> str | None:
        """Parse Bazos location element like 'Nitra (Nitriansky kraj)'."""
        if not location_text:
            return None
        # Remove parenthetical region info
        clean = re.sub(r"\(.*?\)", "", location_text).strip()
        # Take first word/segment before comma or dash
        city = re.split(r"[,–\-]", clean)[0].strip()
        return city if len(city) > 1 else None

    @classmethod
    def _extract_city_bazos(cls, text: str) -> str | None:
        """
        Extract city from description/title text.
        Handles Slovak locative case ('v Nitre' → 'Nitra').
        """
        if not text:
            return None

        # 1. Locative case map: "v Nitre" → "Nitra"
        lower = text.lower()
        for locative, nominative in LOCATIVE_MAP.items():
            if f"v {locative}" in lower or f"v {locative}" in lower:
                return nominative

        # 2. City - district pattern: "Nitra - Zobor"
        m = re.match(r"^([A-ZÁČĎÉÍĹĽŇÓŔŠŤÚÝŽ][a-záčďéíĺľňóŕšťúýž]+(?:\s+\w+)?)\s*[-–]", text)
        if m:
            candidate = m.group(1).strip()
            if is_nitra_district(candidate):
                return candidate

        # 3. Known city directly at start or after comma
        for city in sorted(NITRA_DISTRICT_CITIES, key=len, reverse=True):
            if re.search(r"\b" + re.escape(city) + r"\b", text):
                return city

        # 4. "Lokalita: Nitra"
        m = re.search(r"[Ll]okalit[ay]:\s*([A-ZÁČĎÉÍĹĽŇÓŔŠŤÚÝŽ]\w+)", text)
        if m:
            return m.group(1).strip()

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
            if re.search(r"vila", title, re.IGNORECASE):
                return "vila"
        return None
