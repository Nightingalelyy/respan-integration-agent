"""Deterministic gates for agent-produced integrations."""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path, PurePosixPath

from .config import OnboardingRequest, Product, TracingMode, VerificationProfile


class IntegrationVerificationError(RuntimeError):
    """The generated patch does not satisfy the requested integration contract."""


# Deliberately bounded common credential shapes; this is not a comprehensive
# secret scanner and is only one of the patch-safety gates.
_COMMON_SECRET_PATTERNS = (
    re.compile(r"(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}"),
    re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}"
        r"\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        r"(?![A-Za-z0-9_-])"
    ),
)
_FORBIDDEN_PATH_NAMES = {"claude.md"}
_FORBIDDEN_CREDENTIAL_SUFFIXES = {".key", ".pem"}
_BARE_RESPAN_REQUIREMENT_PATTERN = re.compile(
    r"""(?ix)
    ^\s*["']?respan(?![-_.A-Za-z0-9])
    (?:\[[^\]\r\n]+\])?\s*
    (?:(?:===|==|~=|!=|<=|>=|<|>|@)\s*[^,;\s"']+)?
    (?:\s*;\s*[^#"']+)?\s*["']?,?\s*(?:\#.*)?$
    """
)
_TAGS_ASSIGNMENT_PATTERN = re.compile(r"\btags[ \t]*=")
_SMOKE_BASELINE_SOURCE = """from __future__ import annotations

import os

from openai import OpenAI


def main() -> None:
    marker = os.environ["RESPAN_EXAMPLE_RUN_ID"]
    if not marker.startswith("respan-v0a-") or len(marker) > 96:
        raise ValueError("RESPAN_EXAMPLE_RUN_ID must be a short respan-v0a marker")

    client = OpenAI(
        api_key=os.environ["RESPAN_API_KEY"],
        base_url=os.getenv("RESPAN_BASE_URL", "https://api.respan.ai/api"),
        timeout=30.0,
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=os.getenv("RESPAN_SMOKE_MODEL", "gpt-4o-mini"),
        messages=[
            {
                "role": "user",
                "content": f"Return exactly this text and nothing else: {marker}",
            }
        ],
        temperature=0,
        max_tokens=64,
    )

    text = (response.choices[0].message.content or "").strip()
    if text != marker:
        raise RuntimeError("model did not return the smoke marker exactly")

    print("SMOKE_OK")


if __name__ == "__main__":
    main()
"""
_SMOKE_BASELINE_TREE_DUMP = ast.dump(
    ast.parse(_SMOKE_BASELINE_SOURCE), include_attributes=False
)


