import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

PACKAGES = [
    "geopandas",
    "shapely",
    "networkx",
    "pandas",
    "fastapi",
    "uvicorn",
    "pydantic",
    "httpx",
    "dotenv",
    "supabase",
    "astral",
    "pytest",
]

ENV_KEYS = [
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "NWS_USER_AGENT",
    "XAI_API_KEY",
    "GRAPH_PATH",
]


@pytest.mark.parametrize("package", PACKAGES)
def test_package_imports(package):
    importlib.import_module(package)


def test_claude_md_exists():
    assert (REPO_ROOT / "CLAUDE.md").is_file()


def test_gitignore_covers_env_and_cornell_data():
    content = (REPO_ROOT / ".gitignore").read_text()
    assert ".env" in content
    assert "cornell_data/" in content


def _parse_env_example():
    content = (REPO_ROOT / ".env.example").read_text()
    entries = {}
    for line in content.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        key, _, rest = line.partition("=")
        value = rest.split("#", 1)[0].strip()
        entries[key.strip()] = value
    return entries


def test_env_example_has_every_key():
    entries = _parse_env_example()
    for key in ENV_KEYS:
        assert key in entries


def test_env_example_values_not_too_long():
    entries = _parse_env_example()
    for key, value in entries.items():
        assert len(value) <= 20, f"{key} value looks too long (possible real secret)"
