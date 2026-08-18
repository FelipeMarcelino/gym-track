# gym-track

Multi-agent WhatsApp training assistant: log workouts from natural language and audio,
analyse training history, and generate evidence-grounded recommendations and programs.

## Documentation

| Document | Purpose |
| --- | --- |
| [`doc/whatsapp_training_ai_architecture_v1.1.md`](doc/whatsapp_training_ai_architecture_v1.1.md) | **Normative** architecture specification |
| [`doc/implementation-plan.md`](doc/implementation-plan.md) | Sprint index and working model |
| [`doc/sprints/`](doc/sprints/) | Detailed plan for the current sprint |

The specification is normative. Where code and spec disagree, either the code is wrong or
the change needs an ADR (spec §48).

## Development

The toolchain is provided by a Nix devshell. With [direnv](https://direnv.net) installed,
`cd` into the repository and it loads automatically; otherwise:

```bash
nix develop
```

This gives you Python 3.13, `uv`, `make`, and the Docker CLI. Then:

```bash
make sync       # install dependencies into .venv
make check      # lint + typecheck + test, exactly what CI runs
```

Run `make help` for the full target list.

### Docker daemon

The devshell provides the Docker **client** only. Integration tests use ephemeral
containers (spec §38, Q158), so a running `dockerd` is required from Sprint 1 / WS-3
onward. On WSL2 with systemd enabled, install Docker Engine at the host level — a user
devshell cannot supply a system daemon.

## Layout

```text
src/app/    application code, structured per spec §6
tests/      unit, domain, application, integration, contract, graph, agents, rag, evals, e2e
doc/        specification, implementation plan, ADRs
```

## Contributing

See [`CLAUDE.md`](CLAUDE.md). In short: plan before implementing, ship tests with every
change, and branch as `feat/`, `hotfix/` or `doc/` with a pull request per unit of work.
