"""Экраны пакета описанием (TAI-ADR-0066, CP-ADR-0080): виды View и Component, языки
``package.yaml`` и словари ``i18n/<locale>.yaml`` в ``check`` и ``plan``.

Фикстура — пакет ``tests/fixtures/screens/packages/invoice-payment``: список экземпляров
процесса и его карточка с компонентом, словари en и ru. Каждая находка проверяется в двух
режимах: статикой SDK (``static``) и проверкой ядра (``core``) — второй идёт, если код ядра
рядом знает экраны (``control_plane.domain.views``), иначе пропускается.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from package_sdk import check, cli, core, install, model, schema, screens

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "screens" / "packages" / "invoice-payment"
LIST = "views/invoice-payment-list.yaml"
CARD = "views/invoice-payment-card.yaml"
COMPONENT = "components/invoice-summary.yaml"
EN, RU = "i18n/en.yaml", "i18n/ru.yaml"


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Копия фикстуры, которую тест может портить; пути находок — от tmp_path."""
    monkeypatch.setattr(model, "ROOT", tmp_path)
    root = tmp_path / "packages" / "invoice-payment"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture(params=["static", "core"])
def mode(request: pytest.FixtureRequest) -> Any:
    """Чем проверяются экраны: None — статикой SDK, иначе модулями ядра."""
    if request.param == "static":
        return None
    found = screens.core_views()
    if found is None:
        pytest.skip("the core next to the SDK predates CP-ADR-0080 (no control_plane.domain.views)")
    return found


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _screens(directory: Path, mode: Any) -> tuple[list[str], list[str]]:
    errors, warnings = screens.check_screens(model.resolve_targets([str(directory)]), core=mode)
    return errors, [w for w in warnings if w != screens.CORE_SCREENS_MISSING]


def _codes(found: list[str]) -> list[tuple[str, str, str]]:
    """(файл в пакете, код, путь) каждой находки."""
    out = []
    for line in found:
        file, code, rest = line.split(": ", 2)
        path = rest.rsplit(" [", 1)[1].rstrip("]") if rest.endswith("]") else ""
        out.append((file.split("invoice-payment/", 1)[-1], code, path))
    return out


# --- схема ------------------------------------------------------------------------------


def _core_file(*parts: str) -> Path | None:
    """Файл исходников ядра рядом (extra sandbox): control_plane/… или его корень."""
    try:
        import control_plane
    except ImportError:
        return None
    path = Path(control_plane.__file__).resolve().parent.joinpath(*parts)
    return path if path.exists() else None


def test_the_view_schema_is_a_byte_copy_of_the_cores() -> None:
    source = _core_file("domain", "view.schema.json")
    if source is None:
        pytest.skip("the core next to the SDK has no view.schema.json (older than CP-ADR-0080)")
    assert (schema.schema_dir() / "view.schema.json").read_bytes() == source.read_bytes()


@pytest.mark.parametrize("name", ["object.schema.json", "test.schema.json"])
def test_the_format_schemas_are_byte_copies_of_the_cores_pinned_ones(name: str) -> None:
    """Ядро держит копии схемы формата (tests/fixtures/superproject) и сверяет их с SDK побайтно;
    вид View и языки пакета ядро описывает своей схемой экранов, а не object.schema.json."""
    pinned = _core_file("..", "..", "tests", "fixtures", "superproject", name)
    if pinned is None:
        pytest.skip("the core next to the SDK is not a source checkout")
    assert (schema.schema_dir() / name).read_bytes() == pinned.read_bytes()


@pytest.mark.parametrize("name", [LIST, CARD, COMPONENT])
def test_the_views_and_the_component_follow_the_view_schema(name: str) -> None:
    document = model._read_yaml(FIXTURE / name)
    validator = schema.screen_validator(document["kind"])
    assert [e.message for e in validator.iter_errors(document["spec"])] == []


def test_the_format_schema_accepts_no_view_kind() -> None:
    """object.schema.json — копия ядра без View: вид проверяет схема экранов, а не она."""
    document = model._read_yaml(FIXTURE / LIST)
    assert schema.errors(schema.OBJECT, document)


# --- пакет проходит check ----------------------------------------------------------------


def test_the_package_loads_views_and_components_but_not_dictionaries() -> None:
    package = model.resolve_targets([str(FIXTURE)]).packages[-1]
    kinds = sorted((o.kind, o.key) for o in package.objects)
    assert ("View", "invoice-payment-list") in kinds and ("View", "invoice-payment-card") in kinds
    assert ("Component", "invoice-summary") in kinds
    assert all(model.I18N_DIR not in o.path.parts for o in package.objects)


