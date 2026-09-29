from datetime import date, datetime
from decimal import Decimal

from core.compute.hashing import content_hash
from core.db.models import Consolidation, FactKind, FinancialFact
from core.timezones import IST

RELIANCE = "INE002A01018"


def make_fact(**overrides) -> FinancialFact:
    fields = dict(
        isin=RELIANCE,
        consolidation=Consolidation.CONSOLIDATED,
        fact_kind=FactKind.REPORTED,
        line_item="revenue_from_operations",
        xbrl_element="in-bse-fin:RevenueFromOperations",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 3, 31),
        value=Decimal("2360000000000.00"),
        unit="INR",
        rule_version="tests-1",
        as_of=datetime(2024, 4, 22, 16, 30, tzinfo=IST),
        content_hash=content_hash(b"filing-bytes"),
        source_url="https://www.bseindia.com/example.xml",
        extracted_by="tests.factories",
        model_version=None,
    )
    fields.update(overrides)
    return FinancialFact(**fields)


def pdf_bytes(pages: list[str]) -> bytes:
    """A minimal PDF with a real text layer, one page per string.

    Built here rather than committed as a fixture: a transcript PDF published
    by a company is copyrighted, and what the parser tests need is only that a
    text layer comes back with its page count. The real NSE transcript this
    shape was checked against is recorded in tests/fixtures/concall/SOURCES.md.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    for text in pages:
        lines = []
        for line in text.splitlines() or [""]:
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            lines.append(f"({escaped}) Tj T*".encode("latin-1", "replace"))
        stream = b"BT /F1 10 Tf 12 TL 40 780 Td\n" + b"\n".join(lines) + b"\nET"
        content = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        page_ids.append(add(
            b"<< /Type /Page /Parent 999 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font, content)
        ))
    kids = b" ".join(b"%d 0 R" % i for i in page_ids)
    pages_id = add(b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_ids), kids))
    root = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    objects = [o.replace(b"999 0 R", b"%d 0 R" % pages_id) for o in objects]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start_xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, root, start_xref
    )
    return bytes(out)
