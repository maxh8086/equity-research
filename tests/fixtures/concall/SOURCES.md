# Concall-transcript fixtures: synthetic, and why

Unlike every other fixture directory in this repo, `ACME_2026-07-24.txt` is
**not** a real filing. It was written for these tests.

## Why not a real transcript

An earnings-call transcript is published by the company and is copyrighted.
The other fixtures here are exchange XBRL and bhavcopy files — factual records
this system is entitled to keep a copy of — but a transcript is prose, and
committing a company's transcript into a public repository is a different
thing. The parser needs bytes with a text layer, not this particular
company's words, so the fixture is written rather than copied.

## What was checked against a real one

The shape this fixture imitates was taken from a real NSE-hosted transcript,
read once on 2026-09-29 and not kept:

- fetched from `https://nsearchives.nseindia.com/` via the announcements API,
  with this repo's own User-Agent and no impersonation (CLAUDE.md "Breakage")
- 434 KB, 8 pages, 17,172 characters of extractable text — so a real
  transcript clears `parser.MIN_CHARS` by a wide margin, and the 2,000-char
  floor only catches scans
- the announcement payload carries `exchdisstime`, which is the correct
  `as_of` for a live adapter: exchange dissemination time, never fetch time

What the fixture reproduces from that reading: the moderator's opening and
closing, a `MANAGEMENT:` block, prepared remarks followed by an explicit
"we will now begin the question-and-answer session" handover, named analysts
introduced by the moderator, and a sentence repeated in both halves of the
call (real transcripts repeat boilerplate, which is why
`locate_quote` has to count occurrences rather than take the first).

## Files

| File | What it is |
|---|---|
| `ACME_2026-07-24.txt` | Synthetic transcript, 2,911 characters, follows the drop-folder naming convention `<SYMBOL>_<YYYY-MM-DD>.txt` |

PDFs are not committed here at all: `tests/factories.pdf_bytes` builds a
minimal PDF with a real text layer at test time, which is enough to exercise
the pypdf path and the page count.
