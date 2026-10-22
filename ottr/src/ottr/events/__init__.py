"""
Events and macro: what the US desk must know beyond prices, from four free sources.

- `fred`: macro series (VIX, Treasury yields and curve, high-yield spread, Fed funds,
  breakeven inflation) from FRED's keyless CSV, as context for the council.
- `earnings`: the earnings calendar from Alpha Vantage (one call a day). A single stock
  with no known report date, or one within the blackout, gets no new BUY.
- `sec`: the SEC's ticker lists and company profiles, which tell an operating company from
  a fund, trust or SPAC (the screener's pool, PR 10).
- `corporate`: corporate actions from Alpaca (dividends, splits, mergers, spin-offs...),
  classified by whether the book models them and whether they block a BUY.
- `db` keeps all of it in the book's database, `refresh` fetches and stores each feed,
  `view` reads it back for the gate (the `Events` protocol) and the council packet, and
  `check` is the live part of `ottr data --check`.

Why this shape: the clients only parse and validate, and the database is the single
source the engine reads, so a feed that is down leaves the last good data in place with
its age on record. Every reader turns data that is too old into "unknown", never into
"nothing scheduled": an earnings date or corporate action we can't vouch for blocks the
BUY it guards.
"""
