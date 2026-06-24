from pathlib import Path

from lectern import __version__
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
