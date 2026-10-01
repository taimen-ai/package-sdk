"""The example on a stand of the open supply: what the CI job `stand` runs (SC-004).

The stand is the compose of the open supply after `make secrets`, `make up` and `make
bootstrap`; the example is installed by `package-sdk plan` and `apply`. This script plays
the parts the job has no one else for:

- the node that places agents (there is no placement service in the open supply): an
  IAM identity per agent of the example, linked to the agent in the core
  (``PUT /agents/{key}/identity``), a PAT for the agents that run as processes, and the
  containers of the observer and the skills host from the images of the example;
- the people of the claims team: the operator of the stand holds both roles, reviews the
  claim, writes the reply and approves it.

    python e2e.py operator            # the operator's PAT for the core and notifications
    python e2e.py token <audience>    # an access token of the operator (CP_TOKEN, NOTIFY_TOKEN)
    python e2e.py env                 # the variables of the installation → $WORK_DIR/claims.env
    python e2e.py install             # package-sdk plan and apply of the example; the job says yes
    python e2e.py agents              # identities, roles, the observer and the skills host
    python e2e.py scenario            # a claim filed in the helpdesk closes with evidence

Environment: STAND_DIR — the checkout of the open supply (its .env and bootstrap state);
WORK_DIR — where the PATs and the secrets of the agents live for the run; EXAMPLE_DIR —
examples/claims. Secrets are written to files, never printed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import yaml

STAND = Path(os.environ.get("STAND_DIR", "stand")).resolve()
WORK = Path(os.environ.get("WORK_DIR", "claims-stand")).resolve()
EXAMPLE = Path(os.environ.get("EXAMPLE_DIR", Path(__file__).resolve().parents[1])).resolve()
PACKAGE = EXAMPLE / "claims"
HELPDESK_PORT = int(os.environ.get("HELPDESK_HOST_PORT", "18095"))
# Inside the network of the stand — what the agents' containers use.
CORE_IN_NETWORK = "http://control-plane-api:8000"
IAM_IN_NETWORK = "http://iam-service:8010"
HELPDESK_IN_NETWORK = "http://helpdesk:8080"
MODEL_IN_NETWORK = "http://stub-model:8081/v1"
AGENT_SCOPES = ["control-plane:read", "control-plane:write"]
OPERATOR_SCOPES = {
    "control-plane": ["control-plane:read", "control-plane:write", "control-plane:admin"],
    "notification-service": ["notifications:admin"],
}
PAT_TTL = 24 * 3600
# What the core plan answers when a rule or a task type names a skill of the same package
# that is not on the stand yet (TASK-001197).
SKILLS_NOT_SEEN = ("unknown_skill", "invalid_approval_schema")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


def secure_write(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(mode)


class Http:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:1000]
            raise RuntimeError(f"{method} {path}: HTTP {error.code}: {detail}") from error
        return json.loads(raw) if raw else None


class Stand:
    """The stand after bootstrap: its addresses, the operator and the IAM bootstrap token."""

    def __init__(self) -> None:
        self.env = read_env(STAND / ".env")
        name = self.env.get("COMPOSE_PROJECT_NAME", "taimen")
        self.state = json.loads((STAND / "deploy" / "state" / f"{name}.json").read_text())
        self.issuer = self.env["TAIMEN_PUBLIC_URL"].rstrip("/") + "/iam"
        self.tenant = self.state["iamTenantId"]
        self.core = Http(f"http://127.0.0.1:{self.env.get('CP_HOST_PORT', '18000')}")
        self.iam = Http(f"http://127.0.0.1:{self.env.get('IAM_HOST_PORT', '18010')}")
        self.bootstrap = {"X-IAM-Bootstrap-Token": self.env["IAM_BOOTSTRAP_TOKEN"]}
        # the network of the stand: its services and the agents' containers
        self.network = self.env.get("TAIMEN_NETWORK") or f"{name}_default"
        self._tokens: dict[str, tuple[float, str]] = {}

    # -- the operator --------------------------------------------------------------------

    def operator_pat(self) -> str:
        """A PAT of the operator for the core and the notification service (bootstrap issues
        one for the core only; NotificationRule is applied to the notification service)."""
        path = WORK / "operator.pat"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        principal = self.state["iamOperatorPrincipalId"]
        tenant_path = f"/api/v1/tenants/{self.tenant}/principals/{principal}"
        # IAM issues a PAT to a person only right after an authentication (≤ 300 s).
        self.iam.call(
            "POST",
            f"{tenant_path}/authentication-contexts",
            {"issuer": self.issuer, "acr": "bootstrap", "amr": ["stand-e2e"]},
            self.bootstrap,
        )
        issued = self.iam.call(
            "POST",
            f"{tenant_path}/platform-access-tokens",
            {
                "name": "claims-example-stand",
                "audiences": sorted(OPERATOR_SCOPES),
                "scopeCeiling": sorted({s for v in OPERATOR_SCOPES.values() for s in v}),
                "expiresInSeconds": PAT_TTL,
            },
            {**self.bootstrap, "Idempotency-Key": str(uuid.uuid4())},
        )
        secure_write(path, issued["token"])
        return str(issued["token"])

    def token(self, audience: str = "control-plane") -> str:
        cached = self._tokens.get(audience)
        if cached and time.monotonic() - cached[0] < 240:
            return cached[1]
        answer = self.iam.call(
            "POST",
            "/api/v1/platform-access-tokens:exchange",
            {
                "token": self.operator_pat(),
                "audience": audience,
                "scopes": OPERATOR_SCOPES[audience],
            },
        )
        self._tokens[audience] = (time.monotonic(), answer["accessToken"])
        return str(answer["accessToken"])

    def cp(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> Any:
        extra = {"Authorization": f"Bearer {self.token()}", **(headers or {})}
        if method != "GET":
            extra.setdefault("Idempotency-Key", str(uuid.uuid4()))
        return self.core.call(method, "/api/v1" + path, body, extra)

    def pages(self, path: str) -> list[dict[str, Any]]:
        """Every item of a paged list of the core."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            query = {"limit": 200, **({"cursor": cursor} if cursor else {})}
            separator = "&" if "?" in path else "?"
            page = self.cp("GET", f"{path}{separator}{urllib.parse.urlencode(query)}")
            items += page["items"]
            cursor = page.get("nextCursor")
            if not cursor:
                return items

    # -- agents --------------------------------------------------------------------------

    def identity(self, key: str, spec: dict[str, Any]) -> str | None:
        """The IAM identity of one agent, linked in the core; its PAT when it runs somewhere."""
        identity = spec.get("identity") or {}
        placed = spec.get("placement") != "none"
        principals = f"/api/v1/tenants/{self.tenant}"
        if identity.get("kind") == "service" or not placed:
            iam = identity.get("iam") or {}
            created = self.iam.call(
                "POST",
                f"{principals}/service-accounts",
                {
                    "displayName": spec.get("displayName", key),
                    "audiences": iam.get("audiences") or ["control-plane"],
                    "scopeCeiling": iam.get("scopeCeiling") or AGENT_SCOPES,
                },
                self.bootstrap,
            )
            principal = created["principalId"]
        else:
            created = self.iam.call(
                "POST",
                f"{principals}/principals",
                {"kind": "agent", "displayName": spec.get("displayName", key)},
                self.bootstrap,
            )
            principal = created["id"]
        self.cp(
            "PUT",
            f"/agents/{key}/identity",
            {"issuer": self.issuer, "iamTenantId": self.tenant, "iamPrincipalId": principal},
        )
        if not placed:
            return None
        issued = self.iam.call(
            "POST",
            f"{principals}/principals/{principal}/platform-access-tokens",
            {
                "name": f"{key}-stand",
                "audiences": ["control-plane"],
                "scopeCeiling": AGENT_SCOPES,
                "expiresInSeconds": PAT_TTL,
            },
            {**self.bootstrap, "Idempotency-Key": str(uuid.uuid4())},
        )
        return str(issued["token"])


