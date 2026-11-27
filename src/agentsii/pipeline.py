"""The ONE-CLICK CONNECT pipeline (8 visible stages, 15 steps)."""
from __future__ import annotations

import hashlib
import json

import tomlkit
from dataclasses import dataclass, field
from typing import Callable

import httpx

from . import bridge, catalog, codex, credentials, system
from .credentials import CredentialError
from .logutil import log, register_secret
from .provider import (MAX_MODELS_TO_TEST, is_model_specific, normalize_base_url, ProbeReport, ProviderClient, ProviderError, pick_default,
                       run_compat_tests, select_candidates)

STAGES = [
    "stage_url",        # [1] Checking API URL            (steps 1-2)
    "stage_key",        # [2] Verifying API Key           (step 3)
    "stage_models",     # [3] Discovering Models          (steps 4-5)
    "stage_compat",     # [4] Checking Codex Compatibility (steps 6-8)
    "stage_codex",      # [5] Detecting Codex Installation (step 9)
    "stage_config",     # [6] Configuring Provider        (steps 10-13)
    "stage_secure",     # [7] Securing API Credentials    (step 14)
    "stage_verify",     # [8] Final Verification          (step 15)
]


@dataclass
class Result:
    ok: bool = False
    provider_name: str = ""
    provider_id: str = ""
    base_url: str = ""
    discovered: int = 0
    tested: int = 0
    added: list[str] = field(default_factory=list)
    default_model: str = ""
    profiles: list[str] = field(default_factory=list)
    supports_responses: bool = False
    supports_chat: bool = False
    bridged: bool = False
    codex_base_url: str = ""
    picker: str = ""
    picker_check: str = ""
    service_running: bool = False
    autostart: bool = False
    backup_path: str = ""
    config_path: str = ""
    credential_backend: str = ""
    failed_stage: int = -1
    error_code: str = ""
    error_detail: str = ""
    rejected: list[tuple[str, str]] = field(default_factory=list)
    codex: codex.CodexInstall | None = None

    def to_json(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "codex"}
        if self.codex:
            d["codex_home"] = str(self.codex.home)
            d["codex_cli"] = self.codex.cli_path
            d["codex_cli_version"] = self.codex.cli_version_text
            d["codex_desktop"] = self.codex.desktop_path or self.codex.desktop_appid
        return d


class Callbacks:
    def stage_started(self, i: int) -> None: ...
    def stage_done(self, i: int, detail: str) -> None: ...
    def stage_failed(self, i: int, code: str, detail: str) -> None: ...
    def progress(self, i: int, text: str) -> None: ...
    def confirm_overwrite(self, provider_id: str, existing: dict) -> bool:
        return False


