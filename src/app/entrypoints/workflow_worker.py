"""The `workflow-worker` process (§9.2, §11).

One process may own several partitions; each partition queue has a single
active consumer, so ordering per user holds however many workers are running.
"""

from __future__ import annotations

import asyncio
import os

from app.config import ServiceName
from app.entrypoints.runtime import consume_forever, run, worker_runtime
from app.infrastructure.rabbitmq.partitioning import partition_queue_name
from app.workers.workflow_worker import WorkflowWorker


def _owned_partitions(total: int) -> list[int]:
    """Partitions this replica subscribes to.

    Every replica subscribes to every partition by default and Single Active
    Consumer decides who gets each one, which is the arrangement that survives
    a replica dying. `GYM_TRACK_WORKFLOW_PARTITIONS_OWNED` narrows it when an
    operator wants a replica pinned to a subset.
    """
    declared = os.environ.get("GYM_TRACK_WORKFLOW_PARTITIONS_OWNED")
    if not declared:
        return list(range(total))
    return [int(value) for value in declared.split(",") if value.strip()]


async def main() -> None:
    async with worker_runtime(ServiceName.WORKFLOW_WORKER) as runtime:
        worker = WorkflowWorker(session_factory=runtime.session_factory)
        partitions = runtime.settings.workflow.partitions

        await asyncio.gather(
            *(
                consume_forever(runtime, partition_queue_name(partition, partitions), worker.handle)
                for partition in _owned_partitions(partitions)
            )
        )


if __name__ == "__main__":  # pragma: no cover
    run(main)