def _safe_changed_path(raw_path: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise IntegrationVerificationError(f"unsafe changed path: {raw_path!r}")
    if ".git" in path.parts:
        raise IntegrationVerificationError(f"agent changed Git internals: {raw_path}")
    return path


def _is_forbidden_changed_path(path: PurePosixPath) -> bool:
    name = path.name.lower()
    return (
        name in _FORBIDDEN_PATH_NAMES
        or name == ".env"
        or name.startswith(".env.")
        or path.suffix.lower() in _FORBIDDEN_CREDENTIAL_SUFFIXES
        or ".claude" in path.parts
    )


def _added_diff_lines(diff: str) -> list[str]:
    return [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def _compile_changed_python(workdir: Path, changed_files: list[str]) -> None:
    for raw_path in changed_files:
        path = _safe_changed_path(raw_path)
        if path.suffix != ".py":
            continue
        absolute = workdir / path
        if not absolute.exists():
            continue
        source = absolute.read_text(encoding="utf-8")
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            raise IntegrationVerificationError(
                f"generated Python is invalid: {path}: {exc}"
            ) from exc


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _normalized_smoke_tree_dump(
    tree: ast.Module,
    *,
    respan_import: ast.ImportFrom,
    respan_assignment: ast.Assign,
    main_guard: ast.If,
) -> str:
    """Remove only the reviewed integration edits, then compare the full app AST."""
    normalized_body: list[ast.stmt] = []
    for statement in tree.body:
        if statement is respan_import or statement is respan_assignment:
            continue
        cloned = copy.deepcopy(statement)
        if statement is main_guard:
            assert isinstance(cloned, ast.If)
            cloned.body = [
                ast.Expr(
                    value=ast.Call(
                        func=ast.Name(id="main", ctx=ast.Load()),
                        args=[],
                        keywords=[],
                    )
                )
            ]
        normalized_body.append(cloned)
    normalized = ast.Module(
        body=normalized_body,
        type_ignores=copy.deepcopy(tree.type_ignores),
    )
    return ast.dump(normalized, include_attributes=False)


def _validate_python_auto_smoke(
    workdir: Path,
    req: OnboardingRequest,
    changed_files: list[str],
) -> None:
    assert req.verification is not None
    expected_paths = {"app.py", "requirements.txt"}
    if set(changed_files) != expected_paths:
        raise IntegrationVerificationError(
            f"smoke patch must change exactly {sorted(expected_paths)}, got {sorted(changed_files)}"
        )

    requirements = (workdir / "requirements.txt").read_text(encoding="utf-8")
    requirement_lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    expected_requirement = f"respan-ai=={req.verification.respan_ai_version}"
    expected_openai_instrumentation = (
        f"opentelemetry-instrumentation-openai=={req.verification.openai_otel_version}"
    )
    if requirement_lines != [
        "openai==1.99.9",
        expected_requirement,
        expected_openai_instrumentation,
    ]:
        raise IntegrationVerificationError(
            "smoke requirements must contain only openai==1.99.9, "
            f"{expected_requirement}, and {expected_openai_instrumentation}"
        )

    source = (workdir / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="app.py")

    main_functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    ]
    if len(main_functions) != 1:
        raise IntegrationVerificationError(
            "smoke app must retain exactly one main() function"
        )
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "respan"
        and any(alias.name == "Respan" for alias in node.names)
    ]
    if len(imports) != 1:
        raise IntegrationVerificationError("smoke app must import Respan exactly once")
    respan_import = imports[0]
    if (
        respan_import.level != 0
        or len(respan_import.names) != 1
        or respan_import.names[0].name != "Respan"
        or respan_import.names[0].asname is not None
    ):
        raise IntegrationVerificationError(
            "smoke app must use exactly `from respan import Respan`"
        )

    respan_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == "Respan"
    ]
    if len(respan_calls) != 1:
        raise IntegrationVerificationError(
            "smoke app must initialize Respan exactly once"
        )
    respan_call = respan_calls[0]
    if not (isinstance(respan_call.func, ast.Name) and respan_call.func.id == "Respan"):
        raise IntegrationVerificationError(
            "smoke app must call the imported Respan class directly"
        )
    if respan_call.args:
        raise IntegrationVerificationError(
            "smoke Respan initialization must use keyword arguments only"
        )
    keyword_names = {keyword.arg for keyword in respan_call.keywords}
    if None in keyword_names or not keyword_names <= {
        "app_name",
        "environment",
        "metadata",
    }:
        raise IntegrationVerificationError(
            "smoke Respan initialization may use only app_name, environment, and metadata"
        )
    if "metadata" not in keyword_names:
        raise IntegrationVerificationError(
            "smoke Respan initialization must attach metadata.run_id"
        )

    keywords = {
        keyword.arg: keyword.value for keyword in respan_call.keywords if keyword.arg
    }
    tracing = req.tracing
    assert tracing is not None
    if (
        tracing.service_name
        and _literal_string(keywords.get("app_name")) != tracing.service_name
    ):
        raise IntegrationVerificationError(
            "Respan app_name does not match tracing.service_name"
        )
    if (
        tracing.environment
        and _literal_string(keywords.get("environment")) != tracing.environment
    ):
        raise IntegrationVerificationError(
            "Respan environment does not match tracing.environment"
        )
    metadata = keywords.get("metadata")
    valid_metadata = (
        isinstance(metadata, ast.Dict)
        and len(metadata.keys) == 1
        and _literal_string(metadata.keys[0]) == "run_id"
        and isinstance(metadata.values[0], ast.Subscript)
        and isinstance(metadata.values[0].value, ast.Attribute)
        and isinstance(metadata.values[0].value.value, ast.Name)
        and metadata.values[0].value.value.id == "os"
        and metadata.values[0].value.attr == "environ"
        and _literal_string(metadata.values[0].slice) == "RESPAN_EXAMPLE_RUN_ID"
    )
    if not valid_metadata:
        raise IntegrationVerificationError(
            "metadata.run_id must come from os.environ['RESPAN_EXAMPLE_RUN_ID']"
        )

    respan_assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign) and node.value is respan_call
    ]
    if len(respan_assignments) != 1:
        raise IntegrationVerificationError(
            "the Respan instance must be retained by one top-level assignment"
        )
    respan_assignment = respan_assignments[0]
    targets = respan_assignment.targets
    if len(targets) != 1 or not isinstance(targets[0], ast.Name):
        raise IntegrationVerificationError(
            "the Respan instance must be retained in one simple variable"
        )
    respan_name = targets[0].id
    if respan_name != "respan":
        raise IntegrationVerificationError(
            "the smoke Respan instance must use the exact variable name `respan`"
        )

    flush_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "flush"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == respan_name
    ]
    if len(flush_calls) != 1:
        raise IntegrationVerificationError(
            "the retained Respan instance must be flushed exactly once"
        )
    if flush_calls[0].args or flush_calls[0].keywords:
        raise IntegrationVerificationError("Respan.flush() must not receive arguments")

    main_guard_pairs = [
        (statement, nested)
        for statement in tree.body
        if isinstance(statement, ast.If)
        for nested in statement.body
        if isinstance(nested, ast.Try)
    ]
    if len(main_guard_pairs) != 1:
        raise IntegrationVerificationError(
            "the smoke main call must be protected by one try/finally"
        )
    main_guard, guarded = main_guard_pairs[0]
    final_nodes = [
        node for statement in guarded.finalbody for node in ast.walk(statement)
    ]
    if not guarded.finalbody or flush_calls[0] not in final_nodes:
        raise IntegrationVerificationError(
            "Respan.flush() must run from the main guard's finally block"
        )
    main_statement = guarded.body[0] if len(guarded.body) == 1 else None
    main_call = main_statement.value if isinstance(main_statement, ast.Expr) else None
    if not (
        isinstance(main_call, ast.Call)
        and isinstance(main_call.func, ast.Name)
        and main_call.func.id == "main"
        and not main_call.args
        and not main_call.keywords
    ):
        raise IntegrationVerificationError(
            "the try block must contain only a direct main() call"
        )
    final_statement = guarded.finalbody[0] if len(guarded.finalbody) == 1 else None
    final_call = (
        final_statement.value if isinstance(final_statement, ast.Expr) else None
    )
    if final_call is not flush_calls[0]:
        raise IntegrationVerificationError(
            "the finally block must contain only the direct Respan.flush() call"
        )
    if (
        guarded.handlers
        or guarded.orelse
        or len(guarded.body) != 1
        or len(guarded.finalbody) != 1
        or len(main_guard.body) != 1
        or main_guard.orelse
    ):
        raise IntegrationVerificationError(
            "the smoke main guard must contain only main() and finally flush()"
        )

    if (
        _normalized_smoke_tree_dump(
            tree,
            respan_import=respan_import,
            respan_assignment=respan_assignment,
            main_guard=main_guard,
        )
        != _SMOKE_BASELINE_TREE_DUMP
    ):
        raise IntegrationVerificationError(
            "the agent changed code outside the reviewed Respan integration"
        )

    openai_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) == "OpenAI"
    ]
    if not openai_calls or respan_call.lineno >= min(
        node.lineno for node in openai_calls
    ):
        raise IntegrationVerificationError(
            "Respan must initialize before the OpenAI client"
        )

    forbidden_names = {"workflow", "task", "agent", "tool"}
    if any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            _call_name(decorator) in forbidden_names
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
        )
        for node in ast.walk(tree)
    ):
        raise IntegrationVerificationError("Auto mode must not add Respan decorators")
    if "respan_instrumentation" in source:
        raise IntegrationVerificationError(
            "Auto mode must not add a framework instrumentor"
        )


