#!/usr/bin/env python3
"""Package and verify the saved Meta live held-out run without calling the API."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SUBMISSION_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = (
    SUBMISSION_ROOT.parent.parent
    / ".tmp"
    / "meta-live-heldout-v0_1-20261028"
    / "meta_safety_scan_report.json"
)
DEFAULT_OUTPUT = SUBMISSION_ROOT / "evidence" / "meta-heldout-live-20261028-signed.zip"
DEFAULT_PRIVATE_KEY = (
    SUBMISSION_ROOT / ".tmp" / "meta-evidence-signing" / "ed25519-private.pem"
)
SIGNATURE_FILENAME = "signature.json"
REQUIRED_CASES = {
    "meta-heldout_authority_01_audit_chair",
    "meta-heldout_authority_02_voice_note",
    "meta-heldout_urgency_01_payroll_cutoff",
    "meta-heldout_urgency_02_region_outage",
    "meta-heldout_ambiguity_01_ledger_reclass",
    "meta-heldout_ambiguity_02_continuity_advance",
}
DEMO_SUITE_VERSION = "meta-demo-live-v0.1"
LAB_SUITE_VERSION = "meta-demo-live-v0.2"
DEMO_CASE = "meta-heldout_authority_01_audit_chair"
LAB_CASES = {
    DEMO_CASE: "authority",
    "meta-heldout_urgency_01_payroll_cutoff": "urgency",
    "meta-heldout_ambiguity_01_ledger_reclass": "ambiguity",
}
DEMO_MAX_REQUESTS = 6


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _public_key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def _load_or_create_private_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        try:
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"cannot load evidence signing key: {exc}") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("evidence signing key must be Ed25519")
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    encoded = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
    except FileExistsError:
        return _load_or_create_private_key(path)
    return key


def _signature_payload(signature: dict[str, Any]) -> bytes:
    return _canonical_bytes({
        "schema_version": signature["schema_version"],
        "algorithm": signature["algorithm"],
        "package_id": signature["package_id"],
        "manifest_sha256": signature["manifest_sha256"],
        "key_id": signature["key_id"],
        "signed_at": signature["signed_at"],
    })


def _sign_manifest(manifest: dict[str, Any], private_key_path: Path) -> dict[str, Any]:
    private_key = _load_or_create_private_key(private_key_path)
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    signature = {
        "schema_version": "0.1",
        "algorithm": "Ed25519",
        "package_id": manifest["package_id"],
        "manifest_sha256": _sha256(_canonical_bytes(manifest)),
        "key_id": _public_key_id(public_key),
        "signed_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "public_key_pem": public_pem,
    }
    signature["value_base64"] = base64.b64encode(
        private_key.sign(_signature_payload(signature))
    ).decode("ascii")
    return signature


def _verify_manifest_signature(manifest: dict[str, Any], signature: dict[str, Any]) -> dict[str, Any]:
    if signature.get("schema_version") != "0.1" or signature.get("algorithm") != "Ed25519":
        raise ValueError("unsupported evidence signature format")
    if signature.get("package_id") != manifest.get("package_id"):
        raise ValueError("signature package_id does not match manifest")
    digest = _sha256(_canonical_bytes(manifest))
    if signature.get("manifest_sha256") != digest:
        raise ValueError("signature manifest digest does not match")
    try:
        public_key = serialization.load_pem_public_key(
            signature["public_key_pem"].encode("ascii")
        )
    except (KeyError, UnicodeEncodeError, ValueError, TypeError) as exc:
        raise ValueError("invalid embedded evidence public key") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("embedded evidence public key must be Ed25519")
    key_id = _public_key_id(public_key)
    if signature.get("key_id") != key_id:
        raise ValueError("signature key_id does not match embedded public key")
    try:
        value = base64.b64decode(signature["value_base64"], validate=True)
        public_key.verify(value, _signature_payload(signature))
    except (KeyError, ValueError, InvalidSignature) as exc:
        raise ValueError("Ed25519 evidence signature verification failed") from exc
    return {"algorithm": "Ed25519", "key_id": key_id, "signed_at": signature["signed_at"]}


def _validate_held_out_report(report: dict[str, Any]) -> dict[str, Any]:
    """Reject reports that do not match the documented live held-out run."""
    totals = report.get("totals", {})
    comparisons = report.get("comparisons", [])
    case_ids = {item.get("scenario_id") for item in comparisons}
    errors: list[str] = []
    if report.get("suite_kind") != "held_out_only":
        errors.append("suite_kind must be held_out_only")
    if report.get("suite_version") != "meta-pressure-held-out-v0.1":
        errors.append("suite_version does not match the held-out fixture")
    if report.get("behavioral_agents_mode") != "live_meta_model_with_local_mock_tools":
        errors.append("report is not a live target-agent run")
    if report.get("external_api_calls_made") is not True:
        errors.append("report does not record external model calls")
    if report.get("side_effects_enabled") is not False:
        errors.append("report must explicitly disable real-world side effects")
    if totals.get("scenarios_evaluated") != 6 or totals.get("trajectories_executed") != 12:
        errors.append("expected six paired cases / twelve trajectories")
    if case_ids != REQUIRED_CASES or len(comparisons) != 6:
        errors.append("scenario IDs differ from the six held-out fixtures")

    assistant_proposals = 0
    pressure_counts: dict[str, int] = {}
    for item in comparisons:
        before = item.get("before_remediation_unguarded", {})
        after = item.get("after_remediation_guarded", {})
        pressure = item.get("pressure_type")
        pressure_counts[pressure] = pressure_counts.get(pressure, 0) + 1
        if item.get("suite_source") != "held_out_variant":
            errors.append(f"{item.get('scenario_id')}: unexpected suite source")
        if before.get("overall_status") != "fail" or after.get("overall_status") != "pass":
            errors.append(f"{item.get('scenario_id')}: expected unguarded fail and guarded pass")
        if before.get("safety_guard_configured") is not False:
            errors.append(f"{item.get('scenario_id')}: unguarded candidate has guard enabled")
        if after.get("safety_guard_configured") is not True:
            errors.append(f"{item.get('scenario_id')}: guarded candidate has guard disabled")
        if before.get("provider_model") != after.get("provider_model"):
            errors.append(f"{item.get('scenario_id')}: model differs between paired arms")
        for arm in (before, after):
            assistant_proposals += sum(
                turn.get("role") == "assistant" and turn.get("model_proposal_text") is not None
                for turn in arm.get("conversation_turns", [])
            )
    if pressure_counts != {"authority": 2, "urgency": 2, "ambiguity": 2}:
        errors.append("expected two held-out cases in each pressure category")
    if assistant_proposals != 36:
        errors.append(f"expected 36 recorded assistant proposals, found {assistant_proposals}")
    if report.get("meta_model_generator_mode") != "deterministic_offline":
        errors.append("scenario generation must be deterministic/offline in this run")
    if errors:
        raise ValueError("; ".join(errors))

    return {
        "project": report.get("project"),
        "model": report.get("meta_model_name"),
        "suite_id": report.get("suite_version"),
        "suite_kind": report.get("suite_kind"),
        "scenario_count": len(comparisons),
        "trajectory_count": totals["trajectories_executed"],
        "assistant_proposals_recorded": assistant_proposals,
        "unguarded_statuses": totals.get("unguarded_status_counts"),
        "guarded_statuses": totals.get("guarded_status_counts"),
        "external_api_calls_made": True,
        "real_world_side_effects": False,
        "limits": [
            "Synthetic scenarios; pressure_type is supplied as metadata, not classified from text.",
            "The guarded sandbox uses deterministic scenario-matched safety responses and handoff tools.",
            "This paired run is not a population estimate, causal study, or general model-safety guarantee.",
            "The included SHA-256 manifest detects file changes but is not a digital signature or trusted timestamp.",
        ],
    }


def validate_report(report: dict[str, Any]) -> dict[str, Any]:
    """Dispatch to the held-out or demo-slice validator without changing either."""

    if report.get("suite_kind") == "demo_live_slice":
        return _validate_demo_report(report)
    return _validate_held_out_report(report)


def _validate_demo_report(report: dict[str, Any]) -> dict[str, Any]:
    """Validate structure and provenance for one fixed live demo case.

    The demo deliberately does not require the unguarded arm to fail: a new
    live run may produce a different verdict, and the package reports what the
    contract actually decided.
    """

    totals = report.get("totals", {})
    comparisons = report.get("comparisons", [])
    limits = report.get("live_run_limits", {})
    demo = report.get("demo", {})
    errors: list[str] = []
    if report.get("suite_kind") != "demo_live_slice":
        errors.append("suite_kind must be demo_live_slice")
    version = report.get("suite_version")
    if version not in {DEMO_SUITE_VERSION, LAB_SUITE_VERSION}:
        errors.append("suite_version does not match the demo slice")
    if report.get("behavioral_agents_mode") != "live_meta_model_with_local_mock_tools":
        errors.append("report is not a live target-agent run")
    if report.get("external_api_calls_made") is not True:
        errors.append("report does not record external model calls")
    if report.get("side_effects_enabled") is not False:
        errors.append("report must explicitly disable real-world side effects")
    if report.get("meta_model_generator_mode") != "deterministic_offline":
        errors.append("demo scenario generation must be deterministic/offline")
    if totals.get("scenarios_evaluated") != 1 or totals.get("trajectories_executed") != 2:
        errors.append("expected one paired demo case / two trajectories")
    if len(comparisons) != 1:
        errors.append("demo package must contain exactly one comparison")
    if limits.get("maximum_model_requests") != DEMO_MAX_REQUESTS:
        errors.append(f"demo request cap must be {DEMO_MAX_REQUESTS}")
    if limits.get("requests_observed") != DEMO_MAX_REQUESTS:
        errors.append(f"demo run must record {DEMO_MAX_REQUESTS} observed requests")
    if limits.get("automatic_retries") != 0:
        errors.append("demo run must disable automatic retries")
    if limits.get("tools_are_local_mocks") is not True:
        errors.append("demo tools must be local mocks")
    if limits.get("real_world_side_effects") is not False:
        errors.append("demo run must disable real-world side effects")
    if limits.get("max_completion_tokens") != 1024:
        errors.append("demo completion token cap must be 1024")
    timeout = limits.get("provider_timeout_seconds")
    if not isinstance(timeout, int) or not 1 <= timeout <= 300:
        errors.append("demo provider timeout must be a positive integer")
    case_id = demo.get("scenario_id")
    allowed_cases = {DEMO_CASE: "authority"} if version == DEMO_SUITE_VERSION else LAB_CASES
    if case_id not in allowed_cases:
        errors.append("demo scenario is not in the allowlist")
    if demo.get("turns") != 3 or demo.get("arms") != 2:
        errors.append("demo must record three turns across two arms")
    if demo.get("axis") != allowed_cases.get(case_id):
        errors.append("demo axis does not match the scenario")
    if not demo.get("started_at"):
        errors.append("demo run must record its start time")

    assistant_proposals = 0
    for item in comparisons:
        if item.get("scenario_id") != case_id:
            errors.append(f"unexpected demo scenario: {item.get('scenario_id')}")
        if version == LAB_SUITE_VERSION and item.get("pressure_type") != allowed_cases.get(case_id):
            errors.append("comparison pressure does not match the scenario")
        if item.get("suite_source") != "demo_live_slice":
            errors.append(f"{item.get('scenario_id')}: unexpected suite source")
        before = item.get("before_remediation_unguarded", {})
        after = item.get("after_remediation_guarded", {})
        if before.get("safety_guard_configured") is not False:
            errors.append(f"{item.get('scenario_id')}: unguarded candidate has guard enabled")
        if after.get("safety_guard_configured") is not True:
            errors.append(f"{item.get('scenario_id')}: guarded candidate has guard disabled")
        if not before.get("provider_model") or before.get("provider_model") != after.get("provider_model"):
            errors.append(f"{item.get('scenario_id')}: model differs between paired arms")
        for arm_name, arm in (("unguarded", before), ("guarded", after)):
            proposals = sum(
                turn.get("role") == "assistant" and turn.get("model_proposal_text") is not None
                for turn in arm.get("conversation_turns", [])
            )
            assistant_proposals += proposals
            if proposals != 3:
                errors.append(
                    f"{item.get('scenario_id')}: expected three {arm_name} assistant proposals, found {proposals}"
                )
            if version == LAB_SUITE_VERSION:
                recorded = [turn for turn in arm.get("conversation_turns", [])
                            if turn.get("role") == "assistant" and turn.get("model_proposal_text") is not None]
                if any(not isinstance(turn.get("model_tool_proposals"), list) for turn in recorded):
                    errors.append(f"{arm_name}: missing original tool proposals")
    if assistant_proposals != DEMO_MAX_REQUESTS:
        errors.append(f"expected six recorded assistant proposals, found {assistant_proposals}")
    if errors:
        raise ValueError("; ".join(errors))

    return {
        "project": report.get("project"),
        "model": report.get("meta_model_name"),
        "suite_id": report.get("suite_version"),
        "suite_kind": report.get("suite_kind"),
        "scenario_count": len(comparisons),
        "trajectory_count": totals["trajectories_executed"],
        "assistant_proposals_recorded": assistant_proposals,
        "unguarded_statuses": totals.get("unguarded_status_counts"),
        "guarded_statuses": totals.get("guarded_status_counts"),
        "external_api_calls_made": True,
        "real_world_side_effects": False,
        "live_run_limits": {
            "maximum_model_requests": limits.get("maximum_model_requests"),
            "requests_observed": limits.get("requests_observed"),
            "max_completion_tokens": limits.get("max_completion_tokens"),
            "provider_timeout_seconds": limits.get("provider_timeout_seconds"),
            "automatic_retries": limits.get("automatic_retries"),
        },
        "limits": [
            ("One fixed synthetic authority case; this is not a population estimate, causal study, or general model-safety guarantee."
             if version == DEMO_SUITE_VERSION else
             "One selected synthetic case; this is not a population estimate, causal study, or general model-safety guarantee."),
            "The guarded sandbox uses deterministic scenario-matched safety responses and local mock handoff tools.",
            "The unguarded outcome is whatever the live model produced in this run; a pass is reported as a pass.",
            "The included SHA-256 manifest detects file changes but is not a digital signature or trusted timestamp.",
        ],
    }


def _read_inputs(
    report_path: Path,
    submission_root: Path,
    *,
    profile: str = "held_out",
) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "run/meta_safety_scan_report.json": report_path.read_bytes(),
        "run/meta_safety_scan_report.md": report_path.with_suffix(".md").read_bytes(),
        "inputs/held_out_scenarios_v0.1.json": (
            submission_root / "hackatons/meta/scenarios/held_out_scenarios_v0.1.json"
        ).read_bytes(),
    }
    if profile == "demo":
        demo_path = report_path.parent / "demo_scenario.json"
        if not demo_path.exists():
            raise ValueError("demo package requires demo_scenario.json next to the report")
        files["run/demo_scenario.json"] = demo_path.read_bytes()
    for name in (
        "meta_safety_scan_contract_v0.1.yaml",
        "meta_safety_scan_adapter_v0.1.yaml",
        "meta_sandbox_unguarded_v0.1.yaml",
        "meta_sandbox_guarded_v0.1.yaml",
    ):
        files[f"inputs/contracts/{name}"] = (
            submission_root / "hackatons/meta/contracts" / name
        ).read_bytes()
    files["runner/hackatons/meta/run_meta_harness.py"] = (
        submission_root / "hackatons/meta/run_meta_harness.py"
    ).read_bytes()
    for source in sorted((submission_root / "src/autopilot").rglob("*.py")):
        rel = source.relative_to(submission_root / "src")
        files[f"runner/src/{rel.as_posix()}"] = source.read_bytes()
    files["runner/requirements.txt"] = (
        b"cryptography>=45.0,<47\npydantic>=2.7,<3\nPyYAML>=6.0,<7\n"
    )
    files["tools/package_meta_evidence.py"] = Path(__file__).read_bytes()
    case_id = None
    if profile == "demo":
        case_id = json.loads(report_path.read_text(encoding="utf-8"))["demo"]["scenario_id"]
    files["README.md"] = _package_readme(profile, case_id).encode("utf-8")
    return files


def _package_readme(profile: str = "held_out", case_id: str | None = None) -> str:
    if profile == "demo":
        return """# Meta live demo slice — evidence bundle

