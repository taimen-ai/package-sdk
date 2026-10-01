"""Вид KnowledgePack в каталоге и включение онтологий установкой (S011, FR-024)."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from package_sdk import cli, manifest
from package_sdk.apply import Applier, HttpError, knowledge_targets
from package_sdk.check import check
from package_sdk.core import static_error
from package_sdk.model import PackageError, load_installation, resolve

FIXTURES = Path(__file__).parent / "fixtures" / "manifest"
WORKSPACE = "11111111-2222-4333-8444-555555555555"
CLAIMS = {
    "name": "claims",
    "version": 1,
    "extends": ["company@1"],
    "kinds": [{"kind": "case"}, {"kind": "claim_outcome"}],
    "relations": [{"relation": "filed_by"}, {"relation": "resolved_by"}],
}
COMPANY = {"name": "company", "version": 1, "kinds": [{"kind": "customer"}]}


def _pack(directory: Path, spec: dict[str, Any], key: str | None = None) -> Path:
    path = directory / "knowledge-packs" / f"{key or spec['name']}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"apiVersion": "taimen.ai/v1", "kind": "KnowledgePack", "key": key or spec["name"]}
    path.write_text(yaml.safe_dump({**document, "spec": spec}, allow_unicode=True), "utf-8")
    return path


@pytest.fixture
def packages(tmp_path: Path) -> Path:
    target = tmp_path / "packages"
    shutil.copytree(FIXTURES, target)
    _pack(target / "acme-base", COMPANY)
    _pack(target / "acme-claims", CLAIMS)
    return target


def _codes(messages: list[str]) -> list[str]:
    return [static_error(m)["code"] for m in messages]


def test_pack_files_load_as_catalog_objects_and_pass_check(packages: Path) -> None:
    installation = resolve(["acme-claims"], packages_dir=packages)
    packs = [o for o in installation.objects if o.kind == "KnowledgePack"]
    assert [(o.package, o.key) for o in packs] == [
        ("acme-base", "company"),
        ("acme-claims", "claims"),
    ]
    errors, warnings = manifest.check_manifest(installation)
    assert errors == [] and warnings == []
    errors, _ = check(installation, env={})
    assert not [e for e in errors if "knowledge" in e], errors


def test_key_must_be_the_ontology_name(packages: Path) -> None:
    _pack(packages / "acme-claims", {**CLAIMS, "name": "claims"}, key="claims-v2")
    errors, _ = manifest.check_manifest(resolve(["acme-claims"], packages_dir=packages))
    assert "knowledge_pack_key" in _codes(errors)


def test_extends_must_be_in_the_package_or_its_requires(packages: Path) -> None:
    (packages / "acme-base" / "knowledge-packs" / "company.yaml").unlink()
    errors, _ = manifest.check_manifest(resolve(["acme-claims"], packages_dir=packages))
    assert _codes(errors) == ["knowledge_extends_unknown"]
    assert "extends company@1" in errors[0]


def test_platform_ontology_may_be_extended(packages: Path) -> None:
    (packages / "acme-base" / "knowledge-packs" / "company.yaml").unlink()
    _pack(
        packages / "acme-claims",
        {**CLAIMS, "extends": ["default@1"], "kinds": CLAIMS["kinds"] + [{"kind": "customer"}]},
    )
    errors, _ = manifest.check_manifest(resolve(["acme-claims"], packages_dir=packages))
    assert errors == []


def test_same_version_with_other_content_is_a_conflict(packages: Path) -> None:
    changed = copy.deepcopy(COMPANY)
    changed["kinds"].append({"kind": "branch"})
    _pack(packages / "acme-claims", changed)
    errors, _ = manifest.check_manifest(resolve(["acme-claims"], packages_dir=packages))
    assert _codes(errors) == ["knowledge_pack_conflict"]
    assert "поднимите version" in errors[0]


def _installation_file(packages: Path, knowledge: list[dict[str, Any]]) -> Path:
    path = packages.parent / "install.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "taimen.ai/v1",
                "kind": "Installation",
                "key": "stand",
                "spec": {
                    "packages": ["acme-claims"],
                    "packagesDir": "packages",
                    "knowledge": knowledge,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_installation_knowledge_section(packages: Path) -> None:
    installation = load_installation(
        _installation_file(
            packages,
            [
                {"workspace": "${CLAIMS_WORKSPACE_ID}", "packs": ["company@1", "claims@1"]},
                {"workspace": "${CLAIMS_WORKSPACE_ID}", "packs": ["default@1", "claims@1"]},
                {"workspace": WORKSPACE, "packs": ["tenant:other@2"]},
            ],
        )
    )
    targets = knowledge_targets(installation, {"CLAIMS_WORKSPACE_ID": "ws-1"})
    assert targets == [
        {"workspace": "ws-1", "packs": ["company@1", "claims@1", "default@1"], "strict": False},
        {"workspace": WORKSPACE, "packs": ["tenant:other@2"], "strict": False},
    ]
    _errors, warnings = manifest.check_manifest(installation)
    (warning,) = warnings  # онтологии нет в пакетах установки — должна уже быть на стенде
    assert "tenant:other@2" in warning


class FakeCore:
    def __init__(self) -> None:
        self.packs: dict[tuple[str, str], dict[str, Any]] = {}
        self.enabled: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, path: str, body: Any = None, headers: Any = None) -> dict:
        self.calls.append((method, path))
        if (method, path) == ("POST", "/api/v1/knowledge/packs"):
            ident = (body["name"], str(body["version"]))
            if ident in self.packs and self.packs[ident] != body:
                raise HttpError(f"POST {path}: HTTP 409: conflict", 409, {"detail": "conflict"})
            self.packs[ident] = copy.deepcopy(body)
            return {"name": body["name"], "version": body["version"]}
        if method == "PUT" and path.endswith("/knowledge-packs"):
            workspace = path.split("/")[-2]
            self.enabled[workspace] = copy.deepcopy(body)
            return {"settings": body}
        raise AssertionError((method, path))


def _apply(
    installation: Any, core: FakeCore, *, dry_run: bool = False, env: dict | None = None
) -> list[str]:
    lines: list[str] = []
    applier = Applier(core, {}, env=env or {}, dry_run=dry_run, log=lines.append)  # type: ignore[arg-type]
    for obj in installation.objects:
        if obj.kind == "KnowledgePack":
            applier._apply_KnowledgePack(obj, obj.spec)
    for target in knowledge_targets(installation, env or {}):
        applier._enable_knowledge(target)
    return lines


def test_registration_and_enablement_on_a_fake_core(packages: Path) -> None:
    installation = load_installation(
        _installation_file(
            packages, [{"workspace": "${CLAIMS_WORKSPACE_ID}", "packs": ["company@1", "claims@1"]}]
        )
    )
    core = FakeCore()
    lines = _apply(installation, core, env={"CLAIMS_WORKSPACE_ID": WORKSPACE})
    assert set(core.packs) == {("company", "1"), ("claims", "1")}
    assert core.enabled == {WORKSPACE: {"packs": ["company@1", "claims@1"], "strict": False}}
    assert any("онтологии: company@1, claims@1" in line for line in lines)

    # повтор с тем же содержимым — без ошибок
    _apply(installation, core, env={"CLAIMS_WORKSPACE_ID": WORKSPACE})
    # та же версия с другим содержимым — ошибка «поднимите версию»
    obj = next(o for o in installation.objects if o.key == "claims")
    obj.spec["kinds"].append({"kind": "extra"})
    with pytest.raises(PackageError, match="поднимите version"):
        _apply(installation, core, env={"CLAIMS_WORKSPACE_ID": WORKSPACE})


def test_plan_writes_nothing_and_shows_the_final_set(packages: Path) -> None:
    installation = load_installation(
        _installation_file(packages, [{"workspace": WORKSPACE, "packs": ["claims@1"]}])
    )
    core = FakeCore()
    lines = _apply(installation, core, dry_run=True)
    assert core.calls == []
    assert any("онтологии → claims@1 (набор заменит текущий)" in line for line in lines)


@pytest.mark.parametrize(
    ("knowledge", "message"),
    [
        ({"workspace": WORKSPACE}, "список {workspace, packs}"),
        ([{"packs": ["company@1"]}], "workspace — UUID"),
        ([{"workspace": WORKSPACE, "packs": "company@1"}], "packs — список"),
        ([{"workspace": WORKSPACE, "packs": ["company"]}], "company — нужно name@версия"),
        ([{"workspace": WORKSPACE, "packs": ["company@1.2"]}], "нужно name@версия (целое)"),
        ([{"workspace": WORKSPACE, "packs": ["company@1"], "strict": "yes"}], "strict — true"),
        ([{"workspace": WORKSPACE, "packs": [], "mode": "x"}], "лишние поля mode"),
    ],
)
def test_malformed_knowledge_is_a_clear_error_not_a_traceback(
    packages: Path, knowledge: Any, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _installation_file(packages, knowledge)
    with pytest.raises(PackageError, match=r"spec\.knowledge") as error:
        load_installation(path)
    assert message in str(error.value)
    assert cli.main(["check", "--install", str(path)]) != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err + captured.out
    assert message in captured.err + captured.out


def test_strict_reaches_the_core_and_wins_over_the_workspace_set(packages: Path) -> None:
    installation = load_installation(
        _installation_file(
            packages,
            [
                {"workspace": WORKSPACE, "packs": ["company@1"]},
                {"workspace": WORKSPACE, "packs": ["claims@1"], "strict": True},
            ],
        )
    )
    core = FakeCore()
    lines = _apply(installation, core)
    assert core.enabled == {WORKSPACE: {"packs": ["company@1", "claims@1"], "strict": True}}
    assert any("строгий режим" in line for line in lines)
    plan_lines = _apply(installation, FakeCore(), dry_run=True)
    assert any("claims@1, строгий режим (набор заменит текущий)" in line for line in plan_lines)


def test_pack_version_is_an_integer(packages: Path) -> None:
    _pack(packages / "acme-claims", {**CLAIMS, "version": "1.2"})
    errors, _ = check(resolve(["acme-claims"], packages_dir=packages), env={})
    assert [e for e in errors if "version" in e and "integer" in e], errors


def test_pack_files_do_not_go_to_the_core(packages: Path) -> None:
    from package_sdk.source import package_files

    installation = resolve(["acme-claims"], packages_dir=packages)
    package = installation.packages[-1]
    paths = [f["path"] for f in package_files(package, {}, strict=False)]
    assert "package.yaml" in paths
    assert not [p for p in paths if p.startswith("knowledge-packs/")]
