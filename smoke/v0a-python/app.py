from __future__ import annotations

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
