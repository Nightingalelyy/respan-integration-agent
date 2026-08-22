import pytest
from pydantic import ValidationError

from respan_integration_agent.config import OnboardingRequest


def test_unknown_config_field_is_rejected():
    with pytest.raises(ValidationError):
        OnboardingRequest.model_validate(
            {
                "repo_url": "/tmp/fixture",
                "product": "tracing",
                "trcaing": {"mode": "auto"},
            }
        )


def test_smoke_profile_requires_tracing_auto():
    with pytest.raises(ValidationError, match="requires product=tracing and mode=auto"):
        OnboardingRequest.model_validate(
            {
                "repo_url": "/tmp/fixture",
                "product": "gateway",
                "gateway": {"funding": "credits"},
                "verification": {
                    "profile": "python-openai-auto-smoke",
                    "respan_ai_version": "4.1.0",
                    "openai_otel_version": "0.62.3",
                },
            }
        )
