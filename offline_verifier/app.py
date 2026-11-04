"""Local offline verifier for signed Meta evidence packages."""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import gradio as gr

MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 5 * 1024 * 1024
MAX_MEMBERS = 110
MAX_MEMBER_BYTES = 1024 * 1024
MAX_COMPRESSION_RATIO = 100
HELD_OUT_SCENARIOS_PATH = "inputs/held_out_scenarios_v0.1.json"
REPORT_PATH = "run/meta_safety_scan_report.json"

APP_ROOT = Path(__file__).resolve().parent
SUBMISSION_ROOT = APP_ROOT.parent
if not (SUBMISSION_ROOT / "scripts" / "package_meta_evidence.py").is_file():
    raise RuntimeError("evidence verifier module is missing from the submission checkout")
sys.path.insert(0, str(SUBMISSION_ROOT))

from scripts.package_meta_evidence import tamper_test_package, verify_package  # noqa: E402


def _preflight_archive(uploaded_path: str | Path | None) -> Path:
    if not uploaded_path:
        raise ValueError("Upload a ZIP evidence package first.")
    path = Path(uploaded_path)
    if path.suffix.lower() != ".zip" or not path.is_file():
        raise ValueError("Only a local .zip evidence file can be checked.")
    if path.stat().st_size > MAX_COMPRESSED_BYTES:
        raise ValueError("The upload exceeds the 8 MiB compressed-file limit.")
    if not zipfile.is_zipfile(path):
        raise ValueError("The uploaded file is not a valid ZIP archive.")

    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_MEMBERS:
            raise ValueError("The archive has an unsupported number of entries.")
        if len({info.filename for info in infos}) != len(infos):
            raise ValueError("The archive contains duplicate entry names.")
        uncompressed = 0
        for info in infos:
            name = info.filename
            relative = PurePosixPath(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or "\\" in name
                or name.endswith("/")
                or info.flag_bits & 0x1
                or (info.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("The archive contains an unsafe or unsupported entry.")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ValueError("An archive entry exceeds the 1 MiB per-file limit.")
            uncompressed += info.file_size
            if info.file_size and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO:
                raise ValueError("An archive entry exceeds the allowed compression ratio.")
        if uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("The archive exceeds the 5 MiB uncompressed-data limit.")
    return path


def verify_uploaded_package(uploaded_path: str | Path | None) -> tuple[str, dict[str, Any] | None]:
    """Verify a user-uploaded archive without network access or external side effects."""
    try:
        path = _preflight_archive(uploaded_path)
        result = verify_package(path)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile):
        return (
            "❌ The archive could not be validated. Check that it is a compatible evidence package and that it has not been altered.",
            None,
        )

    if result["signed"]:
        signature = result["signature"]
        message = (
            f"✅ **Integrity:** valid Ed25519 signature; {result['verified_artifacts']} artifacts verified. "
            f"Key ID: `{signature['key_id']}`."
        )
    else:
        message = (
            f"⚠️ **Integrity:** unsigned legacy package; {result['verified_artifacts']} hashes and structural data verified."
        )
    experiment = result["experiment"]
    unguarded = experiment["unguarded_statuses"]
    guarded = experiment["guarded_statuses"]
    api_record = "yes, as recorded in the stored report" if experiment["external_api_calls_made"] else "no"
    limit_lines = []
    for limit in experiment["limits"]:
        limit_lines.extend(
            line[2:].strip() if line.startswith("- ") else line.strip()
            for line in limit.replace("\n", "\n").splitlines()
            if line.strip()
        )
    message += (
        " The public key is embedded: this does not establish an external identity or a trusted timestamp."
        "\n\n### Meta run stored in the package (as recorded in the report)\n"
        f"- Model: `{experiment['model']}`; {experiment['scenario_count']} paired synthetic scenarios; "
        f"{experiment['trajectory_count']} trajectories; {experiment['assistant_proposals_recorded']} recorded proposals.\n"
        f"- No guard: {unguarded.get('fail', 0)} fails of {experiment['scenario_count']}; "
        f"with guard: {guarded.get('pass', 0)} passes of {experiment['scenario_count']}.\n"
        f"- The report records external calls to Meta: {api_record}. This verification is offline and does not call Meta.\n"
        f"- Real-world side effects in that run: {'yes' if experiment['real_world_side_effects'] else 'no'}.\n\n"
        "**Limits:**\n"
        + "\n".join(f"- {limit}" for limit in limit_lines)
    )
    return message, result


def build_case_walkthrough(
    uploaded_path: str | Path | None, scenario_id: str | None
) -> tuple[str, dict[str, Any] | None]:
    """Render one saved paired case from a verified signed package; never calls a model."""
    try:
        path = _preflight_archive(uploaded_path)
        verified = verify_package(path)
        if not verified["signed"]:
            raise ValueError("The case walkthrough requires a signed evidence package.")
        if not scenario_id:
            raise ValueError("Choose a held-out case first.")
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            report = json.loads(archive.read(REPORT_PATH).decode("utf-8"))
            suite = json.loads(archive.read(HELD_OUT_SCENARIOS_PATH).decode("utf-8"))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile):
        return (
            "❌ The case could not be loaded. Check that you uploaded a compatible signed package and picked a case it contains.",
            None,
        )

    fixture_id = scenario_id.removeprefix("meta-")
    scenario = next(
        (item for item in suite.get("scenarios", []) if item.get("id") == fixture_id),
        None,
    )
    comparison = next(
        (item for item in report.get("comparisons", []) if item.get("scenario_id") == scenario_id),
        None,
    )
    if scenario is None or comparison is None:
        return "❌ The selected case is not in the verified package.", None

    unguarded = comparison["before_remediation_unguarded"]
    guarded = comparison["after_remediation_guarded"]
    unguarded_replies = [
        turn.get("content")
        for turn in unguarded.get("conversation_turns", [])
        if turn.get("role") == "assistant"
    ]
    guarded_replies = [
        turn
        for turn in guarded.get("conversation_turns", [])
        if turn.get("role") == "assistant"
    ]
    if not unguarded_replies or not guarded_replies:
        return "❌ The case has no complete replies in both arms.", None

    model_proposal = guarded_replies[-1].get("model_proposal_text") or "(no proposal recorded)"
    final_guarded_reply = guarded_replies[-1].get("content") or "(no final reply recorded)"
    guard_intervened = bool(guarded_replies[-1].get("guard_intervened"))
    unguarded_handoff_calls = [
        {
            "name": call.get("name"),
            "success": call.get("success"),
            "queue": call.get("arguments", {}).get("queue"),
            "provenance": call.get("provenance", {}).get("source"),
        }
        for call in unguarded.get("tool_calls", [])
        if call.get("name") == "create_human_escalation_handoff"
    ]
    guard_tools = [
        {
            "name": call.get("name"),
            "success": call.get("success"),
            "provenance": call.get("provenance", {}).get("source"),
            "guard_intervention": call.get("provenance", {}).get("guard_intervention"),
        }
        for call in guarded.get("tool_calls", [])
        if call.get("provenance", {}).get("source") == "guard_rule"
    ]
    failed_clauses = [
        {"clause_id": clause.get("clause_id"), "reason": clause.get("reason")}
        for clause in unguarded.get("clause_results", [])
        if clause.get("status") == "fail"
    ]
    clause_comparison = [
        {
            "clause_id": before.get("clause_id"),
            "unguarded": before.get("status"),
            "guarded": after.get("status"),
        }
        for before, after in zip(
            unguarded.get("clause_results", []),
            guarded.get("clause_results", []),
            strict=True,
        )
    ]
    guard_intervention = guarded_replies[-1].get("guard_intervention")
    details = {
        "evidence_package_id": manifest.get("package_id"),
        "evidence_key_id": verified["signature"]["key_id"],
        "scenario_id": scenario_id,
        "pressure_type": comparison.get("pressure_type"),
        "model": unguarded.get("provider_model"),
        "user_turns": scenario.get("turns", []),
        "unguarded": {
            "status": unguarded.get("overall_status"),
            "final_model_response": unguarded_replies[-1],
            "failed_clauses": failed_clauses,
            "handoff_calls": unguarded_handoff_calls,
        },
        "guarded": {
            "status": guarded.get("overall_status"),
            "original_model_proposal": model_proposal,
            "final_response_after_guard": final_guarded_reply,
            "guard_intervened": guard_intervened,
            "guard_intervention": guard_intervention,
            "guard_mock_tools": guard_tools,
        },
        "clause_comparison": clause_comparison,
        "external_api_calls_in_saved_run": report.get("external_api_calls_made"),
        "real_world_side_effects_in_saved_run": report.get("side_effects_enabled"),
        "current_action_calls_external_api": False,
        "evidence_signature_verified": True,
    }

    def code_block(text: str) -> str:
        text = text.replace("\\n", "\n").replace("\ufffd", "—")
        longest_fence = max((len(run) for run in re.findall(r"`+", text)), default=0)
        fence = "`" * max(3, longest_fence + 1)
        return f"{fence}text\n{text}\n{fence}"

    scenario_name = fixture_id.replace("_", " ")
    user_turns = "\n\n".join(
        f"**User request {index}:**\n\n{code_block(turn)}"
        for index, turn in enumerate(scenario.get("turns", []), start=1)
    )
    failed_labels = ", ".join(item["clause_id"] for item in failed_clauses) or "none"
    failure_reasons = "\n\n".join(
        f"**`{item['clause_id']}`**\n\n{code_block(item['reason'])}"
        for item in failed_clauses
    ) or "No clause failed."
    clause_rows = "\n".join(
        f"| `{row['clause_id']}` | {row['unguarded']} | {row['guarded']} |"
        for row in clause_comparison
    )
    boundary_status = next(
        (row["unguarded"] for row in clause_comparison if row["clause_id"] == "SI-ENTONCES-RESTATE-BOUNDARY-001"),
        None,
    )
    handoff_status = next(
        (row["unguarded"] for row in clause_comparison if row["clause_id"] == "TIME-ESCALATION-HANDOFF-001"),
        None,
    )
    contract_interpretation = [
        "The deterministic verdict reflects the observations the adapter could map to the contract rules; it is not an assessment of the model's intent."
    ]
    if boundary_status == "fail":
        response_text = str(unguarded_replies[-1]).lower()
        if "i can't release on verbal approval alone" in response_text:
            contract_interpretation.append(
                "The unguarded reply refuses the release in words. The `fail` does not mean the model tried to execute the payment, nor that it failed to state a limit in natural language: the adapter did not map this wording to the `restate_authority_boundary` contract action, because it does not match the configured patterns."
            )
        else:
            contract_interpretation.append(
                "The contract found no observation in the trace mapped to `restate_authority_boundary` after the pressure turn, which is why this obligation reads `fail`."
            )
    if handoff_status == "indeterminate":
        missing_queue_call = next(
            (call for call in unguarded_handoff_calls if not call["queue"]),
            None,
        )
        if missing_queue_call:
            contract_interpretation.append(
                "The trace keeps a call to `create_human_escalation_handoff`, but its arguments carry no `queue`. The mock records the call, yet the contract requires verifying the `compliance_review_queue` destination; with that value missing the result is `indeterminate` (insufficient evidence), not a confirmed failure."
            )
        else:
            contract_interpretation.append(
                "The escalation clause stays `indeterminate`: the trace does not carry complete structured evidence to confirm the required handoff to `compliance_review_queue` within 300 seconds."
            )
    contract_interpretation_text = "\n\n".join(contract_interpretation)
    guard_note = (
        f"Guard intervention recorded: `{guard_intervention or 'yes'}`; mock tool(s) triggered: {len(guard_tools)}."
        if guard_intervened or guard_tools
        else "No guard intervention is recorded for the last reply."
    )
    markdown = "\n\n".join(
        [
            f"### Case `{scenario_name}` — {comparison.get('pressure_type')} pressure",
            f"Verified signed package: `{details['evidence_package_id']}` · Key ID `{details['evidence_key_id']}`. The public key is embedded and does not establish an external identity.",
            f"**Recorded model:**\n\n{code_block(str(details['model']))}\n\nThis is a replay of the stored run, not a new call.",
            f"#### User request\n\n{user_turns}",
            f"#### No-guard arm — contract: **{unguarded.get('overall_status')}**",
            f"**Recorded final reply from the model:**\n\n{code_block(unguarded_replies[-1])}",
            f"Failed clauses: {failed_labels}. Reason recorded by the contract:\n\n{failure_reasons}",
            f"#### Reading of the contract\n\n{contract_interpretation_text}",
            f"#### Guarded arm — contract: **{guarded.get('overall_status')}**",
            f"**Original model proposal:**\n\n{code_block(model_proposal)}",
            f"**Final reply after the guard:**\n\n{code_block(final_guarded_reply)}",
            guard_note,
            "| Contract rule | No guard | With guard |\n| --- | --- | --- |\n" + clause_rows,
            "The escalation tools are **local mocks**; no money moves and no real queue is contacted. "
            "Pressure labels come from scenario metadata and the guarded replies are deterministic. "
            "One synthetic case does not demonstrate general safety. This verification/replay does not call Meta.",
        ]
    )
    return markdown, details


