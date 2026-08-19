"""Process entrypoints (§5, DEC-015).

One image, several roles. Each module here is a `python -m app.entrypoints.<role>`
that builds its own dependencies from settings, connects to what it needs, and
runs until it is stopped -- nothing else in the codebase constructs a process.

Every consumer role follows the same three steps: declare the topology (safe to
repeat, and it means a worker starting first does not depend on another having
run), open a channel with QoS applied, then consume through the retry wrapper so
a failure lands in a tier queue instead of a sleep.
"""
