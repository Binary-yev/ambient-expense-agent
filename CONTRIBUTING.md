# Contributing to Ambient Expense Agent

Thank you for your interest in contributing! This guide covers everything you
need to get started.

## Development Setup

```bash
# 1. Fork and clone the repo
git clone https://github.com/<your-username>/ambient_expense_agent.git
cd ambient_expense_agent

# 2. Set up credentials
cp .env.example .env
# Edit .env with your GCP project or AI Studio API key

# 3. Install dependencies
make install

# 4. Install pre-commit hooks (runs ruff, codespell, security checks on every commit)
pip install pre-commit
pre-commit install
```

## Workflow

1. **Create a branch** — `git checkout -b feat/your-feature`
2. **Make changes** in `expense_agent/` or `tests/`
3. **Run checks** before committing:

```bash
# Lint and format
agents-cli lint

# Unit tests
uv run pytest tests/unit -v

# Integration tests (requires GCP credentials)
uv run pytest tests/integration -v
```

4. **Open a Pull Request** against `main`

## Code Style

- Formatter: **ruff format** (88-char line length)
- Linter: **ruff** (pycodestyle, pyflakes, isort, bugbear, pyupgrade)
- Spell check: **codespell**

All checks run automatically via pre-commit and GitHub Actions CI.

## Adding a New Node

Each workflow node lives in [`expense_agent/agent.py`](expense_agent/agent.py).
A node is a plain function that takes `(ctx: Context, node_input: <type>) -> Event`:

```python
def my_new_node(ctx: Context, node_input: dict) -> Event:
    """Does something useful."""
    return Event(output=node_input, route="next_step")
```

Then wire it into the `Workflow` edges at the bottom of the file.

## Eval Dataset

If your change affects routing logic, add a new eval case to
[`tests/eval/datasets/basic-dataset.json`](tests/eval/datasets/basic-dataset.json)
and regenerate baselines:

```bash
make generate-traces
make grade
```

## Security

Please read [SECURITY.md](SECURITY.md) before reporting any vulnerabilities.
Do **not** commit real credentials, SSNs, or PII — even in tests. Use synthetic
values (the eval dataset uses fictional `@company.com` emails and fake SSN
patterns).
