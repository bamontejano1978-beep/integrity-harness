"""Offline tests for signed Meta held-out evidence packages."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import package_meta_evidence as evidence  # noqa: E402


def test_signed_package_verifies_and_rejects_rehashed_report_tampering(tmp_path: Path) -> None:
    output = tmp_path / "heldout-signed.zip"
    private_key = tmp_path / "private.pem"
    built = evidence.build_package(
        evidence.DEFAULT_REPORT, output, evidence.SUBMISSION_ROOT, private_key
    )

    verified = evidence.verify_package(output)
    tamper = evidence.tamper_test_package(output)

    assert built["signature"]["algorithm"] == "Ed25519"
    assert verified["signed"] is True
    assert verified["verified_artifacts"] == 100
    assert tamper["changed_bytes"] == 1
    assert tamper["manifest_rehashed"] is True
    assert tamper["rejected"] is True
    assert "signature" in tamper["reason"].lower()
    assert evidence.verify_package(output)["signed"] is True
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert private_key.read_bytes() not in b"".join(archive.read(name) for name in names)
        assert "signature.json" in names


def test_legacy_unsigned_package_remains_verifiable() -> None:
    legacy = evidence.SUBMISSION_ROOT / "evidence/meta-heldout-live-20261028.zip"
    result = evidence.verify_package(legacy)
    assert result["signed"] is False
    assert result["verified_artifacts"] == 100
