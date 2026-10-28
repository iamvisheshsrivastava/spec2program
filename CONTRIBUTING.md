# Contributing to spec2program

Thanks for considering a contribution. This project is a FastAPI backend (plus
a small static frontend and a CLI) with a SQLite persistence layer, so most
contributions touch Python in `backend/` and tests in `tests/`.

## Dev setup

```bash
# 1. Clone and enter the repo
git clone https://github.com/iamvisheshsrivastava/spec2program.git
cd spec2program

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install runtime + dev dependencies (pytest, ruff, mypy)
pip install -r requirements-dev.txt

# 4. Run the server (defaults to the offline mock LLM — no API key needed)
uvicorn backend.main:app --reload
```

Open <http://localhost:8000>, load a bundled sample spec, and press
**Generate** to confirm things work end to end.

You don't need a real LLM key to develop or test: `LLM_PROVIDER` defaults to
`mock`, a deterministic rule-based planner, and the app falls back to it
automatically if a real provider is configured without a key. See the
[README's "Use a real LLM" section](README.md#use-a-real-llm) if you do want
to exercise a real provider.

### The CLI

There's also an offline CLI that calls the same pipeline in-process, no
server required — useful while iterating:

```bash
python -m backend.cli generate data/sample_id4_bev.json
```

See the docstring at the top of `backend/cli.py` for all subcommands
(`generate`, `batch`, `export-otx`, `export-csv`, `compare`).

### The database

SQLite persistence (`backend/db.py`) lives at `data/spec2program.db` by
default (`DATABASE_PATH` in `.env`). Migrations are forward-only and run
automatically at startup (see the module docstring in `backend/db.py`). You
don't need to do anything to get a working schema — just run the app or the
tests and it creates/migrates the file itself. If you want a clean slate,
delete the `.db` file; it will be recreated on next startup.

## Running tests, lint, and type-check

These are exactly the checks CI runs (`.github/workflows/ci.yml`), so running
them locally before opening a PR saves a round trip:

```bash
# Tests
pytest -v

# Lint (ruff — pyflakes + pycodestyle + import sort; see pyproject.toml)
ruff check backend/ tests/ scripts/

# Type-check (mypy, opt-in strictness; see pyproject.toml)
mypy backend/
```

Ruff can also fix most of its own findings (unused imports, import order,
etc.):

```bash
ruff check --fix backend/ tests/ scripts/
ruff format backend/ tests/ scripts/
```

CI runs on Python 3.11 and 3.12, so if you can, sanity-check locally on
whichever of those you have available.

### Writing tests

- Tests live in `tests/`, one file per module under test
  (`tests/test_validator.py` for `backend/validator.py`, etc.) — follow that
  convention for new modules.
- Favor small, direct unit tests over end-to-end ones where the module allows
  it (e.g. `diff_programs()`, `analyse_program()`, `to_csv()` are pure
  functions — test them directly rather than only through `/api/*`).
- If you touch an API route, add or extend a test in `tests/test_main.py` (or
  `tests/test_new_endpoints.py`) that exercises it through FastAPI's
  `TestClient`.
- If you touch the CLI, add a test in `tests/test_cli.py` calling
  `backend.cli.main([...])` directly rather than shelling out.

## Code style

- Formatting/lint rules are in `pyproject.toml` (`[tool.ruff]`): 110-char
  lines, `select = ["E", "F", "I"]` (pyflakes, pycodestyle errors, import
  sort). Don't add new rule families unless you're also fixing the resulting
  findings across the codebase — see the comment above `[tool.ruff.lint]`.
- Type-check config is in `[tool.mypy]`: opt-in, not `--strict`. Annotate new
  public functions' signatures; a full-coverage pass isn't required, but
  don't introduce new mypy errors.
- Most modules open with a short docstring explaining *why* they exist,
  often referencing the GitHub issue that motivated them (e.g. "Issue #19:
  ..."). Please keep that pattern for new modules or non-obvious changes —
  it's the main design record this project has.

## Pull request process

1. **Open an issue first** for anything beyond a trivial fix, so the
   approach can be agreed on before you invest time. For small, well-scoped
   bugs or `good first issue`-labeled tasks you can skip straight to a PR.
2. **Fork the repo and branch from `main`.** Use a short, descriptive branch
   name (e.g. `fix/cli-version-flag`).
3. **Keep PRs focused.** One logical change per PR — don't mix an unrelated
   refactor or formatting pass into a bug fix.
4. **Add/update tests** for any behavior change. A PR that changes behavior
   without a test covering it will likely be asked to add one.
5. **Run `pytest`, `ruff check`, and `mypy backend/` locally** and make sure
   they pass — CI enforces the same three gates on every PR (see
   `.github/workflows/ci.yml`), across Python 3.11 and 3.12.
6. **Write a clear PR description**: what changed and why, and which issue
   (if any) it closes (`Closes #N`).
7. A maintainer will review, may ask for changes, and will merge once CI is
   green and the change looks good.

## Reporting bugs / proposing features

Open a GitHub issue. For bugs, include: what you did, what you expected,
what happened instead, and (if applicable) the smallest `VehicleSpec`/program
JSON that reproduces it. For features, a short description of the use case
is more useful up front than an implementation sketch.

## Security

This project has no auth and is a prototype/research project — please don't
report routine hardening gaps as security issues unless they're exploitable
in the deployed demo. If you do find something exploitable in the live
deployment, open an issue describing the impact; no private disclosure
channel is set up for this project yet.
