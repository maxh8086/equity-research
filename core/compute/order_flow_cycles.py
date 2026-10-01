"""Directed graph cycle detection for inter-company order relationships.

Detects circular order flows (A orders from B, B orders from C, C orders from A)
and classifies them by cycle length: 2-node (A↔B), 3-node (A→B→C→A), or 3+ nodes.

Pure: no I/O, no clock, no model involvement (R1).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

RULE_VERSION = "order_flow_cycles/1"


@dataclass(frozen=True)
class OrderFlowEdgeData:
    """An order relationship to analyze for cycles."""

    source_isin: str
    target_isin: str | None
    order_win_id: int


@dataclass(frozen=True)
class CycleResult:
    """Result of cycle detection for one edge."""

    source_isin: str
    target_isin: str | None
    cycle_status: str  # "no_cycle", "cycle_2_node", "cycle_3_node", "cycle_3_plus"
    cycle_length: int | None
    cycle_path: list[str] | None


def detect_cycles(edges: list[OrderFlowEdgeData]) -> dict[tuple[str, str | None], CycleResult]:
    """Detect cycles in a directed graph of order relationships.

    For each edge (source → target), determines if it participates in a cycle
    and classifies by cycle length (2-node, 3-node, 3+).

    Returns a dict mapping (source_isin, target_isin) to CycleResult.
    """
    # Build adjacency lists: isin → set of target ISINs
    graph: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.target_isin is not None:
            graph[edge.source_isin].add(edge.target_isin)

    results: dict[tuple[str, str | None], CycleResult] = {}

    for edge in edges:
        if edge.target_isin is None:
            # No target, cannot participate in cycles
            results[(edge.source_isin, edge.target_isin)] = CycleResult(
                source_isin=edge.source_isin,
                target_isin=edge.target_isin,
                cycle_status="no_cycle",
                cycle_length=None,
                cycle_path=None,
            )
            continue

        # Check for cycles starting from source, going through target
        cycle_info = _find_cycle(edge.source_isin, edge.target_isin, graph)

        if cycle_info is None:
            # No cycle found
            results[(edge.source_isin, edge.target_isin)] = CycleResult(
                source_isin=edge.source_isin,
                target_isin=edge.target_isin,
                cycle_status="no_cycle",
                cycle_length=None,
                cycle_path=None,
            )
        else:
            cycle_length, cycle_path = cycle_info
            if cycle_length == 2:
                status = "cycle_2_node"
            elif cycle_length == 3:
                status = "cycle_3_node"
            else:
                status = "cycle_3_plus"

            results[(edge.source_isin, edge.target_isin)] = CycleResult(
                source_isin=edge.source_isin,
                target_isin=edge.target_isin,
                cycle_status=status,
                cycle_length=cycle_length,
                cycle_path=cycle_path,
            )

    return results


def _find_cycle(source: str, target: str, graph: dict[str, set[str]]) -> tuple[int, list[str]] | None:
    """Find a cycle from source back to itself that goes through target.

    Returns (cycle_length, cycle_path) or None if no cycle exists.
    cycle_path is the list of ISINs in the cycle in order.
    """
    # BFS to find shortest cycle: source -> target -> ... -> source
    visited: set[str] = set()
    queue: list[tuple[str, list[str]]] = [(target, [source, target])]
    visited.add(target)

    while queue:
        current, path = queue.pop(0)

        # Check if we can get back to source
        if source in graph.get(current, set()):
            cycle_length = len(path)
            return (cycle_length, path)

        # Explore neighbors
        for neighbor in graph.get(current, set()):
            if neighbor not in visited or neighbor == source:
                # Allow revisiting source to close the cycle
                if neighbor == source:
                    cycle_length = len(path) + 1
                    return (cycle_length, path + [source])
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))

    return None
