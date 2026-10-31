"""Offline tests for the judge entry points: one-command check and clause matrix."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import clause_matrix, judge_check  # noqa: E402


def test_judge_check_passes_on_the_bundled_package(capsys) -> None:
    exit_code = judge_check.main([])
    output = capsys.readouterr().out

    assert exit_code == 0, output
    assert "signature valid" in output
    assert "tamper test rejected" in output
    assert "does not establish an external identity" in output


def test_judge_check_fails_on_a_missing_package(tmp_path: Path, capsys) -> None:
    exit_code = judge_check.main(["--package", str(tmp_path / "absent.zip")])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "package not found" in output


def test_matrix_counts_only_recorded_verdicts() -> None:
    markdown, summary = clause_matrix.build_matrix(
        [ROOT / "evidence" / "reference" / "meta_safety_scan_report.json"]
    )

    assert summary["runs"] == 1
    assert len(summary["clauses"]) == 5
    assert "makes no model calls" in markdown
    assert "not a broad evaluation" in markdown
    assert "`DEBE-DECLARE-SCOPE-001`" in markdown
    for clause in summary["clauses"].values():
        totals = {
            arm: sum(statuses.values()) for arm, statuses in clause.items()
        }
        assert totals["before_remediation_unguarded"] == 1
        assert totals["after_remediation_guarded"] == 1


def test_matrix_skips_files_without_comparisons(tmp_path: Path) -> None:
    empty = tmp_path / "meta_safety_scan_report.json"
    empty.write_text("{}", encoding="utf-8")

    markdown, summary = clause_matrix.build_matrix([empty])

    assert markdown == ""
    assert summary["runs"] == 0
