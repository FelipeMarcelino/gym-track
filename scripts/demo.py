"""Send a fragmented message to a running stack and report what came back.

`make demo` runs this against `make up`. It is the sprint's acceptance check in
executable form: three webhook requests go in, and one batch, one workflow
execution and one dispatched reply must come out — with the same interaction
trace on all of them (Q131).

It talks to the stack from outside, over HTTP and SQL, so it proves the
processes are wired to each other rather than that the code composes in a test.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from pydantic import SecretStr

from app.security.signatures import SIGNATURE_HEADER, sign

API = "http://localhost:8000"
APP_SECRET = SecretStr("local-dev-only")
BSUID = "5511987654321"
FRAGMENTS = ("fiz supino", "3x10", "80kg")
#: The debounce window is 3s sliding with a 10s cap; give the pipeline room.
SETTLE_SECONDS = 15


def webhook_body(external_message_id: str, text: str) -> bytes:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": BSUID,
                                        "id": external_message_id,
                                        "timestamp": str(int(time.time())),
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()


def post(body: bytes) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{API}/webhooks/whatsapp",
        data=body,
        headers={
            SIGNATURE_HEADER: sign(body, APP_SECRET),
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        decoded: dict[str, Any] = json.loads(response.read())
        return decoded


def main() -> int:
    try:
        with urllib.request.urlopen(f"{API}/ready", timeout=5) as response:
            if response.status != 200:
                sys.stderr.write("the stack is not ready; run `make up` first\n")
                return 1
    except urllib.error.URLError:
        sys.stderr.write(f"no API at {API}; run `make up` first\n")
        return 1

    run_id = int(time.time())
    for index, text in enumerate(FRAGMENTS):
        accepted = post(webhook_body(f"wamid.demo.{run_id}.{index}", text))
        sys.stdout.write(f"  fragment {index + 1}/{len(FRAGMENTS)} accepted: {accepted}\n")

    sys.stdout.write(f"  waiting {SETTLE_SECONDS}s for the debounce window and the pipeline\n")
    time.sleep(SETTLE_SECONDS)

    sys.stdout.write(
        "\nNow check what the pipeline produced:\n"
        "  docker compose -f docker/compose.yaml exec postgres \\\n"
        "    psql -U gym_track -d gym_track -c \\\n"
        '    "SELECT sequence, delivery_state, text FROM outbound_messages ORDER BY sequence;"\n'
        "\nAnd follow one interaction across the processes:\n"
        "  docker compose -f docker/compose.yaml logs | grep <trace_id>\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
