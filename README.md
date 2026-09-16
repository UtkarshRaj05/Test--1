Multiplex Pricing Engine

A pricing engine for a cinema box-office counter — built for any show, not one screening. It cleans a messy raw seat-price feed, prices a booking against live seat inventory, stacks discount offers, adds a convenience fee, applies slab-based GST, and prints a line-by-line bill that always sums to the exact paisa.

Requirements
Python 3.8+ (uses dataclasses and f-strings; no third-party packages)
Nothing to install — the standard library (decimal, re, dataclasses) is all it uses
Project layout


pricing_engine.py   # the entire engine: import, inventory, offers, tax, engine, demo
README.md           # this file
REASONING.md         # design rationale and the trade-offs behind each decision
Everything lives in one file by design — there's no package boundary worth paying for at this size, and it keeps the whole flow (messy data in → clean bill out) readable top to bottom.

Running it


bash
python3 pricing_engine.py
This runs the built-in demo, which:

Imports a deliberately messy 14-row raw price list (duplicate tier names in different cases, inconsistent price formats, blank fields, a negative price) and prints an import report — what was imported, de-duplicated, and rejected, with a reason for every row.
Builds a Show from the cleaned tiers.
Prices a real booking (2 Silver + 3 Gold) with a flat festival discount and a capped member discount stacked together, and prints the full line-by-line bill.
Tries to overbook a nearly sold-out Recliner tier and shows the booking being correctly rejected before any seats are touched.
Books the last two Recliners with no offers, so you can see the plain GST slab calculation on its own.
Using it in your own code


python
from pricing_engine import (
    Show, SeatTier, PricingEngine, FlatOffer, PercentOffer,
    GSTConfig, import_seat_price_list, SoldOutError,
)

# 1. Clean a raw price feed (optional — you can also build tiers directly)
report = import_seat_price_list(raw_rows)   # raw_rows: list of {"tier", "price", "seats"}
print(report.render())                       # audit trail

# 2. Build the show
show = Show(title="Your show", tiers={t.name: t for t in report.imported})

# 3. Price a booking
engine = PricingEngine(convenience_fee_per_ticket=Decimal("30"))
bill = engine.price_booking(
    show,
    selections={"Silver": 2, "Gold": 3},
    flat_offer=FlatOffer(code="FEST50", amount=Decimal("50")),
    percent_offer=PercentOffer(code="MEMBER10", percent=Decimal("10"), cap=Decimal("100")),
)
print(bill.render())
Set reserve=False on price_booking(...) if you want a quote without touching seat inventory (e.g. showing a price before payment is confirmed).

Configuring tax and fees for your cinema


python
engine = PricingEngine(
    gst=GSTConfig(
        ticket_low_rate=Decimal("12"),
        ticket_high_rate=Decimal("18"),
        ticket_slab_threshold=Decimal("100"),  # ₹, per ticket, post-discount
        convenience_fee_rate=Decimal("18"),
    ),
    convenience_fee_per_ticket=Decimal("30"),
)
All rates and the fee amount are constructor arguments — nothing is hard-coded past this point, so a different multiplex's tax slabs or fee structure is a config change, not a code change.

Debugging
SoldOutError — a booking asked for more seats in a tier than are currently available. price_booking validates every requested tier's availability before reserving anything, so a booking never partially succeeds and leaves inventory in a half-booked state.
UnknownTierError — a booking referenced a tier name that isn't on the show (check spelling/casing against show.tiers.keys()).
EmptyBookingError — selections had no seats in it (all zero or missing).
Line items don't seem to add up — they will, to the paisa; if a manual check looks off, you're probably eyeballing intermediate Decimal values before they're quantized. Use bill.render() or read bill.grand_total directly rather than re-summing by hand.
A row you expected to import got rejected — read report.render(); every dropped row states its reason (blank field, unparseable price, negative price/seats, or a name collision with a conflicting price). If two rows for the same tier name should both count, they need to agree on price — that's treated as a genuine data conflict, not something the engine silently resolves.
Want to change how duplicates or conflicts are handled? — that logic lives entirely inside import_seat_price_list; see REASONING.md for why it currently keeps the first valid row and rejects later conflicting ones.
Testing changes
There's no test framework wired up — the if __name__ == "__main__" block at the bottom of pricing_engine.py doubles as a smoke test. After any change, rerun it and check:

the import report still shows the expected imported / deduplicated / rejected counts,
Booking #1's line items still sum exactly to GRAND TOTAL,
the sold-out booking still raises SoldOutError.