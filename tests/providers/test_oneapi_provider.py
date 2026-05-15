"""Tests for the One API provider registration."""

import sys
from types import SimpleNamespace

from nanobot.config.schema import Config, ProvidersConfig
from nanobot.providers.factory import make_provider
from nanobot.providers.registry import PROVIDERS


def test_oneapi_config_field_exists() -> None:
    """ProvidersConfig should expose a oneapi field."""
    config = ProvidersConfig()
    assert hasattr(config, "oneapi")


def test_oneapi_provider_in_registry() -> None:
    """One API should be registered as an OpenAI-compatible gateway."""
    specs = {s.name: s for s in PROVIDERS}
    assert "oneapi" in specs

    oneapi = specs["oneapi"]
    assert oneapi.env_key == "OPENAI_API_KEY"
    assert oneapi.display_name == "One API"
    assert oneapi.backend == "openai_compat"
    assert oneapi.is_gateway is True
    assert oneapi.default_api_base == "http://localhost:3000/v1"


def test_config_explicit_oneapi_provider_uses_default_api_base() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "oneapi",
                    "model": "openai/gpt-4o-mini",
                }
            },
            "providers": {
                "oneapi": {
                    "apiKey": "oneapi-token",
                }
            },
        }
    )

    assert config.get_provider_name() == "oneapi"
    assert config.get_api_key() == "oneapi-token"
    assert config.get_api_base() == "http://localhost:3000/v1"


def test_config_auto_detects_oneapi_from_model_prefix() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "auto",
                    "model": "oneapi/openai/gpt-4o-mini",
                }
            },
            "providers": {
                "oneapi": {
                    "apiKey": "oneapi-token",
                    "apiBase": "https://oneapi.example.com/v1",
                }
            },
        }
    )

    assert config.get_provider_name() == "oneapi"
    assert config.get_api_base() == "https://oneapi.example.com/v1"


def test_config_falls_back_to_oneapi_gateway_for_arbitrary_models() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "auto",
                    "model": "anthropic/claude-sonnet-4-5",
                }
            },
            "providers": {
                "oneapi": {
                    "apiKey": "oneapi-token",
                    "apiBase": "https://oneapi.example.com/v1",
                }
            },
        }
    )

    assert config.get_provider_name() == "oneapi"


def test_make_provider_passes_oneapi_settings_to_openai_compat_client(monkeypatch) -> None:
    seen = {}

    class FakeOpenAICompatProvider:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.generation = None

        def get_default_model(self):
            return seen["default_model"]

    fake_module = SimpleNamespace(OpenAICompatProvider=FakeOpenAICompatProvider)

    config = Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "provider": "oneapi",
                    "model": "openai/gpt-4o-mini",
                }
            },
            "providers": {
                "oneapi": {
                    "apiKey": "oneapi-token",
                    "apiBase": "https://oneapi.example.com/v1",
                }
            },
        }
    )

    monkeypatch.setitem(sys.modules, "nanobot.providers.openai_compat_provider", fake_module)
    provider = make_provider(config)

    assert seen["api_key"] == "oneapi-token"
    assert seen["api_base"] == "https://oneapi.example.com/v1"
    assert seen["spec"].name == "oneapi"
    assert provider.get_default_model() == "openai/gpt-4o-mini"
