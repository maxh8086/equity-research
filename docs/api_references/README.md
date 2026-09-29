# API reference knowledge base

Notes on the external APIs this system reads. One file per service, holding
what the provider documents: endpoints, parameters, limits, response shapes and
error codes. How *our* adapters use them lives in the adapter's own module and
its tests, not here — a doc that restates code goes stale silently.

## Files

| API | File | Adapter | Last checked |
|---|---|---|---|
| Upstox v3 (historical candles) | `upstox_api.md` | `ingest/upstox/` | 2026-09-29 |

## When a provider changes their API

1. Update the sections that changed and the "Last updated" date in the file.
2. Fix the adapter and its contract tests in the same change (CLAUDE.md
   "Code conventions": an API upgrade should touch one adapter and its tests).

The daily canary is what detects the change; this file is what explains it.

## Adding a reference

Create `{service}_api.md` with the official documentation links at the top,
then: authentication, endpoints (parameters and response format), constraints
and error codes. Name the adapter that uses it and stop there — do not copy
configuration keys, migration numbers or function names into the doc.

Cite it from the adapter instead:

```python
# See docs/api_references/upstox_api.md for the candle array format.
```
