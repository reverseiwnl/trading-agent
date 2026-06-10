"""
execute.py — order submission against the Alpaca PAPER endpoint.

Takes approved orders from decision_engine.py, submits them, polls for fill
status, and logs every order + outcome to data/trades.db. Never assumes success.

Hard guard: refuses to run unless config mode == "paper" AND the API base URL
is the paper endpoint. Both must be true.

TODO: implement with alpaca-py TradingClient (paper=True), SQLite logging.
"""
