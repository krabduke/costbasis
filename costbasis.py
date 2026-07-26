#!/usr/bin/env python3
"""Cost-basis accounting for a transaction log: FIFO, LIFO, HIFO, average.

Fetching balances from a chain or an exchange is the easy half. The hard half
is deciding which units you sold, because the answer changes your realised gain
and every method gives a different one. This does that half.

Handles partial disposals across multiple lots, fees on both sides, transfers
that are not disposals, and a running unrealised position.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

# Decimal, not float. Money that does not reconcile to the cent is worse than
# no answer, and 0.1 + 0.2 != 0.3 in binary floating point.
ZERO = Decimal("0")


class Method(Enum):
    FIFO = "fifo"     # oldest units first
    LIFO = "lifo"     # newest units first
    HIFO = "hifo"     # highest cost first, which minimises realised gain
    AVERAGE = "average"


class Kind(Enum):
    BUY = "buy"
    SELL = "sell"
    TRANSFER = "transfer"   # moves units, realises nothing


@dataclass
class Transaction:
    when: date
    kind: Kind
    asset: str
    quantity: Decimal
    price: Decimal = ZERO     # per unit, in the accounting currency
    fee: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.price < 0 or self.fee < 0:
            raise ValueError("price and fee cannot be negative")


@dataclass
class Lot:
    """A parcel of units acquired together at one cost."""

    when: date
    quantity: Decimal
    cost_per_unit: Decimal   # acquisition fee already amortised in

    @property
    def total_cost(self) -> Decimal:
        return self.quantity * self.cost_per_unit


@dataclass
class Disposal:
    when: date
    asset: str
    quantity: Decimal
    proceeds: Decimal
    cost: Decimal
    acquired: date

    @property
    def gain(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def holding_days(self) -> int:
        return (self.when - self.acquired).days

    @property
    def long_term(self) -> bool:
        """Over a year. Jurisdictions differ; this is the common threshold and
        the place to change it if yours is different."""
        return self.holding_days > 365


class Ledger:
    """Applies transactions and tracks lots per asset."""

    def __init__(self, method: Method = Method.FIFO) -> None:
        self.method = method
        self.lots: dict[str, list[Lot]] = {}
        self.disposals: list[Disposal] = []

    # ------------------------------------------------------------- applying
    def apply(self, tx: Transaction) -> None:
        if tx.kind is Kind.TRANSFER:
            return  # moving your own units between wallets realises nothing
        if tx.kind is Kind.BUY:
            self._buy(tx)
        else:
            self._sell(tx)

    def apply_all(self, transactions: list[Transaction]) -> None:
        for tx in sorted(transactions, key=lambda t: t.when):
            self.apply(tx)

    def _buy(self, tx: Transaction) -> None:
        # The acquisition fee is part of what the units cost you.
        total = tx.quantity * tx.price + tx.fee
        self.lots.setdefault(tx.asset, []).append(
            Lot(tx.when, tx.quantity, total / tx.quantity)
        )

    def _sell(self, tx: Transaction) -> None:
        lots = self.lots.get(tx.asset, [])
        held = sum((l.quantity for l in lots), ZERO)
        if tx.quantity > held:
            raise ValueError(
                f"{tx.when}: selling {tx.quantity} {tx.asset} but only {held} held. "
                f"A missing acquisition usually means an incomplete import."
            )

        # The disposal fee reduces what you actually received.
        net_proceeds = tx.quantity * tx.price - tx.fee
        remaining = tx.quantity

        if self.method is Method.AVERAGE:
            total_cost = sum((l.total_cost for l in lots), ZERO)
            avg = total_cost / held
            earliest = min(l.when for l in lots)
            self.disposals.append(Disposal(
                tx.when, tx.asset, tx.quantity, net_proceeds, avg * tx.quantity, earliest
            ))
            # Reduce every lot proportionally so the average is preserved.
            factor = (held - tx.quantity) / held
            for lot in lots:
                lot.quantity *= factor
            self.lots[tx.asset] = [l for l in lots if l.quantity > ZERO]
            return

        order = self._ordered(lots)
        consumed: list[Lot] = []

        for lot in order:
            if remaining <= ZERO:
                break
            take = min(lot.quantity, remaining)
            share = take / tx.quantity
            self.disposals.append(Disposal(
                tx.when, tx.asset, take,
                net_proceeds * share,
                take * lot.cost_per_unit,
                lot.when,
            ))
            lot.quantity -= take
            remaining -= take
            if lot.quantity <= ZERO:
                consumed.append(lot)

        self.lots[tx.asset] = [l for l in lots if l not in consumed and l.quantity > ZERO]

    def _ordered(self, lots: list[Lot]) -> list[Lot]:
        if self.method is Method.FIFO:
            return sorted(lots, key=lambda l: l.when)
        if self.method is Method.LIFO:
            return sorted(lots, key=lambda l: l.when, reverse=True)
        return sorted(lots, key=lambda l: -l.cost_per_unit)   # HIFO

    # ------------------------------------------------------------- reporting
    def holding(self, asset: str) -> Decimal:
        return sum((l.quantity for l in self.lots.get(asset, [])), ZERO)

    def cost_basis(self, asset: str) -> Decimal:
        return sum((l.total_cost for l in self.lots.get(asset, [])), ZERO)

    def average_cost(self, asset: str) -> Decimal:
        held = self.holding(asset)
        return self.cost_basis(asset) / held if held else ZERO

    def unrealised(self, marks: dict[str, Decimal]) -> Decimal:
        total = ZERO
        for asset, lots in self.lots.items():
            mark = marks.get(asset)
            if mark is None:
                continue
            total += sum((l.quantity * mark - l.total_cost for l in lots), ZERO)
        return total

    def realised(self, year: int | None = None) -> Decimal:
        return sum((d.gain for d in self.disposals
                    if year is None or d.when.year == year), ZERO)

    def realised_split(self, year: int | None = None) -> tuple[Decimal, Decimal]:
        """Returns (short_term, long_term)."""
        short = long = ZERO
        for d in self.disposals:
            if year is not None and d.when.year != year:
                continue
            if d.long_term:
                long += d.gain
            else:
                short += d.gain
        return short, long


def load_csv(path: Path) -> list[Transaction]:
    """Columns: date, kind, asset, quantity, [price], [fee]."""
    out: list[Transaction] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not any(row.values()):
                continue
            try:
                out.append(Transaction(
                    when=datetime.strptime(row["date"], "%Y-%m-%d").date(),
                    kind=Kind(row["kind"].lower()),
                    asset=row["asset"].upper(),
                    quantity=Decimal(row["quantity"]),
                    price=Decimal(row.get("price") or "0"),
                    fee=Decimal(row.get("fee") or "0"),
                ))
            except (KeyError, ValueError, InvalidOperation) as exc:
                raise SystemExit(f"{path}:{lineno}: {exc}") from exc
    if not out:
        raise SystemExit(f"{path}: no transactions found")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Cost-basis accounting from a transaction log.")
    ap.add_argument("csv", type=Path, nargs="?")
    ap.add_argument("--method", choices=[m.value for m in Method], default=None,
                    help="omit to compare every method")
    ap.add_argument("--year", type=int, default=None)
    args = ap.parse_args()

    source = args.csv or Path(__file__).parent / "sample_transactions.csv"
    transactions = load_csv(source)
    methods = [Method(args.method)] if args.method else list(Method)

    print(f"\n  {source.name}: {len(transactions)} transactions\n")
    print(f"  {'method':<10} {'realised':>14} {'short term':>14} {'long term':>14} {'disposals':>10}")
    print("  " + "-" * 66)

    for method in methods:
        ledger = Ledger(method)
        ledger.apply_all(transactions)
        short, long = ledger.realised_split(args.year)
        print(f"  {method.value:<10} {ledger.realised(args.year):>14,.2f} "
              f"{short:>14,.2f} {long:>14,.2f} {len(ledger.disposals):>10}")

    if len(methods) > 1:
        gains = {}
        for method in methods:
            ledger = Ledger(method)
            ledger.apply_all(transactions)
            gains[method.value] = ledger.realised(args.year)
        best, worst = min(gains, key=gains.get), max(gains, key=gains.get)
        print(f"\n  Spread between methods: {gains[worst] - gains[best]:,.2f}. "
              f"{best} realises least here.")
        print("  Which one you may use is a tax question, not a preference.")

    ledger = Ledger(methods[0])
    ledger.apply_all(transactions)
    print(f"\n  {'asset':<8} {'held':>16} {'cost basis':>14} {'avg cost':>12}")
    print("  " + "-" * 54)
    for asset in sorted(ledger.lots):
        if ledger.holding(asset) > ZERO:
            print(f"  {asset:<8} {ledger.holding(asset):>16,.8f} "
                  f"{ledger.cost_basis(asset):>14,.2f} {ledger.average_cost(asset):>12,.2f}")
    print()


if __name__ == "__main__":
    main()
