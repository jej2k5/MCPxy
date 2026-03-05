# Contributing

## Development Setup

1. Create and activate a virtual environment.
2. Install dependencies:
   ```bash
   pip install -e .[dev]
   ```
3. Run the proxy locally:
   ```bash
   mcp-proxy --config config.example.json
   ```

## How to Run Tests

```bash
pytest
```

## Code Style Guidelines

- Python 3.11+.
- Type hints required across the codebase.
- Docstrings required for public classes/functions.
- Keep modules cohesive and testable.
- Prefer explicit error handling and JSON-RPC compliant errors.

## Pull Request Workflow

1. Fork and branch from `main`.
2. Add tests for behavior changes.
3. Run test suite and linters locally.
4. Submit PR with clear summary and rationale.
5. Address reviewer feedback promptly.

## Issue Reporting Guidelines

When opening issues, include:
- Expected behavior
- Actual behavior
- Reproduction steps
- Configuration snippet (with secrets removed)
- Logs or stack traces

## Feature Proposal Process

Open a discussion or issue with:
- Problem statement
- Proposed API/UX
- Backward compatibility notes
- Testing plan
