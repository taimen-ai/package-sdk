# Targets called by CI (.github/workflows/ci.yml) and by the executor's checks
# (.agents/runner.yaml). Neighbours in the umbrella layout (TAI-ADR-0064) are path
# dependencies of extras: ../../services/control-plane (sandbox, connector),
# ../skill-sdk (skills) and ../platform-auth-sdk (a dependency of the core).
.PHONY: install lint fmt typecheck test check

# With all extras: checks and the sandbox run the core's code; without them some tests
# are skipped.
install:
	uv sync --frozen --all-extras

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check . --fix
	uv run ruff format .

typecheck:
	uv run --all-extras mypy

# The neighbouring core is always present here (all extras): without it, checking the fake
# core and the response snapshots against the control_plane.api.v1.schemas models fails
# instead of being silently skipped; skips and their reasons show in -rs (TASK-001186).
test:
	PACKAGE_SDK_REQUIRE_CORE_CONTRACT=1 uv run --all-extras pytest -q -rs

check: lint typecheck test
