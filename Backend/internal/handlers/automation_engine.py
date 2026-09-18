from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from ..store.automation import AutomationAction, AutomationCondition
from ..store.tasks import TaskUpdateFields

if TYPE_CHECKING:
    from .handlers import Handlers


def task_field_value(task, field: str) -> tuple[str, bool]:
    if field == "priority":
        value = task.priority or ""
        return value, bool(value)

    if field == "status":
        value = task.status or ""
        return value, bool(value)

    if field == "assignee":
        if task.assignee_id is None:
            return "", False
        return str(task.assignee_id), True

    if field == "story_points":
        if task.story_points is None:
            return "", False
        return format(float(task.story_points), "g"), True

    if field == "moscow":
        if task.moscow is None:
            return "", False
        return str(task.moscow), True

    return "", False


def eval_condition(
    task,
    condition: AutomationCondition,
) -> bool:
    actual, present = task_field_value(
        task,
        condition.field,
    )

    op = condition.op

    if op == "is_empty":
        return not present

    if op == "not_empty":
        return present

    if op == "eq":
        return (
            present
            and actual.casefold() == condition.value.casefold()
        )

    if op == "neq":
        return (
            not present
            or actual.casefold() != condition.value.casefold()
        )

    if op in {"gt", "lt"}:
        try:
            left = float(actual)
            right = float(condition.value)
        except (TypeError, ValueError):
            return False

        return left > right if op == "gt" else left < right

    return False


def conditions_met(
    task,
    conditions: list[AutomationCondition],
) -> bool:
    return all(
        eval_condition(task, condition)
        for condition in conditions
    )


async def apply_action(
    self: "Handlers",
    task,
    rule_name: str,
    action: AutomationAction,
    actor_id: uuid.UUID,
) -> bool:
    if action.type == "assign":
        try:
            user_id = uuid.UUID(action.user_id)
        except (ValueError, TypeError):
            return False

        try:
            await self.store.tasks.update(
                task.id,
                TaskUpdateFields(
                    set_assignee=True,
                    assignee_id=user_id,
                ),
            )
        except Exception:
            return False

        try:
            await self.store.tasks.record_activity(
                task.id,
                actor_id,
                "automation",
                {
                    "rule": rule_name,
                    "assigned": str(user_id),
                },
            )
        except Exception:
            pass

        if user_id != actor_id:
            try:
                await self.store.notifications.create(
                    user_id,
                    "assigned",
                    "Tự động giao việc (automation)",
                    task.title,
                    task.id,
                    f"/projects/{task.project_id}/tasks/{task.id}",
                )
            except Exception:
                pass

        return True

    if action.type == "set_priority":
        if not action.value:
            return False

        try:
            await self.store.tasks.update(
                task.id,
                TaskUpdateFields(priority=action.value),
            )
        except Exception:
            return False

        try:
            await self.store.tasks.record_activity(
                task.id,
                actor_id,
                "automation",
                {
                    "rule": rule_name,
                    "priority": action.value,
                },
            )
        except Exception:
            pass

        return True

    if action.type == "set_status":
        if not action.value or action.value == task.status:
            return False

        try:
            await self.store.tasks.update_status(
                task.id,
                action.value,
            )
        except Exception:
            return False

        try:
            await self.store.tasks.record_activity(
                task.id,
                actor_id,
                "automation",
                {
                    "rule": rule_name,
                    "status": action.value,
                },
            )
        except Exception:
            pass

        # Exactly one automation pass, matching Go: do not recursively invoke
        # run_automations after this status mutation.
        return True

    if action.type == "notify":
        try:
            user_id = uuid.UUID(action.user_id)
        except (ValueError, TypeError):
            return False

        message = action.message or task.title

        try:
            await self.store.notifications.create(
                user_id,
                "automation",
                "Automation: " + rule_name,
                message,
                task.id,
                f"/projects/{task.project_id}/tasks/{task.id}",
            )
        except Exception:
            pass

        # Notification-only action does not mutate the task.
        return False

    return False


async def run_automations(
    self: "Handlers",
    task,
    actor_id: uuid.UUID,
) -> None:
    try:
        rules = await self.store.automations.active_for_trigger(
            task.project_id,
            task.status,
        )
    except Exception:
        return

    for rule in rules:
        if not conditions_met(task, rule.conditions):
            continue

        actions = list(rule.actions)

        # Backward compatibility for v1 rows created before the DSL.
        if (
            not actions
            and rule.action_type == "assign"
            and rule.action_assignee_id is not None
        ):
            actions = [
                AutomationAction(
                    type="assign",
                    user_id=str(rule.action_assignee_id),
                )
            ]

        for action in actions:
            await apply_action(
                self,
                task,
                rule.name,
                action,
                actor_id,
            )

