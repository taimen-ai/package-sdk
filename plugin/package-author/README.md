# package-author — the package author in Claude Code

A Claude Code plugin that leads the whole cycle of a catalog package: task types,
roles and artifacts, work rules, processes, agents, integrations, ontologies,
notification rules, tests and installation. The person describes the work in their
own words or points at a regulation in the knowledge base; the agent asks, writes the
tests before the objects, brings the package to green tests by machine-readable
findings, builds the installation plan and shows it. **It applies a plan only after
the person's explicit yes to the plan they were shown.**

The skills are written in Russian, like the rest of the documentation of this
repository.

## Cycle

```text
describe-process ─┐                                                     ┌─► pkg_plan ─► human yes ─► pkg_apply
                  ├─► write-tests-first ─► author-* ─► validate-and-fix ─┤
process-from-regulation ─┘                                              └─► release-package (version, tag, lock)
```

| Skill | What it does | Tools |
|---|---|---|
| `describe-process` | interview by a template → a draft specification in words to confirm | `cp_recall`, `cp_process_get` |
| `process-from-regulation` | a regulation from the knowledge base → clauses → `governedBy` and a test per verifiable requirement → clause coverage in the plan | `cp_recall`, `pkg_plan` |
| `write-tests-first` | `tests/*.test.yaml` before the objects; every test fails first | `pkg_check`, `pkg_test` |
| `author-package` | a process by the language schema; edits by `package-sdk edit` operations | `pkg_edit`, `pkg_test` |
| `author-work` | task types, roles, artifact types with `subject: taskType` tests | `pkg_check`, `pkg_test` |
| `author-rule` | work rules with `subject: rule` tests | `pkg_check`, `pkg_test` |
| `author-agent` | agents: identity, work, executor, skills, placement | `pkg_check`, `pkg_describe` |
| `author-integration` | observer and skills of an integration, their tests and images | `pkg_test` |
| `author-notification` | notification rules | `pkg_check` |
| `validate-and-fix` | the loop over findings `{code, file, line, path, message, hint}` | `pkg_check`, `pkg_test` |
| `simulate-and-plan` | tests with coverage, the installation plan by section, applying on a yes | `pkg_test`, `pkg_plan`, `pkg_apply` |
| `release-package` | version, changelog, tag on a yes, lock, plan and apply on a yes | `pkg_edit`, `pkg_test`, `pkg_plan`, `pkg_apply` |
| `explain-instance` | why an instance is in its state, what an event would do | `cp_process_get`, `cp_process_explain` |
| `goal-as-process` | a desired state as a reconciling process without `complete` | — |
| `knowledge-model` | an own kind of knowledge → a tenant ontology package → plan → register on a yes | `pkg_check`, `pkg_plan`, `pkg_apply` |
| `knowledge-import` | a person's table → the template of a kind (`knowledge.template@1`) → import in the console → the platform's plan → decision on a yes | `cp_invoke_skill`, `cp_approve`, `cp_reject` |

Paths in the skills such as `schema/v1/…` and `examples/claims/…` are paths in this
repository: the reference example is the end-to-end package `examples/claims/claims/`;
constructs it does not use are shown by the fixtures in `tests/fixtures/…`.

## Consent rule

Applying without the person's consent is forbidden. Three lines hold the rule:

1. **Skills.** `simulate-and-plan`, `release-package` and `knowledge-model` call
   `pkg_apply` only after the whole plan was shown and the person said yes to it in
   the conversation; `knowledge-import` approves the import decision (`cp_approve`)
   the same way; publishing a release tag needs its own yes. The other skills do not
   write to a stand.
2. **The plugin hook** (`hooks/hooks.json` → `hooks/confirm_apply.py`, PreToolUse).
   Before every `pkg_apply` it asks the host to confirm the call (`ask`: plan file,
   stand, `planHash` and the number of changes by section); a call without
   `plan_hash`, with a relative or unreadable `plan_file`, or with a hash other than
   the one in the plan file, is denied (`deny`). In `bypassPermissions` mode the host may
   not show the prompt, so the main line is the rule of the skills.
3. **package-sdk.** `pkg_apply` reads the saved plan once and applies exactly that
   document with the confirmed `planHash` (`plan_hash_mismatch` otherwise); before its
   first write it builds every section again and refuses `plan_stale` when the stand,
   the sources or the variables moved.

The server also keeps two boundaries of its own, whatever the agent asks:

- **stands** — the token goes only to the stands listed in `PACKAGE_SDK_SERVERS` of the
  server's environment (addresses separated by spaces or commas; `https://`, or
  `http://` only for localhost). Any other address is `server_not_allowed` before a
  token is requested, so text in a package cannot lead the credential elsewhere.
  With one stand configured, `server` may be omitted;
- **the session root** — `PACKAGE_SDK_ROOT`, otherwise the first `file://` root of
  the client, otherwise the current directory. Relative paths and `.env` resolve from
  it; `pkg_edit` (including `@<file>` fragments) writes only inside it; plans go only to
  `<root>/.package-sdk/*.json`, and a file there that is not a plan is never
  overwritten. `package-sdk init` adds `.package-sdk/` to the package's `.gitignore`.

## Installation

The plugin brings its MCP server: `package-sdk mcp` (`.mcp.json`) with the tools
`pkg_check`, `pkg_test`, `pkg_describe`, `pkg_edit`, `pkg_plan` and `pkg_apply`.

1. **package-sdk with the `mcp` extra** on `PATH` (the `sandbox` extra adds the core's
   code for checks and tests without a stand, `skills` the skill contract stage):

   ```bash
   uv tool install "package-sdk[mcp,sandbox,skills] @ git+https://github.com/<org>/package-sdk@<tag>"
   package-sdk mcp --help
   ```

2. **The stands** — `PACKAGE_SDK_SERVERS` in the environment the host starts the
   server with (for example `PACKAGE_SDK_SERVERS=https://cp.example.com`); without it
   the server works without a stand: checks, tests, edits.
3. **The stand credential** — the same as the operator's: `CP_TOKEN`, or the
   credential `control-plane-client` finds (the IAM credential file, then the API
   key). Only `pkg_plan`, `pkg_apply` and the `server` option of `pkg_check` and
   `pkg_test` need it. Rights: `packages.test` and `packages.plan`, plus the rights of
   the kinds being installed.
4. **The operator plugin** (`control-plane-operator`) for the `cp_*` tools some skills
   use: `cp_recall`, `cp_process_get`, `cp_process_explain`, `cp_invoke_skill`,
   `cp_approve` and others.
5. **Add the marketplace and install the plugin.** The repository root is the
   marketplace `package-sdk`:

   ```bash
   claude plugin marketplace add <org>/package-sdk        # or a local checkout path
   claude plugin install package-author@package-sdk
   ```

   For plugin development without installing: `claude --plugin-dir plugin/package-author`.
6. **Check.** In a new session `/plugin` shows `package-author`, `/mcp` shows the
   `package-sdk` server with six tools, and a request like "describe our approval
   process" starts the interview.

Rule, task type and scenario tests of the sandbox need an empty PostgreSQL database:
set `PACKAGE_SDK_SANDBOX_DATABASE_URL` in the environment of the session.

## Plugin checks

`tests/test_package_author_plugin.py` checks the manifest, the marketplace, the
frontmatter of the skills, the hook, that the `pkg_*` tools the skills name exist in
the SDK's MCP server, that the `cp_*` tools exist in the core's MCP server, the
`package-sdk` commands the skills name, the paths of the reference examples and the
YAML examples against the schemas. `tests/test_mcp.py` runs the MCP tools against the
server in process and over stdio.
