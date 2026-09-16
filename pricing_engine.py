"""
pricing_engine.py
==================
A pricing engine for a cinema box-office counter — any show, any multiplex.

Handles, in order, the things that actually make ticket pricing messy:

  1. Seat tiers (Silver / Gold / Recliner ...) each with their own price
     and their own live inventory — a tier that's sold out simply can't
     be booked, full stop.
  2. Offers stack on top of the gross amount:
       - a flat festival discount (₹X off the order)
       - a percentage member discount, capped at a max ₹ amount
     Both can be live on the same booking; flat is applied first, then
     the percentage is taken off what's left.
  3. A per-ticket convenience fee, charged on top of the (discounted)
     ticket price — the fee itself isn't discounted.
  4. GST, applied the way Indian multiplexes actually apply it:
       - ticket GST is slab-based on the *net, per-ticket* price
         (≤ ₹100/ticket → 12%, above that → 18%)
       - convenience fee GST is a flat rate (18%) regardless of ticket price
  5. Every rupee is tracked in Decimal, and every discount is allocated
     back across tiers with a largest-remainder split, so the line items
     always sum to *exactly* the grand total — never off by a paisa.

Run this file directly to see a worked Friday-night example, including
a sold-out tier being correctly rejected.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# money helpers — everything is Decimal, quantized to the paisa (2 dp)
# ---------------------------------------------------------------------------

TWO_PLACES = Decimal("0.01")


def money(x) -> Decimal:
    """Coerce to Decimal and round to the nearest paisa."""
    return Decimal(str(x)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def rupees(x: Decimal) -> str:
    return f"₹{x:,.2f}"


# ---------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------

class PricingError(Exception):
    """Base class for all engine-level pricing errors."""


class SoldOutError(PricingError):
    """Raised when a booking asks for more seats than a tier has left."""


class UnknownTierError(PricingError):
    """Raised when a booking references a tier the show doesn't have."""


class EmptyBookingError(PricingError):
    """Raised when a booking has no seats in it at all."""


# ---------------------------------------------------------------------------
# inventory model
# ---------------------------------------------------------------------------

@dataclass
class SeatTier:
    name: str
    price: Decimal          # price per seat, before any discount
    total_seats: int
    sold_seats: int = 0

    def __post_init__(self):
        self.price = money(self.price)

    @property
    def available(self) -> int:
        return self.total_seats - self.sold_seats

    @property
    def sold_out(self) -> bool:
        return self.available <= 0

    def reserve(self, qty: int) -> None:
        if qty > self.available:
            raise SoldOutError(
                f"{self.name}: only {self.available} seat(s) left, {qty} requested"
            )
        self.sold_seats += qty


@dataclass
class Show:
    title: str
    tiers: Dict[str, SeatTier] = field(default_factory=dict)

    @classmethod
    def create(cls, title: str, tier_specs: List[tuple]) -> "Show":
        """tier_specs: list of (name, price, total_seats)"""
        tiers = {name: SeatTier(name, price, total_seats) for name, price, total_seats in tier_specs}
        return cls(title, tiers)

    def tier(self, name: str) -> SeatTier:
        if name not in self.tiers:
            raise UnknownTierError(f"No such tier at this counter: {name!r}")
        return self.tiers[name]


# ---------------------------------------------------------------------------
# messy price-list import
# ---------------------------------------------------------------------------
#
# Real price feeds from box-office systems, spreadsheets and vendor exports
# are never clean: the same tier shows up under different capitalisations,
# prices arrive as "150", "₹150.00", "Rs. 250/-", "1,200", or "INR 700",
# some fields are just blank, and typos produce stray negative prices.
# This step turns that mess into a trustworthy SeatTier list, plus a full
# audit trail of what happened to every row.

