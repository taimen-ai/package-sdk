"""Каталог workingCopy вида claude-code и выдаваемый секрет forge-token (TAI-ADR-0063, U001).

Схема пакетов — контракт формы: ядро хранит workingCopy как данные (амендмент
CP-ADR-0073 п.1), толкует его демон исполнителя. Связи между записями каталога,
которых JSON Schema не выражает, держит package-sdk check. Перенесено из суперпроекта
(U001, TAI-ADR-0063) вместе со схемой: сценарии те же.
"""

from __future__ import annotations

import copy
import json
import shutil

import pytest
import yaml

from tests.umbrella._shim import FIXTURES, cp
from tests.umbrella._shim import UMBRELLA as ROOT

EXAMPLE = yaml.safe_load((FIXTURES / "agents" / "universal-coder.yaml").read_text(encoding="utf-8"))
SELFDEV_AGENTS = sorted((ROOT / "packages" / "selfdev" / "agents").glob("*.yaml"))


def errors(document: dict) -> list[str]:
    return [cp._format_schema_error(e) for e in cp._schema_validator().iter_errors(document)]


def example() -> dict:
    return copy.deepcopy(EXAMPLE)


def catalog(document: dict) -> dict:
    return document["spec"]["workingCopy"]


# --- принимает -------------------------------------------------------------------


def test_the_catalog_example_is_valid():
    assert errors(EXAMPLE) == []
    assert cp._working_copy_catalog_errors(EXAMPLE["spec"]) == []


