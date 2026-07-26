from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from costbasis import Kind, Ledger, Method, Transaction, ZERO, load_csv

D = Decimal
SAMPLE = Path(__file__).parent / "sample_transactions.csv"


def buy(day, qty, price, fee="0", asset="BTC"):
    return Transaction(date(2024, 1, day), Kind.BUY, asset, D(qty), D(price), D(fee))


def sell(day, qty, price, fee="0", asset="BTC", year=2025):
    return Transaction(date(year, 1, day), Kind.SELL, asset, D(qty), D(price), D(fee))


def ledger_for(method, *txs):
    l = Ledger(method)
    l.apply_all(list(txs))
    return l


# ----------------------------------------------------------------- validation

def test_rejects_zero_quantity():
    with pytest.raises(ValueError, match="quantity must be positive"):
        Transaction(date(2024, 1, 1), Kind.BUY, "BTC", D("0"), D("100"))


def test_rejects_negative_fee():
    with pytest.raises(ValueError, match="cannot be negative"):
        Transaction(date(2024, 1, 1), Kind.BUY, "BTC", D("1"), D("100"), D("-1"))


def test_selling_more_than_held_is_an_error():
    with pytest.raises(ValueError, match="only"):
        ledger_for(Method.FIFO, buy(1, "1", "100"), sell(2, "2", "200"))


def test_uses_decimal_not_float():
    l = ledger_for(Method.FIFO, buy(1, "0.1", "100"), buy(2, "0.2", "100"))
    assert l.holding("BTC") == D("0.3")   # exactly, which floats cannot manage


# ---------------------------------------------------------------- accounting

def test_buy_adds_a_lot():
    l = ledger_for(Method.FIFO, buy(1, "2", "100"))
    assert l.holding("BTC") == D("2")
    assert l.cost_basis("BTC") == D("200")


def test_acquisition_fee_raises_the_basis():
    l = ledger_for(Method.FIFO, buy(1, "2", "100", fee="10"))
    assert l.cost_basis("BTC") == D("210")
    assert l.average_cost("BTC") == D("105")


def test_disposal_fee_reduces_proceeds():
    plain = ledger_for(Method.FIFO, buy(1, "1", "100"), sell(2, "1", "200"))
    feed = ledger_for(Method.FIFO, buy(1, "1", "100"), sell(2, "1", "200", fee="5"))
    assert feed.realised() == plain.realised() - D("5")


def test_transfer_realises_nothing():
    l = Ledger(Method.FIFO)
    l.apply_all([
        buy(1, "1", "100"),
        Transaction(date(2024, 6, 1), Kind.TRANSFER, "BTC", D("1")),
    ])
    assert l.disposals == []
    assert l.holding("BTC") == D("1")


def test_full_disposal_empties_the_position():
    l = ledger_for(Method.FIFO, buy(1, "1", "100"), sell(2, "1", "200"))
    assert l.holding("BTC") == ZERO


# ------------------------------------------------------------------- methods

def test_fifo_uses_the_oldest_lot():
    l = ledger_for(Method.FIFO, buy(1, "1", "100"), buy(2, "1", "300"), sell(3, "1", "400"))
    assert l.realised() == D("300")    # sold the 100 lot


def test_lifo_uses_the_newest_lot():
    l = ledger_for(Method.LIFO, buy(1, "1", "100"), buy(2, "1", "300"), sell(3, "1", "400"))
    assert l.realised() == D("100")    # sold the 300 lot


def test_hifo_uses_the_most_expensive_lot():
    l = ledger_for(Method.HIFO, buy(1, "1", "100"), buy(2, "1", "500"), buy(3, "1", "300"),
                   sell(4, "1", "600"))
    assert l.realised() == D("100")    # sold the 500 lot


def test_hifo_differs_from_lifo_when_newest_is_not_dearest():
    txs = (buy(1, "1", "500"), buy(2, "1", "100"), sell(3, "1", "600"))
    assert ledger_for(Method.HIFO, *txs).realised() == D("100")
    assert ledger_for(Method.LIFO, *txs).realised() == D("500")


