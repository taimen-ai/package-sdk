import pytest

from package_sdk import __version__, cli


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"package-sdk {__version__}"


def test_help_without_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    assert "usage: package-sdk" in capsys.readouterr().out


@pytest.mark.parametrize(
    "command", ["check", "test", "lock", "plan", "apply", "export", "migrate-expr"]
)
def test_catalog_commands_are_routed(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "--help"])
    assert exit_info.value.code == 0
    assert f"package-sdk {command}" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["edit", "sandbox"])
def test_edit_and_sandbox_are_routed(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([command, "--help"])
    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_check_without_core_code_is_an_error_unless_schema_only(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from package_sdk import check

    monkeypatch.chdir(tmp_path)
    (tmp_path / "packages" / "demo").mkdir(parents=True)
    (tmp_path / "packages" / "demo" / "package.yaml").write_text(
        "apiVersion: taimen.ai/v1\nkind: Package\nkey: demo\n"
        "spec: {version: 0.1.0, displayName: Demo}\n",
        encoding="utf-8",
    )
    from package_sdk import model

    monkeypatch.setattr(model, "PACKAGES_DIR", tmp_path / "packages")
    monkeypatch.setattr(model, "ROOT", tmp_path)
    monkeypatch.setattr(check, "_domain", lambda: None)
    assert cli.main(["check", "--package", "demo"]) == 1
    assert "package-sdk[sandbox]" in capsys.readouterr().out
    assert cli.main(["check", "--package", "demo", "--schema-only"]) == 0
