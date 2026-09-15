# Recorded responses

Fetched 2026-09-15 with the client's own user agent. Bytes are unmodified
(`.gitattributes` marks fixtures `-text`).

| File | Source |
|---|---|
| `ind_nifty50list_20160310050302.csv` | `https://web.archive.org/web/20160310050302id_/http://www.nseindia.com/content/indices/ind_nifty50list.csv` (digest `X4NQ6AAHLIWVUEQQITE4FUJ4YWIECV2V`) |
| `ind_nifty50list_20171016083142.csv` | `https://web.archive.org/web/20171016083142id_/http://niftyindices.com:80/IndexConstituent/ind_nifty50list.csv` (digest `KH6SOBBVQ5CRSCCU6GDS4CPXBSCMCSXM`) |
| `ind_nifty50list_20260915.csv` | `https://archives.nseindia.com/content/indices/ind_nifty50list.csv` (Last-Modified 2026-09-12 03:30:25 GMT) |
| `ind_niftynext50list_20260915.csv` | `https://archives.nseindia.com/content/indices/ind_niftynext50list.csv` (Last-Modified 2026-09-12 04:45:12 GMT) |
| `cdx_niftyindices_ind_nifty50list.json` | `https://web.archive.org/cdx/search/cdx?url=niftyindices.com/IndexConstituent/ind_nifty50list.csv&output=json&fl=timestamp,original,mimetype,statuscode,digest,length&filter=statuscode:200` |

The CDX fixture was recorded with a `length` column; the adapter no longer
requests it, so tests drop that column (`length` is the archive record size,
not the payload size).
