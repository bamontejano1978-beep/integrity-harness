"""One offline command for a judge: verify the signed package and the tamper test.

It reads local files only. No model call, no key and no network access happen.

Usage:
    python scripts/judge_check.py
    python scripts/judge_check.py --package evidence/meta-heldout-live-20261028-signed.zip
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.package_meta_evidence import tamper_test_package, verify_package  # noqa: E402

DEFAULT_PACKAGE = ROOT / "evidence" / "meta-heldout-live-20261028-signed.zip"
DEFAULT_REFERENCE = ROOT / "evidence" / "reference" / "meta_safety_scan_report.json"


def _verdicts(report: dict[str, Any] | None) -> str:
    if not report:
        return ""
    comparisons = report.get("comparisons") or []
    if not comparisons:
        return ""
    before = comparisons[0].get("before_remediation_unguarded", {})
    after = comparisons[0].get("after_remediation_guarded", {})
    return (
        f"no guard {before.get('overall_status', '—')}, "
        f"with guard {after.get('overall_status', '—')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    def fail(message: str) -> int:
        print(f"FAIL  {message}")
        return 1

    if not args.package.exists():
        return fail(f"package not found: {args.package}")
    try:
        verification = verify_package(args.package)
    except Exception as exc:  # noqa: BLE001 - any parser error is a failed check
        return fail(f"verification raised {type(exc).__name__}: {exc}")
    if not verification.get("signed"):
        return fail("package is not signed")
    try:
        tamper = tamper_test_package(args.package)
    except Exception as exc:  # noqa: BLE001 - any parser error is a failed check
        return fail(f"tamper test raised {type(exc).__name__}: {exc}")
    if not tamper.get("rejected"):
        return fail("tamper test did not reject the altered copy")

    reference: dict[str, Any] | None = None
    if args.reference.exists():
        try:
            loaded = json.loads(args.reference.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            reference = loaded

    signature = verification.get("signature") or {}
    summary = {
        "package": str(args.package),
        "signed": True,
        "verified_artifacts": verification.get("verified_artifacts"),
        "key_id": signature.get("key_id"),
        "tamper_rejected": True,
        "tampered_member": tamper.get("tampered_member"),
        "reference_model": (reference or {}).get("meta_model_name"),
        "reference_verdicts": _verdicts(reference),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(
        f"OK    signature valid · {summary['verified_artifacts']} artifacts · "
        f"key {summary['key_id']}"
    )
    print(f"OK    tamper test rejected the altered copy ({summary['tampered_member']})")
    if summary["reference_verdicts"]:
        print(
            f"OK    stored reference run with {summary['reference_model']}: "
            f"{summary['reference_verdicts']}"
        )
    else:
        print("NOTE  no stored reference run is bundled in this checkout")
    print(
        "NOTE  the embedded key detects changes; it does not establish an external "
        "identity or a trusted timestamp."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
