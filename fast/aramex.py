"""
Aramex without a browser.

The public results page is server-rendered: GET
/us/en/track/results?ShipmentNumber=A,B,C returns one `a.shipment-card` per
number, already filled in. Each card links to a details page that is also
server-rendered and carries the full history table plus weight / pieces /
shipment type. So a lookup is two plain HTTPS GETs, no JavaScript:

  1. one results page per chunk of numbers   (~1-2s for 10 numbers)
  2. one details page per card, in parallel   (~1.5s each)

Card / table markup is the same the browser scraper reads (scrapers/aramex.py),
so its parsing helpers are reused here.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

import config
from fast.http import TransientError, session
from models import Carrier, Status, TrackingEvent, TrackingResult
from scrapers.aramex import _STAGE_STATUS, _join, _parse_dt, _parse_history_table

BASE = "https://www.aramex.com/us/en"
RESULTS = f"{BASE}/track/results"
NOT_FOUND = "Aramex has no shipment with this number."


def _text(node, selector: str) -> Optional[str]:
    el = node.select_one(selector)
    if el is None:
        return None
    return " ".join(el.get_text(" ", strip=True).split()) or None


def _get(url: str, **kw) -> str:
    try:
        resp = session().get(url, timeout=config.FAST_TIMEOUT, **kw)
    except Exception as exc:
        raise TransientError(f"Aramex did not answer: {exc}") from exc
    if resp.status_code != 200:
        raise TransientError(f"Aramex answered HTTP {resp.status_code}")
    return resp.text


def _stage(point) -> str:
    """'Destination Saudi Arabia Riyadh Delivered' -> 'Delivered'.

    The first/last points carry the place before the label; the label is the
    `.track-check-info > span` the browser scraper reads, else the last word(s)
    matching a known stage."""
    span = point.select_one(".track-check-info > span")
    if span and span.get_text(strip=True):
        return span.get_text(" ", strip=True)
    text = " ".join(point.get_text(" ", strip=True).split()).lower()
    for label in sorted(_STAGE_STATUS, key=len, reverse=True):
        if text.endswith(label):
            return label
    return text


def parse_cards(html: str) -> dict[str, dict]:
    """Results page -> {tracking number: card summary}."""
    soup = BeautifulSoup(html, "html.parser")
    cards: dict[str, dict] = {}
    for card in soup.select("a.shipment-card"):
        number = _text(card, ".shipment-num h5")
        if not number:
            continue
        points = card.select(".shipment-progess-point")
        current = next((p for p in points if "current" in (p.get("class") or [])), None)
        if current is None:
            done = [p for p in points if "done" in (p.get("class") or [])]
            current = done[-1] if done else None
        cards[number] = {
            "href": card.get("href"),
            "tag": _text(card, ".shipment-tag"),
            "update": _text(card, ".shipment-update-descp"),
            "datetime": _text(card, ".shipment-update-datetime"),
            "origin": _join(_text(card, ".orgin-info .country"), _text(card, ".orgin-info .city")),
            "destination": _join(_text(card, ".dest-info .country"), _text(card, ".dest-info .city")),
            "stage": _stage(current) if current is not None else None,
            "steps": [(_stage(p), [c for c in p.get("class") or []]) for p in points],
        }
    return cards


def parse_details(html: str) -> tuple[list[TrackingEvent], dict[str, str]]:
    """Details page -> (history newest-first, {weight, items, type})."""
    soup = BeautifulSoup(html, "html.parser")
    events: list[TrackingEvent] = []
    for table in soup.select("table"):
        rows = [[c.get_text(" ", strip=True) for c in tr.select("th,td")]
                for tr in table.select("tr")]
        head = [c.lower() for c in rows[0]] if rows else []
        if "date" in head and "activity" in head:
            events = _parse_history_table(rows)
            break
    lines = [s.strip() for s in soup.get_text("\n").split("\n") if s.strip()]
    labels = {"weight": "weight", "number of items": "items", "shipment type": "type"}
    fields: dict[str, str] = {}
    for i, line in enumerate(lines[:-1]):
        key = labels.get(line.lower())
        nxt = lines[i + 1]
        if key and key not in fields and nxt.lower() not in labels:
            fields[key] = nxt
    return events, fields


def build_result(number: str, card: dict, events: list[TrackingEvent],
                 fields: dict[str, str]) -> TrackingResult:
    status = _STAGE_STATUS.get((card.get("stage") or "").lower(), Status.UNKNOWN)
    when = _parse_dt(card.get("datetime"))
    if not events:
        # Details page unavailable: fall back to the card's summary, as the
        # browser scraper does.
        if card.get("update"):
            events.append(TrackingEvent(timestamp=when, description=card["update"],
                                        status=status, location=card.get("destination")))
        for label, classes in card.get("steps") or []:
            if "done" in classes or "current" in classes:
                events.append(TrackingEvent(
                    description=label,
                    status=_STAGE_STATUS.get(label.lower(), Status.UNKNOWN)))
    weight = fields.get("weight")
    if weight and re.fullmatch(r"[\d.]+", weight):
        weight = f"{weight} kg"   # Aramex shows a bare number; it is kilograms
    pieces = None
    if fields.get("items"):
        m = re.search(r"\d+", fields["items"])
        pieces = int(m.group()) if m else None
    details = {"Latest update": card["update"]} if card.get("update") else {}
    return TrackingResult(
        tracking_number=number,
        carrier=Carrier.ARAMEX,
        status=status,
        origin=card.get("origin"),
        destination=card.get("destination"),
        service=fields.get("type") or card.get("tag"),
        weight=weight,
        pieces=pieces,
        delivered_at=when if status == Status.DELIVERED else None,
        details=details,
        events=events,
        scraped_at=datetime.now(timezone.utc),
    )


class AramexFast:
    carrier = Carrier.ARAMEX

    def __init__(self, chunk: Optional[int] = None, workers: Optional[int] = None):
        self.chunk = chunk or config.ARAMEX_CHUNK
        self.workers = workers or config.FAST_WORKERS

    def _one(self, number: str, card: dict) -> TrackingResult:
        events: list[TrackingEvent] = []
        fields: dict[str, str] = {}
        if card.get("href"):
            try:
                events, fields = parse_details(_get(BASE + card["href"]))
            except TransientError:
                pass   # the card alone is still a usable answer
        return build_result(number, card, events, fields)

    def track_many(self, numbers: list[str]) -> tuple[dict[str, TrackingResult], dict[str, str]]:
        """-> ({number: result}, {number: transient error}).

        A number absent from a page that otherwise loaded is a definitive
        "not found"; a page that failed to load leaves its numbers transient.
        """
        numbers = list(dict.fromkeys(numbers))
        chunks = [numbers[i:i + self.chunk] for i in range(0, len(numbers), self.chunk)]
        results: dict[str, TrackingResult] = {}
        transient: dict[str, str] = {}

        def page(chunk: list[str]):
            try:
                return chunk, parse_cards(_get(RESULTS, params={"ShipmentNumber": ",".join(chunk)})), None
            except TransientError as exc:
                return chunk, {}, str(exc)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            found: list[tuple[str, dict]] = []
            for chunk, cards, err in pool.map(page, chunks):
                upper = {k.upper(): v for k, v in cards.items()}
                for n in chunk:
                    if err:
                        transient[n] = err
                    elif n.upper() in upper:
                        found.append((n, upper[n.upper()]))
                    else:
                        results[n] = TrackingResult.failure(n, Carrier.ARAMEX, NOT_FOUND)
            for n, result in zip([n for n, _ in found],
                                 pool.map(lambda nc: self._one(*nc), found)):
                results[n] = result
        return results, transient
