"""Which PostgreSQL schemas this system owns.

A plain constants module on purpose: `grants.py`, `provisioning.py` and the
migrations all need these names, and none of them should have to import an
adapter -- let alone LangGraph and a connection pool -- to learn a string. The
dependency runs the other way: the adapter knows where the database put its
tables.
"""

from __future__ import annotations

from typing import Final

#: LangGraph's checkpoint store, kept out of `public` so checkpoint state and
#: domain state are separated by a boundary the database enforces (Q124).
CHECKPOINT_SCHEMA: Final = "langgraph"

#: Every schema outside `public` whose grants this system manages.
#:
#: Provisioning revokes across this whole tuple before granting the policy, so
#: removing a schema from `SCHEMA_GRANTS` actually takes the privileges away
#: rather than freezing them at whatever they were. A policy that can only add
#: is not a policy.
MANAGED_SCHEMAS: Final[tuple[str, ...]] = (CHECKPOINT_SCHEMA,)
