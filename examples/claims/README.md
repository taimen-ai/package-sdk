# Customer claims — the end-to-end example

A package of a domain that is not software development, with its integration, built
with package-sdk and checked by its CI. A customer files a claim in a helpdesk; the
platform classifies it, routes it, a person reviews it, a manager approves a large
refund, the reply goes back to the helpdesk once a person approved it, and the case
closes with its outcome. The core knows nothing of claims: everything below is data of
the package and code of its integration.

```text
examples/claims/
├── packages.yaml                  the installation: the package and the ontologies of its workspace
├── claims/                        the package
│   ├── package.yaml               manifest: variables, compatibility, ontologies
│   ├── knowledge-packs/           claims@1 on top of the platform ontology default@1
│   ├── processes/claim.yaml       the case: intake → review → reply
│   ├── rules/claim-reopened.yaml  a reopened ticket of a closed case → a follow-up
│   ├── notification-rules/        a reply waiting for a decision → approve / reject buttons
│   ├── task-types/, roles/        the work of the claims team
│   ├── skills/                    contracts exported from the integration code
│   ├── agents/                    observer, skills host, identities of the process and the rule
│   ├── tests/                     scenarios of the process, the rule and the task type
│   ├── integration/               the observer and the skills, with their unit tests
│   └── Dockerfile, Dockerfile.skills   images of the observer and the skills host
├── helpdesk/demo_helpdesk.py      a demo helpdesk: the source of claims, no accounts needed
└── stand/                         what the CI job with a stand of the open supply runs
```

## How a claim flows

```mermaid
flowchart LR
    H["Demo helpdesk"] -->|"new ticket"| O["helpdesk-observer"]
    O -->|"helpdesk.ticket_created"| P["Process claim"]
    P -->|"claims.classify@1"| S["claims-skills"]
    P -->|"decision table claim-route"| R["Review task<br/>(claims officer or manager)"]
    R -->|"refund above the threshold"| A["Approval by a manager<br/>(not the reviewer)"]
    R --> Y["Reply task"]
    A --> Y
    Y -->|"approved gate: helpdesk.reply@1"| S
    S -->|"reply, ticket closed"| H
    H -->|"reopened"| O
    O -->|"helpdesk.ticket_reopened"| W["Rule claim-reopened<br/>→ follow-up task"]
```

- **Observer** (`integration/src/claims_helpdesk/observer.py`) — `package_sdk.connector`:
  new tickets become `helpdesk.ticket_created`, reopened ones `helpdesk.ticket_reopened`;
  the dedup key is the ticket and its version, the cursor is the change number of the
  helpdesk.
- **Skills** (`integration/src/claims_helpdesk/skills.py`) — skill-sdk:
  `claims.classify@1` asks the installation's model (`ctx.llm`); `helpdesk.reply@1` is an
  external write, so the core runs it only on an approved gate of the task `claim-reply`.
- **Process** `claim` — starts on the observation, one case per ticket; classification,
  routing by a decision table, review, a refund above `CLAIMS_REFUND_THRESHOLD` approved
  by a manager with separation of duties, the reply, the decision remembered.
- **Rule** `claim-reopened` — one-off work on a closed case: a rule, not a process.
- **Ontology** `claims@1` — the claim and its relations to the customer (`legal_entity`),
  the product and the decision of the platform ontology `default@1`.

## Check and test without a stand

```bash
uv tool install "package-sdk[all] @ git+https://…/package-sdk@<tag>"
cd examples/claims/claims
package-sdk check --package .
PACKAGE_SDK_SANDBOX_DATABASE_URL=postgresql://postgres:sandbox@localhost:5432/sandbox \
  package-sdk test . --env /dev/null
```

`test` runs the whole pyramid: the check, the skill contracts against the code
(`skill-sdk export --check`), the unit tests of the integration, and the scenarios —
processes in memory, the rule and the task type in a rolled-back transaction of an empty
PostgreSQL. Without the database the rule and task type scenarios are skipped and the
run is not green.

## Install on a stand

```bash
export CP_TOKEN=…                    # the operator's token for the core
export NOTIFY_TOKEN=…                # and for the notification service
cat > claims.env <<'EOF'
CLAIMS_WORKSPACE_ID=<root workspace of the claims>
HELPDESK_URL=http://helpdesk:8080
NOTIFICATION_SERVICE_URL=https://platform.example.com/notify
EOF
package-sdk plan  --install packages.yaml --server https://platform.example.com --env claims.env --out plan.json
package-sdk apply --plan plan.json        --server https://platform.example.com --env claims.env
```

The images are built by the author's CI from the generated Dockerfiles, on top of the base
images of the platform:

```bash
cd claims
docker build --build-arg BASE_IMAGE=<observer base> -f Dockerfile \
  -t registry.example.com/claims/helpdesk-observer:0.1.0 .
docker build --build-arg RUNNER_IMAGE=<executor image> -f Dockerfile.skills \
  -t registry.example.com/claims/claims-skills:0.1.0 .
```

The skills host needs `HELPDESK_URL` and a model (`SKILL_LLM_PROVIDER`,
`SKILL_LLM_BASE_URL`, `SKILL_LLM_API_KEY`, `SKILL_LLM_MODELS`) in its environment; both
agents get the helpdesk token as the node secret `helpdesk-token`. Run the demo helpdesk
with `python helpdesk/demo_helpdesk.py --port 8080`: it listens on 127.0.0.1; on another
address (`--host 0.0.0.0` in a container) it needs `HELPDESK_TOKEN`, its bearer token.

## The stand job of the CI

The job `example-stand` is checked on the public mirror of the repository, where CI runs.

`stand/e2e.py` is what the job `example-stand` of `.github/workflows/ci.yml` runs on a
clean installation of the open supply (its compose, `make bootstrap`): the scenarios by
the server, `plan` and `apply` of the example, the identities and containers of its
agents (the part of a placement node, which the open supply does not ship), a claim filed
in the demo helpdesk and the case closed with evidence — the reply in the helpdesk, the
reply task done, the journal of the case — and a reopened ticket turned into a follow-up.
`stand/stub_model.py` answers the classification deterministically, so the run needs no
model account; `stand/*-base.Dockerfile` build the base images from the components.
