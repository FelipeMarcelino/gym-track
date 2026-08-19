# gym-track

Multi-agent WhatsApp training assistant. The architecture spec lives in
[`doc/whatsapp_training_ai_architecture_v1.1.md`](doc/whatsapp_training_ai_architecture_v1.1.md);
the current sprint plan lives in [`doc/sprints/`](doc/sprints).

## Development environment

The devshell is the supported entrypoint — it pins Python 3.13, `uv` and `make`:

```bash
nix develop      # or: direnv allow, which does the same on cd
```

The Docker CLI deliberately does **not** come from the devshell. On WSL it comes
from Docker Desktop's integration (`/usr/bin/docker`); a second client in the
devshell would shadow it and lose the Desktop context.

## Common tasks

| Command | What it does |
| --- | --- |
| `make sync` | Install dependencies into `.venv` from `uv.lock` |
| `make fmt` | Format and autofix with ruff |
| `make lint` | Formatting and lint gate |
| `make typecheck` | `mypy --strict` |
| `make test` | Test suite |
| `make check` | Everything CI runs |
| `make up` / `make down` | The whole stack: infrastructure and every process |
| `make logs` | Follow the application processes |
| `make demo` | Send a fragmented message to the running stack |
| `make migrate` | Apply migrations from the host |
| `make provision` | Reconcile database roles and grants with the policy |

## Running the walking skeleton

```bash
make up      # postgres, rabbitmq, redis, migrations, api and four workers
make demo    # three fragments in; one batch, one workflow, one reply out
make logs    # follow the processes
make down    # stop everything and drop the volumes
```

`make up` builds one image and runs it as five roles (ADR-001). Migrations run
as a one-shot service that every process waits for, so a fresh volume produces
a working stack rather than a race.

Locally the dispatcher uses `FakeWhatsAppClient`: there is no Meta integration
yet (decision D6), and nothing leaves the machine. In a deployed environment
that process refuses to start rather than silently dropping replies.

## Layout

`src/app/` follows §6 of the spec, and `tests/unit/test_project_layout.py` parses that
section to keep the tree and the spec from drifting apart.