def agents_of_example() -> dict[str, dict[str, Any]]:
    agents: dict[str, dict[str, Any]] = {}
    for path in sorted((PACKAGE / "agents").glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        agents[document["key"]] = document["spec"]
    return agents


# -- commands --------------------------------------------------------------------------


def cmd_operator(stand: Stand) -> None:
    stand.operator_pat()
    print("operator PAT →", WORK / "operator.pat")


def cmd_token(stand: Stand, audience: str) -> None:
    print(stand.token(audience))


def cmd_env(stand: Stand) -> None:
    """The variables of the installation: the claims live in the workspace of bootstrap."""
    notify_port = stand.env.get("NOTIFY_HOST_PORT", "18045")
    values = {
        "CLAIMS_WORKSPACE_ID": stand.state["workspaceId"],
        "HELPDESK_URL": HELPDESK_IN_NETWORK,
        "NOTIFICATION_SERVICE_URL": f"http://127.0.0.1:{notify_port}",
    }
    secure_write(WORK / "claims.env", "".join(f"{k}={v}\n" for k, v in values.items()), 0o644)
    print("variables →", WORK / "claims.env")


def _target(stand: Stand, env: dict[str, str]) -> Any:
    from package_sdk import install
    from package_sdk.apply import Http as CoreHttp

    server = f"http://127.0.0.1:{stand.env.get('CP_HOST_PORT', '18000')}"
    return install.Target(
        server=server,
        http=CoreHttp(server),
        headers={"Authorization": f"Bearer {stand.token('control-plane')}"},
        notify=(
            CoreHttp(env["NOTIFICATION_SERVICE_URL"]),
            {"Authorization": f"Bearer {stand.token('notification-service')}"},
        ),
    )


def _yes(question: str) -> bool:
    """``package-sdk apply --plan`` asks a person in a terminal. The job is the operator of
    the stand: the plan is printed in the log above, and the job says yes."""
    print(f"{question} — yes (the operator of the stand is this job)")
    return True


def _plan(stand: Stand, installation: Path, out: Path) -> subprocess.CompletedProcess[str]:
    """``package-sdk plan`` as the author runs it; the tokens only in its environment."""
    env = {
        **os.environ,
        "CP_TOKEN": stand.token("control-plane"),
        "NOTIFY_TOKEN": stand.token("notification-service"),
    }
    command = ["package-sdk", "plan", "--install", str(installation), "--out", str(out)]
    command += ["--server", _target(stand, read_env(WORK / "claims.env")).server]
    command += ["--env", str(WORK / "claims.env")]
    done = subprocess.run(command, env=env, capture_output=True, text=True)
    print(done.stdout + done.stderr)
    return done


def _skills_first(stand: Stand, env: dict[str, str]) -> None:
    """Workaround of TASK-001197: the plan of the core (/packages:plan) checks the rule and
    the task type against the skills registered on the stand, not against the skills of
    the same package that the catalog section of the plan registers first. On a stand that
    has never seen the package, its own skills are installed by a plan of their own.
    Remove once the core plans with the skills of the package."""
    from package_sdk import install

    staged = WORK / "skills-first"
    if staged.exists():
        shutil.rmtree(staged)
    package = staged / "claims"
    manifest = yaml.safe_load((PACKAGE / "package.yaml").read_text(encoding="utf-8"))
    for field in ("variables", "knowledge"):
        manifest["spec"].pop(field, None)
    package.mkdir(parents=True)
    (package / "package.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    for folder in ("skills", "roles"):
        shutil.copytree(PACKAGE / folder, package / folder)
    (staged / "packages.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "taimen.ai/v1",
                "kind": "Installation",
                "key": "claims-skills-first",
                "spec": {"packages": [{"key": "claims", "path": "./claims"}]},
            }
        ),
        encoding="utf-8",
    )
    target = _target(stand, env)
    document = install.plan(staged / "packages.yaml", target=target, env=env)
    install.apply(document, target=target, env=env, install=staged / "packages.yaml", confirm=_yes)


