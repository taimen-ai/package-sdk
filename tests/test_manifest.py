"""Манифест пакета (S008, TAI-ADR-0062 п.4): переменные, requires, онтологии, describe, docs."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from package_sdk import cli, manifest, schema, source
from package_sdk.apply import Applier
from package_sdk.core import static_error
from package_sdk.model import Installation, Obj, PackageError, _read_yaml, resolve

FIXTURES = Path(__file__).parent / "fixtures" / "manifest"
UUID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def packages(tmp_path: Path) -> Path:
    """Копия пакетов-фикстур: тесты правят манифесты на месте."""
    target = tmp_path / "packages"
    shutil.copytree(FIXTURES, target)
    return target


def _installation(packages: Path, key: str = "acme-claims", *, pack: bool = True) -> Installation:
    """Установка из фикстур; pack — с онтологией claims@1 (вид KnowledgePack — S011)."""
    installation = resolve([key], packages_dir=packages)
    return _with_pack(installation, CLAIMS_PACK) if pack else installation


def _set_spec(packages: Path, key: str, **changes: object) -> None:
    import yaml

    path = packages / key / "package.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for name, value in changes.items():
        if value is None:
            doc["spec"].pop(name, None)
        else:
            doc["spec"][name] = value
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _codes(messages: list[str]) -> list[str]:
    return [static_error(m)["code"] for m in messages]


# --- диапазоны SemVer ---------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "spec", "expected"),
    [
        ("0.10.2", ">=0.9,<0.11", True),
        ("0.11.0", ">=0.9,<0.11", False),
        ("0.8.9", ">=0.9", False),
        ("1.4.0", "*", True),
        ("1.2.9", "1.2", True),
        ("1.3.0", "1.2", False),
        ("1.2.3", "1.2.3", True),
        ("1.2.4", "=1.2.3", False),
        ("1.9.0", "^1.2.3", True),
        ("2.0.0", "^1.2.3", False),
        ("0.2.9", "^0.2", True),
        ("0.3.0", "^0.2.3", False),
        ("0.0.3", "^0.0.3", True),
        ("0.0.4", "^0.0.3", False),
        ("1.2.9", "~1.2.3", True),
        ("1.3.0", "~1.2.3", False),
        ("1.9.9", "~1", True),
        # частичные версии — префикс, как в npm
        ("1.3.0", ">1.2", True),
        ("1.2.9", ">1.2", False),
        ("1.2.9", "<=1.2", True),
        ("1.3.0", "<=1.2", False),
        ("1.1.9", "<1.2", True),
        ("1.2.0", ">=1.2", True),
        ("1.0.0-rc.1", "<1.0.0", True),
        ("1.0.0-rc.2", ">1.0.0-rc.10", False),
        ("1.0.0-alpha", "<1.0.0-alpha.1", True),
    ],
)
def test_semver_ranges(version: str, spec: str, expected: bool) -> None:
    assert manifest.satisfies(version, spec) is expected


GOOD_RANGES = [">=0.9,<0.11", "^1.2", "~0.3.1", "*", ">= 1.0 , < 2", "1.2", "=1.0.0-rc.1"]
BAD_RANGES = ["<>1", "=>1", "~>0.9", "^~1", "!1", "!=1.0", "==1", "1.0.0+build", ""]


def test_semver_range_grammar_matches_the_schema() -> None:
    """Код и схема принимают одни и те же диапазоны: операторы >=, >, <=, <, =, ^, ~, *."""
    definitions = schema.load(schema.OBJECT)["$defs"]
    validator = Draft202012Validator(definitions["semverRange"])
    for spec in GOOD_RANGES:
        manifest.satisfies("1.0.0", spec)
        assert validator.is_valid(spec), spec
    for spec in BAD_RANGES:
        assert not validator.is_valid(spec), spec
        with pytest.raises(PackageError):
            for part in spec.split(","):
                manifest._condition(part)
    with pytest.raises(PackageError, match="не SemVer"):
        manifest.satisfies("v1", ">=1.0")


@pytest.mark.parametrize("spec", ["<>1", "~>0.9", "!1"])
def test_bad_ranges_are_errors_in_requires_and_engines(packages: Path, spec: str) -> None:
    _set_spec(
        packages,
        "acme-claims",
        requires=[{"package": "acme-base", "version": spec}],
        engines={"control-plane": spec},
    )
    errors, _ = manifest.check_manifest(_installation(packages))
    assert sorted(_codes(errors)) == ["engines_invalid", "requires_version_invalid"]


def test_engines_against_the_core_code_nearby(
    packages: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manifest, "local_versions", lambda: {"control-plane": "0.12.0"})
    errors, warnings = manifest.check_manifest(_installation(packages))
    assert errors == []
    assert _codes(warnings) == ["engines_mismatch"]
    assert "control-plane >=0.9,<0.11" in warnings[0] and "0.12.0" in warnings[0]
    monkeypatch.setattr(manifest, "local_versions", lambda: {"control-plane": "0.10.3"})
    assert manifest.check_manifest(_installation(packages)) == ([], [])


def test_fixtures_pass_the_schema_whole() -> None:
    validator = schema.validator(schema.OBJECT)
    for path in sorted(FIXTURES.rglob("*.yaml")):
        errors = [e.message for e in validator.iter_errors(_read_yaml(path))]
        assert errors == [], (path, errors)


# --- переменные ---------------------------------------------------------------


def test_fixture_manifest_is_clean(packages: Path) -> None:
    errors, warnings = manifest.check_manifest(_installation(packages))
    assert errors == []
    assert warnings == []


def test_undeclared_variable_is_an_error_in_the_new_form(packages: Path) -> None:
    _set_spec(
        packages,
        "acme-claims",
        variables={"CLAIMS_WORKSPACE_ID": {"kind": "workspace", "description": "Root"}},
    )
    errors, _ = manifest.check_manifest(_installation(packages))
    assert _codes(errors).count("variable_undeclared") == 2
    assert any("${HELPDESK_URL}" in e and "Agent/helpdesk-observer" in e for e in errors)


def test_package_without_variables_is_an_error(packages: Path) -> None:
    """Режим перехода закрыт: ${…} без spec.variables — ошибка, а не предупреждение."""
    _set_spec(packages, "acme-claims", variables=None)
    errors, _ = manifest.check_manifest(_installation(packages))
    (error,) = errors
    assert static_error(error)["code"] == "variable_undeclared"
    assert "CLAIMS_REFUND_THRESHOLD, CLAIMS_WORKSPACE_ID, HELPDESK_URL" in error


def test_declared_but_unused_variable_is_an_error(packages: Path) -> None:
    _set_spec(packages, "acme-base", variables={"BASE_URL": {"kind": "url", "description": "x"}})
    errors, _ = manifest.check_manifest(_installation(packages))
    assert _codes(errors) == ["variable_unused"]
    assert "BASE_URL" in errors[0] and "acme-base/package.yaml" in errors[0]


@pytest.mark.parametrize(
    ("kind", "value", "problem"),
    [
        ("url", "helpdesk.example.com", "абсолютный URL"),
        ("workspace", "root", "UUID"),
        ("integer", "50k", "целое"),
    ],
)
def test_default_and_example_must_fit_the_kind(
    packages: Path, kind: str, value: str, problem: str
) -> None:
    variables = {
        "CLAIMS_WORKSPACE_ID": {"kind": "workspace", "description": "Root"},
        "CLAIMS_REFUND_THRESHOLD": {"kind": "integer", "description": "t", "default": "1"},
        "HELPDESK_URL": {"kind": "url", "description": "h"},
    }
    name = {
        "url": "HELPDESK_URL",
        "workspace": "CLAIMS_WORKSPACE_ID",
        "integer": "CLAIMS_REFUND_THRESHOLD",
    }[kind]
    variables[name] = {**variables[name], "example": value}
    _set_spec(packages, "acme-claims", variables=variables)
    errors, _ = manifest.check_manifest(_installation(packages))
    assert _codes(errors) == ["variable_invalid_value"]
    assert f"{name}.example" in errors[0] and problem in errors[0]


def test_values_of_a_good_kind_pass() -> None:
    assert manifest.variable_value_error("url", "https://x.example/api") is None
    assert manifest.variable_value_error("role", UUID) is None
    assert manifest.variable_value_error("integer", "-3") is None
    assert manifest.variable_value_error("string", "anything") is None
    assert manifest.variable_value_error("url", "${BASE}/api") is None  # подставит установка


def test_default_without_required_is_the_canonical_form(packages: Path) -> None:
    """Пример plan Р3: default без required — необязательная переменная, без замечаний."""
    variables = manifest.package_env(_installation(packages).packages[-1], {})
    assert variables["CLAIMS_REFUND_THRESHOLD"] == "50000"
    assert manifest.check_manifest(_installation(packages)) == ([], [])


def test_required_with_default_is_a_warning(packages: Path) -> None:
    variables = {
        "CLAIMS_WORKSPACE_ID": {"kind": "workspace", "description": "Root"},
        "CLAIMS_REFUND_THRESHOLD": {
            "kind": "integer",
            "description": "t",
            "default": "1",
            "required": True,
        },
        "HELPDESK_URL": {"kind": "url", "description": "h"},
    }
    _set_spec(packages, "acme-claims", variables=variables)
    errors, warnings = manifest.check_manifest(_installation(packages))
    assert errors == []
    assert _codes(warnings) == ["variable_required_with_default"]
    assert "CLAIMS_REFUND_THRESHOLD" in warnings[0]


def test_apply_uses_defaults_and_names_the_missing_variable(packages: Path) -> None:
    installation = _installation(packages)
    applier = Applier(http=None, headers={}, env={"CLAIMS_WORKSPACE_ID": UUID})  # type: ignore[arg-type]
    task_type = next(o for o in installation.objects if o.kind == "TaskType")
    assert "above 50000 need" in applier._spec(installation, task_type)["description"]
    agent = next(o for o in installation.objects if o.kind == "Agent")
    with pytest.raises(PackageError) as error:
        applier._spec(installation, agent)
    # описание из манифеста, без заглушки вместо значения
    assert "HELPDESK_URL не задана" in str(error.value)
    assert "Helpdesk API the observer polls" in str(error.value)
    assert "https://helpdesk.example.com/api" in str(error.value)


def test_core_requests_get_defaults_and_the_description(packages: Path) -> None:
    """plan, test и песочница собирают тело запроса ядру той же подстановкой, что check и
    apply: default доходит, незаданная — с описанием, без литерала ${…} в плане."""
    package = _installation(packages).packages[-1]
    env = {"CLAIMS_WORKSPACE_ID": UUID, "HELPDESK_URL": "https://h.example/api"}
    files = {f["path"]: f["content"] for f in source.package_files(package, env, strict=True)}
    assert "refunds above 50000 need" in files["task-types/claim-review.yaml"]
    with pytest.raises(PackageError, match="Helpdesk API the observer polls"):
        source.package_files(package, {"CLAIMS_WORKSPACE_ID": UUID}, strict=True)
    loose = {
        f["path"]: f["content"]
        for f in source.package_files(package, {"CLAIMS_WORKSPACE_ID": UUID}, strict=False)
    }
    assert "${HELPDESK_URL}" in loose["agents/helpdesk-observer.yaml"]
    assert "refunds above 50000 need" in loose["task-types/claim-review.yaml"]


def test_installation_value_wins_over_default(packages: Path) -> None:
    package = _installation(packages).packages[-1]
    env = manifest.package_env(package, {"CLAIMS_REFUND_THRESHOLD": "7"})
    assert env["CLAIMS_REFUND_THRESHOLD"] == "7"


# --- requires -----------------------------------------------------------------


def test_requires_range_is_checked_against_the_installation(packages: Path) -> None:
    _set_spec(packages, "acme-base", version="0.1.4")
    errors, _ = manifest.check_manifest(_installation(packages))
    assert _codes(errors) == ["requires_version_mismatch"]
    assert "нужен acme-base ^0.2, в установке acme-base 0.1.4" in errors[0]


def test_requires_object_form_resolves_like_a_key(packages: Path) -> None:
    installation = _installation(packages)
    assert [p.key for p in installation.packages] == ["acme-base", "acme-claims"]
    assert installation.packages[-1].requirements == [("acme-base", "^0.2")]
    assert installation.packages[-1].requires == ["acme-base"]


# --- онтологии ----------------------------------------------------------------


def _with_pack(
    installation: Installation, spec: dict, package: str = "acme-claims"
) -> Installation:
    """KnowledgePack в пакете (вид каталога появляется в S011): объект в памяти."""
    installation = copy.deepcopy(installation)
    owner = next(p for p in installation.packages if p.key == package)
    owner.objects.append(
        Obj("KnowledgePack", spec["name"], spec, package, owner.path / "knowledge-packs" / "x.yaml")
    )
    return installation


CLAIMS_PACK = {
    "name": "claims",
    "version": 1,
    "kinds": [{"kind": "case"}, {"kind": "customer"}, {"kind": "claim_outcome"}],
    "relations": [{"relation": "filed_by"}, {"relation": "resolved_by"}],
}


def test_knowledge_uses_are_collected_from_memory_recall_and_context(packages: Path) -> None:
    use = manifest.knowledge_uses(_installation(packages).packages[-1])
    # вид дела по умолчанию — case; факт с именем kind и фильтр where — не виды
    assert sorted(use.kinds) == ["case", "claim_outcome", "customer"]
    assert sorted(use.relations) == ["filed_by", "resolved_by"]


def test_declared_ontology_must_come_from_a_knowledge_pack(packages: Path) -> None:
    errors, _ = manifest.check_manifest(_installation(packages, pack=False))
    assert _codes(errors) == ["knowledge_unknown"]  # claims@1 объявлена, KnowledgePack нет
    _set_spec(packages, "acme-claims", knowledge=["claims@1", "company@1"])
    errors, _ = manifest.check_manifest(_installation(packages))
    assert _codes(errors) == ["knowledge_unknown"]
    assert "company@1" in errors[0]


def test_every_used_kind_and_relation_is_in_the_declared_ontologies(packages: Path) -> None:
    pack = {**CLAIMS_PACK, "relations": [{"relation": "filed_by"}]}
    errors, _ = manifest.check_manifest(_with_pack(_installation(packages, pack=False), pack))
    assert _codes(errors) == ["knowledge_term_unknown"]
    assert "связь 'resolved_by'" in errors[0] and "Process/claim" in errors[0]
    errors, _ = manifest.check_manifest(_installation(packages))
    assert errors == []


def test_extends_brings_the_base_ontology(packages: Path) -> None:
    base = {"name": "company", "version": 1, "kinds": [{"kind": "customer"}]}
    claims = {
        **CLAIMS_PACK,
        "kinds": [{"kind": "case"}, {"kind": "claim_outcome"}],
        "extends": ["company@1"],
    }
    installation = _with_pack(
        _with_pack(_installation(packages, pack=False), base, "acme-base"), claims
    )
    errors, _ = manifest.check_manifest(installation)
    assert errors == []


def test_pack_of_an_unrelated_package_is_not_visible(packages: Path) -> None:
    (packages / "other").mkdir()
    (packages / "other" / "package.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Package\nkey: other\n"
        "spec: {version: 1.0.0, displayName: O}\n"
    )
    installation = resolve(["acme-claims", "other"], packages_dir=packages)
    installation = _with_pack(installation, CLAIMS_PACK, "other")
    errors, _ = manifest.check_manifest(installation)
    assert _codes(errors) == ["knowledge_unknown"]


def test_platform_ontology_is_checked_by_its_snapshot(packages: Path) -> None:
    """default@1 известна SDK снимком memory-service: виды процесса, которых в ней нет, —
    ошибка, как у онтологий пакетов."""
    _set_spec(packages, "acme-claims", knowledge=["default@1"])
    errors, warnings = manifest.check_manifest(_installation(packages))
    assert _codes(errors) == ["knowledge_term_unknown"] * len(errors) and errors
    assert warnings == []


def test_memory_without_knowledge_is_a_transition_warning(packages: Path) -> None:
    _set_spec(packages, "acme-claims", knowledge=None)
    errors, warnings = manifest.check_manifest(_installation(packages))
    assert errors == []
    assert _codes(warnings) == ["knowledge_undeclared"]


# --- describe и docs ------------------------------------------------------------


def test_describe_lists_every_prerequisite(packages: Path) -> None:
    package, installation, problem = manifest.load_for_describe(packages / "acme-claims")
    assert problem is None
    info = manifest.describe(package, installation)
    assert info["engines"] == {"control-plane": ">=0.9,<0.11"}
    assert info["requires"] == [{"package": "acme-base", "version": "^0.2", "resolved": "0.2.1"}]
    variables = {v["name"]: v for v in info["variables"]}
    assert set(variables) == {"CLAIMS_WORKSPACE_ID", "CLAIMS_REFUND_THRESHOLD", "HELPDESK_URL"}
    assert variables["CLAIMS_WORKSPACE_ID"]["required"] is True
    assert variables["CLAIMS_REFUND_THRESHOLD"]["required"] is False
    assert variables["HELPDESK_URL"]["usedBy"] == ["Agent/helpdesk-observer"]
    (agent,) = info["agents"]
    assert agent["nodeLabels"] == ["helpdesk-access", "region=eu"]
    assert agent["nodeSecrets"] == ["helpdesk-token"]
    assert agent["image"] == "registry.example.com/acme/claims-observer:0.3.0"
    assert agent["roles"] == ["claims-officer"]
    assert info["knowledge"]["declared"] == ["claims@1"]
    assert info["knowledge"]["used"]["kinds"] == ["case", "claim_outcome", "customer"]
    assert info["planKinds"] == ["Process"]


def test_describe_without_neighbours_still_answers(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "acme-claims", tmp_path / "acme-claims")
    package, installation, problem = manifest.load_for_describe(tmp_path / "acme-claims")
    assert installation is None and "acme-base" in (problem or "")
    info = manifest.describe(package)
    assert info["requires"][0]["resolved"] is None
    assert "(не найдена рядом)" in manifest.format_describe(info, problem)


def test_describe_cli_and_env_example_carry_no_secret_values(
    packages: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HELPDESK_URL", "https://user:s3cr3t@helpdesk.example.com")
    monkeypatch.setenv("CLAIMS_WORKSPACE_ID", UUID)
    assert cli.main(["describe", str(packages / "acme-claims")]) == 0
    text = capsys.readouterr().out
    assert "helpdesk-token" in text and "s3cr3t" not in text and UUID not in text
    assert "CLAIMS_WORKSPACE_ID [workspace, обязательна]" in text

    assert cli.main(["describe", str(packages / "acme-claims"), "--env-example"]) == 0
    example = capsys.readouterr().out
    assert "s3cr3t" not in example and UUID not in example
    assert "CLAIMS_WORKSPACE_ID=\n" in example
    assert "CLAIMS_REFUND_THRESHOLD=50000\n" in example
    assert "# example: https://helpdesk.example.com/api" in example
    # заготовка — законный файл переменных: строки NAME=… и комментарии
    for line in example.splitlines():
        assert not line or line.startswith("#") or "=" in line

    assert cli.main(["describe", str(packages / "acme-claims"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["package"] == "acme-claims"


def test_docs_write_then_check(packages: Path, capsys: pytest.CaptureFixture[str]) -> None:
    readme = packages / "acme-claims" / "README.md"
    readme.write_text("# Customer claims\n\nHand-written intro.\n", encoding="utf-8")
    assert cli.main(["docs", str(packages / "acme-claims"), "--check"]) == 1
    assert cli.main(["docs", str(packages / "acme-claims"), "--write"]) == 0
    text = readme.read_text(encoding="utf-8")
    assert text.startswith("# Customer claims\n\nHand-written intro.\n\n" + manifest.DOCS_BEGIN)
    assert "| `HELPDESK_URL` | url | yes | — | Helpdesk API the observer polls |" in text
    assert (
        "| `helpdesk-observer` | observer | helpdesk-access, region=eu | helpdesk-token |" in text
    )
    assert "`acme-base ^0.2`" in text
    assert cli.main(["docs", str(packages / "acme-claims"), "--check"]) == 0

    # правка пакета делает раздел устаревшим, повторная запись меняет только раздел
    _set_spec(packages, "acme-claims", license="MIT", engines={"control-plane": ">=0.10"})
    assert cli.main(["docs", str(packages / "acme-claims"), "--check"]) == 1
    readme.write_text(text + "\nFooter.\n", encoding="utf-8")
    assert cli.main(["docs", str(packages / "acme-claims"), "--write"]) == 0
    updated = readme.read_text(encoding="utf-8")
    assert "`control-plane >=0.10`" in updated and updated.endswith("\nFooter.\n")
    assert updated.count(manifest.DOCS_BEGIN) == 1
    capsys.readouterr()


def test_describe_and_docs_are_routed(capsys: pytest.CaptureFixture[str]) -> None:
    for command in ("describe", "docs"):
        with pytest.raises(SystemExit) as exit_info:
            cli.main([command, "--help"])
        assert exit_info.value.code == 0
        assert f"package-sdk {command}" in capsys.readouterr().out


# --- находки и схема ------------------------------------------------------------


def test_static_error_carries_the_rule_code() -> None:
    found = static_error("packages/a/package.yaml: variable_unused: X объявлена")
    assert found == {
        "code": "variable_unused",
        "severity": "error",
        "file": "packages/a/package.yaml",
        "message": "X объявлена",
    }
    plain = static_error("packages/a/x.yaml: execution ссылается на Skill")
    assert plain["code"] == "static_check" and plain["message"] == "execution ссылается на Skill"
    assert static_error("retire: вид X")["code"] == "static_check"


def _agent(image: str) -> dict:
    return {
        "apiVersion": "taimen.ai/v1",
        "kind": "Agent",
        "key": "obs",
        "spec": {
            "displayName": "Obs",
            "identity": {"kind": "agent"},
            "executor": {"kind": "observer", "image": image, "params": {"entrypoint": "a.b:c"}},
            "placement": {},
        },
    }


DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("image", "valid"),
    [
        ("registry.example.com/acme/obs:0.3.0", True),
        ("registry.example.com:5000/acme/obs:latest", True),
        (f"ghcr.io/acme/obs@{DIGEST}", True),
        (f"ghcr.io/acme/obs:1.0@{DIGEST}", True),
        ("acme/obs:1", True),
        ("ghcr.io/acme/obs", False),  # тег или дайджест обязателен (CP-ADR-0073 Е1)
        ("ghcr.io/Acme/obs:1", False),
        ("user:secret@ghcr.io/acme/obs:1", False),
        ("ghcr.io/acme/obs@sha256:abc", False),
        ("r.example.com/" + "a" * 240 + ":1", False),  # длиннее 255
    ],
)
def test_executor_image_follows_the_core_grammar(image: str, valid: bool) -> None:
    errors = list(schema.validator(schema.OBJECT).iter_errors(_agent(image)))
    assert (not errors) is valid, [e.message for e in errors]


def test_case_entity_rel_is_a_fact_predicate_not_an_ontology_relation() -> None:
    """`rel` сущности дела — предикат факта: память его не сверяет с онтологиями."""
    assert "rel" not in manifest._RELATION_FIELDS