def parse_price(raw) -> Optional[Decimal]:
    """Parse a messy price string into a Decimal, or None if it can't be read.
    Strips ₹ / Rs. / Rs / INR prefixes, trailing '/-', commas and whitespace."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    s = s.replace("₹", "").replace(",", "")
    s = re.sub(r"(?i)^\s*(rs\.?|inr)\s*", "", s)   # leading "Rs.", "Rs", "INR"
    s = re.sub(r"(?i)\s*(rs\.?|inr)\s*$", "", s)   # trailing, just in case
    s = re.sub(r"/-\s*$", "", s)                    # trailing "/-"
    s = s.strip()
    if s == "":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_seats(raw) -> Optional[int]:
    """Parse a messy seat-count field into an int, or None if it can't be read."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


@dataclass
class ImportReport:
    imported: List[SeatTier] = field(default_factory=list)
    deduplicated: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)

    def render(self) -> str:
        w = 66
        out = ["PRICE LIST IMPORT REPORT", "=" * w]
        out.append(f"Imported — {len(self.imported)} clean tier(s):")
        for t in self.imported:
            out.append(f"  ✓ {t.name:<14}{rupees(t.price):>10}   {t.total_seats} seat(s)")
        out.append("")
        out.append(f"De-duplicated — {len(self.deduplicated)} row(s) merged away:")
        for note in self.deduplicated:
            out.append(f"  ~ {note}")
        if not self.deduplicated:
            out.append("  (none)")
        out.append("")
        out.append(f"Rejected — {len(self.rejected)} row(s) thrown out:")
        for note in self.rejected:
            out.append(f"  ✗ {note}")
        if not self.rejected:
            out.append("  (none)")
        out.append("=" * w)
        return "\n".join(out)


def import_seat_price_list(raw_rows: List[dict]) -> ImportReport:
    """
    Clean a messy raw price-list feed into a de-duplicated, validated list
    of SeatTiers, with a full report of what was imported, merged, or
    rejected and why.

    Each raw row is a dict with (possibly messy) keys: "tier", "price", "seats".

    Dedup rule: tier names are matched case-insensitively after trimming
    whitespace. The first valid row for a given name wins. A later row for
    the same name is:
      - merged away ("deduplicated") if its price matches the first row, or
      - rejected as a conflict if its price differs from the first row.
    """
    report = ImportReport()
    canon: Dict[str, SeatTier] = {}

    for i, row in enumerate(raw_rows, start=1):
        raw_name, raw_price, raw_seats = row.get("tier"), row.get("price"), row.get("seats")
        label = f"row {i} (tier={raw_name!r}, price={raw_price!r}, seats={raw_seats!r})"

        name = (raw_name or "").strip()
        if name == "":
            report.rejected.append(f"{label}: blank tier name")
            continue

        price = parse_price(raw_price)
        if price is None:
            reason = ("missing price" if raw_price is None or str(raw_price).strip() == ""
                       else f"unparseable price {raw_price!r}")
            report.rejected.append(f"{label}: {reason}")
            continue
        if price < 0:
            report.rejected.append(f"{label}: negative price ({rupees(money(price))})")
            continue

        seats = parse_seats(raw_seats)
        if seats is None:
            reason = ("missing seat count" if raw_seats is None or str(raw_seats).strip() == ""
                       else f"unparseable seat count {raw_seats!r}")
            report.rejected.append(f"{label}: {reason}")
            continue
        if seats <= 0:
            report.rejected.append(f"{label}: invalid seat count ({seats})")
            continue

        key = name.lower()
        canonical_name = name.title()
        price = money(price)

        if key in canon:
            existing = canon[key]
            if price == existing.price:
                report.deduplicated.append(
                    f"{label}: duplicate of '{existing.name}' at the same price "
                    f"({rupees(price)}) — merged, first entry kept"
                )
            else:
                report.rejected.append(
                    f"{label}: duplicate tier name '{canonical_name}' but conflicting price "
                    f"({rupees(price)} vs already-imported {rupees(existing.price)}) — "
                    f"kept the first entry, discarded this row"
                )
            continue

        tier = SeatTier(canonical_name, price, seats)
        canon[key] = tier
        report.imported.append(tier)

    return report


# ---------------------------------------------------------------------------
# offers
# ---------------------------------------------------------------------------

