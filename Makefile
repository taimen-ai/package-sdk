# Цели, которые зовут CI (.github/workflows/ci.yml) и проверки исполнителя
# (.agents/runner.yaml). Соседи по плоской раскладке — path-зависимости экстр:
# ../control-plane (sandbox, connector), ../skill-sdk (skills) и
# ../platform-auth-sdk (зависимость ядра).
.PHONY: install lint fmt typecheck test check

# Со всеми экстрами: проверки и песочница идут кодом ядра, без них часть тестов
# пропускается.
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

# Ядро-сосед здесь всегда есть (все экстры): сверка поддельного ядра и снимков ответов с
# моделями control_plane.api.v1.schemas без него — провал, а не молчаливый пропуск;
# пропуски с причинами видны в -rs (TASK-001186).
test:
	PACKAGE_SDK_REQUIRE_CORE_CONTRACT=1 uv run --all-extras pytest -q -rs

check: lint typecheck test
