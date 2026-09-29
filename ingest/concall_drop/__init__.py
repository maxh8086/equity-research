"""Earnings-call transcripts saved by hand: text extraction only (CLAUDE.md store 2, Session 9).

This package gets the words out of the file and nothing else. Reading the
words -- which sentence carries a commitment, who said it -- is a model's job
and lives in `extract/`, which runs as its own step over the bytes this
adapter stored. Keeping them apart means a transcript can be ingested, hashed
and reviewed without any model spend, and a re-extraction under a newer model
never re-downloads anything.
"""
