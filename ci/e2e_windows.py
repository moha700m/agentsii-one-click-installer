"""End-to-end test of the BUILT Windows executables (run on a Windows runner).

1. start the mock OpenAI-compatible provider
2. run AgentsiiPortable.exe --headless against it (real Windows Credential Manager)
3. check config.toml, backup, profiles, Credential Manager entry, auth helper
4. run the REAL Codex CLI through the configured provider (default + a profile)
5. conflict handling: same provider again without approval must change nothing
6. AgentsiiInstaller.exe (onedir) and the setup installer
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
OUT = ROOT / "e2e-out"
OUT.mkdir(exist_ok=True)
KEY = "sk-e2e-" + os.urandom(12).hex()
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}/v1"
results: list[tuple[str, bool, str]] = []


def check(name, cond, info=""):
    results.append((name, bool(cond), str(info)[:500]))
    print(("PASS " if cond else "FAIL ") + name + (f"  -- {info}" if info and not cond else ""), flush=True)
    return cond


def run_exe(exe: Path, args: list[str], env: dict, json_out: Path, timeout=600) -> tuple[int, dict]:
    if json_out.exists():
        json_out.unlink()
    p = subprocess.run([str(exe), *args, "--json-out", str(json_out)], env=env, timeout=timeout)
    data: dict = {}
    if json_out.exists():
        raw = json_out.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"raw": raw.strip()}
    return p.returncode, data


def main() -> int:
    codex_home = Path(tempfile.mkdtemp(prefix="codexhome-"))
    original = ('# user comment that must survive\nmodel = "gpt-5.5"\napproval_policy = "never"\n\n'
                '[mcp_servers.demo]\ncommand = "npx" # inline comment\nargs = ["-y", "demo"]\n')
    (codex_home / "config.toml").write_text(original, encoding="utf-8")

    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home)
    env["E2E_KEY"] = KEY
    env.pop("PYTHON_KEYRING_BACKEND", None)

    record = OUT / "mock-requests.jsonl"
    mock = subprocess.Popen([sys.executable, str(ROOT / "tests" / "mock_provider.py"), "--port", str(PORT),
                             "--key", KEY, "--record", str(record)])
    time.sleep(3)
    try:
        # ---------------------------------------------------------------- portable EXE
        portable = DIST / "AgentsiiPortable.exe"
        t0 = time.time()
        rc, data = run_exe(portable, ["--headless", "--name", "E2E Gateway", "--base-url", BASE,
                                      "--api-key-env", "E2E_KEY"], env, OUT / "portable.json")
        (OUT / "portable-result.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        check("portable exe exit code 0", rc == 0, data)
        check("portable reports ok", data.get("ok") is True, data.get("error_code"))
        check("models discovered == 5", data.get("discovered") == 5, data.get("discovered"))
        check("compatible models == 2", sorted(data.get("added", [])) == ["agent-codex-1", "agent-pro-2"], data.get("added"))
        check("credential backend is Windows", "Windows" in (data.get("credential_backend") or ""), data.get("credential_backend"))
        print(f"portable run took {time.time() - t0:.1f}s")

        cfg_text = (codex_home / "config.toml").read_text(encoding="utf-8")
        (OUT / "config.toml.after").write_text(cfg_text, encoding="utf-8")
        cfg = tomllib.loads(cfg_text)
        check("comments preserved", "# user comment that must survive" in cfg_text and "# inline comment" in cfg_text)
        check("mcp_servers preserved", cfg.get("mcp_servers", {}).get("demo", {}).get("command") == "npx")
        check("model_provider set", cfg.get("model_provider") == "e2e-gateway", cfg.get("model_provider"))
        prov = cfg.get("model_providers", {}).get("e2e-gateway", {})
        check("wire_api responses", prov.get("wire_api") == "responses")
        check("no plaintext key in config", KEY not in cfg_text)
        helper = Path(prov.get("auth", {}).get("command", ""))
        check("auth helper installed in LOCALAPPDATA", helper.exists() and "Agentsii" in str(helper), helper)
        backups = list((codex_home / "agentsii-backups").glob("*.bak"))
        check("backup created and identical", backups and backups[0].read_text(encoding="utf-8") == original)
        profiles = sorted(p.name for p in codex_home.glob("agentsii-*.config.toml"))
        check("profile files created", len(profiles) == 2 or "profiles" in cfg, profiles)

        ck = subprocess.run(["cmdkey", "/list"], capture_output=True, text=True)
        check("Windows Credential Manager has the entry", "agentsii-codex:e2e-gateway" in ck.stdout,
              [l for l in ck.stdout.splitlines() if "agentsii" in l.lower()])

        hp = subprocess.run([str(helper), *prov["auth"]["args"]], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        check("auth helper prints the key on stdout", hp.stdout.decode().strip() == KEY,
              f"rc={hp.returncode} len={len(hp.stdout)} err={hp.stderr[:200]!r}")

        logs = Path(os.environ["LOCALAPPDATA"]) / "Agentsii" / "logs" / "agentsii.log"
        logtxt = logs.read_text(encoding="utf-8") if logs.exists() else ""
        shutil.copy(logs, OUT / "agentsii.log") if logs.exists() else None
        check("log exists and has no key", logtxt and KEY not in logtxt)

        # ---------------------------------------------------------------- real Codex CLI
        codex_cli = shutil.which("codex")
        check("codex CLI available on runner", codex_cli, codex_cli)
        if codex_cli:
            ver = subprocess.run(["cmd", "/c", codex_cli, "--version"], capture_output=True, text=True, env=env)
            print("codex version:", ver.stdout.strip(), ver.stderr.strip())
            work = Path(tempfile.mkdtemp(prefix="codexwork-"))
            n_before = len(record.read_text().splitlines()) if record.exists() else 0
            cx = subprocess.run(["cmd", "/c", codex_cli, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                                 "Reply with the word hello."], capture_output=True, text=True, env=env,
                                cwd=work, timeout=300, encoding="utf-8", errors="replace")
            (OUT / "codex-exec.txt").write_text(f"rc={cx.returncode}\nSTDOUT:\n{cx.stdout}\nSTDERR:\n{cx.stderr}",
                                                encoding="utf-8")
            time.sleep(2)
            reqs = [json.loads(l) for l in record.read_text().splitlines()[n_before:]] if record.exists() else []
            codex_posts = [r for r in reqs if r.get("method") == "POST" and r.get("path") == "/v1/responses"]
            check("Codex sent Responses request to provider", codex_posts, reqs[-5:])
            check("Codex authenticated via Credential Manager helper",
                  codex_posts and all(r["auth_ok"] for r in codex_posts), codex_posts[:2])
            check("Codex used default model", any(r.get("model") == "agent-codex-1" for r in codex_posts))
            check("Codex exec printed provider reply", "AGENTSII_E2E_OK" in (cx.stdout + cx.stderr),
                  (cx.stdout + cx.stderr)[-800:])

            if profiles:
                prof = profiles[0].removesuffix(".config.toml")
                other = [p for p in profiles if "agent-pro-2" in p]
                prof = other[0].removesuffix(".config.toml") if other else prof
                n_before = len(record.read_text().splitlines())
                cx2 = subprocess.run(["cmd", "/c", codex_cli, "exec", "--profile", prof, "--skip-git-repo-check",
                                      "--sandbox", "read-only", "Say hi."], capture_output=True, text=True,
                                     env=env, cwd=work, timeout=300, encoding="utf-8", errors="replace")
                (OUT / "codex-exec-profile.txt").write_text(
                    f"rc={cx2.returncode}\nSTDOUT:\n{cx2.stdout}\nSTDERR:\n{cx2.stderr}", encoding="utf-8")
                time.sleep(2)
                reqs = [json.loads(l) for l in record.read_text().splitlines()[n_before:]]
                check(f"Codex profile {prof} uses its model",
                      any(r.get("model") == "agent-pro-2" for r in reqs if r.get("path") == "/v1/responses"),
                      reqs[-3:])

        # ---------------------------------------------------------------- conflict, no approval
        before = (codex_home / "config.toml").read_bytes()
        rc, data = run_exe(portable, ["--headless", "--name", "E2E Gateway", "--base-url", BASE,
                                      "--api-key-env", "E2E_KEY"], env, OUT / "conflict.json")
        check("existing provider not replaced without approval",
              rc == 1 and data.get("error_code") == "overwrite_declined"
              and (codex_home / "config.toml").read_bytes() == before, data.get("error_code"))

        # wrong key -> nothing changes
        env_bad = dict(env, E2E_KEY="sk-wrong-000000000000")
        rc, data = run_exe(portable, ["--headless", "--name", "Bad", "--base-url", BASE,
                                      "--api-key-env", "E2E_KEY"], env_bad, OUT / "badkey.json")
        check("wrong key rejected, config untouched",
              rc == 1 and data.get("error_code") == "key_invalid"
              and (codex_home / "config.toml").read_bytes() == before, data.get("error_code"))

        # ---------------------------------------------------------------- onedir app
        onedir = DIST / "AgentsiiInstaller" / "AgentsiiInstaller.exe"
        rc, data = run_exe(onedir, ["--headless", "--name", "E2E Second", "--base-url", BASE,
                                    "--api-key-env", "E2E_KEY"], env, OUT / "onedir.json")
        check("AgentsiiInstaller.exe (onedir) works", rc == 0 and data.get("ok"), data.get("error_code"))
        cfg = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        check("both providers present", {"e2e-gateway", "e2e-second"} <= set(cfg.get("model_providers", {})))

        # ---------------------------------------------------------------- installer
        setup = DIST / "AgentsiiSetup.exe"
        if check("setup exists", setup.exists()):
            sp = subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
                                 f"/LOG={OUT / 'setup.log'}"], timeout=600)
            installed = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Agentsii" / "AgentsiiInstaller.exe"
            check("installer installs app", sp.returncode == 0 and installed.exists(), installed)
            if installed.exists():
                rc, data = run_exe(installed, ["--version"], env, OUT / "installed-version.json")
                check("installed app runs", rc == 0 and data.get("raw") == "1.0.0", data)
            unins = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Agentsii" / "unins000.exe"
            if unins.exists():
                up = subprocess.run([str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES"], timeout=300)
                time.sleep(3)
                check("uninstaller removes app", up.returncode == 0 and not installed.exists())
    finally:
        mock.terminate()
        for pid in ("e2e-gateway", "e2e-second"):
            subprocess.run(["cmdkey", f"/delete:agentsii-codex:{pid}"], capture_output=True)

    failed = [r for r in results if not r[1]]
    summary = {"passed": len(results) - len(failed), "failed": len(failed),
               "checks": [{"name": n, "ok": ok, "info": i} for n, ok, i in results]}
    (OUT / "e2e-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n{summary['passed']} passed, {summary['failed']} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
