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
| `make up` / `make down` | Local infrastructure (arrives in WS-2) |
| `make migrate` | Apply database migrations (arrives in WS-3) |

## Layout

`src/app/` follows §6 of the spec, and `tests/unit/test_project_layout.py` parses that
section to keep the tree and the spec from drifting apart.
