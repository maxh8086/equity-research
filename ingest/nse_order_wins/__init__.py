"""Order-win adapter (Session 7e): NSE announcements in, `order_win` rows out.

Reads the raw listings the Session 7c adapter (`ingest/nse_announcements`) has
already stored, and makes no request of its own. Each announcement goes
through `core.compute.order_wins.extract_order_win`; the result is an
`order_win` row or a held-back `order_win_quarantine` row. Off unless
EQUITY_SOURCE_NSE_ORDER_WINS_ENABLED is true.
"""
