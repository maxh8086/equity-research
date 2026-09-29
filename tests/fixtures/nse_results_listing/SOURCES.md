# NSE financial-results listing fixture

`results_listing_infy_quarterly.json` is the shape of the response of NSE's
public financial-results listing:

    https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=INFY&period=Quarterly

It is a JSON list, one object per filing. It was **not recorded live**: it was
written by hand in that shape. The two XBRL URLs and their `exchdisstime`
values are the real ones for the recorded filings in `tests/fixtures/nse_xbrl/`
(see that folder's SOURCES.md). The other keys are reconstructed and unverified.
The adapter depends only on `symbol`, `xbrl` and `exchdisstime`, and ignores the
rest, so a wrong guess about them cannot corrupt a load. The third row has no
XBRL attachment (`xbrl` is "-"), as older filings do.

The first live run must record a real response here and replace this file.
