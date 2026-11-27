# Agentsii One-Click API Installer

Windows desktop app that connects any OpenAI-Codex-compatible API provider to Codex in one click.
Enter **Provider Name**, **API Base URL**, **API Key** → **ONE-CLICK CONNECT**.

- Real provider checks: URL, key, `/models` discovery, Responses + Chat Completions detection,
  per-model streamed Responses test with tools (Codex only supports `wire_api = "responses"`).
- Codex config via the official format (`[model_providers.<id>]`, command-backed `auth`, profile files),
  respects `CODEX_HOME`, backup first, comments preserved, full rollback on failure.
- API key stored only in Windows Credential Manager (keyring); Codex reads it through `agentsii-auth.exe`.

Stack: Python, PySide6, tomlkit, keyring, httpx, PyInstaller, Inno Setup.
Builds and end-to-end tests (built EXEs + real Credential Manager + real Codex CLI) run on GitHub Actions Windows runners:
see `.github/workflows/build.yml` and `ci/e2e_windows.py`.

Dev: `pip install -r requirements-build.txt && pytest -q tests && PYTHONPATH=src python -m agentsii.main`

Arabic user guide: `docs/UserGuide-AR.html`.