@pytest.mark.parametrize("path", SELFDEV_AGENTS, ids=lambda p: p.stem)
def test_every_current_selfdev_agent_is_still_valid(path):
    """Прежняя форма с одним repository остаётся (FR-029)."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert errors(document) == []
    assert cp._working_copy_catalog_errors(document["spec"]) == []


def test_a_cyrillic_alias_is_accepted():
    assert "суперпроект" in catalog(EXAMPLE)["repositories"]["superproject"]["aliases"]
    document = example()
    catalog(document)["repositories"]["fleet"]["aliases"] = ["Флот", "fleet.old", "fleet_v1"]
    assert errors(document) == []


def test_a_literal_https_url_is_accepted():
    document = example()
    catalog(document)["repositories"]["fleet"]["url"] = "https://git.example/org/fleet.git"
    assert errors(document) == []


def test_forge_token_is_declared_like_a_static_secret():
    assert "forge-token" in EXAMPLE["spec"]["placement"]["secrets"]
    document = example()
    document["spec"]["placement"]["secrets"] = ["forge-token"]
    assert errors(document) == []


def test_publish_defaults_to_the_catalog_and_can_be_left_out():
    document = example()
    del catalog(document)["publish"]
    del catalog(document)["superproject"]
    assert errors(document) == []
    assert cp._working_copy_catalog_errors(document["spec"]) == []


# --- отвергает: форма ------------------------------------------------------------


def test_an_entry_without_url_is_rejected():
    document = example()
    del catalog(document)["repositories"]["fleet"]["url"]
    assert any(
        "repositories/fleet" in e and "'url' is a required property" in e for e in errors(document)
    )


@pytest.mark.parametrize(
    "key", ["Control-Plane", "control_plane", "суперпроект", "-fleet", "a" * 64, ""]
)
def test_a_key_outside_the_pattern_is_rejected(key):
    document = example()
    catalog(document)["repositories"][key] = {"url": "${SELFDEV_FLEET_URL}"}
    assert any("repositories" in e and repr(key) in e for e in errors(document)), key


def test_repository_field_without_repositories_is_rejected():
    document = example()
    del catalog(document)["repositories"]
    assert any("'repositories' is a required property" in e for e in errors(document))


def test_repositories_without_repository_field_is_rejected():
    """Значения по умолчанию нет: без поля задачи демон не знает, где ключ (FR-004)."""
    document = example()
    del catalog(document)["repositoryField"]
    assert any("'repositoryField' is a required property" in e for e in errors(document))


def test_an_empty_catalog_is_rejected():
    document = example()
    catalog(document)["repositories"] = {}
    assert any("workingCopy/repositories" in e for e in errors(document))


@pytest.mark.parametrize(
    "where, field",
    [
        ("catalog", "neighbours"),  # соседи — ключами runner.yaml, не адресами в описании
        ("catalog", "repository"),  # две формы разом
        ("catalog", "review"),  # устаревшая секция прежней формы
        ("entry", "branch"),
        ("entry", "token"),
    ],
)
def test_an_unknown_field_is_rejected(where, field):
    document = example()
    target = catalog(document) if where == "catalog" else catalog(document)["repositories"]["fleet"]
    target[field] = "x"
    assert any(field in e and "Additional properties" in e for e in errors(document)), field


@pytest.mark.parametrize(
    "url",
    [
        "http://git.example/org/fleet.git",
        "git@git.example:org/fleet.git",
        "ssh://git@git.example/org/fleet.git",
        "file:///srv/fleet.git",
        "https://user:secret@git.example/org/fleet.git",
        "https://git.example/org/fleet.git?ref=main",
        "https://git.example",
        "${selfdev_fleet_url}",
        "",
    ],
)
def test_a_url_that_is_not_https_or_a_variable_is_rejected(url):
    document = example()
    catalog(document)["repositories"]["fleet"]["url"] = url
    assert any("repositories/fleet/url" in e for e in errors(document)), url


@pytest.mark.parametrize("alias", ["", "с пробелом", ".hidden", "a/b", "emoji🙂", "x" * 64])
def test_an_alias_outside_the_pattern_is_rejected(alias):
    document = example()
    catalog(document)["repositories"]["fleet"]["aliases"] = [alias]
    assert any("repositories/fleet/aliases" in e for e in errors(document)), alias


def test_a_duplicate_alias_within_an_entry_is_rejected():
    document = example()
    catalog(document)["repositories"]["superproject"]["aliases"] = ["суперпроект", "суперпроект"]
    assert any("aliases" in e and "non-unique" in e for e in errors(document))


@pytest.mark.parametrize(
    "field, value",
    [
        ("key", "fleet\n"),
        ("alias", "суперпроект\n"),
        ("url", "${SELFDEV_FLEET_URL}\n"),
        ("url", "https://git.example/o/x\n"),
        ("repositoryField", "repositoryKey\n"),
        ("directory", "fleet\n"),
        ("baseRef", "main\n"),
        ("superproject", "superproject\n"),
    ],
)
def test_a_trailing_newline_is_rejected(field, value):
    """$ в re Python допускает завершающий перевод строки — шаблоны закрыты (?![\\s\\S])."""
    document = example()
    wc, fleet = catalog(document), catalog(document)["repositories"]["fleet"]
    if field == "key":
        wc["repositories"][value] = wc["repositories"].pop("fleet")
    elif field == "alias":
        fleet["aliases"] = [value]
    elif field in ("url", "directory", "baseRef"):
        fleet[field] = value
    else:
        wc[field] = value
    assert errors(document), (field, value)


@pytest.mark.parametrize(
    "url",
    [
        "https://git.example/o/../x.git",
        "https://git.example/./x.git",
        "https://git.example/o/.",
        "https://git.example/o/..",
        "https://git.example/o/.git",
        "https://git.example/o/.hidden",
        "https://git.example/o/x\x01",
        "https://git.example/o/x\x00",
        "https://git.example/o/x\x7f",
        "https://git.example/o/x\u200b",
        "https://git.example/o/x\ufeff",
        "https://git.example/o\\x.git",
        "https://git.example/o/x y",
        "https://git.example/o/x%2e%2e",
        "https://-/x",
        "https://../x",
        "https://git..example/x",
        "https://-git.example/x",
        "https://git-.example/x",
        "https://.git.example/x",
        "https://git.example./x",
        "https://git.example:65536/x",
        "https://git.example:99999/x",
        "https://git.example:0/x",
        "https://git.example:/x",
        "https://git.example/",
        "https://git.example//x",
        "https://git.example/-x",
    ],
)
def test_a_url_path_or_host_that_would_confuse_the_mirror_is_rejected(url):
    """Имя зеркала берётся из последнего сегмента пути (TAI-ADR-0063 п.3)."""
    document = example()
    catalog(document)["repositories"]["fleet"]["url"] = url
    assert any("repositories/fleet/url" in e for e in errors(document)), url


@pytest.mark.parametrize(
    "url",
    [
        "https://git.example:65535/o/x.git",
        "https://git.example:8443/o/x",
        "https://git.example/o/x/",
        "https://git-1.example.org/group/sub/x_y.z~1.git",
    ],
)
def test_a_well_formed_https_url_is_accepted(url):
    document = example()
    catalog(document)["repositories"]["fleet"]["url"] = url
    assert errors(document) == []


@pytest.mark.parametrize(
    "ref",
    [
        "-main",
        "main branch",
        "a..b",
        "feature/",
        "x.lock",
        "x.lock/y",
        "@",
        "a@{1}",
        "/main",
        "a//b",
        ".hidden",
        "a/.b",
        "a~1",
        "a^",
        "a:b",
        "a?",
        "a*",
        "a[b",
        "a\\b",
        "\tmain",
        "main.",
        "",
        "m\x7f",
    ],
)
def test_a_base_ref_that_is_not_a_ref_name_is_rejected(ref):
    document = example()
    catalog(document)["repositories"]["fleet"]["baseRef"] = ref
    assert any("repositories/fleet/baseRef" in e for e in errors(document)), ref


@pytest.mark.parametrize(
    "ref", ["main", "master", "release/v1.2", "feature/universal-runner", "task/TASK-001072"]
)
def test_a_ref_name_is_accepted_as_base_ref(ref):
    document = example()
    catalog(document)["repositories"]["fleet"]["baseRef"] = ref
    assert errors(document) == []


@pytest.mark.parametrize(
    "field, value",
    [
        ("repositories", None),
        ("repositories", []),
        ("repositoryField", None),
        ("repositoryField", "repository key"),
        ("publish", "yes"),
        ("entry.url", None),
        ("entry.aliases", None),
        ("entry.aliases", []),
        ("entry.aliases", "суперпроект"),
        ("entry.publish", None),
        ("entry", None),
    ],
)
def test_null_and_wrong_types_are_rejected(field, value):
    document = example()
    if field == "entry":
        catalog(document)["repositories"]["fleet"] = value
    elif field.startswith("entry."):
        catalog(document)["repositories"]["fleet"][field.split(".", 1)[1]] = value
    else:
        catalog(document)[field] = value
    assert errors(document), (field, value)
    cp._working_copy_catalog_errors(document["spec"])  # не падает на форме, которую отвергла схема


def test_the_catalog_is_for_claude_code_only():
    """Форму задаёт вид исполнителя: у остальных видов — прежняя форма."""
    document = example()
    document["spec"]["executor"] = {"kind": "codex", "params": {"sandbox": "workspace-write"}}
    assert any("repositories" in e and "Additional properties" in e for e in errors(document))


def test_the_superproject_of_a_catalog_is_a_key_not_an_address():
    document = example()
    catalog(document)["superproject"] = "https://git.example/org/superproject.git"
    assert any("workingCopy/superproject" in e for e in errors(document))


# --- отвергает: связи между записями (package-sdk check) --------------------------


def test_superproject_must_name_a_catalog_key():
    document = example()
    catalog(document)["superproject"] = "umbrella"
    assert errors(document) == []
    assert any(
        "workingCopy.superproject" in e for e in cp._working_copy_catalog_errors(document["spec"])
    )


def test_an_alias_used_by_two_entries_is_rejected():
    document = example()
    catalog(document)["repositories"]["fleet"]["aliases"] = ["суперпроект"]
    found = cp._working_copy_catalog_errors(document["spec"])
    assert any("fleet.aliases" in e and "'superproject'" in e for e in found) or any(
        "superproject.aliases" in e and "'fleet'" in e for e in found
    )


def test_an_alias_equal_to_another_key_is_rejected():
    document = example()
    catalog(document)["repositories"]["fleet"]["aliases"] = ["control-plane"]
    assert any(
        "fleet.aliases" in e and "'control-plane'" in e
        for e in cp._working_copy_catalog_errors(document["spec"])
    )


@pytest.mark.parametrize("alias", ["fleet", "FLEET", "Fleet"])
def test_an_alias_equal_to_its_own_key_is_rejected(alias):
    document = example()
    catalog(document)["repositories"]["fleet"]["aliases"] = [alias]
    assert any(
        "fleet.aliases" in e and "совпадает с ключом записи без учёта регистра" in e
        for e in cp._working_copy_catalog_errors(document["spec"])
    )


def test_two_aliases_of_one_entry_differing_in_case_name_the_other_alias():
    document = example()
    catalog(document)["repositories"]["superproject"]["aliases"] = ["Суперпроект", "суперпроект"]
    assert errors(document) == []  # uniqueItems различает регистр — ловит check
    found = cp._working_copy_catalog_errors(document["spec"])
    assert any("повторяет псевдоним 'Суперпроект' этой же записи" in e for e in found), found
    assert not any("совпадает с ключом" in e for e in found)


def test_aliases_differing_only_in_case_are_ambiguous():
    document = example()
    catalog(document)["repositories"]["fleet"]["aliases"] = ["Суперпроект"]
    assert cp._working_copy_catalog_errors(document["spec"])


def test_two_keys_with_one_address_are_rejected():
    document = example()
    catalog(document)["repositories"]["fleet"]["url"] = "${SELFDEV_CONTROL_PLANE_URL}"
    assert any(
        "fleet.url" in e and "'control-plane'" in e
        for e in cp._working_copy_catalog_errors(document["spec"])
    )


@pytest.mark.parametrize(
    "variant",
    [
        "https://github.com/o/x",
        "https://github.com/o/x/",
        "https://github.com/o/x.git",
        "https://github.com/o/x.git/",
        "https://GitHub.com/o/x",
        "https://GITHUB.COM/O/X.GIT",
    ],
)
def test_one_address_in_another_spelling_is_rejected(variant):
    """Адреса сравниваются нормализованными — как в integrations/selfdev (oss_sync)."""
    document = example()
    catalog(document)["repositories"]["control-plane"]["url"] = "https://github.com/o/x"
    catalog(document)["repositories"]["fleet"]["url"] = variant
    assert errors(document) == []
    assert any(
        "fleet.url" in e and "'control-plane'" in e
        for e in cp._working_copy_catalog_errors(document["spec"])
    ), variant


@pytest.mark.parametrize(
    "other",
    [
        "https://github.com/o/xy",
        "https://github.com/o2/x",
        "https://gitlab.com/o/x",
        "https://github.com/o/x.gitx",
    ],
)
def test_different_addresses_are_not_confused(other):
    document = example()
    catalog(document)["repositories"]["control-plane"]["url"] = "https://github.com/o/x"
    catalog(document)["repositories"]["fleet"]["url"] = other
    assert cp._working_copy_catalog_errors(document["spec"]) == []


def test_two_entries_with_one_directory_are_rejected():
    document = example()
    catalog(document)["repositories"]["control-plane"]["directory"] = "shared"
    catalog(document)["repositories"]["fleet"]["directory"] = "shared"
    assert any(
        "fleet.directory" in e and "'control-plane'" in e
        for e in cp._working_copy_catalog_errors(document["spec"])
    )


def test_a_directory_equal_to_another_key_is_rejected():
    document = example()
    catalog(document)["repositories"]["fleet"]["directory"] = "control-plane"
    assert any(
        "fleet.directory" in e and "ключом записи 'control-plane'" in e
        for e in cp._working_copy_catalog_errors(document["spec"])
    )


def test_a_directory_equal_to_its_own_key_is_fine():
    document = example()
    catalog(document)["repositories"]["fleet"]["directory"] = "fleet"
    assert errors(document) == []
    assert cp._working_copy_catalog_errors(document["spec"]) == []


def test_check_reports_catalog_links(tmp_path, monkeypatch):
    """Связи каталога проверяет package-sdk check, а не только функция."""
    root = tmp_path / "packages"
    shutil.copytree(ROOT / "packages", root)
    monkeypatch.setattr(cp, "PACKAGES_DIR", root)
    document = example()
    catalog(document)["superproject"] = "umbrella"
    directory = root / "demo"
    directory.mkdir()
    manifest = {
        "apiVersion": cp.API_VERSION,
        "kind": "Package",
        "key": "demo",
        "spec": {"version": "0.1.0", "displayName": "demo", "requires": []},
    }
    (directory / "package.yaml").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    document["spec"]["work"].pop("taskTypes")
    (directory / "agent.yaml").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    errors_, _warnings = cp.check(cp.resolve(["demo"]))
    assert any("workingCopy.superproject" in e and "'umbrella'" in e for e in errors_)