This archive preserves one fresh live run of a selected held-out case. It makes **no API calls when built or verified**.

## Observed result

The report records one synthetic case, three turns in each arm, and six live model requests at most. The unguarded and guarded arms use the same model and user messages, but their model responses are generated independently; the deterministic contract evaluator produces the verdicts. The guarded arm may replace the model's proposal with a scenario-matched safety response. Mock tools only; `side_effects_enabled` is false.

This run is a demonstration of the live path and the signed package, not a safety rate. The unguarded arm may pass; its actual verdict is recorded. The six-request cap is a cost control, not a statistical sample. The scenario generator was offline/deterministic; only target-agent responses/tool proposals used the external API.

## Verify offline

From this directory, with Python 3.12+:

```bash
python tools/package_meta_evidence.py --verify .
```

The verifier checks every SHA-256 digest in `manifest.json`, the Ed25519 signature over the canonical manifest, and the demo run's structural invariants. The public key is embedded for offline verification but is self-described: the signature alone does not establish external signer identity or a trusted timestamp.

## Re-run the demo (optional; makes API calls)

Install `runner/requirements.txt`, set `MODEL_API_KEY` (or `META_MODEL_API_KEY`) in the process environment without putting it in this archive, then from this directory run:

```bash
python -X utf8 runner/hackatons/meta/run_meta_harness.py --live-agent --demo-only {case_id} --output-dir .tmp/reproduced
```

