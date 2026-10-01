# Package SDK

Tools and library for authoring **catalog packages** — the data that sets the
rules of the game in an organization: task types, work rules, processes,
agents, notification rules, ontologies and skills. A vertical is a package with
no runtime of its own; an integration is a class package, provider packages and
the code of its observer and skills.

Design record: TAI-ADR-0062.

> **Status: under construction.** Available now: `check`, `test`, `plan`, `apply`,
> `export`, `migrate-expr`, `edit` and `sandbox` — the commands of the tools this
> component replaces, with the same behaviour. `check` without the core's code is an
> error; `--schema-only` checks the schema and references alone. The manifest is
> checked too (variables, `requires` ranges, ontologies), and `describe` / `docs` print
> what an installation needs and the generated README sections of a package.
> Installation is one plan: `lock` pins the sources (a commit and a content hash per
> package in `packages.lock`; a git source is `https://` without credentials in the address
> or `git@host:path`, and its `ref` is a tag), `plan --install … --out plan.json` builds the plan of every
> kind (`package-sdk.plan/v1`) without writing anything and refuses a core version outside
> a package's `engines` or a missing required variable, and `apply --plan plan.json`
> applies exactly that plan after a human yes, stopping with `plan_stale` before its first
> write when the stand has moved. Fields a person changed in the console since the last
> apply are kept unless `plan --overwrite-console` (`overwrite_console` of `pkg_plan`):
> the flag is part of the plan and its hash, and the plan lists the console edits it
> overwrites or keeps. The library calls are `package_sdk.install.lock()`, `plan()` and
> `apply()`. The author works in an AI assistant through `package-sdk mcp` and the
> `package-author` plugin (below). The rest of the table arrives with the feature tasks.

## What it will provide

| Area | Commands and modules |
|---|---|
| Authoring | `package-sdk init`, `package-sdk add <kind> <key>`, `package-sdk edit` (style-preserving edits) |
| Checks | `package-sdk check` — schema, closed references, declared variables, compatibility with the core |
| Tests | `package-sdk test` — the test pyramid; scenarios run by the code of the core (a server or an in-process sandbox) |
| Installation | `package-sdk lock`, `package-sdk plan --out`, `package-sdk apply --plan` — one plan for every kind, sources by path or git tag |
| Integrations | `package_sdk.connector` — observer runtime; `package-sdk image` — images of observers and skill hosts |
| Author in an AI assistant | `package-sdk mcp` and the `package-author` plugin |

## Testing a package

`package-sdk test [paths…]` runs the whole pyramid of a package and prints one report
with coverage (`--json` for a document):

1. **check** — schema, closed references, the core's validators; if it fails, no
   test runs;
2. **skills** — the skill contracts of the integration code (`integration/src`)
   against the package's `kind: Skill` files (`skill-sdk export --check`);
3. **integration** — pytest of the integration code (`integration/tests`), if any;
4. **scenarios** — `tests/*.test.yaml` of processes, work rules and task types, run by
   the code of the core: a server (`--server`, `POST /packages:test`) or the in-process
   sandbox (the default, extra `sandbox`).

