# Contributing

## Development setup

Python 3.11 or newer.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Tests and checks

```bash
python -m pytest -q
python scripts/boundary_check.py imports    # dependency direction
python scripts/boundary_check.py sanitize   # no paths, secrets or private markers in public files
```

Tests against a real Hermes installation are skipped unless you point them at a
Python interpreter that can import `hermes_cli`:

```bash
GE_HERMES_PYTHON=<hermes-python> python -m pytest -q tests/hermes
```

Those tests install the plugin into throwaway Hermes homes only.

## Coding expectations

- `ge_runtime` must stay host-independent: standard library and PyYAML only, never a
  Hermes import. `ge_hermes` talks to Hermes only through the `PluginContext`.
- Fail closed: reject invalid input with an `ErrorCode` instead of guessing.
- Commands and tool handlers must never raise into Hermes.
- Keep persisted formats versioned; readers reject unsupported versions.
- Do not commit machine-specific paths, credentials, run state or logs. The sanitizer
  test enforces this for tracked text files. Deployment-specific markers you want to
  enforce locally belong in an untracked `.private/sanitizer_policy.local.json`
  overlay (see `scripts/sanitizer_policy.json`).
- Match the surrounding style; keep changes focused.

## Adding tests

- Runtime behaviour: `tests/runtime/` (use `tmp_path` stores; no global state).
- Hermes binding: `tests/hermes/test_binding.py` uses a `PluginContext` double;
  real-loader behaviour goes into `tests/hermes/test_real_hermes_loader.py`.
- Installer and multi-profile loading: `tests/installer/`.
- Documentation guarantees (command reference, version consistency, examples):
  `tests/docs/`.
- Fixtures must contain obviously fake values only.

## Building locally

```bash
python -m build
python scripts/install_plugin.py install --hermes-home <hermes-home>
python scripts/install_plugin.py verify --hermes-home <hermes-home>
```

Artifacts are written to `dist/`. Update `CHANGELOG.md` and keep the version in
`pyproject.toml`, `src/ge_runtime/version.py` and `src/ge_hermes/plugin.yaml` in sync
(the tests check this).