def test_the_example_passes_check(package: Path, mode: Any) -> None:
    assert _screens(package, mode) == ([], [])


def test_the_example_passes_the_whole_check(package: Path) -> None:
    errors, warnings = check.check(model.resolve_targets([str(package)]))
    assert errors == []
    if screens.core_views() is None:
        # без кода ядра экранов пути и выражения проверит ядро в plan — об этом сказано
        assert screens.CORE_SCREENS_MISSING in warnings


def test_the_example_passes_check_from_the_cli(package: Path, capsys: Any) -> None:
    assert cli.main(["check", "--package", str(package)]) == 0
    assert "ok: packages 1, objects 5" in capsys.readouterr().out


def test_check_is_repeatable(package: Path, mode: Any) -> None:
    _edit(package / RU, "invoice-payment.list.title: Счета\n", "")
    assert _screens(package, mode) == _screens(package, mode)


# --- находки plan/check: по одной на пункт ------------------------------------------------


def test_a_key_missing_in_a_declared_language_is_an_error(package: Path, mode: Any) -> None:
    _edit(package / RU, "invoice-payment.list.title: Счета\n", "")
    errors, _ = _screens(package, mode)
    assert _codes(errors) == [(LIST, "missing_message", "/spec/title")]
    assert "dictionary of ru" in errors[0]


def test_a_default_label_missing_in_a_dictionary_is_an_error(package: Path, mode: Any) -> None:
    """Колонка без label — ключ <пакет>.fields.<путь> (TAI-ADR-0066 п.1а)."""
    for name in (EN, RU):
        text = (package / name).read_text(encoding="utf-8")
        kept = [
            x for x in text.splitlines() if not x.startswith("invoice-payment.fields.supplier:")
        ]
        (package / name).write_text("\n".join(kept) + "\n", encoding="utf-8")
    errors, _ = _screens(package, mode)
    assert _codes(errors) == [(LIST, "missing_message", "/spec/layout/1/columns/1/label")]


def test_an_extra_key_is_a_warning(package: Path, mode: Any) -> None:
    with (package / EN).open("a", encoding="utf-8") as dictionary:
        dictionary.write("invoice-payment.list.unused: Unused\n")
    errors, warnings = _screens(package, mode)
    assert errors == []
    assert _codes(warnings) == [(EN, "unused_message", "/invoice-payment.list.unused")]


def test_a_dictionary_of_an_undeclared_language_is_an_error(package: Path, mode: Any) -> None:
    shutil.copy(package / EN, package / "i18n" / "de.yaml")
    errors, _ = _screens(package, mode)
    assert _codes(errors) == [("i18n/de.yaml", "undeclared_locale", "")]


def test_an_unknown_block_is_an_error(package: Path, mode: Any) -> None:
    _edit(package / CARD, "- block: timeline", "- block: gallery")
    errors, _ = _screens(package, mode)
    assert _codes(errors) == [(CARD, "unknown_block", "/spec/layout/4/block")]


def test_an_unknown_format_is_an_error(package: Path, mode: Any) -> None:
    _edit(
        package / LIST, "{field: data.amount, format: money}", "{field: data.amount, format: cash}"
    )
    errors, _ = _screens(package, mode)
    assert _codes(errors) == [(LIST, "unknown_format", "/spec/layout/1/columns/2/format")]


def test_an_aggregate_outside_metrics_and_chart_is_an_error(package: Path, mode: Any) -> None:
    _edit(
        package / LIST,
        "{field: data.supplier}",
        '{label: invoice-payment.list.total, value: "sum(data.amount)"}',
    )
    errors, _ = _screens(package, mode)
    assert _codes(errors) == [(LIST, "aggregate_outside_metrics", "/spec/layout/1/columns/1/value")]


def test_an_aggregate_name_in_a_string_literal_is_no_call(package: Path, mode: Any) -> None:
    _edit(
        package / LIST, "filter: \"status == 'running'\"", "filter: \"data.supplier != 'max(x)'\""
    )
    assert _screens(package, mode)[0] == []


def test_open_of_a_missing_view_is_an_error(package: Path, mode: Any) -> None:
    _edit(package / LIST, "open: {view: invoice-payment-card", "open: {view: invoice-payment-sheet")
    errors, _ = _screens(package, mode)
    assert _codes(errors) == [(LIST, "unknown_view", "/spec/layout/1/open/view")]


def test_a_source_process_missing_in_the_package_and_its_requires_is_an_error(
    package: Path, mode: Any
) -> None:
    _edit(package / CARD, "{process: invoice-payment,", "{process: invoice-approval,")
    errors, _ = _screens(package, mode)
    assert (CARD, "unknown_source", "/spec/source/process") in _codes(errors)


