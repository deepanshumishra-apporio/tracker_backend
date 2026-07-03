"""
The ONE normalized data shape. Every carrier scraper — no matter how different
its raw response — must return a TrackingResult. Downstream code (storage, API,
UI) never has to care which carrier the data came from.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Carrier(str, Enum):
    UPS = "ups"
    FEDEX = "fedex"
    DHL = "dhl"
    ARAMEX = "aramex"


class Status(str, Enum):
    """Normalized status buckets so 4 carriers' wording maps to one vocabulary."""
    PENDING = "pending"            # label created / info received
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    EXCEPTION = "exception"        # failed attempt, held, returned, etc.
    UNKNOWN = "unknown"


class TrackingEvent(BaseModel):
    """A single scan/checkpoint in the shipment's history."""
    timestamp: Optional[datetime] = None
    location: Optional[str] = None
    description: str = ""
    status: Status = Status.UNKNOWN


class TrackingResult(BaseModel):
    tracking_number: str
    carrier: Carrier
    status: Status = Status.UNKNOWN
    estimated_delivery: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    origin: Optional[str] = None
    destination: Optional[str] = None

    # Shipment attributes. Populated when a carrier exposes them; several
    # (weight, signed_by) are gated behind identity checks on public tracking,
    # so they are frequently None.
    service: Optional[str] = None          # e.g. "EXPRESS WORLDWIDE"
    weight: Optional[str] = None           # string to preserve units, e.g. "2.5 kg"
    pieces: Optional[int] = None           # number of packages in the shipment
    signed_by: Optional[str] = None        # proof-of-delivery signatory
    # Flexible bag for any extra carrier-specific key/values (Waybill Number,
    # Piece ID, Reference, ...) so we never drop data we lack a field for.
    details: dict[str, str] = Field(default_factory=dict)

    events: list[TrackingEvent] = Field(default_factory=list)

    # Bookkeeping
    scraped_at: Optional[datetime] = None
    ok: bool = True                 # False if scrape failed (blocked, not found)
    error: Optional[str] = None

    @classmethod
    def failure(cls, tracking_number: str, carrier: Carrier, error: str) -> "TrackingResult":
        return cls(tracking_number=tracking_number, carrier=carrier,
                   status=Status.UNKNOWN, ok=False, error=error)
