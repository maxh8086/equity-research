"""Narrative extraction from documents. One of the two packages that may call a model.

`extract/` asks a model to point at things a parser cannot find: what a person
said in prepared remarks, which sentence carries a commitment. It never asks a
model for a number that exists in XBRL, for arithmetic, or for a judgement
about a threshold (R1). Whatever comes back is checked against the source
document by code before it becomes a row, and what fails a check is
quarantined rather than repaired.

Model access is through `gateway` only; no provider SDK is imported here.
"""