def test_a_source_process_of_a_required_package_is_found(
    package: Path, mode: Any, tmp_path: Path
) -> None:
    """Процесс источника — в пакете из requires: ссылка замкнута, как у прочих ссылок."""
    base = tmp_path / "packages" / "invoice-base"
    (base / "processes").mkdir(parents=True)
    (base / "package.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Package\nkey: invoice-base\n"
        "spec: {version: 1.0.0, displayName: Base}\n",
        encoding="utf-8",
    )
    shutil.move(package / "processes" / "invoice-payment.yaml", base / "processes")
    shutil.copy(package / "roles" / "accounting.yaml", base / "roles.yaml")
    _edit(
        package / "package.yaml",
        "defaultLocale: en",
        "defaultLocale: en\n  requires: [invoice-base]",
    )
    installation = model.resolve_targets([str(base), str(package)])
    errors, _ = screens.check_screens(installation, core=mode)
    assert errors == []
    # без requires процесс другого пакета той же установки не виден
    _edit(package / "package.yaml", "\n  requires: [invoice-base]", "")
    installation = model.resolve_targets([str(base), str(package)])
    found = _codes(screens.check_screens(installation, core=mode)[0])
    assert (LIST, "unknown_source", "/spec/source/process") in found


# --- языки, словари, компоненты (статика SDK) --------------------------------------------


def test_screens_without_locales_are_an_error(package: Path, mode: Any) -> None:
    _edit(package / "package.yaml", "  locales: [en, ru]\n  defaultLocale: en\n", "")
    errors, _ = _screens(package, mode)
    assert ("package.yaml", "locales_required", "/spec") in _codes(errors)


@pytest.mark.parametrize(
    "locales,code,path",
    [
        ("locales: []\n  defaultLocale: en", "invalid_locales", "/spec/locales"),
        ("locales: en\n  defaultLocale: en", "invalid_locales", "/spec/locales"),
        ("locales: [en, en, ru]\n  defaultLocale: en", "invalid_locales", "/spec/locales/1"),
        ("locales: [EN_us, en, ru]\n  defaultLocale: en", "invalid_locales", "/spec/locales/0"),
        ("locales: [en, ru]\n  defaultLocale: de", "invalid_default_locale", "/spec/defaultLocale"),
        (
            "locales: [en, ru]\n  defaultLocale: null",
            "invalid_default_locale",
            "/spec/defaultLocale",
        ),
        ("locales: [en, ru, fr]\n  defaultLocale: en", "missing_dictionary", "/spec/locales/2"),
    ],
)
def test_locales_of_the_manifest(
    package: Path, mode: Any, locales: str, code: str, path: str
) -> None:
    _edit(package / "package.yaml", "locales: [en, ru]\n  defaultLocale: en", locales)
    errors, _ = _screens(package, mode)
    assert ("package.yaml", code, path) in _codes(errors)


def test_the_format_schema_check_does_not_refuse_locales(package: Path) -> None:
    """locales и defaultLocale — поля манифеста, которых нет в object.schema.json ядра."""
    errors, _ = check.check(model.resolve_targets([str(package)]))
    assert not [e for e in errors if "package.yaml" in e]


@pytest.mark.parametrize(
    "text,problem",
    [
        ("{count, plural, one {# invoice} other {# invoices}", "not closed"),
        ("Amount}", "closes no argument"),
    ],
)
def test_unpaired_braces_of_a_message_are_an_error(
    package: Path, mode: Any, text: str, problem: str
) -> None:
    _edit(
        package / EN,
        "invoice-payment.list.title: Invoices",
        f'invoice-payment.list.title: "{text}"',
    )
    errors, _ = _screens(package, mode)
    found = _codes(errors)
    assert (EN, "invalid_message", "/invoice-payment.list.title") in found
    assert any(problem in e for e in errors)


def test_quoted_braces_and_icu_plurals_are_texts(package: Path, mode: Any) -> None:
    _edit(
        package / EN,
        "invoice-payment.list.title: Invoices",
        "invoice-payment.list.title: \"Invoices '{'draft'}' {n, plural, one {#} other {#}}\"",
    )
    assert _screens(package, mode)[0] == []


