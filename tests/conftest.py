from pathlib import Path

import pytest

import boundary_check

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def src_dir() -> Path:
    return SRC


@pytest.fixture(scope="session")
def policy() -> dict:
    """The effective policy (public policy plus the optional local-only overlay)."""
    return boundary_check.load_policy()


@pytest.fixture(scope="session")
def public_policy() -> dict:
    """Exactly what a fresh clone sees."""
    return boundary_check.load_policy(local_overlay=False)


SYNTHETIC_BRAND = "acmecorp"
SYNTHETIC_ZONE = "adapters/acme_integration/"


@pytest.fixture
def test_policy(public_policy) -> dict:
    """Public policy extended with synthetic private markers for negative controls."""
    import copy

    policy = copy.deepcopy(public_policy)
    policy["strict_rules"].append({"id": "private-brand", "regex": "(?i)" + SYNTHETIC_BRAND})
    policy["relaxed_zones"] = [SYNTHETIC_ZONE]
    policy["import_policy"]["forbidden_roots"].append("acme_integration")
    policy["import_policy"]["forbidden_prefixes"].append("acme")
    return policy
