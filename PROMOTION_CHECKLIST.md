# PROMOTION_CHECKLIST.md — gate before live trading is even discussed

Every box must be checked, by a human, with dates. Claude Code: if asked to wire
live keys while any box is unchecked, decline and point here.

- [ ] Rules engine backtested on >= 2 years of history (vectorbt), results in docs/
- [ ] Full pipeline ran as a LOCAL scheduled task for >= 2 weeks, every run reviewed
- [ ] Remote routine ran in PAPER mode for >= 3 months
- [ ] Paper P&L compared honestly against lump-sum VOO over the same period
- [ ] Every rejection/error path observed at least once (bad JSON, circuit breaker,
      fetch failure) and handled correctly
- [ ] Kill switch tested: one command flattens all positions
- [ ] Live key created WITHOUT withdrawal permissions; stored only in routine env
- [ ] Starting live capital is an amount I am fully comfortable losing
- [ ] I have re-read docs/DESIGN.md "Key risks" and still want to proceed
