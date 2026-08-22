from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from respan_integration_agent.config import OnboardingRequest
from respan_integration_agent.verify import (
    IntegrationVerificationError,
    _SMOKE_BASELINE_SOURCE,
    verify_integration,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "smoke" / "v0a-python"


def _request() -> OnboardingRequest:
    return OnboardingRequest.model_validate(
        {
            "repo_url": "/tmp/fixture",
            "product": "tracing",
            "tracing": {
                "mode": "auto",
                "environment": "smoke",
                "service_name": "respan-v0a-python-smoke",
            },
            "verification": {
                "profile": "python-openai-auto-smoke",
                "respan_ai_version": "4.1.0",
                "openai_otel_version": "0.62.3",
            },
        }
    )


def _generic_request() -> OnboardingRequest:
    return OnboardingRequest.model_validate(
        {
            "repo_url": "/tmp/fixture",
            "product": "tracing",
            "tracing": {"mode": "auto"},
        }
    )


def _golden_tree(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    shutil.copytree(FIXTURE, target)
    requirements = target / "requirements.txt"
    requirements.write_text(
        requirements.read_text()
        + "respan-ai==4.1.0\n"
        + "opentelemetry-instrumentation-openai==0.62.3\n"
    )
    app = target / "app.py"
    source = app.read_text()
    source = source.replace(
        "from openai import OpenAI\n",
        "from openai import OpenAI\n"
        "from respan import Respan\n\n\n"
        "respan = Respan(\n"
        '    app_name="respan-v0a-python-smoke",\n'
        '    environment="smoke",\n'
        '    metadata={"run_id": os.environ["RESPAN_EXAMPLE_RUN_ID"]},\n'
        ")\n",
    )
    source = source.replace(
        'if __name__ == "__main__":\n    main()\n',
        'if __name__ == "__main__":\n'
        "    try:\n"
        "        main()\n"
        "    finally:\n"
        "        respan.flush()\n",
    )
    app.write_text(source)
    return target


def _verify(target: Path, diff: str = "+respan-ai==4.1.0\n") -> None:
    verify_integration(
        target,
        _request(),
        ["app.py", "requirements.txt"],
        diff,
        respan_api_key="not-a-real-key",
    )


def test_golden_auto_smoke_patch_passes(tmp_path):
    _verify(_golden_tree(tmp_path))


def test_checked_in_fixture_matches_embedded_baseline():
    import ast

    assert ast.dump(ast.parse((FIXTURE / "app.py").read_text())) == ast.dump(
        ast.parse(_SMOKE_BASELINE_SOURCE)
    )


def test_wrong_distribution_is_rejected(tmp_path):
    target = _golden_tree(tmp_path)
    (target / "requirements.txt").write_text("openai==1.99.9\nrespan\n")
    with pytest.raises(IntegrationVerificationError, match="distribution"):
        _verify(target, "+respan\n")


def test_versioned_bare_respan_distribution_is_rejected(tmp_path):
    with pytest.raises(IntegrationVerificationError, match="distribution"):
        verify_integration(
            tmp_path,
            _generic_request(),
            ["requirements.txt"],
            "+respan==1.2.3\n",
            respan_api_key="",
        )


def test_respan_ai_distribution_is_not_mistaken_for_bare_respan(tmp_path):
    verify_integration(
        tmp_path,
        _generic_request(),
        ["requirements.txt"],
        "+respan-ai==4.1.0\n",
        respan_api_key="",
    )


def test_invalid_tags_argument_is_rejected(tmp_path):
    target = _golden_tree(tmp_path)
    app = target / "app.py"
    app.write_text(app.read_text().replace('environment="smoke",', "tags={},"))
    with pytest.raises(IntegrationVerificationError):
        _verify(target, "+    tags={},\n")


def test_spaced_tags_argument_is_rejected_in_generic_auto_mode(tmp_path):
    app = tmp_path / "app.py"
    app.write_text("client = Respan(tags = {})\n")
    with pytest.raises(IntegrationVerificationError, match="tags="):
        verify_integration(
            tmp_path,
            _generic_request(),
            ["app.py"],
            "+client = Respan(tags = {})\n",
            respan_api_key="",
        )


def test_missing_flush_is_rejected(tmp_path):
    target = _golden_tree(tmp_path)
    app = target / "app.py"
    app.write_text(
        app.read_text().replace("        respan.flush()\n", "        pass\n")
    )
    with pytest.raises(IntegrationVerificationError, match="flushed"):
        _verify(target)


def test_secret_value_is_rejected(tmp_path):
    target = _golden_tree(tmp_path)
    with pytest.raises(IntegrationVerificationError, match="RESPAN_API_KEY value"):
        verify_integration(
            target,
            _request(),
            ["app.py", "requirements.txt"],
            "+secret-value\n",
            respan_api_key="secret-value",
        )


@pytest.mark.parametrize(
    "path",
    [
        ".env.local",
        "config/.env.production",
        "certificates/client.pem",
        "secrets/deploy.KEY",
    ],
)
def test_common_credential_paths_are_rejected(tmp_path, path):
    with pytest.raises(IntegrationVerificationError, match="forbidden path"):
        verify_integration(
            tmp_path,
            _generic_request(),
            [path],
            "+credential material\n",
            respan_api_key="",
        )


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        "github_pat_11AA00_exampletokenvalue1234567890",
        "AKIAABCDEFGHIJKLMNOP",
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dGVzdHNpZ25hdHVyZQ"
        ),
    ],
)
def test_common_secret_shapes_are_rejected(tmp_path, secret):
    with pytest.raises(IntegrationVerificationError, match="secret-like"):
        verify_integration(
            tmp_path,
            _generic_request(),
            ["requirements.txt"],
            f"+{secret}\n",
            respan_api_key="",
        )


def test_extra_top_level_code_is_rejected(tmp_path):
    target = _golden_tree(tmp_path)
    app = target / "app.py"
    app.write_text(
        app.read_text().replace(
            "from openai import OpenAI\n",
            "from openai import OpenAI\nimport urllib.request\n",
        )
    )
    with pytest.raises(IntegrationVerificationError, match="outside the reviewed"):
        _verify(target)


def test_attribute_named_respan_factory_is_rejected(tmp_path):
    target = _golden_tree(tmp_path)
    app = target / "app.py"
    app.write_text(
        app.read_text().replace("respan = Respan(", "respan = factory.Respan(")
    )
    with pytest.raises(IntegrationVerificationError, match="directly"):
        _verify(target)


@pytest.mark.parametrize("handle_name", ["__name__", "client"])
def test_smoke_respan_handle_must_use_exact_name(tmp_path, handle_name):
    target = _golden_tree(tmp_path)
    app = target / "app.py"
    app.write_text(
        app.read_text()
        .replace("respan = Respan(", f"{handle_name} = Respan(")
        .replace("respan.flush()", f"{handle_name}.flush()")
    )
    with pytest.raises(IntegrationVerificationError, match="exact variable name"):
        _verify(target)


@pytest.mark.parametrize(
    "before,after",
    [
        (
            "    try:\n        main()\n",
            "    try:\n        if True:\n            main()\n",
        ),
        (
            "    finally:\n        respan.flush()\n",
            "    finally:\n        if False:\n            respan.flush()\n",
        ),
    ],
)
def test_nested_main_or_flush_call_is_rejected(tmp_path, before, after):
    target = _golden_tree(tmp_path)
    app = target / "app.py"
    app.write_text(app.read_text().replace(before, after))
    with pytest.raises(IntegrationVerificationError):
        _verify(target)
