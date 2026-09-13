# Notes API server

Reference implementation of the Notes API contract in [`../openapi.yaml`](../openapi.yaml), version 2.0.0. The contract is the source of truth: request bodies are validated by the spec's own JSON Schemas, typed models are generated from the document, and every response in the test suite is checked against it. Work in progress; the plan and task list are in [`../tasks/`](../tasks/).

## Development

```sh
cd server
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