@pytest.mark.parametrize(
    "line,where",
    [
        ("invoice-payment.list.extra: 42", "/invoice-payment.list.extra"),
        ("invoice-payment.list.extra: null", "/invoice-payment.list.extra"),
        ("invoice-payment.list.extra: [a, b]", "/invoice-payment.list.extra"),
        ("'Invoice payment': Text", "/Invoice payment"),
        ("'': Text", "/"),
        ("12: Text", "/12"),
    ],
)
def test_a_key_or_a_text_of_a_wrong_type_is_an_error(
    package: Path, mode: Any, line: str, where: str
) -> None:
    with (package / EN).open("a", encoding="utf-8") as dictionary:
        dictionary.write(line + "\n")
    errors, _ = _screens(package, mode)
    assert (EN, "invalid_message", where) in _codes(errors)


@pytest.mark.parametrize(
    "name,content",
    [
        ("i18n/en/extra.yaml", "a: b\n"),
        ("i18n/English.yaml", "a: b\n"),
        ("i18n/fr.yaml", "[a, b]\n"),
    ],
)
def test_a_file_that_is_no_dictionary_is_an_error(
    package: Path, mode: Any, name: str, content: str
) -> None:
    _edit(package / "package.yaml", "locales: [en, ru]", "locales: [en, ru, fr]")
    path = package / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    errors, _ = _screens(package, mode)
    assert (name, "invalid_dictionary", "") in _codes(errors)


def test_an_empty_dictionary_misses_every_key(package: Path, mode: Any) -> None:
    (package / RU).write_text("", encoding="utf-8")
    errors, _ = _screens(package, mode)
    assert {code for _, code, _ in _codes(errors)} == {"missing_message"}
    assert all("dictionary of ru" in e for e in errors)


def test_code_of_a_component_is_refused(package: Path, mode: Any) -> None:
    _edit(package / COMPONENT, "spec:\n", "spec:\n  code: {bundle: ./summary.js}\n")
    errors, _ = _screens(package, mode)
    assert (COMPONENT, "component_code_not_supported", "/spec/code") in _codes(errors)


def test_a_component_of_components_is_refused(package: Path, mode: Any) -> None:
    _edit(
        package / COMPONENT,
        "  layout:\n",
        "  layout:\n    - {block: component, component: invoice-summary}\n",
    )
    errors, _ = _screens(package, mode)
    assert (COMPONENT, "nested_component", "/spec/layout/0") in _codes(errors)


def test_a_missing_component_is_an_error(package: Path, mode: Any) -> None:
    _edit(package / CARD, "component: invoice-summary", "component: invoice-totals")
    errors, _ = _screens(package, mode)
    assert (CARD, "unknown_component", "/spec/layout/1/component") in _codes(errors)


def test_a_param_schema_of_a_package_file_is_read(package: Path, mode: Any) -> None:
    (package / "schemas").mkdir()
    (package / "schemas" / "invoice.yaml").write_text(
        "type: object\nproperties:\n  invoice:\n    type: object\n    properties:\n"
        "      supplier: {type: string}\n      amount: {type: number}\n"
        "      currency: {type: string}\n",
        encoding="utf-8",
    )
    text = (package / COMPONENT).read_text(encoding="utf-8")
    head, _, tail = text.partition("      schema:\n")
    tail = tail.split("  layout:\n", 1)[1]
    (package / COMPONENT).write_text(
        head
        + "      schema: {$ref: ../schemas/invoice.yaml#/properties/invoice}\n  layout:\n"
        + tail,
        encoding="utf-8",
    )
    assert _screens(package, mode)[0] == []
    _edit(package / COMPONENT, "#/properties/invoice", "#/properties/payment")
    errors, _ = _screens(package, mode)
    assert (COMPONENT, "unresolved_schema_ref", "/spec/params/invoice/schema/$ref") in _codes(
        errors
    )


def test_an_audience_role_outside_the_package_is_a_warning(package: Path, mode: Any) -> None:
    _edit(package / LIST, "roles: [accounting]", "roles: [accounting, treasury]")
    errors, warnings = _screens(package, mode)
    assert errors == []
    assert _codes(warnings) == [(LIST, "unknown_role", "/spec/audience/roles/1")]


def test_a_nav_group_outside_the_console_menu_is_an_error(package: Path, mode: Any) -> None:
    _edit(package / LIST, "group: work", "group: finance")
    errors, _ = _screens(package, mode)
    assert _codes(errors) == [(LIST, "invalid_view", "/spec/nav/group")]


def test_a_view_key_is_a_slug(package: Path) -> None:
    _edit(package / CARD, "key: invoice-payment-card", "key: Invoice_Card")
    _edit(package / LIST, "view: invoice-payment-card", "view: Invoice_Card")
    errors, _ = check.check(model.resolve_targets([str(package)]))
    assert any("invalid_view: the key of a View" in e for e in errors)


