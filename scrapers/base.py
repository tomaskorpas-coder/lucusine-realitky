"""
scrapers/base.py — Abstract base scraper with rotating user-agents and error handling.
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Generator

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# A pool of realistic browser user-agents for rotation
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en-US;q=0.7,en;q=0.6",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

NITRA_DISTRICT_CITIES = {
    # Okres Nitra — všetky obce a mestá
    "Nitra", "Šurany", "Zlaté Moravce", "Vráble",
    "Mojmírovce", "Čab", "Cabaj-Čápor", "Lužianky", "Paňa",
    "Pohranice", "Rumanová", "Štefanovičová", "Veľké Zálužie",
    "Jarok", "Malé Zálužie", "Ivánka pri Nitre", "Branč",
    "Beladice", "Bíňa", "Ľudovítová", "Čechynce", "Andač",
    "Báb", "Babindol", "Dolné Obdokovce", "Horné Obdokovce",
    "Klasov", "Komjatice", "Krškany", "Melek", "Michal nad Žitavou",
    "Nová Ves nad Žitavou", "Ľudovítová", "Zbehy", "Žirany",
    "Štitáre", "Sľažany", "Výčapy-Opatovce", "Veľký Cetín",
    "Malý Cetín", "Novosady", "Telince", "Zlatno", "Jelenec",
    "Golianovo", "Hruboňovo", "Dražovce", "Čermáň",
    "Nové Zámky",  # susedný okres, ale blízko a často vyhľadávaný
}


def is_nitra_district(city: str) -> bool:
    """Return True if city name (nominative or locative form) matches Nitriansky okres."""
    city_clean = city.strip().title()
    if city_clean in NITRA_DISTRICT_CITIES:
        return True
    # Fuzzy: check if any known city is a substring of or contained in city_clean
    for known in NITRA_DISTRICT_CITIES:
        if known in city_clean or city_clean in known:
            return True
    return False


class BaseScraper(ABC):
    """
    Abstract base. Subclasses implement `iter_listings()` which yields RawListing objects.
    """

    source_name: str = "unknown"
    base_url: str = ""

    def __init__(self, delay_range: tuple[float, float] = (1.5, 3.5)):
        self._session = requests.Session()
        self._delay_range = delay_range

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get_headers(self) -> dict:
        headers = DEFAULT_HEADERS.copy()
        headers["User-Agent"] = random.choice(USER_AGENTS)
        return headers

    def _fetch(self, url: str, retries: int = 3) -> BeautifulSoup | None:
        """
        Fetch URL with rotating user-agent, polite delay, and retry logic.
        Returns parsed BeautifulSoup or None on failure.
        """
        for attempt in range(1, retries + 1):
            try:
                time.sleep(random.uniform(*self._delay_range))
                resp = self._session.get(
                    url,
                    headers=self._get_headers(),
                    timeout=15,
                )
                resp.raise_for_status()
                return BeautifulSoup(resp.text, "lxml")
            except requests.HTTPError as e:
                logger.warning("[%s] HTTP %s on %s (attempt %d/%d)",
                               self.source_name, e.response.status_code, url, attempt, retries)
                if e.response.status_code in (403, 429):
                    time.sleep(10 * attempt)   # back-off on rate limit
            except requests.RequestException as e:
                logger.warning("[%s] Request error on %s attempt %d: %s",
                               self.source_name, url, attempt, e)
        logger.error("[%s] All %d attempts failed for %s", self.source_name, retries, url)
        return None

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def iter_listings(self) -> Generator:
        """
        Yield RawListing objects for all relevant listings found on the portal.
        Must handle pagination internally.
        """
        ...

    # ── Price parsing utilities ───────────────────────────────────────────────

    @staticmethod
    def parse_price(raw: str) -> float | None:
        """
        Convert Slovak price strings like '125 000 €' or '1.250.000 Kč' to float.
        Returns None if unparseable.
        """
        import re
        cleaned = re.sub(r"[^\d,.]", "", raw.replace(" ", "").replace("\xa0", ""))
        cleaned = cleaned.replace(",", ".")
        # handle '.' as thousands separator: 1.250.000 → 1250000
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = "".join(parts)
        try:
            return float(cleaned)
        except ValueError:
            return None

    @staticmethod
    def parse_area(raw: str) -> float | None:
        """
        Convert area strings like '85 m²' to float.
        """
        import re
        match = re.search(r"(\d+[\.,]?\d*)", raw.replace(" ", "").replace("\xa0", ""))
        if match:
            return float(match.group(1).replace(",", "."))
        return None