The sandbox builds its catalog from every kind of the package and its `requires`.
Processes run in memory. Rule and task type scenarios run the core's application code
in a transaction that is always rolled back, so they need an empty PostgreSQL database
(with `pg_trgm`): `--database-url` or `PACKAGE_SDK_SANDBOX_DATABASE_URL`. The sandbox
migrates it to the pinned core's schema and creates its own tenant there, under a
PostgreSQL advisory lock. It writes only to an empty database or to one it prepared
itself (the core's schema and the sandbox tenant): a database with other tables, other
tenants (a stand's, for example) or the core's schema without the sandbox tenant is
refused before anything is written. Without a
database these scenarios are reported as `skipped` with the finding
`sandbox_database_required`, and the run is not green. A stage with nothing to run (no
integration code) is `skipped`; a stage that cannot run (no `skill-sdk`, `pytest` or
core code) is an error.

The pyramid runs the code of the packages it tests. The integration code of every
tested package — each named package; with `--install` or with no arguments, every
package of the installation, `requires` included, and packages from git sources too —
is imported by `skill-sdk export --check` and its tests are run by pytest, in
subprocesses of the current Python. So `test --install` executes the code of every
integration of the installation, whoever published its git source. The dependencies
of `integration/pyproject.toml` are not installed: install them into the environment
yourself. The subprocesses get the environment without secrets: variables named like
tokens, keys (`*API_KEY*`, `*APIKEY*`, `*ACCESS_KEY*`), personal access tokens
(`*_PAT`), `*_AUTH`, passwords and passphrases, credentials, `*_PEM`, database addresses,
`CP_*`, `CONTROL_PLANE_*`, `IAM_*`, `PACKAGE_SDK_*`, any variable whose value is an
address with a password (`scheme://user:password@host`, as in `POSTGRES_URL` or
`REDIS_URL`) or a connection string with `AccountKey=`, `Password=` or `Pwd=`, and
`SSH_AUTH_SOCK`, `KUBECONFIG`, `DOCKER_AUTH_CONFIG` are removed. This is not a sandbox:
`HOME` is kept, and the credential files in it (`~/.aws`, `~/.ssh`,
`~/.docker/config.json`, `~/.netrc` and the like) stay readable by that code. Test only
packages whose code you trust.

## Author in an AI assistant

`package-sdk mcp` is an MCP server over stdio (extra `mcp`) with the tools of the
package author on the same functions as the CLI:

| Tool | Same as |
|---|---|
| `pkg_check(path \| install, server?, workspace_id?, schema_only?)` | `package-sdk check --json` |
| `pkg_test(path \| install, tests?, server?, workspace_id?)` | `package-sdk test --json` |
| `pkg_describe(path, env_example?)` | `package-sdk describe --json` |
| `pkg_edit(operation, options, dry_run?)` | `package-sdk edit <operation> …` |
| `pkg_plan(server?, install \| path, out?, workspace_id?, replay_limit?, overwrite_console?)` | `package-sdk plan --out` |
| `pkg_apply(plan_file, plan_hash)` | `package-sdk apply --plan` |

`pkg_plan` saves the plan to a file (by default `.package-sdk/plan.json`; for a single
package directory it writes the installation next to the plan) and returns its
`planHash`. `pkg_apply` applies only that saved plan and only by its hash: a call without
`plan_hash` or with the hash of another plan is refused before the stand is read, and a
stand that moved since the plan gives `plan_stale` before the first write. The human's
confirmation is taken by the host: the plugin's hook asks it before every `pkg_apply`.
The stand credential is the operator's: `CP_TOKEN` or the credential
`control-plane-client` finds (`resolve_credential`). An access token lives minutes while a
plan or an apply may take longer, so the credential's token is taken before every request
and renewed as it expires; a `401 invalid_credentials` is retried once with a renewed token.
`CP_TOKEN` and `NOTIFY_TOKEN` are the human's choice and are used as given.

The server talks only to the stands in `PACKAGE_SDK_SERVERS` of its environment (any
other address is `server_not_allowed` before a token is requested) and works inside the
session root (`PACKAGE_SDK_ROOT`, the client's `file://` root or the current directory):
edits only inside it, plans only under `<root>/.package-sdk/`, and a file there that is
not a plan is never overwritten. It reads a plan once and applies exactly that document.

The Claude Code plugin `package-author` (`plugin/package-author`, marketplace
`package-sdk` in `.claude-plugin/` of this repository) brings the server and sixteen
skills for the whole cycle of a package — task types, rules, processes, agents,
integrations, ontologies, notifications, tests, release and installation — and the
hook `confirm_apply.py` on `pkg_apply`:

```bash
uv tool install "package-sdk[mcp,sandbox,skills] @ git+https://github.com/<org>/package-sdk@<tag>"
claude plugin marketplace add <org>/package-sdk
claude plugin install package-author@package-sdk
```

See [plugin/package-author/README.md](plugin/package-author/README.md).

## Example

[examples/claims](examples/claims/README.md) — customer claims end to end: a package with a
process, a work rule, a notification rule, an ontology, agents with their images and the
integration code of a helpdesk (observer and skills), a demo helpdesk as the source, and
scenarios and unit tests. CI runs its whole pyramid in the sandbox on every change; the job
that installs it on a stand of the open supply, where a claim filed in the helpdesk is
followed until its case closes, is checked on the public mirror.

## Cache of git sources

Git sources live in `$PACKAGE_SDK_CACHE` (default `~/.cache/package-sdk`): a mirror of
each source and the checkouts of its commits. Temporary checkouts left by a crashed run
(`.staging-*`) are removed once they are older than an hour, whenever the cache of that
source is used. Checkouts no lock needs any more are removed by hand:

```bash
package-sdk cache prune                  # keep what the packages.lock files under . refer to
package-sdk cache prune --lock deploy/packages.lock --lock other/packages.lock
package-sdk cache prune --all            # the whole cache: checkouts and mirrors
```

Without `--all` the mirrors stay: a checkout is extracted from the mirror again without
the network. A checkout used by an installation in another directory counts as
unreferenced — run `prune` where all your installations are, or pass their locks.
Without a lock file under the current directory (searched six levels deep, never from
the home directory) `prune` refuses instead of removing everything; `--all` does that
explicitly. A checkout used within the last hour is kept even with no lock referring to
it: a `lock` or `plan` running in parallel may be reading it. `--all` has no such
grace and removes fresh checkouts too, so a `lock`, `plan` or `test` running in
parallel fails with "checkout removed while reading, retry" — run `--all` when nothing
else uses the cache. Symbolic links in the cache are neither followed nor removed.

## Observer secrets

An observer (`package_sdk.connector`) reads node secrets only from files:
`ctx.secret(name)` reads `$CONNECTOR_SECRETS_DIR/<name>` (default `/run/secrets`) on
every call, by the canon of skill-sdk (`skill_sdk.secrets.read_secret_file`). The
observer image carries only the `connector` extra, so the rule is repeated in
`package_sdk.connector.secrets`, and one table of examples runs against both
implementations.

- The name follows `[a-z0-9][a-z0-9-]{0,62}`; `agent-pat` is reserved.
- A missing file, an empty one or one of whitespace only is "no secret":
  `SecretMissing`, a daily `connector.secret_missing`, the cycle is skipped.
  Otherwise only trailing `\r`/`\n` are trimmed; spaces of a non-empty value stay.
- Anything else is `SecretRejected` with the code and reason of the canon — a name
  not matching the pattern, a link or `..` out of the directory, a path swapped on
  every one of three attempts, not a regular file, over 64 KiB, not UTF-8, or no read
  permission. The cycle fails with `connector.cycle_failed` carrying `secret`, `code`
  and `reason`, never the value.

The path is walked from a descriptor of the secrets directory with `O_NOFOLLOW` on
every component. This is defense in depth, not a guarantee: whoever can swap the
secrets directory path itself or write into it controls the secrets.

Behaviour changes against the previous reader (`read_text().strip()`, any `OSError`
meaning "no secret"):

- a file without read permission is now `cycle_failed` (`secret_unreadable`), not a
  missing secret: an observer that works without a token when the secret is missing
  (selfdev `_github_token`) no longer silently does so on a permissions error;
- leading spaces of a non-empty value are kept;
- names outside the pattern (`EXT_TOKEN`) and `agent-pat` are rejected.

## Installation

The component is a [uv](https://docs.astral.sh/uv/) project on Python 3.12.

```bash
uv tool install "package-sdk @ git+https://github.com/<org>/package-sdk@<tag>"
package-sdk --version   # package-sdk 0.1.0
```

Optional extras: `sandbox` (checks and tests without a running core),
`connector` (observer runtime), `mcp` (author MCP server and the core client for its
credential), `all`.

## Compatibility

The SDK checks, tests and runs the sandbox with the code of the core, so each release
is tested against particular tags of its sibling components. Clone them next to the SDK
at these tags (the flat layout of the extras), and pin the same tags in the CI of a
package (`CONTROL_PLANE_REF`, `PLATFORM_AUTH_SDK_REF`, `SKILL_SDK_REF` in the workflow
`init` writes).

| package-sdk | control-plane | platform-auth-sdk | skill-sdk |
|---|---|---|---|
| 0.1.0 | ≥ the core release with the `deadlines_total` model (set at release) | the tag of the same platform release, the umbrella submodule pointer (set at release) | the tag of the same platform release as the core (set at release) |

How a row is filled:

- **control-plane** — the lowest core tag whose API and models the release is checked
  against; the `engines` range `init` writes for a package follows the same minor.
- **platform-auth-sdk** — a dependency of the core, not of the SDK: the tag of the same
  platform release, that is the umbrella's submodule pointer. The core only requires
  `platform-auth-sdk>=0.1.0`, so the tag cannot be read off its `pyproject.toml`.
- **skill-sdk** — needed only by the `skills` extra: the tag released in the same
  platform release as the core.

A row gets tag numbers when those tags are cut; until then it names the rule, never a
guessed number.

## Development

Develop from the umbrella checkout, where the siblings are submodules:

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Notes:

- `uv.lock` resolves the extras against the sibling `../control-plane` of the umbrella
  checkout, so it follows the core revision the umbrella pins; refresh it with
  `uv lock` after moving that pointer.
- Tests of the moved tools run on an anonymised snapshot of packages and
  installations in `tests/fixtures/umbrella`. `PACKAGE_SDK_UMBRELLA=<umbrella>` runs
  them on a live tree instead.
- `tests/test_pyramid.py` runs the scenarios of `tests/fixtures/pyramid` by both
  executors and compares the reports (`package_sdk.testing.divergence`). The
  comparison needs PostgreSQL: with `PACKAGE_SDK_SANDBOX_DATABASE_URL` (a server where
  the test may create and drop a database) the server is the core's application in
  process on that database; with `PACKAGE_SDK_TEST_SERVER` (and `CP_TOKEN`) it is a
  live stand as well. Without them these two tests are skipped.

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the Apache License 2.0.
