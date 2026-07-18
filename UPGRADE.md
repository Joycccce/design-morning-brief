# Upgrade from the previous repository

Replace the repository with the files in this package.

Important additions:

- `data/history.json`
- `search-stages.json` in each Artifact
- Three-layer time strategy
- Detail-page validation
- Dynamic fallback to recent insights and classic methods
- No hard minimum number of items
- History persistence after successful full or scheduled runs

Recommended upgrade sequence:

1. Back up the current repository.
2. Upload and overwrite `src/main.py`, `requirements.txt`, `README.md`, `.gitignore`, `tests/test_core.py`, and `.github/workflows/daily-brief.yml`.
3. Add `data/history.json` and `output/.gitkeep`.
4. Confirm the four repository secrets remain unchanged.
5. Run `test-feishu`.
6. Run `dry-run` and inspect `brief.json`, `candidates.json`, and `rejected-candidates.json`.
7. Run `full` only after the dry run looks correct.

The workflow now needs `contents: write` so it can persist `data/history.json`. If branch protection blocks the history commit, the report still runs and sends, but cross-day repetition control will not persist.