def test_static_check_leaves_paths_and_expressions_to_the_core(package: Path) -> None:
    """Путь data.*, которого нет в схеме данных процесса, — находка ядра (undeclared_path):
    без кода ядра экранов SDK её не ищет, а предупреждает, что её найдёт план ядра."""
    _edit(
        package / CARD,
        "{field: data.review}",
        "{label: invoice-payment.fields.review, field: data.reviewer}",
    )
    installation = model.resolve_targets([str(package)])
    errors, warnings = screens.check_screens(installation, core=None)
    assert errors == [] and screens.CORE_SCREENS_MISSING in warnings
    found = screens.core_views()
    if found is None:
        return
    errors, _ = screens.check_screens(installation, core=found)
    assert (CARD, "undeclared_path", "/spec/layout/2/items/0/field") in _codes(errors)


# --- вывод ------------------------------------------------------------------------------


def test_check_json_gives_the_code_file_and_path(package: Path, capsys: Any) -> None:
    _edit(package / LIST, "- block: table", "- block: grid")
    assert cli.main(["check", "--package", str(package), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    [found] = [e for e in report["errors"] if e["code"] == "unknown_block"]
    assert found["file"].endswith(LIST) and found["path"] == "/spec/layout/1/block"
    assert found["message"].startswith("block 'grid' is not in the set of version 1")


def test_static_error_takes_the_path_off_the_message() -> None:
    found = core.static_error(
        "views/a.yaml: unknown_view: there is no view 'b' [/spec/layout/0/open/view]"
    )
    assert found == {
        "code": "unknown_view",
        "severity": "error",
        "message": "there is no view 'b'",
        "path": "/spec/layout/0/open/view",
        "file": "views/a.yaml",
    }


# --- plan -------------------------------------------------------------------------------


def test_the_example_passes_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """План установки с пакетом экранов: пакет целиком — файлы видов, компонента и словарей —
    уходит в план ядра (секция core), которое планирует его виды; установщик их не ставит."""
    from tests.test_install import SERVER, FakeCore, _target

    monkeypatch.setattr(model, "ROOT", tmp_path)
    monkeypatch.setattr(model, "PACKAGES_DIR", tmp_path / "packages")
    monkeypatch.setenv("PACKAGE_SDK_CACHE", str(tmp_path / "cache"))
    shutil.copytree(FIXTURE, tmp_path / "packages" / "invoice-payment")
    (tmp_path / "packages.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Installation\nkey: screens\n"
        "spec: {packages: [invoice-payment]}\n",
        encoding="utf-8",
    )
    fake = FakeCore()
    lines: list[str] = []
    document = install.plan(
        tmp_path / "packages.yaml",
        target=_target(fake),
        env={},
        out=tmp_path / "plan.json",
        log=lines.append,
    )
    assert document["server"] == SERVER and (tmp_path / "plan.json").is_file()
    [section] = [s for s in document["sections"] if s["kind"] == "core"]
    assert section["package"] == "invoice-payment"
    [request] = fake.plan_calls
    sent = {f["path"] for f in request["package"]["files"]}
    assert {LIST, CARD, COMPONENT, EN, RU, "package.yaml"} <= sent
    planned = {(c["kind"], c["key"]) for c in section["plan"]["changes"]}
    assert {("View", "invoice-payment-list"), ("View", "invoice-payment-card")} <= planned
    catalog = next(s for s in document["sections"] if s["kind"] == "catalog")["changes"]
    assert not [c for c in catalog if c.get("kind") in model.SCREEN_KINDS]
    assert any("+ View/invoice-payment-list" in line for line in lines)


def test_plan_refuses_a_package_whose_screens_fail_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_install import FakeCore, _target

    monkeypatch.setattr(model, "ROOT", tmp_path)
    monkeypatch.setenv("PACKAGE_SDK_CACHE", str(tmp_path / "cache"))
    root = tmp_path / "packages" / "invoice-payment"
    shutil.copytree(FIXTURE, root)
    _edit(root / LIST, "open: {view: invoice-payment-card", "open: {view: invoice-payment-sheet")
    (tmp_path / "packages.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Installation\nkey: screens\n"
        "spec: {packages: [invoice-payment]}\n",
        encoding="utf-8",
    )
    fake = FakeCore()
    with pytest.raises(model.PackageError, match="unknown_view"):
        install.plan(tmp_path / "packages.yaml", target=_target(fake), env={}, log=lambda _m: None)
    assert fake.plan_calls == []


def test_a_package_with_screens_goes_to_the_core_plan() -> None:
    from package_sdk.install.plan import core_packages

    installation = model.resolve_targets([str(FIXTURE)])
    assert [p.key for p in core_packages(installation)] == ["invoice-payment"]
    assert "View" in core.CORE_PLANNED_KINDS