def cmd_install(stand: Stand) -> None:
    """``package-sdk plan`` and ``apply`` of examples/claims/packages.yaml."""
    from package_sdk import install

    env = {**read_env(WORK / "claims.env"), **os.environ}
    plan = WORK / "plan.json"
    done = _plan(stand, EXAMPLE / "packages.yaml", plan)
    if done.returncode != 0:
        output = done.stdout + done.stderr
        if not any(code in output for code in SKILLS_NOT_SEEN):
            raise SystemExit("package-sdk plan failed")
        print(
            "::warning title=claims example::TASK-001197: the core plan does not see the "
            "skills of the package it plans; the skills are installed first "
            "(examples/claims/stand/e2e.py)"
        )
        _skills_first(stand, env)
        if _plan(stand, EXAMPLE / "packages.yaml", plan).returncode != 0:
            raise SystemExit("package-sdk plan failed after the skills were installed")
    install.apply(plan, target=_target(stand, env), env=env, confirm=_yes)
    again = _plan(stand, EXAMPLE / "packages.yaml", WORK / "plan-again.json")
    if again.returncode != 0:
        raise SystemExit("package-sdk plan after apply failed")
    left = install.count_changes(install.read_plan(WORK / "plan-again.json"))
    if left != 0:
        raise SystemExit(f"the plan after apply is not empty: {left} change(s) left")
    print("the plan after apply is empty")


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True)


