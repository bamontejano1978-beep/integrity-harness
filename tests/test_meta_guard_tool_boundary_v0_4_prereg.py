"""Offline integrity tests for the v0.4 preregistered stimulus battery."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import verify_meta_guard_tool_boundary_freeze_v0_4 as verifier  # noqa: E402


def test_v04_freeze_is_valid_and_discloses_non_independent_review() -> None:
    result = verifier.verify()
    assert result["stimuli"] == 28
    assert result["matched_action_pairs"] == 8
    assert result["matched_no_action_pairs"] == 3
    assert result["explicit_unauthorized_bypass_cases"] == 6
    assert result["maximum_new_live_requests"] == 28
    assert result["internal_review_complete"] is True
    assert result["review_independent"] is False
    assert result["external_review_complete"] is False
    assert result["execution_authorized"] is False
    assert result["live_capture_made"] is False


def test_v04_action_pairs_hold_user_text_exactly_constant() -> None:
    stimuli = json.loads(verifier.STIMULI_PATH.read_text(encoding="utf-8"))["stimuli"]
    pairs: dict[str, dict[str, dict]] = {}
    for stimulus in stimuli:
        pair_id = stimulus.get("pair_id")
        if pair_id and pair_id.startswith("pair-"):
            pairs.setdefault(pair_id, {})[stimulus["condition"]] = stimulus
    assert len(pairs) == 8
    for pair_id, pair in pairs.items():
        assert pair["verified"]["text"] == pair["unverified"]["text"], pair_id
        assert pair["verified"]["condition"] != pair["unverified"]["condition"]


def test_v04_oracle_is_separate_and_matches_each_fixture_state() -> None:
    stimuli = json.loads(verifier.STIMULI_PATH.read_text(encoding="utf-8"))["stimuli"]
    oracle = json.loads(verifier.ORACLE_PATH.read_text(encoding="utf-8"))
    assert "oracle" not in json.dumps(stimuli).lower()
    assert "restricted_action_expected" not in json.dumps(stimuli)
    labels = {item["id"]: item for item in oracle["labels"]}
    assert len(labels) == len(stimuli) == 28
    for stimulus in stimuli:
        assert labels[stimulus["id"]]["authorization_verified"] is (
            stimulus["condition"] == "verified"
        )
        assert labels[stimulus["id"]]["pair_id"] == stimulus["pair_id"]


def test_v04_verifier_rejects_tampered_artifact(tmp_path: Path, monkeypatch) -> None:
    freeze = json.loads(verifier.FREEZE_PATH.read_text(encoding="utf-8"))
    paths: dict[str, Path] = {}
    for artifact in freeze["artifacts"]:
        source = verifier.ROOT / artifact["path"]
        target = tmp_path / artifact["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        paths[artifact["path"]] = target
    tampered = paths["hackatons/meta/scenarios/blind_guard_tool_boundary_stimuli_v0.4.json"]
    tampered.write_text(tampered.read_text(encoding="utf-8") + " ", encoding="utf-8")
    copied_freeze = tmp_path / "hackatons/meta/scenarios/blind_guard_tool_boundary_freeze_v0.4.json"
    copied_freeze.parent.mkdir(parents=True, exist_ok=True)
    copied_freeze.write_text(json.dumps(freeze), encoding="utf-8")
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setattr(verifier, "FREEZE_PATH", copied_freeze)
    monkeypatch.setattr(verifier, "PROTOCOL_PATH", paths["docs/meta-guard-tool-boundary-prereg-v0.4-review.md"])
    monkeypatch.setattr(verifier, "STIMULI_PATH", tampered)
    monkeypatch.setattr(verifier, "ORACLE_PATH", paths["hackatons/meta/scenarios/blind_guard_tool_boundary_oracle_v0.4.json"])
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verifier.verify()
