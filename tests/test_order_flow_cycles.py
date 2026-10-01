"""Tests for core.compute.order_flow_cycles and core.resolve.order_flow_cycle_flag."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from core.compute.order_flow_cycles import (
    OrderFlowEdgeData,
    CycleResult,
    detect_cycles,
    RULE_VERSION,
)
from core.resolve.order_flow_cycle_flag import to_company_event
from core.db.models import CompanyEventSeverity, CompanyEventType

ISIN_A = "INE848E01016"
ISIN_B = "INE848E01017"
ISIN_C = "INE848E01018"
ISIN_D = "INE848E01019"


def _make_edge(source: str, target: str | None, order_win_id: int = 1) -> OrderFlowEdgeData:
    """Create a test edge."""
    return OrderFlowEdgeData(
        source_isin=source,
        target_isin=target,
        order_win_id=order_win_id,
    )


class TestDetectCyclesNoCycle:
    """Tests for no-cycle cases."""

    def test_no_cycle_single_edge(self) -> None:
        """Single edge with no return path -> no cycle."""
        edges = [_make_edge(ISIN_A, ISIN_B)]
        results = detect_cycles(edges)
        assert len(results) == 1
        result = results[(ISIN_A, ISIN_B)]
        assert result.cycle_status == "no_cycle"
        assert result.cycle_length is None
        assert result.cycle_path is None

    def test_no_cycle_linear_chain(self) -> None:
        """A -> B -> C (no return) -> no cycles."""
        edges = [
            _make_edge(ISIN_A, ISIN_B, 1),
            _make_edge(ISIN_B, ISIN_C, 2),
        ]
        results = detect_cycles(edges)
        assert len(results) == 2
        for result in results.values():
            assert result.cycle_status == "no_cycle"

    def test_no_cycle_null_target(self) -> None:
        """Edge with null target cannot participate in cycles."""
        edges = [_make_edge(ISIN_A, None, 1)]
        results = detect_cycles(edges)
        assert len(results) == 1
        result = results[(ISIN_A, None)]
        assert result.cycle_status == "no_cycle"
        assert result.cycle_length is None


class TestDetectCycles2Node:
    """Tests for 2-node cycles (A ↔ B)."""

    def test_detects_2_node_cycle(self) -> None:
        """A -> B and B -> A -> 2-node cycle."""
        edges = [
            _make_edge(ISIN_A, ISIN_B, 1),
            _make_edge(ISIN_B, ISIN_A, 2),
        ]
        results = detect_cycles(edges)
        assert len(results) == 2
        # A -> B should detect cycle
        result_ab = results[(ISIN_A, ISIN_B)]
        assert result_ab.cycle_status == "cycle_2_node"
        assert result_ab.cycle_length == 2
        assert result_ab.cycle_path == [ISIN_A, ISIN_B]
        # B -> A should also detect cycle
        result_ba = results[(ISIN_B, ISIN_A)]
        assert result_ba.cycle_status == "cycle_2_node"
        assert result_ba.cycle_length == 2
        assert result_ba.cycle_path == [ISIN_B, ISIN_A]

    def test_2_node_cycle_different_order_win_ids(self) -> None:
        """Cycles are detected regardless of order_win_id."""
        edges = [
            _make_edge(ISIN_A, ISIN_B, 1),
            _make_edge(ISIN_B, ISIN_A, 99),
        ]
        results = detect_cycles(edges)
        assert results[(ISIN_A, ISIN_B)].cycle_status == "cycle_2_node"
        assert results[(ISIN_B, ISIN_A)].cycle_status == "cycle_2_node"


class TestDetectCycles3Node:
    """Tests for 3-node cycles (A -> B -> C -> A)."""

    def test_detects_3_node_cycle(self) -> None:
        """A -> B -> C -> A -> 3-node cycle."""
        edges = [
            _make_edge(ISIN_A, ISIN_B, 1),
            _make_edge(ISIN_B, ISIN_C, 2),
            _make_edge(ISIN_C, ISIN_A, 3),
        ]
        results = detect_cycles(edges)
        assert len(results) == 3
        # All three edges should detect the cycle
        result_ab = results[(ISIN_A, ISIN_B)]
        assert result_ab.cycle_status == "cycle_3_node"
        assert result_ab.cycle_length == 3
        assert result_ab.cycle_path == [ISIN_A, ISIN_B, ISIN_C]

    def test_3_node_cycle_starting_from_different_nodes(self) -> None:
        """Cycles are detected from all entry points."""
        edges = [
            _make_edge(ISIN_A, ISIN_B, 1),
            _make_edge(ISIN_B, ISIN_C, 2),
            _make_edge(ISIN_C, ISIN_A, 3),
        ]
        results = detect_cycles(edges)
        # B -> C should also detect the cycle
        result_bc = results[(ISIN_B, ISIN_C)]
        assert result_bc.cycle_status == "cycle_3_node"
        assert result_bc.cycle_path == [ISIN_B, ISIN_C, ISIN_A]


class TestDetectCyclesMultiNode:
    """Tests for 3+ node cycles."""

    def test_detects_4_node_cycle(self) -> None:
        """A -> B -> C -> D -> A -> 3_plus cycle."""
        edges = [
            _make_edge(ISIN_A, ISIN_B, 1),
            _make_edge(ISIN_B, ISIN_C, 2),
            _make_edge(ISIN_C, ISIN_D, 3),
            _make_edge(ISIN_D, ISIN_A, 4),
        ]
        results = detect_cycles(edges)
        result_ab = results[(ISIN_A, ISIN_B)]
        assert result_ab.cycle_status == "cycle_3_plus"
        assert result_ab.cycle_length == 4
        assert result_ab.cycle_path == [ISIN_A, ISIN_B, ISIN_C, ISIN_D]

    def test_complex_graph_with_multiple_cycles(self) -> None:
        """Graph with both 2-node and 3-node cycles."""
        edges = [
            # 2-node cycle: A <-> B
            _make_edge(ISIN_A, ISIN_B, 1),
            _make_edge(ISIN_B, ISIN_A, 2),
            # 3-node cycle: B -> C -> D -> B
            _make_edge(ISIN_B, ISIN_C, 3),
            _make_edge(ISIN_C, ISIN_D, 4),
            _make_edge(ISIN_D, ISIN_B, 5),
        ]
        results = detect_cycles(edges)
        # A -> B has 2-node cycle
        assert results[(ISIN_A, ISIN_B)].cycle_status == "cycle_2_node"
        # B -> C has 3-node cycle
        assert results[(ISIN_B, ISIN_C)].cycle_status == "cycle_3_node"


class TestCompanyEventGeneration:
    """Tests for converting CycleResult to CompanyEvent."""

    def test_no_event_for_no_cycle(self) -> None:
        """No cycle -> to_company_event returns None."""
        result = CycleResult(
            source_isin=ISIN_A,
            target_isin=ISIN_B,
            cycle_status="no_cycle",
            cycle_length=None,
            cycle_path=None,
        )
        event = to_company_event(
            source_isin=ISIN_A,
            cycle_result=result,
            event_date=date(2024, 10, 1),
            evidence_url="https://example.com/edge.pdf",
            rule_version=RULE_VERSION,
            as_of=datetime(2024, 10, 1, tzinfo=timezone.utc),
            extracted_by="test",
            content_hash="a" * 64,
            source_url="https://example.com/edge.pdf",
        )
        assert event is None

    def test_2_node_cycle_severity_is_high(self) -> None:
        """2-node cycle -> HIGH severity."""
        result = CycleResult(
            source_isin=ISIN_A,
            target_isin=ISIN_B,
            cycle_status="cycle_2_node",
            cycle_length=2,
            cycle_path=[ISIN_A, ISIN_B],
        )
        event = to_company_event(
            source_isin=ISIN_A,
            cycle_result=result,
            event_date=date(2024, 10, 1),
            evidence_url="https://example.com/edge.pdf",
            rule_version=RULE_VERSION,
            as_of=datetime(2024, 10, 1, tzinfo=timezone.utc),
            extracted_by="test",
            content_hash="b" * 64,
            source_url="https://example.com/edge.pdf",
        )
        assert event is not None
        assert event.severity == CompanyEventSeverity.HIGH
        assert event.event_type == CompanyEventType.ORDER_FLOW_CYCLE
        assert event.value == Decimal("2")

    def test_3_node_cycle_severity_is_medium(self) -> None:
        """3-node cycle -> MEDIUM severity."""
        result = CycleResult(
            source_isin=ISIN_A,
            target_isin=ISIN_B,
            cycle_status="cycle_3_node",
            cycle_length=3,
            cycle_path=[ISIN_A, ISIN_B, ISIN_C],
        )
        event = to_company_event(
            source_isin=ISIN_A,
            cycle_result=result,
            event_date=date(2024, 10, 1),
            evidence_url="https://example.com/edge.pdf",
            rule_version=RULE_VERSION,
            as_of=datetime(2024, 10, 1, tzinfo=timezone.utc),
            extracted_by="test",
            content_hash="c" * 64,
            source_url="https://example.com/edge.pdf",
        )
        assert event is not None
        assert event.severity == CompanyEventSeverity.MEDIUM
        assert event.value == Decimal("3")

    def test_3plus_node_cycle_severity_is_low(self) -> None:
        """3+ node cycle -> LOW severity."""
        result = CycleResult(
            source_isin=ISIN_A,
            target_isin=ISIN_B,
            cycle_status="cycle_3_plus",
            cycle_length=4,
            cycle_path=[ISIN_A, ISIN_B, ISIN_C, ISIN_D],
        )
        event = to_company_event(
            source_isin=ISIN_A,
            cycle_result=result,
            event_date=date(2024, 10, 1),
            evidence_url="https://example.com/edge.pdf",
            rule_version=RULE_VERSION,
            as_of=datetime(2024, 10, 1, tzinfo=timezone.utc),
            extracted_by="test",
            content_hash="d" * 64,
            source_url="https://example.com/edge.pdf",
        )
        assert event is not None
        assert event.severity == CompanyEventSeverity.LOW
        assert event.value == Decimal("4")

    def test_company_event_model_version_is_none(self) -> None:
        """model_version must be None: no model involvement (R1)."""
        result = CycleResult(
            source_isin=ISIN_A,
            target_isin=ISIN_B,
            cycle_status="cycle_2_node",
            cycle_length=2,
            cycle_path=[ISIN_A, ISIN_B, ISIN_A],
        )
        event = to_company_event(
            source_isin=ISIN_A,
            cycle_result=result,
            event_date=date(2024, 10, 1),
            evidence_url="https://example.com/edge.pdf",
            rule_version=RULE_VERSION,
            as_of=datetime(2024, 10, 1, tzinfo=timezone.utc),
            extracted_by="test",
            content_hash="e" * 64,
            source_url="https://example.com/edge.pdf",
        )
        assert event is not None
        assert event.model_version is None

    def test_company_event_has_required_fields(self) -> None:
        """CompanyEvent has all required fields including evidence_url."""
        result = CycleResult(
            source_isin=ISIN_A,
            target_isin=ISIN_B,
            cycle_status="cycle_2_node",
            cycle_length=2,
            cycle_path=[ISIN_A, ISIN_B],
        )
        evidence_url = "https://example.com/filing.pdf#page=5"
        event = to_company_event(
            source_isin=ISIN_A,
            cycle_result=result,
            event_date=date(2024, 10, 1),
            evidence_url=evidence_url,
            rule_version=RULE_VERSION,
            as_of=datetime(2024, 10, 1, tzinfo=timezone.utc),
            extracted_by="test_adapter",
            content_hash="f" * 64,
            source_url="https://example.com/filing.pdf",
        )
        assert event is not None
        assert event.isin == ISIN_A
        assert event.evidence_url == evidence_url
        assert event.rule_version == RULE_VERSION
        assert event.extracted_by == "test_adapter"
        assert event.metric == "cycle_length"
        assert event.value == Decimal("2")
        assert event.threshold == Decimal("2")