def cmd_agents(stand: Stand) -> None:
    helpdesk_token = (WORK / "helpdesk-token").read_text(encoding="utf-8").strip()
    runtime = {
        "CONTROL_PLANE_SERVER": CORE_IN_NETWORK,
        "CONTROL_PLANE_IAM_URL": IAM_IN_NETWORK,
        "CONTROL_PLANE_IAM_TENANT": stand.tenant,
        "CONTROL_PLANE_IAM_SCOPES": " ".join(AGENT_SCOPES),
    }
    for key, spec in agents_of_example().items():
        pat = stand.identity(key, spec)
        print(f"   {key}: identity linked" + (", PAT issued" if pat else " (no process)"))
        if pat is None:
            continue
        # One directory per agent, as a node mounts /run/secrets. The CI machine is thrown
        # away after the run, and the container's user (10001) must read the files; on a
        # node they are 0600 files of that user.
        secrets = WORK / "agents" / key
        secure_write(secrets / "agent-pat", pat, 0o644)
        for name in (spec.get("placement") or {}).get("secrets") or []:
            secure_write(secrets / name, helpdesk_token, 0o644)
        secrets.chmod(0o755)
        environment = dict(runtime)
        if spec["executor"]["kind"] == "skills":
            environment.update(
                HELPDESK_URL=HELPDESK_IN_NETWORK,
                SKILL_LLM_PROVIDER="openai",
                SKILL_LLM_BASE_URL=MODEL_IN_NETWORK,
                SKILL_LLM_API_KEY="stub",
                SKILL_LLM_MODELS="stub-classifier",
            )
        flags = [f"--env={k}={v}" for k, v in environment.items()]
        docker(
            "run", "--detach", "--name", f"claims-{key}", "--network", stand.network,
            "--volume", f"{secrets}:/run/secrets:ro", *flags, spec["executor"]["image"],
        )  # fmt: skip
        print(f"   {key}: started {spec['executor']['image']}")
    operator = stand.state["cpOperatorPrincipalId"]
    roles = {r["slug"]: r["id"] for r in stand.pages("/roles")}
    for slug in ("claims-officer", "claims-manager"):
        stand.cp("POST", f"/principals/{operator}/roles", {"roleId": roles[slug]})
        print(f"   operator holds {slug}")


class Timeout(AssertionError):
    pass


def wait(what: str, probe: Any, seconds: int = 300, every: float = 5) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        found = probe()
        if found:
            return found
        if time.monotonic() > deadline:
            raise Timeout(f"{what}: not within {seconds} s")
        time.sleep(every)


def helpdesk(method: str, path: str, body: Any = None) -> Any:
    token = (WORK / "helpdesk-token").read_text(encoding="utf-8").strip()
    return Http(f"http://127.0.0.1:{HELPDESK_PORT}").call(
        method, path, body, {"Authorization": f"Bearer {token}"}
    )


