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
| Authoring | `package-sdk init`, `package-sdk add <kind> <key>`, `package-sdk edit` (style-preserving edits), `package-sdk workflow` (the CI workflow by the installation layout) |
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

## Screens of a package

A screen of a package is a description, not code (TAI-ADR-0066, CP-ADR-0080): a `View`
(`views/`) names its source — instances of a process, tasks of a type, knowledge
records — and lays out blocks of the closed set of version 1 (`table`, `list`, `board`,
`header`, `fields`, `timeline`, `steps`, `artifacts`, `related`, `metrics`, `chart`,
`invoke`, `component`) with the formats of their values; a `Component` (`components/`)
is a reusable part of a screen made of the same blocks, inlined by the core into every
view that names it. A field `code` is refused. Their shape is
`schema/v1/view.schema.json`, a byte copy of the core's schema (`$defs.viewSpec`,
`$defs.componentSpec`).

Every string a person sees is a key of the package dictionaries: `package.yaml`
declares `locales: [en, ru]` and `defaultLocale`, and each locale has
`i18n/<locale>.yaml` — a flat mapping of keys to texts with ICU arguments
(`{count, plural, one {# item} other {# items}}`). A column without a `label` is
named by the key `<package>.fields.<path>`.

`check` reports, with the file and the JSON pointer of the place: a key missing in the
dictionary of a declared locale (`missing_message`), a dictionary of an undeclared
locale (`undeclared_locale`), an unknown block or format (`unknown_block`,
`unknown_format`), an aggregate outside `metrics` and `chart`
(`aggregate_outside_metrics`), `open.view` of a missing view (`unknown_view`), a source
process or task type outside the package and its `requires` (`unknown_source`), and a
key no view shows (`unused_message`, a warning). Data paths and CEL expressions are
checked by the code of the core, the same that plans the package; with a core next to
the SDK older than its screens, `check` says they are left to the plan. A package with
screens goes to the core's plan with its dictionaries: the core plans and publishes its
views. The reference is `tests/fixtures/screens/packages/invoice-payment`.

## Package settings

`spec.settings` of the manifest declares the values an organization administrator
changes in the live system — without a new package version or an installation plan
(variables stay for the topology of an installation). The core keeps the values;
processes, work rules and views read them as `settings.<field>`.

```yaml
spec:
  settings:
    schema:
      type: object
      required: [refundThreshold]
      properties:
        refundThreshold: {type: integer, minimum: 0, default: 50000}
        escalationRole: {type: string, x-ref: role, default: claims-lead}
    uischema:
      type: VerticalLayout
      elements:
        - {type: Group, label: acme-claims.settings.groups.approval, elements: [
            {type: Control, scope: "#/properties/refundThreshold"},
            {type: Control, scope: "#/properties/escalationRole"}]}
```

- `schema` is a closed subset of JSON Schema: `type`, `properties`, `required`, `enum`,
  `minimum`/`maximum`, `minLength`/`maxLength`, `pattern`, `format` (`date`, `uri`,
  `email`, `uuid`), `items`, `minItems`/`maxItems`, `default`, `additionalProperties:
  false` and `x-ref` (`role`, `principal`, `workspace`, `calendar`, `taskType` — the
  value references a platform object: the id of a role, principal or workspace, the key
  of a task type or calendar). The root is an object, objects nest at most 3 levels (the
  `items` of an array count as a level, the deepest holds scalars only), at most 100
  properties per object. The subset is exactly what the core accepts (CP-ADR-0081
  §1–2).
  Anything else — `writeOnly`, `format: password`, `title`, `$ref`, combinators — is
  refused by the schema.
- `uischema` (optional) is the subset of JSON Forms the console renders:
  `VerticalLayout`, `HorizontalLayout`, `Group` (with `label`), `Control` (`scope` is
  `#/properties/…`, `label`) and `Label` (`text`), and rules with the effects `SHOW`,
  `HIDE`, `ENABLE`, `DISABLE` on a `{scope, schema}` condition (`schema` takes the
  keywords of a field without `x-ref` and `default`, and `const`). `options` of a
  `Control`, `Categorization`, `ListWithDetail` and custom renderers are refused.
- Labels are not texts: `label` and `text` are keys of the package dictionaries; a field
  is labelled by the key `<package>.settings.<path>`.

A package test sets the values before the scenario with `given.settings` (process and
rule tests) and changes them in the middle of a process scenario with a step
`settings: {...}`. The values replace the saved ones; fields not given take their
`default`.

