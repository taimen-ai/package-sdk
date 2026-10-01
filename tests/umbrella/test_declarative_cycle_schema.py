"""Декларативный цикл (TAI-ADR-0053): контракт каталога — схема пакетов.

Приёмка по умолчанию у типа задачи с условием `when`, личность и связи у правил,
вид исполнителя `git-connector`, вид `NotificationRule`. Грамматику выражений,
шаблонов и условий проверяют ядро и сервис уведомлений; схема — форму.
"""

from __future__ import annotations

import copy

import pytest

from tests.umbrella._shim import UMBRELLA as ROOT
from tests.umbrella._shim import cp

LIFECYCLE = {
    "initialStatus": "todo",
    "statuses": [
        {"key": "todo", "category": "active"},
        {"key": "done", "category": "terminal_success"},
    ],
    "transitions": [{"from": "todo", "to": ["done"]}],
    "claimTransition": None,
    "releaseTransition": None,
    "completionTransition": "done",
}

CODING_TASK = {
    "apiVersion": "taimen.ai/v1",
    "kind": "TaskType",
    "key": "coding-task",
    "spec": {
        "displayName": "Задача с кодом",
        "lifecycleSchema": LIFECYCLE,
        "acceptance": [
            {
                "key": "review",
                "kind": "human",
                "description": "Ревью",
                "spec": {"approver": "${SELFDEV_REVIEWER_PRINCIPAL}"},
                "when": ["$.task.artifact[commit].metadata.published"],
            },
            {
                "key": "merge",
                "kind": "deterministic",
                "description": "Вливание",
                "spec": {
                    "skill": "git.merge@1",
                    "inputs": {"branch": "$.task.artifact[commit].metadata.branch!"},
                    "expect": {"merged": True},
                },
                "when": ["$.task.artifact[commit].metadata.published"],
            },
        ],
    },
}

EXPAND_RULE = {
    "apiVersion": "taimen.ai/v1",
    "kind": "WorkRule",
    "key": "feature-expand",
    "spec": {
        "workspaceId": "${SELFDEV_WORKSPACE_ID}",
        "identity": {"agent": "sdd-rules"},
        "trigger": {"kind": "event", "type": "task.completed"},
        "condition": {"eq": [{"var": "task.typeKey"}, "feature-tasks"]},
        "interpretation": {"skill": "tasks.check@1", "inputs": {"task": "{{task.id}}"}},
        "action": {
            "kind": "ensure_work",
            "forEach": "skill.output.items",
            "taskType": "{{item.type}}",
            "taskTypes": ["coding-task", "feature-converge"],
            "dedupKeyTemplate": "feature:{{task.customFields.featureSlug}}:{{item.id}}",
            "fields": {
                "title": "{{item.title}}",
                "assignee": "{{item.assignee}}",
                "relations": {"spawnedBy": "{{task.id}}", "dependsOn": "{{item.dependsOn}}"},
            },
        },
    },
}

CONNECTOR = {
    "apiVersion": "taimen.ai/v1",
    "kind": "Agent",
    "key": "git-connector",
    "spec": {
        "displayName": "Git connector",
        "identity": {"kind": "agent", "permissions": ["observations.write", "tasks.read"]},
        "work": {"workspace": "${SELFDEV_WORKSPACE_ID}"},
        "executor": {
            "kind": "git-connector",
            "params": {
                "repositories": [
                    {"name": "control-plane", "url": "https://git.example/org/control-plane.git"},
                    {
                        "name": "memory-service",
                        "url": "https://git.example/org/memory-service.git",
                        "branch": "master",
                    },
                ],
                "observe": ["commits", "adrRegistry", "ciRuns"],
                "intervalSeconds": 300,
                "registryRepository": "control-plane",
                "ciRepository": "org/superproject",
            },
        },
        "placement": {"requires": ["staging"], "secrets": ["github-token"]},
    },
}

APPROVAL_REQUESTED = {
    "apiVersion": "taimen.ai/v1",
    "kind": "NotificationRule",
    "key": "approval-requested",
    "spec": {
        "on": {"type": "approval.requested"},
        "recipient": {"kind": "assigned", "ref": "payload.assignedPrincipalId", "fallback": "none"},
        "notification": {
            "type": "control_plane.approval_requested",
            "title": "Нужно решение: {{task.title}}",
            "body": "Запрашивает: {{payload.requestedBy}}\n{{payload.comment}}",
            "actions": ["approvalDecide"],
        },
        "dedupKeyTemplate": "control-plane:approval:{{payload.approvalId}}",
        "close": {
            "on": ["approval.approved", "approval.rejected", "approval.cancelled"],
            "outcome": "{{event.type}}",
        },
    },
}


def errors(document: dict) -> list[str]:
    return [cp._format_schema_error(e) for e in cp._schema_validator().iter_errors(document)]


@pytest.mark.parametrize(
    "document", [CODING_TASK, EXPAND_RULE, CONNECTOR, APPROVAL_REQUESTED], ids=lambda d: d["kind"]
)
def test_new_fields_and_kinds_are_valid(document):
    assert errors(document) == []


def test_a_criterion_condition_reads_the_task_only():
    doc = copy.deepcopy(CODING_TASK)
    doc["spec"]["acceptance"][0]["when"] = ["$.approval.id"]
    assert errors(doc)


def test_a_rule_identity_names_an_agent_description():
    doc = copy.deepcopy(EXPAND_RULE)
    doc["spec"]["identity"] = {"principal": "7ac62de6"}
    assert errors(doc)


def test_the_connector_needs_repositories():
    doc = copy.deepcopy(CONNECTOR)
    del doc["spec"]["executor"]["params"]["repositories"]
    assert errors(doc)
    doc["spec"]["executor"]["params"] = {
        "repositories": [{"name": "x", "url": "u"}],
        "observe": ["issues"],
    }
    assert errors(doc)


def test_a_notification_rule_names_an_event_a_recipient_and_known_actions():
    for mutate in (
        lambda s: s["on"].update(type="Approval Requested"),
        lambda s: s["recipient"].update(kind="everyone"),
        lambda s: s["notification"].update(actions=["delete"]),
        lambda s: s.pop("notification"),
    ):
        doc = copy.deepcopy(APPROVAL_REQUESTED)
        mutate(doc["spec"])
        assert errors(doc)


def test_existing_packages_still_pass_the_schema():
    for path in sorted((ROOT / "packages").glob("*/**/*.yaml")):
        document = cp._read_yaml(path)
        if isinstance(document, dict) and "kind" in document:
            assert errors(document) == [], path
