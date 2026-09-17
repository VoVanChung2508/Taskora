"""
Critical Path Method (CPM) scheduling maths — Python port of the Go tests
in `store/critical_path_test.go`.

The CPM maths exercised in `store.CriticalPath` is tested here on a plain
in-memory graph, mirroring the forward/backward passes without needing a
database. It guards the schedule arithmetic itself (SQL loading is covered
by runtime checks, not by this file).

Run with: pytest test_critical_path.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pytest


@dataclass
class CPMNode:
    id: str
    duration: float
    es: float = 0.0
    ef: float = 0.0
    ls: float = 0.0
    lf: float = 0.0


def solve(
    durations: dict[str, float],
    edges: Optional[list[tuple[str, str]]],
) -> tuple[dict[str, CPMNode], float, bool]:
    """Run the same forward/backward passes as `store.CriticalPath`."""
    edges = edges or []

    nodes: dict[str, CPMNode] = {}
    order: list[str] = []
    for node_id, d in durations.items():
        nodes[node_id] = CPMNode(id=node_id, duration=d)
        order.append(node_id)

    successors: dict[str, list[str]] = {}
    predecessors: dict[str, list[str]] = {}
    indegree: dict[str, int] = {}
    for src, dst in edges:
        successors.setdefault(src, []).append(dst)
        predecessors.setdefault(dst, []).append(src)
        indegree[dst] = indegree.get(dst, 0) + 1

    queue: list[str] = [node_id for node_id in order if indegree.get(node_id, 0) == 0]
    deg = dict(indegree)
    topo: list[str] = []
    while queue:
        node_id = queue.pop(0)
        topo.append(node_id)
        for s in successors.get(node_id, []):
            deg[s] -= 1
            if deg[s] == 0:
                queue.append(s)

    if len(topo) != len(nodes):
        return nodes, 0.0, False  # cycle

    end = 0.0
    for node_id in topo:
        n = nodes[node_id]
        es = 0.0
        for p in predecessors.get(node_id, []):
            if nodes[p].ef > es:
                es = nodes[p].ef
        n.es, n.ef = es, es + n.duration
        if n.ef > end:
            end = n.ef

    for node_id in reversed(topo):
        n = nodes[node_id]
        ss = successors.get(n.id, [])
        if ss:
            lf = math.inf
            for s in ss:
                if nodes[s].ls < lf:
                    lf = nodes[s].ls
        else:
            lf = end
        n.lf, n.ls = lf, lf - n.duration

    return nodes, end, True


def _is_critical(nodes: dict[str, CPMNode], node_id: str) -> bool:
    return math.isclose(nodes[node_id].ls, nodes[node_id].es, abs_tol=1e-9)


def test_cpm_simple_chain():
    # A(2) -> B(3) -> C(1): everything is on the only path, so nothing has slack.
    nodes, end, ok = solve(
        {"A": 2, "B": 3, "C": 1},
        [("A", "B"), ("B", "C")],
    )
    assert ok, "unexpected cycle"
    assert end == 6, f"project duration = {end}, want 6"

    for node_id in ("A", "B", "C"):
        slack = nodes[node_id].ls - nodes[node_id].es
        assert abs(slack) <= 1e-9, (
            f"{node_id} slack = {slack}, want 0 (chain is fully critical)"
        )


def test_cpm_parallel_branch_has_slack():
    #        +- B(5) -+
    #  A(1) -+        +-> D(1)      critical: A -> B -> D  (duration 7)
    #        +- C(2) -+             C has 3 days of slack
    nodes, end, ok = solve(
        {"A": 1, "B": 5, "C": 2, "D": 1},
        [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")],
    )
    assert ok, "unexpected cycle"
    assert end == 7, f"project duration = {end}, want 7"

    for node_id in ("A", "B", "D"):
        assert _is_critical(nodes, node_id), f"{node_id} should be on the critical path"

    assert not _is_critical(nodes, "C"), "C should not be critical — the shorter branch has slack"

    slack_c = nodes["C"].ls - nodes["C"].es
    assert abs(slack_c - 3) <= 1e-9, f"C slack = {slack_c}, want 3"


def test_cpm_independent_tasks():
    # No edges: the longest task alone sets the duration; shorter ones float.
    nodes, end, ok = solve({"A": 4, "B": 1}, None)
    assert ok, "unexpected cycle"
    assert end == 4, f"duration = {end}, want 4"

    slack_b = nodes["B"].ls - nodes["B"].es
    assert abs(slack_b - 3) <= 1e-9, f"B slack = {slack_b}, want 3"


def test_cpm_detects_cycle():
    # A -> B -> A must be reported rather than looping or emitting wrong numbers.
    _, _, ok = solve({"A": 1, "B": 1}, [("A", "B"), ("B", "A")])
    assert not ok, "expected the cycle to be detected"