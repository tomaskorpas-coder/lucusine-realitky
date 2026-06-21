"""
SQLAlchemy database models for the Real Estate Aggregator.
"""

import json
import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, Float, String, Text,
    DateTime, Enum, event
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

# Works both locally and on Streamlit Cloud
_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_HERE, "data")
os.makedirs(_DATA_DIR, exist_ok=True)
DATABASE_URL = f"sqlite:///{os.path.join(_DATA_DIR, 'realty.db')}"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Listing(Base):
    """
    Core listing model. source_urls stored as JSON string.
    """
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, index=True)
    source_urls = Column(Text, nullable=False, default="[]")
    location_city = Column(String(128), nullable=False, index=True)
    location_district = Column(String(128), nullable=True, index=True)
    property_type = Column(
        Enum("byt", "dom", "pozemok", "komercia", "iny", name="property_type_enum"),
        nullable=False, index=True
    )
    subtype = Column(String(64), nullable=True)
    condition = Column(String(64), nullable=True)
    area_sqm = Column(Float, nullable=False)
    absolute_price = Column(Float, nullable=False)
    price_per_sqm = Column(Float, nullable=False)
    status = Column(
        Enum("active", "inactive", name="status_enum"),
        nullable=False, default="active", index=True
    )
    internal_notes = Column(Text, nullable=True, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def get_source_urls(self) -> list:
        try:
            return json.loads(self.source_urls)
        except (json.JSONDecodeError, TypeError):
            return []

    def set_source_urls(self, urls: list) -> None:
        self.source_urls = json.dumps(list(set(urls)), ensure_ascii=False)

    def add_source_url(self, url: str) -> None:
        urls = self.get_source_urls()
        if url not in urls:
            urls.append(url)
            self.set_source_urls(urls)

    def __repr__(self) -> str:
        return (
            f"<Listing id={self.id} city={self.location_city!r} "
            f"type={self.property_type!r} area={self.area_sqm}m² "
            f"price={self.absolute_price:,.0f}€>"
        )


class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    listing_id = Column(Integer, nullable=False, index=True)
    old_price = Column(Float, nullable=True)
    new_price = Column(Float, nullable=False)
    old_price_per_sqm = Column(Float, nullable=True)
    new_price_per_sqm = Column(Float, nullable=False)
    changed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    note = Column(String(256), nullable=True)


def init_db() -> None:
    """Create all tables (idempotent)."""
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
