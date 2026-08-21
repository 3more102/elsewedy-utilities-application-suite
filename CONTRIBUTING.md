# Contributing to EUAS

Thank you for contributing to the Elsewedy Utilities Application Suite.

## Development Flow

- `main` is the stable demonstration branch.
- `develop` is the integration branch for ongoing work.
- Create focused feature/fix branches from `develop`.
- Open a pull request back into `develop`.
- Promote tested release candidates from `develop` to `main`.

Suggested branch names:

```text
feature/<short-name>
fix/<short-name>
docs/<short-name>
chore/<short-name>
```

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

Run the full regression suite:

```bash
pytest -q
```

Run the clean-process HTTP smoke test:

```bash
python scripts/smoke_test.py
```

Validate Python and frontend syntax:

```bash
python -m compileall -q app scripts
node --check static/app.js
node --check static/sw.js
```

## Pull Request Expectations

A pull request should:

- explain the problem or capability being addressed;
- describe user/developer impact;
- keep database changes backward-compatible or document migration behavior;
- include tests for new business logic;
- preserve RBAC and audit behavior;
- avoid committing databases, secrets, uploads, caches or generated runtime artifacts.

## Security

Do not open public issues for sensitive security vulnerabilities. Follow [SECURITY.md](SECURITY.md).

## Scope

EUAS is an original reference implementation for enterprise asset, maintenance, utility operations and field service workflows. Contributions must not introduce proprietary third-party source code, branding or protected assets.