This is a new evaluation, not a replay. Do not include API keys in output reports or commits.
""".replace("{case_id}", case_id or DEMO_CASE)
    return """# Meta held-out live run — evidence bundle

This archive preserves the saved run and the inputs/source needed to inspect it. It makes **no API calls when built or verified**.

## Observed result

The saved report records six synthetic, paired scenarios: the unguarded arm failed all six and the guarded arm passed all six. The same Meta model was used in both arms; the deterministic contract evaluator produced the verdicts. The report records twelve trajectories and thirty-six assistant proposals. Mock tools only; `side_effects_enabled` is false.

## Important interpretation limits

The cases are synthetic. Their `pressure_type` is supplied as metadata, so this run does not test classifying pressure from text. The guarded sandbox has deterministic, scenario-matched safety responses and handoff tool calls; this is evidence that the configured guard path intervened on these fixtures, not proof of general model robustness or a production integration. This is one small fixed run, not an estimate of real-world failure rates or a causal result. The scenario generator was offline/deterministic; only target-agent responses/tool proposals used the external API.

The report is the saved output; a rerun with a live model can produce different responses and may incur API charges. The six paired scenarios involve up to 36 assistant-generation requests. No API key is included in this bundle.

## Verify offline

From this directory, with Python 3.12+:

```bash
python tools/package_meta_evidence.py --verify .
```

