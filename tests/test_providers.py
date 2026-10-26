"""Tests of the provider configuration and its terms hashes."""
from datetime import date

import pytest

from collectors.sdmx import PROVIDERS
from conftest import PROVIDERS_FIXTURE
from providers import ProvidersError, load_providers, set_terms_hash


def test_licenses_of_the_records_match_the_configuration() -> None:
    providers = {provider.id: provider for provider in load_providers()}
    assert set(providers) == set(PROVIDERS)
    for provider_id, provider in PROVIDERS.items():
        assert provider.license == providers[provider_id].license


def test_set_terms_hash_changes_only_the_provider(tmp_path) -> None:
    text = PROVIDERS_FIXTURE.read_text(encoding="utf-8")
    changed = set_terms_hash(text, "ecb", "abc123", date(2026, 11, 1))
    path = tmp_path / "providers.toml"
    path.write_text(changed, encoding="utf-8")
    providers = {provider.id: provider for provider in load_providers(path)}
    assert providers["ecb"].terms_hash == "abc123" and providers["ecb"].terms_hash_since == date(2026, 11, 1)
    assert providers["eurostat"].terms_hash is None

    again = set_terms_hash(changed, "ecb", "def456", date(2026, 12, 1))
    assert again.count("terms_hash =") == 1 and 'terms_hash = "def456"' in again
    assert changed.split("[[provider]]")[0] == text.split("[[provider]]")[0]


def test_unknown_provider_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="no entry"):
        set_terms_hash(PROVIDERS_FIXTURE.read_text(encoding="utf-8"), "destatis", "x", date(2026, 11, 1))
    path = tmp_path / "providers.toml"
    path.write_text(PROVIDERS_FIXTURE.read_text(encoding="utf-8").replace('id = "ecb"', 'id = "destatis"'),
                    encoding="utf-8")
    with pytest.raises(ProvidersError, match="unknown provider id"):
        load_providers(path)
