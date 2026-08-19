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
SETTLE_SECONDS = 45

#: The stack exposes PostgreSQL on the host, so the check reads the outcome the
#: same way an operator would.
POSTGRES_DSN = "postgresql://gym_track:local-dev-only@localhost:5432/gym_track"

#: One row, only if the whole path completed for *these* fragments: they were
#: batched together, a workflow ran for that batch, and its reply was
#: dispatched. Anything short of that returns nothing, which is the point.
QUERY = """
SELECT b.id,
       count(i.id),
       w.id,
       o.delivery_state,
       o.text,
       b.trace_id
FROM message_batches b
JOIN message_batch_items i ON i.message_batch_id = b.id
JOIN messages m ON m.id = i.message_id
JOIN workflow_executions w ON w.message_batch_id = b.id
JOIN outbound_messages o ON o.workflow_execution_id = w.id
WHERE m.external_message_id = ANY(%s)
  AND o.delivery_state IN ('dispatched', 'delivered')
GROUP BY b.id, w.id, o.delivery_state, o.text, b.trace_id
"""


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
    external_ids = [f"wamid.demo.{run_id}.{index}" for index in range(len(FRAGMENTS))]
    for external_id, text in zip(external_ids, FRAGMENTS, strict=True):
        accepted = post(webhook_body(external_id, text))
        sys.stdout.write(f"  accepted {external_id}: {accepted}\n")

    sys.stdout.write(f"  waiting up to {SETTLE_SECONDS}s for the debounce window and pipeline\n")
    outcome = _await_reply(external_ids)

    if outcome is None:
        sys.stderr.write(
            "\nNo reply was produced. The API accepted the fragments, so the break is\n"
            "downstream — check `make logs` for the aggregator, the workflow worker,\n"
            "the outbox publisher and the dispatcher.\n"
        )
        return 1

    sys.stdout.write(
        f"\n  batch          {outcome['batch_id']} ({outcome['fragments']} fragments)\n"
        f"  execution      {outcome['execution_id']}\n"
        f"  reply          {outcome['delivery_state']}: {outcome['text']}\n"
        f"  trace_id       {outcome['trace_id']}\n"
        "\nFollow that interaction across the processes with:\n"
        f"  docker compose -f docker/compose.yaml logs | grep {outcome['trace_id']}\n"
    )
    return 0


def _await_reply(external_ids: list[str]) -> dict[str, Any] | None:
    """Poll for the reply these fragments should produce.

    Polling rather than sleeping-then-checking: a healthy stack answers in a few
    seconds, and a broken one is reported as broken instead of as success.
    """
    import psycopg

    deadline = time.monotonic() + SETTLE_SECONDS
    while time.monotonic() < deadline:
        with psycopg.connect(POSTGRES_DSN) as connection:
            row = connection.execute(QUERY, (external_ids,)).fetchone()
        if row is not None:
            return {
                "batch_id": row[0],
                "fragments": row[1],
                "execution_id": row[2],
                "delivery_state": row[3],
                "text": row[4],
                "trace_id": row[5],
            }
        time.sleep(1)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