def verify_integration(
    workdir: Path,
    req: OnboardingRequest,
    changed_files: list[str],
    diff: str,
    *,
    respan_api_key: str,
) -> None:
    """Reject unsafe, syntactically invalid, or contract-invalid generated patches."""
    if not changed_files or not diff.strip():
        raise IntegrationVerificationError("agent produced no reviewable patch")

    for raw_path in changed_files:
        path = _safe_changed_path(raw_path)
        if _is_forbidden_changed_path(path):
            raise IntegrationVerificationError(
                f"agent changed a forbidden path: {raw_path}"
            )

    if respan_api_key and respan_api_key in diff:
        raise IntegrationVerificationError(
            "generated patch contains RESPAN_API_KEY value"
        )
    if any(pattern.search(diff) for pattern in _COMMON_SECRET_PATTERNS):
        raise IntegrationVerificationError(
            "generated patch contains a secret-like value"
        )
    if "GIT binary patch" in diff or "Binary files " in diff:
        raise IntegrationVerificationError("binary changes are not allowed in v0a")

    _compile_changed_python(workdir, changed_files)

    if req.product in (Product.tracing, Product.both) and req.tracing:
        added_lines = _added_diff_lines(diff)
        if req.tracing.mode is TracingMode.auto and any(
            _BARE_RESPAN_REQUIREMENT_PATTERN.fullmatch(line)
            for line in added_lines
        ):
            raise IntegrationVerificationError(
                "use the respan-ai distribution, not bare respan"
            )
        if any(_TAGS_ASSIGNMENT_PATTERN.search(line) for line in added_lines):
            raise IntegrationVerificationError(
                "Respan() does not accept tags=; use typed arguments"
            )

    if (
        req.verification
        and req.verification.profile is VerificationProfile.python_openai_auto_smoke
    ):
        _validate_python_auto_smoke(workdir, req, changed_files)
