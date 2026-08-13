from pathlib import Path

from lectern import __version__
from lectern.automation import STATE_SCHEMA_VERSION
from lectern.bundle import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def read_doc(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def normalized(text: str) -> str:
    return " ".join(text.split())


def test_public_docs_track_package_and_manifest_versions() -> None:
    readme = normalized(read_doc("README.md"))
    changelog = normalized(read_doc("CHANGELOG.md"))
    support = normalized(read_doc("SUPPORT.md"))

    assert f"current manifest schema version is `{SCHEMA_VERSION}`" in readme
    assert f"The current package version is `{__version__}`." in changelog
    assert f"The current manifest schema version is `{SCHEMA_VERSION}`." in changelog
    assert f"The current package version is `{__version__}`." in support
    assert f"The current manifest schema version is `{SCHEMA_VERSION}`." in support


def test_support_tracks_automation_state_schema_version() -> None:
    support = normalized(read_doc("SUPPORT.md"))

    # SUPPORT.md documents the state schema version as a third versioned surface;
    # this assertion is what keeps the documented number from drifting away from
    # the code.
    assert f"The current automation state schema version is `{STATE_SCHEMA_VERSION}`." in support


def test_manifest_migration_is_documented_and_linked() -> None:
    readme = normalized(read_doc("README.md"))
    support = normalized(read_doc("SUPPORT.md"))
    assert (ROOT / "docs/MIGRATIONS.md").is_file()
    migrations = normalized(read_doc("docs/MIGRATIONS.md"))
    assert "docs/MIGRATIONS.md" in readme
    assert "docs/MIGRATIONS.md" in support
    assert "lectern migrate BUNDLE" in migrations
    assert ".v0.1.0.bak" in migrations
    assert "0.1.0" in migrations and "1.0.0" in migrations
    assert "does not delete the backup" in migrations


def test_readme_states_multilingual_search_scope_and_limits() -> None:
    readme = normalized(read_doc("README.md"))

    assert "Latin, Greek, Cyrillic, Arabic, and Hebrew" in readme
    assert "Chinese, Japanese, and Korean" in readme
    assert "bounded precision" in readme
    assert "two-character CJK" in readme
    assert "word-boundary-accurate CJK" in readme
    assert "stemming is not provided" in readme
    assert "operator-mode queries do not support CJK" in readme
    assert "reconciles registered transcript files on each command open" in readme
    assert "literal by default" in readme
