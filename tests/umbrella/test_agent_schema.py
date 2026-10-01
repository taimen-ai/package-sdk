"""Вид каталога Agent (TAI-ADR-0052): контракт описания агента — схема пакетов.

Примеры — по одному на вид исполнителя и описание без размещения; ядро
(CP-ADR-0073) и установщик пакетов проверяют то же описание своими валидаторами.
"""

from __future__ import annotations

import copy

import pytest

from tests.umbrella._shim import cp

CODER = {
    "apiVersion": "taimen.ai/v1",
    "kind": "Agent",
    "key": "coder",
    "spec": {
        "displayName": "Autonomous coder",
        "identity": {
            "kind": "agent",
            "roles": ["coder"],
            "permissions": ["tasks.read", "tasks.claim", "sessions.open", "artifacts.write"],
        },
        "work": {
            "workspace": "${SELFDEV_WORKSPACE_ID}",
            "onlyAssigned": True,
            "taskTypes": ["coding-task"],
        },
        "executor": {
            "kind": "claude-code",
            "params": {
                "model": "claude-opus-5-5",
                "permissionMode": "bypassPermissions",
                "timeoutSeconds": 10800,
                "tools": {"deny": ["WebFetch"]},
            },
            "instructions": "Соглашения репозитория: …",
        },
        "workingCopy": {
            "repository": "https://git.example/org/control-plane.git",
            "directory": "control-plane",
            "neighbours": {"platform-auth-sdk": "https://git.example/org/platform-auth-sdk.git"},
            "review": {
                "mode": "human",
                "taskType": "code-review-merge",
                "reviewer": "${SELFDEV_REVIEWER_PRINCIPAL}",
            },
        },
        "skills": {
            "protocols": ["local", "http"],
            "local": ["taimen_selfdev.git_merge:run"],
            "httpOrigins": ["https://platform.example.com"],
        },
        "placement": {
            "requires": ["claude-subscription", "repos"],
            "secrets": ["claude-oauth-token", "github-token"],
            "resources": {"cpus": 2, "memoryMb": 4096},
            "replicas": 1,
            "drainSeconds": 14400,
        },
        "state": "running",
    },
}

REVIEWER = {
    "apiVersion": "taimen.ai/v1",
    "kind": "Agent",
    "key": "reviewer",
    "spec": {
        "displayName": "Code reviewer",
        "identity": {"kind": "agent", "permissions": ["tasks.read", "tasks.write"]},
        "executor": {
            "kind": "codex",
            "params": {"sandbox": "read-only", "credentialClass": "subscription"},
        },
        "placement": {"requires": ["codex"], "secrets": ["codex-auth"]},
    },
}

SKILLS = {
    "apiVersion": "taimen.ai/v1",
    "kind": "Agent",
    "key": "skills-executor",
    "spec": {
        "displayName": "Skills executor",
        "identity": {"kind": "agent", "permissions": ["skills.execute"]},
        "executor": {"kind": "skills"},
        "skills": {"protocols": ["local"], "local": ["taimen_selfdev"], "concurrency": 2},
        "placement": {"replicas": 1},
        "state": "stopped",
    },
}

SERVICE = {
    "apiVersion": "taimen.ai/v1",
    "kind": "Agent",
    "key": "process-bridge",
    "spec": {
        "displayName": "Process runtime bridge",
        "identity": {
            "kind": "service",
            "permissions": ["tasks.read", "tasks.write", "events.read"],
            "iam": {
                "audiences": ["control-plane"],
                "scopeCeiling": ["control-plane:read", "control-plane:write"],
            },
        },
        "placement": "none",
    },
}


def errors(document: dict) -> list[str]:
    return [cp._format_schema_error(e) for e in cp._schema_validator().iter_errors(document)]


@pytest.mark.parametrize("document", [CODER, REVIEWER, SKILLS, SERVICE], ids=lambda d: d["key"])
def test_every_executor_kind_and_identity_only_agent_is_valid(document):
    assert errors(document) == []


def test_a_placed_agent_needs_an_executor():
    placed = copy.deepcopy(SERVICE)
    placed["spec"]["placement"] = {"replicas": 1}
    assert any("executor" in e for e in errors(placed))


def test_executor_params_follow_the_kind():
    wrong = copy.deepcopy(REVIEWER)
    wrong["spec"]["executor"]["params"]["permissionMode"] = (
        "bypassPermissions"  # параметр claude-code у codex
    )
    assert any("permissionMode" in e for e in errors(wrong))


def test_topology_is_a_variable_or_uuid_and_secrets_are_names():
    wrong = copy.deepcopy(CODER)
    wrong["spec"]["work"]["workspace"] = "selfdev"
    wrong["spec"]["placement"]["secrets"] = ["sk-ant-Very_Secret"]
    found = errors(wrong)
    assert any("workspace" in e for e in found) and any("secrets" in e for e in found)