@dataclass
class FlatOffer:
    """A flat rupee amount off the whole order (e.g. a festival promo)."""
    code: str
    amount: Decimal

    def __post_init__(self):
        self.amount = money(self.amount)


@dataclass
class PercentOffer:
    """A percentage off, capped at a maximum rupee amount (e.g. member discount)."""
    code: str
    percent: Decimal
    cap: Decimal

    def __post_init__(self):
        self.cap = money(self.cap)


# ---------------------------------------------------------------------------
# tax configuration
# ---------------------------------------------------------------------------

@dataclass
class GSTConfig:
    ticket_low_rate: Decimal = Decimal("12")     # % — net price/ticket <= threshold
    ticket_high_rate: Decimal = Decimal("18")    # % — net price/ticket >  threshold
    ticket_slab_threshold: Decimal = Decimal("100")  # rupees, per ticket, post-discount
    convenience_fee_rate: Decimal = Decimal("18")    # % — flat, regardless of ticket price


# ---------------------------------------------------------------------------
# output shapes
# ---------------------------------------------------------------------------

@dataclass
class LineItem:
    tier: str
    qty: int
    unit_price: Decimal
    gross: Decimal
    discount: Decimal
    net: Decimal
    gst_rate: Decimal
    gst_amount: Decimal
    line_total: Decimal


@dataclass
class Breakup:
    show_title: str
    lines: List[LineItem]
    subtotal_gross: Decimal
    flat_discount: Decimal
    percent_discount: Decimal
    total_discount: Decimal
    total_net: Decimal
    total_ticket_gst: Decimal
    convenience_fee: Decimal
    convenience_gst: Decimal
    grand_total: Decimal
    offers_applied: List[str]

    def render(self) -> str:
        w = 58
        out = []
        out.append("=" * w)
        out.append(f" {self.show_title}".ljust(w))
        out.append("=" * w)
        out.append(f"{'Tier':<10}{'Qty':>4}{'Price':>10}{'Gross':>12}{'Disc':>10}")
        for l in self.lines:
            out.append(
                f"{l.tier:<10}{l.qty:>4}{rupees(l.unit_price):>10}"
                f"{rupees(l.gross):>12}{'-' + rupees(l.discount):>10}"
            )
        out.append("-" * w)
        out.append(f"{'Subtotal (gross)':<38}{rupees(self.subtotal_gross):>20}")
        if self.flat_discount:
            out.append(f"{'  less: flat discount':<38}{'-' + rupees(self.flat_discount):>20}")
        if self.percent_discount:
            out.append(f"{'  less: member discount':<38}{'-' + rupees(self.percent_discount):>20}")
        out.append(f"{'Net ticket amount':<38}{rupees(self.total_net):>20}")
        out.append("")
        out.append(f"{'GST on tickets (slab-rated)':<38}{rupees(self.total_ticket_gst):>20}")
        for l in self.lines:
            out.append(f"{'   ' + l.tier + f' @ {l.gst_rate}%':<38}{rupees(l.gst_amount):>20}")
        out.append("")
        out.append(f"{'Convenience fee':<38}{rupees(self.convenience_fee):>20}")
        out.append(f"{'GST on convenience fee (' + str(self._fee_rate()) + '%)':<38}{rupees(self.convenience_gst):>20}")
        out.append("=" * w)
        out.append(f"{'GRAND TOTAL':<38}{rupees(self.grand_total):>20}")
        out.append("=" * w)
        if self.offers_applied:
            out.append("Offers applied: " + "; ".join(self.offers_applied))
        return "\n".join(out)

    def _fee_rate(self):
        # convenience: recompute rate purely for display if fee > 0
        if self.convenience_fee == 0:
            return Decimal("0")
        return money(self.convenience_gst / self.convenience_fee * 100).normalize()


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

