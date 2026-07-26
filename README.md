# costbasis

Cost-basis accounting from a transaction log: FIFO, LIFO, HIFO, average.

Fetching balances is the easy half. The hard half is deciding *which units you
sold*, because the answer changes your realised gain and every method gives a
different one.

```
$ python3 costbasis.py

  method           realised     short term      long term  disposals
  ------------------------------------------------------------------
  fifo            41,806.75      29,841.75      11,965.00          5
  lifo            33,398.75      21,819.55      11,579.20          5
  hifo            33,398.75      21,819.55      11,579.20          5
  average         37,936.42      24,940.40      12,996.02          3

  Spread between methods: 8,408.00. lifo realises least here.
  Which one you may use is a tax question, not a preference.
```

## Decimal, not float

Every quantity and price is a `Decimal`. Money that does not reconcile to the
cent is worse than no answer, and `0.1 + 0.2 != 0.3` in binary floating point.
There is a test asserting `0.1 + 0.2 == 0.3` exactly here.

## What it handles

- Disposals spanning several lots, with proceeds split proportionally
- Partial lots surviving a split disposal
- Acquisition fees raising the basis, disposal fees reducing proceeds
- Transfers, which move units and realise nothing
- Short and long term split at 365 days
- Out-of-order input, sorted before application

Selling more than you hold is a hard error, not a silent negative position —
it almost always means an incomplete import.

Stdlib only. Tests: `python3 -m pytest test_costbasis.py` (29 tests)

## Not handled

Wash sales, cross-asset trades (swapping A for B is modelled as two
transactions, which you have to write yourself), staking income, airdrops, and
lot-level jurisdiction rules. The 365-day threshold is hardcoded in one place
and is the first thing to change if yours differs.

This is not tax advice and the method you are permitted to use is not your
choice to make.
