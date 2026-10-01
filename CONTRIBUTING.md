# Contributing to Taimen Package SDK

Thank you for taking the time to contribute. Taimen is an organizational
runtime in which people, AI agents, workflows and services execute the work
of an organization; the platform is developed in the open under the
Apache License 2.0. This repository holds the **Package SDK** — the library and command-line tool
for authoring catalog packages: scaffolding, checks, tests executed by the core,
installation plans, the connector runtime for observers and the author plugin.

## Before you start

- Read the platform overview in the
  [guide](https://github.com/taimen-ai/taimen/tree/main/guide/docs/overview)
  (in Russian).
  Architecture decisions are recorded as ADRs (in Russian, with an English
  title line); English summaries are provided on request in the ADR's
  discussion.
- Check the open issues before starting a large change. For anything that changes
  an API, a contract or a service boundary, open an issue first and propose
  an ADR.

## Contributor License Agreement

We require a signed Contributor License Agreement (CLA) for every
contribution, so that the project can be relicensed or defended without
tracking down every author. You sign once for all Taimen repositories.

- Individuals: [`cla/CLA-individual.md`](https://github.com/taimen-ai/taimen/blob/main/cla/CLA-individual.md)
- Companies contributing on behalf of employees: [`cla/CLA-entity.md`](https://github.com/taimen-ai/taimen/blob/main/cla/CLA-entity.md)

The CLA grants the project a copyright and patent licence to your
contribution; you keep your copyright.

## Development setup

The component is a [uv](https://docs.astral.sh/uv/) project on Python 3.12.
It depends on sibling repositories by path (`../control-plane`, `../control-plane/client` and `../skill-sdk` for the optional extras), so develop it from
the umbrella checkout, where the siblings are submodules:

```bash
git clone --recurse-submodules https://github.com/taimen-ai/taimen.git
cd taimen/package-sdk
uv sync                       # runtime dependencies plus the `dev` group
uv run pytest                 # tests
uv run ruff check .           # lint
uv run ruff format --check .  # formatting
```

Dependency changes regenerate `THIRD_PARTY.md` and `sbom.json` with the
umbrella's `tools/generate_third_party.py` in a runtime-only environment:

```bash
uv sync --no-dev --all-extras
uv run --no-sync python ../tools/generate_third_party.py --component package-sdk \
  --exclude-prefix control-plane --exclude-prefix iam-service --exclude-prefix platform- \
  --exclude-prefix skill-sdk --exclude-prefix package-sdk
uv sync   # back to the development environment
```

## Pull requests

- One logical change per pull request; keep the history linear (rebase, no
  merge commits).
- Tests and `ruff check` / `ruff format --check` must pass; behaviour changes
  come with tests.
- Commit messages explain *why*, not *what*; reference the ADR or issue.
- Public contract changes update the README.
- Do not include secrets, customer data or internal hostnames.

## Reporting bugs and security issues

Bugs: open an issue in this repository with the version, steps to reproduce
and logs. Security issues: see [SECURITY.md](SECURITY.md) and do not open a
public issue.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