class StageFailure(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def run(name: str, base_url: str, api_key: str, cb: Callbacks,
        transport: httpx.BaseTransport | None = None,
        detect_fn: Callable[[], codex.CodexInstall] = codex.detect) -> Result:
    res = Result(provider_name=name.strip())
    api_key = (api_key or "").strip()
    register_secret(api_key)
    stage = 0
    changes: codex.ChangeSet | None = None
    prev_key: str | None = None
    key_written = False
    client: ProviderClient | None = None

    def begin(i: int):
        nonlocal stage
        stage = i
        cb.stage_started(i)

    try:
        if not res.provider_name:
            begin(0)
            raise StageFailure("name_empty")
        if not api_key:
            begin(0)
            raise StageFailure("key_empty")

        # [1] URL --------------------------------------------------------
        begin(0)
        try:
            base = normalize_base_url(base_url)
            client = ProviderClient(base, api_key, transport=transport)
            client.check_reachable()
        except ProviderError as e:
            raise StageFailure(e.code, e.detail) from e
        cb.stage_done(0, base)

        # [2] key --------------------------------------------------------
        begin(1)
        report = ProbeReport()
        try:
            client.discover_models(report)
        except ProviderError as e:
            if e.code == "key_invalid":
                raise StageFailure(e.code, e.detail) from e
            # Models list missing → key cannot be verified here; fail at [3].
            cb.stage_done(1, "unverified")
            begin(2)
            raise StageFailure(e.code, e.detail) from e
        res.base_url = report.base_url
        cb.stage_done(1, "ok")

        # [3] models -----------------------------------------------------
        begin(2)
        res.discovered = len(report.discovered)
        cands, rejected = select_candidates(report.discovered)
        res.rejected = [(m, "not an agent/chat model") for m in rejected]
        if not cands:
            raise StageFailure("no_candidates", ", ".join(rejected[:10]))
        cb.stage_done(2, f"{res.discovered}")

        # [4] compatibility ---------------------------------------------
        begin(3)
        to_test = cands[:MAX_MODELS_TO_TEST]
        for m in cands[MAX_MODELS_TO_TEST:]:
            res.rejected.append((m, "not tested (limit)"))
        try:
            probes = [client.test_responses(m) for m in to_test[:3]]
            res.supports_chat = client.probe_chat(to_test[0])
            endpoint_missing = all(not r.compatible and r.http_status in (404, 405, 501)
                                   and not is_model_specific(r.reason) for r in probes)
            res.supports_responses = not endpoint_missing

            def prog(done, total, r):
                cb.progress(3, f"{done}/{total}  {r.id}")

            results: list = []
            if not endpoint_missing:
                results = probes + run_compat_tests(client, to_test[len(probes):],
                                                    lambda d, t, r: prog(d + len(probes), t + len(probes), r))
            if not any(r.compatible for r in results):
                if not res.supports_chat:
                    raise StageFailure("no_responses_api", "")
                # Chat-Completions-only provider: Codex will reach it through the local bridge.
                res.bridged = True
                cb.progress(3, "bridge")
                results = run_compat_tests(client, to_test, prog, bridged=True)
        except ProviderError as e:
            raise StageFailure(e.code, e.detail) from e
        report.results = results
        res.tested = len(results)
        for r in results:
            if not r.compatible:
                res.rejected.append((r.id, r.reason))
        compatible = report.compatible
        if not compatible:
            reasons = "; ".join(f"{r.id}: {r.reason}" for r in results[:5])
            raise StageFailure("no_compatible", reasons)
        res.added = compatible
        res.default_model = pick_default(compatible)
        cb.stage_done(3, f"{len(compatible)}/{len(results)}")

        # [5] Codex ------------------------------------------------------
        begin(4)
        inst = detect_fn()
        res.codex = inst
        res.config_path = str(inst.config_path)
        if not inst.found:
            raise StageFailure("codex_missing", str(inst.home))
        res.provider_id = codex.provider_id_from_name(res.provider_name)
        cb.stage_done(4, inst.cli_version_text or str(inst.home))

        # [6] config -----------------------------------------------------
        begin(5)
        existing = None
        try:
            existing = codex.existing_provider(inst.config_path, res.provider_id)
        except Exception as e:  # noqa: BLE001 - unreadable config: never touch it
            raise StageFailure("config_unreadable", str(e)[:200]) from e
        if existing is not None and not cb.confirm_overwrite(res.provider_id, existing):
            raise StageFailure("overwrite_declined", res.provider_id)
        changes = codex.ChangeSet(config_path=inst.config_path)
        try:
            backup = codex.backup_config(inst, changes)
        except OSError as e:
            raise StageFailure("backup_failed", str(e)) from e
        res.backup_path = str(backup) if backup else ""
        try:
            auth_cmd, auth_base_args = credentials.ensure_auth_helper()
        except OSError as e:
            raise StageFailure("helper_failed", str(e)) from e
        res.codex_base_url = res.base_url
        try:
            port, prev_reg = bridge.register_provider(
                res.provider_id, res.base_url, models=res.added, bridged=res.bridged,
                service_cmd=system.service_command(), name=res.provider_name,
                context={m: report.context[m] for m in res.added if m in report.context})
            changes.on_rollback.append(lambda: bridge.save_registry(prev_reg))
            if res.bridged:
                res.codex_base_url = bridge.local_base_url(port, res.provider_id)
        except OSError as e:
            raise StageFailure("config_write_failed", f"registry: {e}") from e

        # Model picker: Codex's bundled catalog + every Agentsii provider model.
        catalog_file = None
        try:
            owner = catalog.config_catalog_owner(codex.load_config(inst.config_path))
            bundled = catalog.bundled_catalog(inst.catalog_cli) if owner != "user" else None
            if owner == "user":
                res.picker = "user_catalog"
            elif bundled is None:
                res.picker = "unavailable"
            else:
                cat, _ = catalog.build_catalog(bundled, bridge.load_registry()["providers"])
                catalog_file = catalog.catalog_path(inst.home)
                if catalog_file.exists():
                    changes.overwritten_files[catalog_file] = catalog_file.read_bytes()
                else:
                    changes.created_files.append(catalog_file)
                catalog_file.write_text(json.dumps(cat, indent=1, ensure_ascii=False), encoding="utf-8")
                res.picker = "updated"
        except OSError as e:
            log.error("model catalog skipped: %s", e)
            res.picker = "unavailable"
            catalog_file = None
        try:
            res.profiles = codex.apply_provider(
                inst, changes, provider_id=res.provider_id, display_name=res.provider_name,
                base_url=res.codex_base_url, bridged=res.bridged, auth_command=auth_cmd,
                auth_args=[*auth_base_args, "--provider", res.provider_id],
                models=res.added, default_model=res.default_model, catalog_file=catalog_file)
        except Exception as e:  # noqa: BLE001
            raise StageFailure("config_write_failed", str(e)[:200]) from e
        if catalog_file is not None:
            # Let the Codex binary that Desktop actually runs load config + catalog.
            status, detail = catalog.validate(inst.catalog_cli, inst.home, res.added)
            res.picker_check = status
            log.info("catalog validation: %s %s", status, detail)
            if status == "invalid":
                doc = codex.load_config(inst.config_path)
                doc.pop("model_catalog_json", None)
                codex._atomic_write(inst.config_path, tomlkit.dumps(doc))
                catalog_file.unlink(missing_ok=True)
                res.picker = "rejected"
        cb.stage_done(5, res.provider_id)

        # [7] credentials ------------------------------------------------
        begin(6)
        try:
            prev_key = credentials.store_key(res.provider_id, api_key)
            key_written = True
        except CredentialError as e:
            raise StageFailure("credential_failed", str(e)) from e
        res.credential_backend = credentials.backend_name()
        cb.stage_done(6, res.credential_backend)

        # [8] verification -----------------------------------------------
        begin(7)
        problems = codex.verify_config(inst, res.provider_id, res.codex_base_url, res.default_model,
                                       auth_cmd, res.profiles if inst.uses_profile_files else [])
        if problems:
            raise StageFailure("verify_failed", "; ".join(problems))
        # Background service: local bridge + keeps model_provider in sync with the Codex model picker.
        res.service_running = system.ensure_service()
        if res.bridged and not res.service_running:
            raise StageFailure("verify_bridge", "background service did not start")
        try:
            token = credentials.run_auth_helper(auth_cmd, auth_base_args, res.provider_id)
        except Exception as e:  # noqa: BLE001
            raise StageFailure("verify_helper", str(e)[:200]) from e
        if hashlib.sha256(token.encode()).digest() != hashlib.sha256(api_key.encode()).digest():
            raise StageFailure("verify_helper", "token mismatch")
        if res.bridged:
            # Prove Codex's exact path works end to end (Responses -> bridge -> provider).
            bc = ProviderClient(res.codex_base_url, api_key, transport=transport)
            try:
                check = bc.test_responses(res.default_model)
            finally:
                bc.close()
            if not check.compatible:
                raise StageFailure("verify_bridge", check.reason)
        res.autostart = system.set_autostart(True)
        cb.stage_done(7, "ok")
        res.ok = True
        log.info("SUCCESS provider=%s models=%d default=%s", res.provider_id, len(res.added), res.default_model)
        return res

    except StageFailure as f:
        res.ok = False
        res.failed_stage = stage
        res.error_code = f.code
        res.error_detail = f.detail
        log.error("FAILED stage=%d code=%s detail=%s", stage + 1, f.code, f.detail)
        if changes is not None:
            changes.rollback()
        if key_written:
            credentials.restore_key(res.provider_id, prev_key)
        cb.stage_failed(stage, f.code, f.detail)
        return res
    except Exception as e:  # noqa: BLE001 - last line of defence, still roll back
        log.exception("unexpected error")
        res.ok = False
        res.failed_stage = stage
        res.error_code = "unexpected"
        res.error_detail = f"{type(e).__name__}: {e}"[:300]
        if changes is not None:
            changes.rollback()
        if key_written:
            credentials.restore_key(res.provider_id, prev_key)
        cb.stage_failed(stage, res.error_code, res.error_detail)
        return res
    finally:
        if client:
            client.close()