def test_average_uses_the_blended_cost():
    l = ledger_for(Method.AVERAGE, buy(1, "1", "100"), buy(2, "1", "300"), sell(3, "1", "400"))
    assert l.realised() == D("200")    # basis 200, the average of 100 and 300


def test_average_preserves_the_average_after_a_partial_sale():
    l = ledger_for(Method.AVERAGE, buy(1, "1", "100"), buy(2, "1", "300"), sell(3, "1", "400"))
    assert l.average_cost("BTC") == D("200")


def test_all_methods_agree_when_there_is_one_lot():
    txs = (buy(1, "2", "100"), sell(2, "1", "150"))
    results = {m: ledger_for(m, *txs).realised() for m in Method}
    assert len(set(results.values())) == 1


def test_methods_disagree_on_the_sample():
    txs = load_csv(SAMPLE)
    results = {m.value: ledger_for(m, *txs).realised() for m in Method}
    assert len(set(results.values())) > 1
    assert results["fifo"] > results["lifo"]


# ------------------------------------------------------------ partial lots

def test_disposal_spans_multiple_lots():
    l = ledger_for(Method.FIFO, buy(1, "1", "100"), buy(2, "1", "200"), sell(3, "1.5", "300"))
    assert len(l.disposals) == 2
    assert sum((d.quantity for d in l.disposals), ZERO) == D("1.5")


def test_partial_lot_remains_after_a_split_disposal():
    l = ledger_for(Method.FIFO, buy(1, "1", "100"), buy(2, "1", "200"), sell(3, "1.5", "300"))
    assert l.holding("BTC") == D("0.5")
    assert l.cost_basis("BTC") == D("100")   # half of the 200 lot


def test_proceeds_are_split_proportionally():
    l = ledger_for(Method.FIFO, buy(1, "1", "100"), buy(2, "1", "100"), sell(3, "2", "300"))
    assert sum((d.proceeds for d in l.disposals), ZERO) == D("600")


# ---------------------------------------------------------- holding periods

def test_short_term_under_a_year():
    l = Ledger(Method.FIFO)
    l.apply_all([
        Transaction(date(2024, 6, 1), Kind.BUY, "BTC", D("1"), D("100")),
        Transaction(date(2024, 12, 1), Kind.SELL, "BTC", D("1"), D("200")),
    ])
    assert not l.disposals[0].long_term
    assert l.realised_split() == (D("100"), ZERO)


def test_long_term_over_a_year():
    l = Ledger(Method.FIFO)
    l.apply_all([
        Transaction(date(2023, 1, 1), Kind.BUY, "BTC", D("1"), D("100")),
        Transaction(date(2024, 6, 1), Kind.SELL, "BTC", D("1"), D("200")),
    ])
    assert l.disposals[0].long_term
    assert l.realised_split() == (ZERO, D("100"))


def test_year_filter():
    txs = load_csv(SAMPLE)
    l = ledger_for(Method.FIFO, *txs)
    assert l.realised(2025) != ZERO
    assert l.realised(2020) == ZERO


# ------------------------------------------------------------- unrealised

def test_unrealised_marks_open_lots():
    l = ledger_for(Method.FIFO, buy(1, "2", "100"))
    assert l.unrealised({"BTC": D("150")}) == D("100")


def test_unrealised_ignores_unmarked_assets():
    l = ledger_for(Method.FIFO, buy(1, "1", "100"))
    assert l.unrealised({}) == ZERO


def test_unrealised_can_be_negative():
    l = ledger_for(Method.FIFO, buy(1, "1", "100"))
    assert l.unrealised({"BTC": D("60")}) == D("-40")


# ---------------------------------------------------------------- loading

def test_sample_loads():
    assert len(load_csv(SAMPLE)) == 9


def test_out_of_order_input_is_sorted():
    l = Ledger(Method.FIFO)
    l.apply_all([sell(3, "1", "400"), buy(1, "1", "100"), buy(2, "1", "300")])
    assert l.realised() == D("300")   # FIFO still picked the 100 lot


def test_bad_row_reports_the_line(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("date,kind,asset,quantity,price\n2024-01-01,buy,BTC,1,100\nnope,buy,BTC,1,100\n")
    with pytest.raises(SystemExit, match=":3:"):
        load_csv(bad)