`package-sdk check` finds what the schema cannot say, with the codes and paths of the
core's plan (CP-ADR-0081): `settings_schema_unsupported` (a keyword outside its type,
`required` naming a field missing from `properties`, `enum` on an `object` or `array`,
an array nested below the deepest level), `settings_default_missing` (an optional field without
`default`), `settings_default_invalid` (`default` does not pass its own field, for
example `{type: integer, default: "x"}`), `settings_secret_field` (`writeOnly`,
`format: password`, a field name the core takes for a secret — `password`, `secret`,
`token`, `apiKey`, `privateKey`, `credential`, `authorization`, `clientSecret`, while
`secretRef` is fine — or credential material in `default` or `enum`),
`settings_uischema_unsupported` (a `scope` that names no property, a second `Control`
on one property, a `Group` label that is not `<package>.settings.groups.<id>`),
`settings_uischema_uncovered` (a warning: no `Control` edits a field) and
`settings_label_missing` (a label key missing in a dictionary of a declared language:
`<package>.title`, `<package>.settings.<path>` of every property, the keys of
`uischema`). Reads of `settings.<path>` in processes, work rules and views give
`settings_ref_unknown` (the settings declare no such field, or the package declares no
settings) and `settings_ref_type` (the type of the field does not fit the place of the
read — an object in a comparison or inside a text); the path is the expression's in
its file. When the core's code next to the SDK knows package settings, it checks the
reads itself: processes with `check_process`, work rules with `check_settings_refs`
(the first read of a rule that does not fit, as the plan reports it), views with
`check_view`. Without it `check` checks the rules on its own (every read) and in the
CEL expressions of processes and views catches undeclared fields only, saying the types
are left to the plan. Inside a `catch … as: settings` branch of a process the name is
the error, not the settings: its reads are no references, the other reads of the
process are checked.

A deadline may be counted from the settings: the number of `workdays` or `workhours` of
a `due` (of a step or of the whole process) and of its `warnBefore` is a number or
`{expr: <CEL>}` — a non-negative integer the core computes once, when the step is
entered (`spec.due` — when the instance starts); from then on the deadline, its warning
and a pause behave as with a number (CP-ADR-0081, amendment of 2026-10-03):

```yaml
due: {workdays: {expr: settings.reviewDueWorkdays}, warnBefore: {workhours: 4}}
due: {workdays: 5, warnBefore: {workdays: {expr: settings.warnDays}}}
```

`check` checks the reads of `settings.<path>` there like any other: the path of a
finding is the expression's, for example
`/spec/stages/0/steps/0/human/due/workdays/expr`, and a field that is not an `integer`
(`number`, `string`, an object) is `settings_ref_type`. With a core next to the SDK that
counts such deadlines the check is the core's; with an older one, or without one,
`check` types an expression that is one whole read (`settings.reviewDueWorkdays`) itself
and leaves the types of a compound one to the plan with a warning. A sandbox with an
older core refuses such a `due` as a schema violation. In a test, `given.settings` and
a `settings` step before the step is entered change its deadline; after it — not.

`package-sdk sandbox` passes `given.settings` and the `settings` steps to the core,
which checks them against the schema in the package files as an administrator's save;
a sandbox has no organization, so an `x-ref` to a task type or a calendar must name one
of the package and its `requires`, and the ids of roles, principals and workspaces are
taken as present. With a core older than package settings, a package that reads its
settings or whose tests save them is `sandbox_settings_unsupported`, not a silent pass.
`package-sdk describe` lists the settings next to the installation variables: path,
type, `default`, whether it is required and what `x-ref` names. The reference is
`tests/fixtures/settings/packages/claims-intake`.

## Connection types

A `ConnectionType` (`connection-types/`) describes what it takes to connect a system of
some kind (CP-ADR-0079 §2): the ways to connect (`auth`: `oauth2`, `token`), the OAuth 2
flow (`oauth2`: `authorizeUrl`, `tokenUrlTemplate`, `accountParam`, `authStyle`,
`scopes`), the account field of the key form (`accountField`: `title`, `pattern`), the
JSON Schema of the connection's non-secret settings (`settingsSchema`) and the key of the
default connection (`defaultKey`). It carries no secret values: an administrator enters
them in the console. An agent names the connections whose access material it may read
in `Agent.spec.connections` (at most 20 keys).

```yaml
apiVersion: taimen.ai/v1
kind: ConnectionType
key: helpdesk
spec:
  version: 1
  displayName: Helpdesk
  auth: [oauth2, token]
  oauth2:
    authorizeUrl: https://www.helpdesk.example/oauth
    tokenUrlTemplate: https://{account}/oauth2/access_token
    accountParam: referer
    authStyle: in_params
    scopes: []
  accountField: {title: Portal address, pattern: '^[a-z0-9-]+\.helpdesk\.example$'}
  settingsSchema: {type: object, properties: {}}
  defaultKey: helpdesk
```