class PricingEngine:
    def __init__(self, gst: Optional[GSTConfig] = None,
                 convenience_fee_per_ticket: Decimal = Decimal("30")):
        self.gst = gst or GSTConfig()
        self.fee_per_ticket = money(convenience_fee_per_ticket)

    # -- public API ---------------------------------------------------

    def price_booking(
        self,
        show: Show,
        selections: Dict[str, int],
        flat_offer: Optional[FlatOffer] = None,
        percent_offer: Optional[PercentOffer] = None,
        reserve: bool = True,
    ) -> Breakup:
        """
        selections: {tier_name: quantity}
        reserve:    if True (default), seats are actually deducted from
                    inventory once pricing succeeds. Set False for a
                    quote that doesn't touch the seat map.
        """
        picks = [(name, qty) for name, qty in selections.items() if qty and qty > 0]
        if not picks:
            raise EmptyBookingError("No seats selected.")

        # Validate every tier & availability BEFORE reserving anything,
        # so a booking never partially succeeds.
        tiers_and_qty = []
        for name, qty in picks:
            tier = show.tier(name)
            if qty > tier.available:
                raise SoldOutError(
                    f"{tier.name}: only {tier.available} seat(s) left, {qty} requested"
                )
            tiers_and_qty.append((tier, qty))

        # 1. gross per tier line
        gross_lines = []
        for tier, qty in tiers_and_qty:
            gross = money(tier.price * qty)
            gross_lines.append({"tier": tier.name, "qty": qty, "unit": tier.price, "gross": gross})

        subtotal_gross = money(sum(g["gross"] for g in gross_lines))

        # 2. offers — flat first, then percentage on what's left, both capped sensibly
        offers_applied = []

        flat_amt = Decimal("0.00")
        if flat_offer:
            flat_amt = min(flat_offer.amount, subtotal_gross)
            offers_applied.append(f"{flat_offer.code}: flat -{rupees(flat_amt)}")

        after_flat = subtotal_gross - flat_amt

        percent_amt = Decimal("0.00")
        if percent_offer:
            raw = after_flat * percent_offer.percent / Decimal("100")
            percent_amt = min(money(raw), percent_offer.cap)
            offers_applied.append(
                f"{percent_offer.code}: {percent_offer.percent}% off, capped -{rupees(percent_amt)}"
            )

        total_discount = money(flat_amt + percent_amt)

        # 3. allocate the combined discount back across tier lines,
        #    proportional to each line's share of the gross, exact to the paisa
        discount_alloc = self._allocate_paisa(total_discount, [g["gross"] for g in gross_lines])

        # 4. net amount + slab-based GST, per tier line
        lines: List[LineItem] = []
        total_net = Decimal("0.00")
        total_ticket_gst = Decimal("0.00")
        for g, disc in zip(gross_lines, discount_alloc):
            net = money(g["gross"] - disc)
            net_per_ticket = money(net / g["qty"])
            rate = (
                self.gst.ticket_high_rate
                if net_per_ticket > self.gst.ticket_slab_threshold
                else self.gst.ticket_low_rate
            )
            gst_amt = money(net * rate / Decimal("100"))
            line_total = money(net + gst_amt)
            lines.append(LineItem(
                tier=g["tier"], qty=g["qty"], unit_price=g["unit"], gross=g["gross"],
                discount=disc, net=net, gst_rate=rate, gst_amount=gst_amt, line_total=line_total,
            ))
            total_net += net
            total_ticket_gst += gst_amt

        # 5. convenience fee + its own GST (fee is never discounted)
        total_qty = sum(g["qty"] for g in gross_lines)
        convenience_fee = money(self.fee_per_ticket * total_qty)
        convenience_gst = money(convenience_fee * self.gst.convenience_fee_rate / Decimal("100"))

        grand_total = money(total_net + total_ticket_gst + convenience_fee + convenience_gst)

        # 6. commit inventory only after every number checks out
        if reserve:
            for tier, qty in tiers_and_qty:
                tier.reserve(qty)

        return Breakup(
            show_title=show.title,
            lines=lines,
            subtotal_gross=subtotal_gross,
            flat_discount=flat_amt,
            percent_discount=percent_amt,
            total_discount=total_discount,
            total_net=money(total_net),
            total_ticket_gst=money(total_ticket_gst),
            convenience_fee=convenience_fee,
            convenience_gst=convenience_gst,
            grand_total=grand_total,
            offers_applied=offers_applied,
        )

    # -- internals ------------------------------------------------------

    @staticmethod
    def _allocate_paisa(total: Decimal, weights: List[Decimal]) -> List[Decimal]:
        """
        Split `total` (a rupee amount) across `weights` proportionally,
        working entirely in integer paise so the parts always sum back
        to exactly `total` — no floating point, no stray half-paise.
        Uses the largest-remainder method to hand out the leftover paise.
        """
        n = len(weights)
        total_paise = int(money(total) * 100)
        weight_paise = [int(money(w) * 100) for w in weights]
        weight_sum = sum(weight_paise)

        if total_paise == 0 or weight_sum == 0:
            return [Decimal("0.00")] * n

        shares = [w * total_paise for w in weight_paise]
        base = [s // weight_sum for s in shares]
        remainders = [s % weight_sum for s in shares]

        leftover = total_paise - sum(base)
        order = sorted(range(n), key=lambda i: remainders[i], reverse=True)
        for i in range(leftover):
            base[order[i % n]] += 1

        return [money(Decimal(b) / 100) for b in base]


# ---------------------------------------------------------------------------
# worked example — Friday night at the multiplex
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # ---- Step 0: the messy price list, exactly as it might land from a
    # vendor export — mixed case duplicates, mixed currency formats,
    # blanks, and a negative price a typo let slip through.
    raw_price_list = [
        {"tier": "Silver",       "price": "150",         "seats": "60"},
        {"tier": "SILVER",       "price": "₹150.00",     "seats": "60"},   # dup, same price
        {"tier": "Gold",         "price": "Rs. 250/-",   "seats": "40"},
        {"tier": "gold ",        "price": "275",         "seats": "40"},   # dup, conflicting price
        {"tier": "Recliner",     "price": "450.00",      "seats": "2"},
        {"tier": "RECLINER",     "price": "  450  ",     "seats": "2"},    # dup, same price
        {"tier": "Balcony",      "price": "",            "seats": "30"},   # blank price
        {"tier": "   ",          "price": "300",         "seats": "20"},   # blank name
        {"tier": "VIP",          "price": "-500",        "seats": "10"},   # negative price
        {"tier": "Economy",      "price": "abc",         "seats": "50"},   # unparseable price
        {"tier": "Premium",      "price": "1,200",       "seats": "15"},
        {"tier": "Premium",      "price": "1200.00",     "seats": "15"},   # dup, same price
        {"tier": "Box",          "price": "600",         "seats": "-5"},   # invalid seat count
        {"tier": "Couple Seat",  "price": "INR 700",     "seats": ""},     # blank seats
    ]

    import_report = import_seat_price_list(raw_price_list)
    print(import_report.render())
    print()

    show = Show(title="Screen 4 · 9:40 PM · War of the Reels",
                tiers={t.name: t for t in import_report.imported})

    engine = PricingEngine(convenience_fee_per_ticket=Decimal("30"))

    festival = FlatOffer(code="FEST50", amount=Decimal("50"))
    member = PercentOffer(code="MEMBER10", percent=Decimal("10"), cap=Decimal("100"))

    print("Booking #1 — 2 Silver, 3 Gold, both offers stacked\n")
    bill = engine.price_booking(
        show,
        selections={"Silver": 2, "Gold": 3},
        flat_offer=festival,
        percent_offer=member,
    )
    print(bill.render())

    print("\nBooking #2 — someone tries to grab 3 Recliners (only 2 left)\n")
    try:
        engine.price_booking(show, selections={"Recliner": 3})
    except SoldOutError as e:
        print(f"Rejected at the counter: {e}")

    print("\nBooking #3 — last 2 Recliners, no offers, so we can see plain GST slabs\n")
    bill2 = engine.price_booking(show, selections={"Recliner": 2})
    print(bill2.render())

    print(f"\nRecliner tier now sold out: {show.tier('Recliner').sold_out}")