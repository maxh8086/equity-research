"""NSE shareholding listing adapter: live web_scrape source.

Reads NSE's /api/corporate-share-holdings-master?index=equities&symbol=<SYMBOL>
API for the 100-company universe. Downloads XBRL files from nsearchives.
Stores raw listing responses and XBRL files to blob storage before parsing.
Feeds XBRL files to the existing shareholding drop-folder adapter's parser.
"""
