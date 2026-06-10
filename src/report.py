"""
report.py — daily digest.

Writes reports/digest_<YYYY-MM-DD>.md containing: portfolio snapshot, P&L since
inception vs an equivalent lump-sum VOO benchmark, today's signals, decision
engine verdicts (approved + rejected with reasons), and any errors from the run.
The benchmark comparison is the honesty mechanism for the whole project.

TODO: implement. Benchmark = hypothetical VOO position bought with the same
cash on day one, tracked in data/trades.db.
"""