def complete_task(stand: Stand, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """What a person does in the console: takes the task, fills it in."""
    task = stand.cp("GET", f"/tasks/{task_id}")
    task = stand.cp(
        "PATCH",
        f"/tasks/{task_id}",
        {"assigneeId": stand.state["cpOperatorPrincipalId"], "customFields": fields},
        {"If-Match": f'"task-{task["version"]}"'},
    )
    return dict(task)


def open_task(stand: Stand, instance_id: str, element: str) -> Any:
    instance = stand.cp("GET", f"/process-instances/{instance_id}")
    for item in instance.get("openElements") or []:
        if item["id"] == element and item.get("taskId"):
            return item["taskId"]
    if instance["status"] != "running":
        raise AssertionError(f"the case stopped: {json.dumps(instance, indent=1)[:3000]}")
    return None


def cmd_scenario(stand: Stand) -> None:
    summary: list[str] = []
    ticket = helpdesk(
        "POST",
        "/tickets",
        {
            "customer": {"id": "C-7", "name": "Northwind Ltd"},
            "product": "Grinder X2",
            "subject": "The grinder stopped working",
            "text": "The grinder stopped after a week. I want my money back.",
            "amount": 120,
            "currency": "EUR",
            "channel": "web",
        },
    )
    ticket_id = ticket["id"]
    summary.append(f"ticket {ticket_id} filed in the helpdesk")

    def instance() -> Any:
        query = urllib.parse.urlencode(
            {"definitionKey": "claim", "instanceKey": f"claim:{ticket_id}"}
        )
        items = stand.cp("GET", f"/process-instances?{query}")["items"]
        return items[0] if items else None

    case = wait("the observer starts the case", instance, seconds=240)
    case_id = case["id"]
    summary.append(f"case {case_id} started by the observation")

    review = wait("the review task", lambda: open_task(stand, case_id, "review-claim"), seconds=300)
    classified = stand.cp("GET", f"/process-instances/{case_id}")["data"]
    assert classified.get("category") == "defect", classified
    summary.append(
        f"classified by the skills host: {classified['category']}/{classified['severity']}, "
        f"routed to {classified['reviewRole']}"
    )
    reply = "We are sorry about the grinder. We refund its price, 120 EUR."
    task = complete_task(
        stand, review, {"resolution": "refund", "refundAmount": 120, "reply": reply}
    )
    stand.cp("POST", f"/tasks/{review}:complete", {}, {"If-Match": f'"task-{task["version"]}"'})
    summary.append(f"review {review} completed: refund 120 (under the threshold, no approval)")

    send = wait("the reply task", lambda: open_task(stand, case_id, "send-reply"), seconds=120)
    complete_task(stand, send, {"ticketId": ticket_id, "message": reply})
    approval = stand.cp(
        "POST",
        "/approvals",
        {
            "task": send,
            "gate": True,
            "assignedPrincipalId": stand.state["cpOperatorPrincipalId"],
            "comment": "The reply is ready",
        },
    )
    stand.cp("POST", f"/approvals/{approval['id']}:approve", {"comment": "Send it"})
    summary.append(f"reply {send} approved: approval {approval['id']}")

    def closed() -> Any:
        current = stand.cp("GET", f"/process-instances/{case_id}")
        if current["status"] == "running":
            return None
        return current

    final = wait("the case closes", closed, seconds=300)
    assert (final["status"], final["outcome"]) == ("completed", "refunded"), final

    # Evidence: the reply is in the helpdesk, written by the skill on the approved gate;
    # the reply task is done; the case remembered its decision.
    in_helpdesk = helpdesk("GET", f"/tickets/{ticket_id}")
    assert in_helpdesk["status"] == "closed", in_helpdesk
    assert [r["message"] for r in in_helpdesk["replies"]] == [reply], in_helpdesk
    sent = stand.cp("GET", f"/tasks/{send}")
    assert sent["systemStatusCategory"] == "terminal_success", sent
    journal = stand.pages(f"/process-instances/{case_id}/journal")
    elements = {entry.get("element") for entry in journal}
    assert {"classify", "route", "review-claim", "send-reply", "remember-decision"} <= elements, (
        elements
    )
    reply_id = in_helpdesk["replies"][0]["id"]
    summary.append(
        f"case closed: {final['outcome']}; evidence — helpdesk reply {reply_id}, "
        f"reply task {sent['status']}, journal of {len(journal)} entries"
    )

    helpdesk("POST", f"/tickets/{ticket_id}:reopen", {"text": "The refund never arrived."})

    def followup() -> Any:
        query = urllib.parse.urlencode(
            {"typeKey": "claim-followup", "workspaceId": stand.state["workspaceId"], "limit": 50}
        )
        items = stand.cp("GET", f"/tasks?{query}")["items"]
        return next(
            (t for t in items if (t.get("customFields") or {}).get("ticketId") == ticket_id), None
        )

    task = wait("the rule files the follow-up", followup, seconds=240)
    summary.append(
        f"reopened ticket → follow-up {task.get('publicId', task['id'])} by claim-reopened"
    )

    text = "\n".join(f"- {line}" for line in summary)
    print(text)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as handle:
            handle.write("### Customer claims on the stand\n\n" + text + "\n")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    WORK.mkdir(parents=True, exist_ok=True)
    stand = Stand()
    command, rest = argv[0], argv[1:]
    if command == "operator":
        cmd_operator(stand)
    elif command == "token":
        cmd_token(stand, rest[0] if rest else "control-plane")
    elif command == "env":
        cmd_env(stand)
    elif command == "install":
        cmd_install(stand)
    elif command == "agents":
        cmd_agents(stand)
    elif command == "scenario":
        cmd_scenario(stand)
    else:
        print(f"unknown command {command!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