Versions work as for `Skill`: the package sets `spec.version`, the object is
`ConnectionType/<key>@<version>`, and a published version is immutable — a changed
file with the same version is refused before any write, bump `spec.version`. The plan
publishes a missing version, returns a `deprecated` one to `active` and leaves a version
an administrator `disabled` as it is; `retire: {ConnectionType: [<key>]}` makes every
active version `deprecated` (no new connections, existing ones keep working).

`check` reports, without a stand: `oauth2` without its block, `token` or `{account}` in
`tokenUrlTemplate` without `accountField` (and `{account}` without `accountParam`), any
placeholder other than `{account}`, a token host that is not an external DNS name (one
label, an IP literal, a port or userinfo), an account pattern that does not compile, a
settings property named like a secret, access material in any string of the type, and,
as a warning, a key in `Agent.spec.connections` that is the `defaultKey` of no
connection type in the package and its `requires`. With a core next to the SDK that
predates connections, `check` says the agent's `connections` are left to the stand, and
the sandbox leaves connection types out of the package it tests.

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
package-sdk --version   # package-sdk 0.2.0
```

Optional extras: `sandbox` (checks and tests without a running core),
`connector` (observer runtime), `mcp` (author MCP server and the core client for its
credential), `all`.

## Compatibility

The SDK checks, tests and runs the sandbox with the code of the core, so each release
is tested against particular tags of its sibling components. Clone them next to the SDK
at these tags, laid out as the layout manifest of the release says (below), and pin the
same tags in the CI of a package (`CONTROL_PLANE_REF`, `PLATFORM_AUTH_SDK_REF`, `SKILL_SDK_REF` in the workflow
`init` writes).

| package-sdk | control-plane | platform-auth-sdk | skill-sdk |
|---|---|---|---|
| 0.1.0 | v0.9.4 | v0.1.1 | v0.1.1 |
| 0.2.0 | v0.10.0 | v0.2.0 | v0.2.0 |

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

## Layout of the installation

Components refer to each other by relative paths (`package-sdk` →
`../../services/control-plane`),
so the path between two components is the same wherever they are put together: in the
umbrella, in images, in runner working copies and in the CI of a package. Where each
component lies is the layout manifest of the release, `package_sdk/layout.json`: the
stable name of a component → its path in the tree of the installation. It changes
together with the path dependencies of `pyproject.toml`.

| Component | 0.1.x (flat layout) | after the move to `services/` and `sdk/` |
|---|---|---|
| package-sdk | `package-sdk` | `sdk/package-sdk` |
| control-plane | `control-plane` | `services/control-plane` |
| platform-auth-sdk | `platform-auth-sdk` | `sdk/platform-auth-sdk` |
| skill-sdk | `skill-sdk` | `sdk/skill-sdk` |

The workflow `init` writes clones the components into `.platform/` by this manifest:
`clone <name> "$<NAME>_REF"` in the flat layout, `clone <name> <path> "$<NAME>_REF"`
once paths have segments. A clone is always fetched by name (`$PLATFORM_GIT/<name>.git`).
Releases of different layouts do not mix: the revisions and the layout of the workflow
come from one installation.

The working copy of an `Agent` follows the same rule: `workingCopy.directory`, the keys
of `workingCopy.neighbours` and `directory` of a catalog entry take a flat name
(`control-plane`) or a relative path of segments (`services/control-plane`); `..`,
absolute paths, empty segments and backslashes are rejected, and `check` reports a
catalog entry placed inside the directory of another one.

A package whose workflow was written by an earlier `init` regenerates it after
upgrading package-sdk:

```bash
package-sdk workflow .          # rewrite .github/workflows/package.yml by this installation
package-sdk workflow . --check  # exit 1 if the file differs; nothing is written
```

The command keeps the package key, the integration code (`integration/` or skill-sdk in
the previous file), the database (enabled in the previous file or needed by rule and task
type scenarios) and the `--with` options added to the `uv tool install` line. Other hand
edits of the file are not kept: review the diff before committing. On the flat layout the
result is the previous file byte for byte. A revision this installation does not know is
reported, and the job stops on it until it is set.

## Development

Develop from the umbrella checkout, where the siblings are submodules:

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Notes:

- `uv.lock` resolves the extras against `../../services/control-plane` of the umbrella
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
