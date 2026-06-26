.PHONY: install playground run-service generate-traces grade

install:
	uv sync
	uv run agents-cli install

playground:
	uv run agents-cli playground

run-service:
	uv run uvicorn expense_agent.fast_api_app:app --host 127.0.0.1 --port 8080

generate-traces:
	uv run python tests/eval/generate_traces.py

grade:
	uv run agents-cli eval grade --traces artifacts/traces/generated_traces.json --config tests/eval/eval_config.yaml

