"""NSE corporate announcements adapter (web_scrape source).

Fetches announcements from NSE's /api/corporate-announcements?index=equities endpoint,
stores raw responses to blob, validates with strict Pydantic, classifies by hardcoded
keyword rules and event-date extraction, resolves ISINs from symbols via dated lookup,
and writes ScheduledEvent rows.

Unclassifiable announcements (no type matched or no event date extracted) are stored raw but
not written to any table. Rows that do not resolve to ISIN are quarantined for review.

Session 7c: announcements adapter with drop-folder fallback.
Session 7d will reuse the shared NSE session helper (ingest/nse_session.py).
"""