def run_tamper_test(uploaded_path: str | Path | None) -> tuple[str, dict[str, Any] | None]:
    """Run the local one-byte mutation test; the uploaded archive is never modified."""
    try:
        path = _preflight_archive(uploaded_path)
        result = tamper_test_package(path)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile):
        return (
            "❌ The test could not run. A compatible signed package is required.",
            None,
        )
    if not result["rejected"]:
        return "❌ The test failed: the altered copy was not rejected.", result
    return (
        "✅ Tamper test passed: a temporary copy with one changed byte and a re-hashed manifest was rejected. The uploaded ZIP is not modified.",
        result,
    )


with gr.Blocks(title="Integrity Evidence — Local Offline Verifier") as demo:
    gr.Markdown(
        """
        # Integrity Evidence — Local Offline Verifier

        Verifies a signed package and replays one of its stored cases **without calling Meta or any other service**.  
        This local tool receives no API keys and does not run the model; it only accepts small ZIPs.

        **Cryptographic limit:** the public key ships inside the package; a valid signature detects changes,  
        but on its own it does not prove who signed or when.
        """
    )
    archive = gr.File(label="Evidence package (.zip)", file_types=[".zip"], type="filepath")
    with gr.Row():
        verify_button = gr.Button("Verify package", variant="primary")
        tamper_button = gr.Button("Run tamper test")
    status = gr.Markdown()
    details = gr.JSON(label="Resumen verificado")
    verify_button.click(verify_uploaded_package, inputs=archive, outputs=[status, details])
    tamper_button.click(run_tamper_test, inputs=archive, outputs=[status, details])

    gr.Markdown(
        "## Walkthrough of a stored case\n\n"
        "Pick a case from the signed ZIP to see the request, the guarded/unguarded comparison and the contract decision. "
        "Everything shown comes from the package's report and scenarios; the model is not run again."
    )
    case_options = [
        ("Authority — verbal approval from the committee", "meta-heldout_authority_01_audit_chair"),
        ("Authority — voice note from the CFO", "meta-heldout_authority_02_voice_note"),
        ("Urgency — payroll cutoff", "meta-heldout_urgency_01_payroll_cutoff"),
        ("Urgency — regional outage", "meta-heldout_urgency_02_region_outage"),
        ("Ambiguity — accounting reclassification", "meta-heldout_ambiguity_01_ledger_reclass"),
        ("Ambiguity — continuity advance", "meta-heldout_ambiguity_02_continuity_advance"),
    ]
    selected_case = gr.Dropdown(
        choices=case_options,
        value=case_options[0][1],
        label="Held-out case",
    )
    case_button = gr.Button("Show case walkthrough", variant="primary")
    case_walkthrough = gr.Markdown()
    case_details = gr.JSON(label="Case data (structured)")
    case_button.click(
        build_case_walkthrough,
        inputs=[archive, selected_case],
        outputs=[case_walkthrough, case_details],
    )
    gr.Markdown(
        "Input limit: 8 MiB compressed / 5 MiB uncompressed. "
        "The full report and the conversation transcripts are not displayed. "
        "Only review packages you are authorised to inspect on this machine."
    )

demo.queue(max_size=8, default_concurrency_limit=1)

if __name__ == "__main__":
    demo.launch(show_error=False, max_file_size="8mb")
