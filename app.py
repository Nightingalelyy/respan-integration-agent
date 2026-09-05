"""Small direct-SDK app used as source for the v0b delivery smoke test.

The smoke harness edits this application but never executes it.
"""
from anthropic import Anthropic


def main() -> None:
    import os as _respan_os

    if _respan_os.getenv("RESPAN_API_KEY"):
        import sys as _respan_sys

        if not ((3, 11) <= _respan_sys.version_info[:2] < (3, 14)):
            raise RuntimeError("Respan tracing requires Python >=3.11,<3.14 when RESPAN_API_KEY is set")
        from respan import Respan as _Respan

        _respan = _Respan(api_key=_respan_os.environ["RESPAN_API_KEY"])

    client = Anthropic()
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=32,
        messages=[{"role": "user", "content": "Reply with the word ready."}],
    )
    print(response.content[0].text)


if __name__ == "__main__":
    main()
