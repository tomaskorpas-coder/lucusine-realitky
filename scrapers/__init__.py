"""
scrapers/__init__.py — Scraper registry
"""
from .base import BaseScraper
from .nehnutelnosti import NehnutelnostiScraper
from .bazos import BazosScraper

ALL_SCRAPERS = [NehnutelnostiScraper, BazosScraper]
