# Repository Guidelines

## Project Structure & Module Organization

`doc/` is the current source of truth. Keep code aligned with its product and architecture decisions. Python belongs under `src/werewolf/`: `domain/` for dependency-free models, `game/` for authoritative state, `knowledge/` for published rules, `ruleset_workbench/` for knowledge production, `runtime/` for Pi, and `persistence/` for snapshots. Put the Pi adapter in `extensions/`, prompts in `prompts/`, examples in `config/`, and tests in `tests/{unit,contract,integration,scenarios}/`. Never edit generated `vault/compiled/`; work through `vault/_workbench/` and the publish flow.

## Build, Test, and Development Commands

Use CPython 3.11 and uv exclusively:

```powershell
uv sync --locked                 # install exactly the locked dependencies
uv run werewolf --help           # verify the CLI entry point
uv run pytest                    # run the complete test suite
uv run ruff check .              # lint
uv run ruff format --check .     # verify formatting
uv run mypy src                  # type-check production code
```

Manage dependencies with `uv add`/`uv remove`, never `pip` or `requirements.txt`. Commit `.python-version`, `pyproject.toml`, and `uv.lock` together.

## Coding Style & Naming Conventions

Use four-space indentation and complete type annotations. Use `snake_case` for files, functions, variables, and knowledge IDs; `PascalCase` for classes; and uppercase persisted enum values. Pydantic v2 models normally use `extra="forbid"`. Keep external I/O async, route every `GameState` mutation through the serialized commit path, and read board rules from frozen snapshots instead of Python branches.

## Testing Guidelines

Use pytest and `test_*.py`. Put isolated tests in `unit/`, Pi/Gateway protocol checks in `contract/`, component combinations in `integration/`, and full games in `scenarios/`. Cover success, rejection, and authorization boundaries. Prioritize secret isolation, deterministic snapshots, concurrent voting, dual-potion actions, and Pi process cleanup.

## Commit & Pull Request Guidelines

There is no Git history yet. Until a project convention is established, use concise Conventional Commit subjects such as `feat(knowledge): compile role profiles`. Pull requests must describe behavior and design-document impact, list verification commands, link applicable issues, and identify schema or ruleset compatibility changes. Screenshots are needed only for future UI work.

## Security & Configuration

Never commit API keys, `.env`, `.runtime/`, game data, identity prompts, private logs, or tokens. Bind the knowledge gateway only to `127.0.0.1`. Run Agent Reach from the network-enabled host/approved non-sandbox context; a restricted-sandbox failure does not mean it is unavailable. Prefer HTTP/HTTPS over Socket/WebSocket. Published knowledge is immutable: release a new semantic version instead of editing it.

## Codex Execution and Delegation

- Never execute any terminal or command-line operation inside Codex's restricted sandbox on this device, including read-only commands. Sandbox terminal attempts repeatedly fail with `setup refresh had errors`.
- Run all terminal commands, including `uv`, `git`, and `pytest`, only in an approved host/non-sandbox execution context (for example, `exec_command` with `require_escalated`). Do not retry a failed command in the restricted sandbox.
- Every concrete code-writing step must be assigned to a new `luna-worker` sub-agent using `gpt-5.6-luna` with `xhigh` reasoning. The main agent owns implementation design against the technical plan and reviews the sub-agent's changes.
