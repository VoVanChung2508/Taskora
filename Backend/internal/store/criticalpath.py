"""
Critical path (CPM) computation — Python port of `store.TaskStore.CriticalPath`.

Uses asyncpg for database access.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import asyncpg

EPS = 1e-9


# ---------------------------------------------------------------------------
# Domain models (equivalent of the referenced `domain.CriticalPath*` types)
# ---------------------------------------------------------------------------

@dataclass
class CriticalPathItem:
    task_id: uuid.UUID
    title: str
    status: str
    duration: float
    earliest_es: float
    earliest_ef: float
    latest_ls: float
    latest_lf: float
    slack: float
    critical: bool


@dataclass
class CriticalPath:
    items: list[CriticalPathItem] = field(default_factory=list)
    critical_task_ids: list[uuid.UUID] = field(default_factory=list)
    project_duration_days: float = 0.0


# ---------------------------------------------------------------------------
# Internal node used while running the CPM passes
# ---------------------------------------------------------------------------

@dataclass
class _Node:
    id: uuid.UUID
    title: str
    status: str
    duration: float
    es: float = 0.0  # earliest start
    ef: float = 0.0  # earliest finish
    ls: float = 0.0  # latest start
    lf: float = math.inf  # latest finish


# ---------------------------------------------------------------------------
# TaskStore.critical_path
# ---------------------------------------------------------------------------

class TaskStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def critical_path(self, project_id: uuid.UUID) -> CriticalPath:
        """
        Compute the project's critical path with the Critical Path Method (CPM).

        Duration comes from a task's start/due dates (in days, minimum 1).
        Edges come from task_dependencies: `depends_on_id` must finish
        before `task_id` starts. A task is critical when its total float
        (slack) is zero — delaying it delays the whole project.
        """
        # 1. Load tasks with their durations.
        rows = await self.pool.fetch(
            """
            SELECT id, title, status, start_date, due_date
            FROM tasks
            WHERE project_id = $1 AND parent_task_id IS NULL AND status <> 'done'
            """,
            project_id,
        )

        nodes: dict[uuid.UUID, _Node] = {}
        order: list[uuid.UUID] = []

        for row in rows:
            task_id, title, status, start, due = row[0], row[1], row[2], row[3], row[4]
            duration = 1.0
            if start is not None and due is not None:
                d = (due - start).total_seconds() / 3600 / 24
                if d >= 1:
                    duration = round(d)
            nodes[task_id] = _Node(id=task_id, title=title, status=status, duration=duration)
            order.append(task_id)

        result = CriticalPath()
        if not nodes:
            return result

        # 2. Load edges (predecessor -> successor) limited to the loaded tasks.
        erows = await self.pool.fetch(
            """
            SELECT td.depends_on_id, td.task_id
            FROM task_dependencies td
            JOIN tasks t ON t.id = td.task_id
            WHERE t.project_id = $1
            """,
            project_id,
        )

        successors: dict[uuid.UUID, list[uuid.UUID]] = {}
        predecessors: dict[uuid.UUID, list[uuid.UUID]] = {}
        indegree: dict[uuid.UUID, int] = {}
        for pred, succ in erows:
            # Skip edges pointing at tasks outside the set (e.g. completed ones).
            if pred not in nodes or succ not in nodes:
                continue
            successors.setdefault(pred, []).append(succ)
            predecessors.setdefault(succ, []).append(pred)
            indegree[succ] = indegree.get(succ, 0) + 1

        # 3. Topological order (Kahn). Cycles are prevented when adding
        # dependencies, but any leftover cycle is simply skipped rather than
        # looping forever.
        queue: list[uuid.UUID] = [node_id for node_id in order if indegree.get(node_id, 0) == 0]
        topo: list[uuid.UUID] = []
        deg = dict(indegree)
        while queue:
            node_id = queue.pop(0)
            topo.append(node_id)
            for succ in successors.get(node_id, []):
                deg[succ] -= 1
                if deg[succ] == 0:
                    queue.append(succ)

        if len(topo) != len(nodes):
            # Cyclic graph: report no critical path instead of wrong numbers.
            return result

        # 4. Forward pass — earliest start/finish.
        project_end = 0.0
        for node_id in topo:
            n = nodes[node_id]
            es = 0.0
            for p in predecessors.get(node_id, []):
                if nodes[p].ef > es:
                    es = nodes[p].ef
            n.es = es
            n.ef = es + n.duration
            if n.ef > project_end:
                project_end = n.ef

        # 5. Backward pass — latest start/finish.
        for node_id in reversed(topo):
            n = nodes[node_id]
            succs = successors.get(n.id, [])
            if succs:
                lf = math.inf
                for s in succs:
                    if nodes[s].ls < lf:
                        lf = nodes[s].ls
            else:
                lf = project_end
            n.lf = lf
            n.ls = lf - n.duration

        # 6. Slack = LS - ES; zero slack (within rounding) means critical.
        for node_id in order:
            n = nodes[node_id]
            slack = n.ls - n.es
            if abs(slack) < EPS:
                slack = 0.0
            item = CriticalPathItem(
                task_id=n.id,
                title=n.title,
                status=n.status,
                duration=n.duration,
                earliest_es=n.es,
                earliest_ef=n.ef,
                latest_ls=n.ls,
                latest_lf=n.lf,
                slack=slack,
                critical=(slack == 0),
            )
            result.items.append(item)
            if item.critical:
                result.critical_task_ids.append(n.id)

        result.project_duration_days = project_end
        return result