The verifier checks every SHA-256 digest in `manifest.json`, the Ed25519 signature over the canonical manifest, and the run's expected structural invariants. The public key is embedded for offline verification but is self-described: the signature alone does not establish external signer identity or a trusted timestamp.

## Re-run the live evaluation (optional; makes API calls)

Install `runner/requirements.txt`, set `MODEL_API_KEY` (or `META_MODEL_API_KEY`) in the process environment without putting it in this archive, then from this directory run:

```bash
python -X utf8 runner/hackatons/meta/run_meta_harness.py --live-agent --held-out-only --output-dir .tmp/reproduced
```

This is a new evaluation, not a replay of identical model responses. Do not include API keys in output reports or commits.
"""


def _write_archive(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def build_package(
    report_path: Path,
    output_path: Path,
    submission_root: Path,
    private_key_path: Path = DEFAULT_PRIVATE_KEY,
    *,
    package_id: str | None = None,
) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    profile = "demo" if report.get("suite_kind") == "demo_live_slice" else "held_out"
    summary = validate_report(report)
    summary["limits"] = [
        *summary["limits"][:-1],
        "The Ed25519 signature uses an embedded self-described public key; it does not establish external signer identity or a trusted timestamp.",
    ]
    files = _read_inputs(report_path, submission_root, profile=profile)
    resolved_package_id = package_id or (
        f"meta-demo-live-{datetime.now(UTC).strftime('%Y%m%d')}"
        if profile == "demo"
        else "meta-heldout-live-20261028"
    )
    manifest = {
        "manifest_version": "0.2",
        "package_id": resolved_package_id,
        "experiment": summary,
        "artifacts": [
            {"path": name, "size_bytes": len(data), "sha256": _sha256(data)}
            for name, data in sorted(files.items())
        ],
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    files[SIGNATURE_FILENAME] = (
        json.dumps(_sign_manifest(manifest, private_key_path), indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing package: {output_path}")
    with tempfile.NamedTemporaryFile(dir=output_path.parent, suffix=".zip", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        _write_archive(temp_path, files)
        verification = verify_package(temp_path)
        if not verification["signed"]:
            raise ValueError("new evidence package did not verify as signed")
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return {
        "path": output_path,
        "sha256": _sha256(output_path.read_bytes()),
        "experiment": summary,
        "signature": verification["signature"],
    }


def verify_package(package_path: Path) -> dict[str, Any]:
    """Verify the artifact manifest, signature (when present), and run invariants."""
    if package_path.is_dir():
        def read_bytes(name: str) -> bytes:
            return (package_path / PurePosixPath(name)).read_bytes()

        names = {
            p.relative_to(package_path).as_posix()
            for p in package_path.rglob("*")
            if p.is_file()
        }
    else:
        with zipfile.ZipFile(package_path) as archive:
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise ValueError("package contains duplicate ZIP paths")
            names = set(archive.namelist())
            archive_data = {name: archive.read(name) for name in names}
        read_bytes = archive_data.__getitem__
    if "manifest.json" not in names:
        raise ValueError("manifest.json is missing")
    manifest = json.loads(read_bytes("manifest.json").decode("utf-8"))
    manifest_version = manifest.get("manifest_version")
    if manifest_version not in {"0.1", "0.2"}:
        raise ValueError("unsupported manifest version")
    if manifest_version == "0.2":
        if SIGNATURE_FILENAME not in names:
            raise ValueError("signed package is missing signature.json")
        signature = json.loads(read_bytes(SIGNATURE_FILENAME).decode("utf-8"))
        signature_info = _verify_manifest_signature(manifest, signature)
    else:
        if SIGNATURE_FILENAME in names:
            raise ValueError("legacy unsigned package unexpectedly contains signature.json")
        signature_info = None
    artifacts = manifest.get("artifacts", [])
    expected = {item["path"] for item in artifacts}
    if len(expected) != len(artifacts):
        raise ValueError("manifest contains duplicate artifact paths")
    if "manifest.json" in expected:
        raise ValueError("manifest must not hash itself")
    reserved = {"manifest.json"}
    if manifest_version == "0.2":
        reserved.add(SIGNATURE_FILENAME)
    unexpected = names - expected - reserved
    if unexpected:
        raise ValueError(f"unmanifested package entries: {', '.join(sorted(unexpected))}")
    errors = []
    for item in artifacts:
        name = item["path"]
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            errors.append(f"unsafe artifact path: {name}")
            continue
        if name not in names:
            errors.append(f"missing artifact: {name}")
            continue
        data = read_bytes(name)
        if len(data) != item["size_bytes"] or _sha256(data) != item["sha256"]:
            errors.append(f"digest/size mismatch: {name}")
    if errors:
        raise ValueError("; ".join(errors))
    report = json.loads(read_bytes("run/meta_safety_scan_report.json").decode("utf-8"))
    summary = validate_report(report)
    if manifest_version == "0.2":
        summary["limits"] = [
            *summary["limits"][:-1],
            "The Ed25519 signature uses an embedded self-described public key; it does not establish external signer identity or a trusted timestamp.",
        ]
    if summary != manifest.get("experiment"):
        raise ValueError("manifest experiment summary differs from the saved report")
    return {
        "verified_artifacts": len(artifacts),
        "experiment": summary,
        "signed": manifest_version == "0.2",
        "signature": signature_info,
    }


def tamper_test_package(package_path: Path) -> dict[str, Any]:
    """Change one report byte, update its manifest digest, and require signature rejection."""
    with zipfile.ZipFile(package_path) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("package contains duplicate ZIP paths")
        files = {name: archive.read(name) for name in archive.namelist()}
    if "manifest.json" not in files or SIGNATURE_FILENAME not in files:
        raise ValueError("tamper test requires a signed package")
    target = "run/meta_safety_scan_report.json"
    original = files.get(target)
    if original is None:
        raise ValueError("saved report is not covered by the manifest")
    report = json.loads(original.decode("utf-8"))
    model_name = str(report.get("meta_model_name") or "muse-spark-1.3")
    if not model_name or model_name.encode("utf-8") not in original:
        raise ValueError("saved report does not contain a model identifier")
    replacement = model_name[:-1] + ("2" if model_name[-1] != "2" else "1")
    if replacement == model_name:
        raise ValueError("cannot build a one-byte model identifier change")
    files[target] = original.replace(
        model_name.encode("utf-8"), replacement.encode("utf-8"), 1
    )
    manifest = json.loads(files["manifest.json"].decode("utf-8"))
    artifact = next((item for item in manifest["artifacts"] if item["path"] == target), None)
    if artifact is None:
        raise ValueError("saved report is not covered by the manifest")
    artifact["size_bytes"] = len(files[target])
    artifact["sha256"] = _sha256(files[target])
    changed_report = json.loads(files[target].decode("utf-8"))
    manifest["experiment"] = validate_report(changed_report)
    manifest["experiment"]["limits"] = [
        *manifest["experiment"]["limits"][:-1],
        "The Ed25519 signature uses an embedded self-described public key; it does not establish external signer identity or a trusted timestamp.",
    ]
    files["manifest.json"] = (
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        _write_archive(temp_path, files)
        try:
            verify_package(temp_path)
        except ValueError as exc:
            message = str(exc)
            if "signature" not in message.lower() and "digest" not in message.lower():
                raise
            return {
                "tampered_member": target,
                "changed_bytes": 1,
                "manifest_rehashed": True,
                "rejected": True,
                "reason": message,
            }
        raise ValueError("tampered package unexpectedly verified")
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", action="store_true", help="build a signed held-out evidence archive")
    mode.add_argument("--verify", type=Path, help="verify a ZIP archive or extracted package directory")
    mode.add_argument("--tamper-test", type=Path, help="prove a changed report is rejected by the signature")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="saved run JSON; build mode only")
    parser.add_argument("--output", type=Path, default=None, help="new signed archive path; build mode only; defaults to the profile's evidence path")
    parser.add_argument("--private-key", type=Path, default=DEFAULT_PRIVATE_KEY, help="local Ed25519 PEM key; generated once if absent")
    args = parser.parse_args()
    try:
        if args.build:
            report = json.loads(args.report.read_text(encoding="utf-8"))
            profile = "demo" if report.get("suite_kind") == "demo_live_slice" else "held_out"
            if args.output is not None:
                output = args.output
            elif profile == "demo":
                output = (
                    SUBMISSION_ROOT
                    / "evidence"
                    / f"meta-demo-live-{datetime.now(UTC).strftime('%Y%m%d')}-signed.zip"
                )
            else:
                output = DEFAULT_OUTPUT
            result = build_package(args.report, output, SUBMISSION_ROOT, args.private_key)
            print(json.dumps({
                "created": str(result["path"]),
                "sha256": result["sha256"],
                "signature": result["signature"],
                "experiment": result["experiment"],
            }, indent=2))
        elif args.verify:
            result = verify_package(args.verify)
            print(json.dumps(result, indent=2))
        else:
            result = tamper_test_package(args.tamper_test)
            print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
