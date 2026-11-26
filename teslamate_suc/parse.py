"""Raw `charging/history` session → Invoice.

Facts from the probe (54 sessions, 2025-12 → 2026-11, PL):
- times carry an offset (+01:00 / +02:00), so we convert to naive UTC like TeslaMate does;
- fees[]: CHARGING (uom kwh) and CONGESTION (uom min, the idle fee). pricingType: PAYMENT,
  CREDIT (free from credits, totalDue 0, no invoice), CREDIT_PARTIAL_PAYMENT, NO_CHARGE;
- totalDue is gross (what you pay), netDue is net (totalDue / 1.23). cost = sum of totalDue;
- invoices[].contentId is the PDF id; credit sessions have no invoice;
- status PAID/UNPAID — an unpaid fee still counts towards the cost, flagged as `paid=False`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

F_SESSION_ID = "sessionId"
F_VIN = "vin"
F_SITE = "siteLocationName"
F_START = "chargeStartDateTime"
F_STOP = "chargeStopDateTime"
F_UNLATCH = "unlatchDateTime"
F_COUNTRY = "countryCode"
F_FEES = "fees"
F_INVOICES = "invoices"

FEE_CHARGING = "CHARGING"
FEE_CONGESTION = "CONGESTION"

CENT = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(str(x or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def to_utc_naive(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:  # not seen in the data; assume UTC rather than guess the local zone
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class Invoice:
    session_id: str
    vin: str
    site_name: str | None
    country: str | None
    start: datetime
    stop: datetime
    unlatch: datetime | None
    grid_kwh: Decimal
    charging_fee: Decimal
    congestion_fee: Decimal
    other_fee: Decimal
    total: Decimal
    net_total: Decimal
    currency: str | None
    paid: bool
    pricing_types: list[str]
    rates: list[float]
    invoice_ids: list[str]
    fees_hash: str
    raw: dict = field(repr=False, default_factory=dict)
    provider: str = "tesla"          # tesla | greenway | manual
    charge_kind: str | None = None   # AC | DC | None (Tesla: always DC, not checked)

    @property
    def end(self) -> datetime:
        """End of the session as far as matching is concerned: unplugging, if Tesla reports it."""
        return max(self.stop, self.unlatch) if self.unlatch else self.stop

    @property
    def price_per_kwh(self) -> Decimal | None:
        if self.grid_kwh > 0 and self.charging_fee > 0:
            return (self.charging_fee / self.grid_kwh).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        return None

    @property
    def is_free(self) -> bool:
        return self.total == 0


class ParseError(ValueError):
    pass


def fees_hash(fees: list) -> str:
    canon = json.dumps(sorted(fees, key=lambda f: (str(f.get("feeType")), str(f.get("sessionFeeId")))),
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def parse_session(s: dict) -> Invoice:
    sid = s.get(F_SESSION_ID)
    start, stop = to_utc_naive(s.get(F_START)), to_utc_naive(s.get(F_STOP))
    if sid is None or start is None or stop is None:
        raise ParseError(f"sesja bez id/czasu: {sid!r}")
    fees = s.get(F_FEES) or []
    grid = Decimal(0)
    charging = congestion = other = total = net = Decimal(0)
    currency = None
    paid = True
    pricing, rates = [], []
    for f in fees:
        ftype = f.get("feeType")
        due = money(f.get("totalDue"))
        total += due
        net += money(f.get("netDue"))
        currency = currency or f.get("currencyCode")
        if str(f.get("status", "")).upper() != "PAID" and due > 0:
            paid = False
        if ftype == FEE_CHARGING:
            charging += due
            if str(f.get("uom", "")).lower() == "kwh":
                grid += Decimal(str(f.get("usageBase") or 0))
                for t in ("usageTier1", "usageTier2", "usageTier3", "usageTier4"):
                    grid += Decimal(str(f.get(t) or 0))
            pricing.append(str(f.get("pricingType")))
            if f.get("rateBase") is not None:
                rates.append(float(f["rateBase"]))
        elif ftype == FEE_CONGESTION:
            congestion += due
        else:
            other += due
    return Invoice(
        session_id=str(sid),
        vin=str(s.get(F_VIN) or ""),
        site_name=s.get(F_SITE),
        country=s.get(F_COUNTRY),
        start=start,
        stop=stop,
        unlatch=to_utc_naive(s.get(F_UNLATCH)),
        grid_kwh=grid.quantize(Decimal("0.0001")),
        charging_fee=charging,
        congestion_fee=congestion,
        other_fee=other,
        total=total,
        net_total=net,
        currency=currency,
        paid=paid,
        pricing_types=pricing,
        rates=rates,
        invoice_ids=[i["contentId"] for i in (s.get(F_INVOICES) or []) if i.get("contentId")],
        fees_hash=fees_hash(fees),
        raw=s,
    )


def parse_all(sessions: list[dict]) -> tuple[list[Invoice], list[str]]:
    """Returns (invoices, errors). One bad session does not stop the rest."""
    out, errors = [], []
    seen = set()
    for s in sessions:
        try:
            inv = parse_session(s)
        except (ParseError, ValueError, TypeError, KeyError) as e:
            errors.append(f"{s.get(F_SESSION_ID)!r}: {e}")
            continue
        if inv.session_id in seen:
            continue
        seen.add(inv.session_id)
        out.append(inv)
    return out, errors
