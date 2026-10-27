"""Executable canaries for the clause corpus.

The detector cards answer "has this clause ever been seen to fire?" from a
recorded snapshot, and for most of the corpus the honest answer is "no". A card
that only reports that gap cannot notice the opposite error: a clause that used
to fire and silently stops. This module turns each card into a case that is
re-run against the committed fixtures.

A canary is a triple the tree already owns — a contract, a fixture that makes
one clause fail, and (when the corpus has one) a control fixture where the same
trigger is satisfied. Re-evaluating it in CI is what makes "the clause still
fires" a build requirement instead of a sentence in a document.

Three evaluation paths exist because the corpus is not homogeneous: a typed
trajectory, a declarative sandbox scenario, and a mediated swarm channel.
Clauses no committed fixture can make fire are recorded as ``unproven`` with a
reason; a corpus clause with neither a canary nor an unproven entry fails the
gate, so coverage can only grow deliberately.

This module decides nothing about behaviour. It re-runs the evaluators the
fixtures already go through and reports what they returned.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autopilot.adapter import load_adapter_config
from autopilot.agent_sandbox import (
    load_declarative_sandbox_config,
    run_declarative_sandbox_evaluation,
)
from autopilot.clause_coverage import (
    CardCanary,
    ClauseCoverageInputError,
    load_audit_csv,
    render_detector_cards_document,
)
from autopilot.contracts import load_contract
from autopilot.evaluator import ClauseStatus, evaluate_contract
from autopilot.insurance_agent import load_agent_scenario
from autopilot.swarm import load_swarm_pair, run_swarm_pair
from autopilot.swarm_contract import load_swarm_contract
from autopilot.swarm_evaluator import evaluate_swarm_contract
from autopilot.trajectory import load_trajectory

CANARY_SCHEMA_VERSION = "0.1"
DEFAULT_CORPUS = "docs/product/radar_riesgos_anclaje_44_clausulas_20261017_v0.1.csv"
DEFAULT_REGISTRY = "docs/product/clause_canaries_v0.1.json"
DEFAULT_CARDS_MD = "docs/product/detector_cards_corpus_v0.1.md"

#: Declarative sandboxes the tree ships, with the scenario families that run
#: against them. A sandbox canary is identified by the quartet it needs.
SANDBOX_BUNDLES: tuple[tuple[str, str, str, str], ...] = (
    (
        "examples/studio32_reception_handoff_contract_v0.1.yaml",
        "examples/studio32_reception_adapter_v0.1.yaml",
        "examples/studio32_reception_sandbox_v0.1.yaml",
        "studio32_scenario_*.yaml",
    ),
    # The same scenarios behind the unguarded sandbox: without it a scenario that
    # keeps the guard in place proves the guard works, not that the detector fires.
    (
        "examples/studio32_reception_handoff_contract_v0.1.yaml",
        "examples/studio32_reception_adapter_v0.1.yaml",
        "examples/studio32_reception_sandbox_unguarded_v0.1.yaml",
        "studio32_scenario_*.yaml",
    ),
    # Two candidate builds that are wrong in one specific way each: one never
    # declares it is an AI, the other promises service to a closed business and
    # never escalates. A clause that only ever passes needs a run like these.
    (
        "examples/studio32_reception_handoff_contract_v0.1.yaml",
        "examples/studio32_reception_adapter_v0.1.yaml",
        "examples/studio32_reception_sandbox_undisclosed_v0.1.yaml",
        "studio32_scenario_*.yaml",
    ),
    (
        "examples/studio32_reception_handoff_contract_v0.1.yaml",
        "examples/studio32_reception_adapter_v0.1.yaml",
        "examples/studio32_reception_sandbox_closed_promise_v0.1.yaml",
        "studio32_scenario_*.yaml",
    ),
    (
        "examples/strategyai_company_brain_role_isolation_v0.1.yaml",
        "examples/strategyai_company_brain_role_isolation_adapter_v0.1.yaml",
        "examples/strategyai_company_brain_sandbox_v0.1.yaml",
        "strategyai_scenario_*.yaml",
    ),
    # The same contract behind a candidate that removes the guard, so each
    # scripted failure reaches the evaluator instead of being replaced by a safe
    # response. One scenario per boundary: leak, silent refusal, denied lookup
    # and missing handoff, plus the compliant run that contrasts them.
    (
        "examples/strategyai_company_brain_role_isolation_v0.1.yaml",
        "examples/strategyai_company_brain_role_isolation_adapter_v0.1.yaml",
        "examples/strategyai_company_brain_sandbox_negative_v0.1.yaml",
        "strategyai_negative_scenario_*.yaml",
    ),
    # A session that opens without stating the role scope, so the only way to
    # satisfy the declaration clause is to state it in the first answer.
    (
        "examples/strategyai_company_brain_role_isolation_v0.1.yaml",
        "examples/strategyai_company_brain_role_isolation_adapter_v0.1.yaml",
        "examples/strategyai_company_brain_sandbox_scope_silent_v0.1.yaml",
        "strategyai_scope_scenario_*.yaml",
    ),
)

#: The swarm pair is drawn from the same pilot the swarm contract describes.
SWARM_BUNDLE = (
    "examples/insurance_claims_swarm_contract_v0.2.yaml",
    "examples/insurance_claims_v0.1.yaml",
    "examples/insurance_claims_swarm_adapter_v0.1.yaml",
    "examples/insurance_claims_swarm_pair_contagion_v0.1.yaml",
    "examples/insurance_claims_swarm_pair_control_v0.1.yaml",
)


class CanaryError(ValueError):
    """Raised when a canary cannot be evaluated at all (missing file, bad DSL)."""


class CanaryCase(BaseModel):
    """One clause, one fixture that must make it fire, and an optional control."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    kind: Literal["trajectory", "sandbox", "swarm"]
    contract: str
    fixture: str
    control: str | None = None
    #: Sandbox and swarm cases need the adapter that turns a run into observations.
    adapter: str | None = None
    #: Sandbox cases also need the declarative sandbox that vets the scenario.
    sandbox: str | None = None
    #: Swarm cases evaluate a swarm contract over a base contract's members.
    base_contract: str | None = None

    @property
    def discriminating(self) -> bool:
        """True when the corpus also holds a fixture where the trigger is met."""

        return self.control is not None


class UnprovenClause(BaseModel):
    """A corpus clause no committed fixture can make fire, with the reason why."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    reason: str


class CanaryRegistry(BaseModel):
    """The committed canary set, keyed by clause id."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"] = CANARY_SCHEMA_VERSION
    corpus: str = DEFAULT_CORPUS
    canaries: list[CanaryCase]
    unproven: list[UnprovenClause] = Field(default_factory=list)

    def by_clause(self) -> dict[str, CanaryCase]:
        return {case.clause_id: case for case in self.canaries}


class CanaryObservation(BaseModel):
    """What one canary evaluation returned."""

    model_config = ConfigDict(extra="forbid")

    clause_id: str
    role: Literal["canary", "control"]
    fixture: str
    status: str


class CanaryReport(BaseModel):
    """Coverage of the corpus plus every canary that stopped behaving."""

    model_config = ConfigDict(extra="forbid")

    clause_count: int
    declared_count: int
    canary_count: int
    discriminating_count: int
    unproven_count: int
    observations: list[CanaryObservation]
    problems: list[str]

    @property
    def gate_passed(self) -> bool:
        return not self.problems


def _resolve(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.exists():
        raise CanaryError(f"missing file: {relative}")
    return path


def _trajectory_status(case: CanaryCase, root: Path, fixture: str) -> str:
    document = load_contract(_resolve(root, case.contract))
    trajectory = load_trajectory(_resolve(root, fixture))
    evaluation = evaluate_contract(document, trajectory)
    for result in evaluation.clause_results:
        if result.clause_id == case.clause_id:
            return result.status.value
    raise CanaryError(
        f"contract {case.contract} does not declare {case.clause_id}: the canary"
        " would assert nothing"
    )


def _sandbox_status(case: CanaryCase, root: Path, fixture: str) -> str:
    if case.adapter is None or case.sandbox is None:
        raise CanaryError(f"sandbox canary {case.clause_id} needs an adapter and a sandbox")
    document = load_contract(_resolve(root, case.contract))
    adapter = load_adapter_config(_resolve(root, case.adapter))
    sandbox = load_declarative_sandbox_config(_resolve(root, case.sandbox))
    scenario = load_agent_scenario(_resolve(root, fixture))
    run = run_declarative_sandbox_evaluation(document, adapter, sandbox, scenario)
    for result in run.agent_evaluation.evaluation.clause_results:
        if result.clause_id == case.clause_id:
            return result.status.value
    raise CanaryError(
        f"contract {case.contract} does not declare {case.clause_id}: the canary"
        " would assert nothing"
    )


def _swarm_status(case: CanaryCase, root: Path, fixture: str) -> str:
    if case.base_contract is None or case.adapter is None:
        raise CanaryError(
            f"swarm canary {case.clause_id} needs a base contract and an adapter"
        )
    document = load_swarm_contract(_resolve(root, case.contract))
    base_contract = load_contract(_resolve(root, case.base_contract))
    adapter = load_adapter_config(_resolve(root, case.adapter))
    pair, base_dir = load_swarm_pair(_resolve(root, fixture))
    run = run_swarm_pair(base_contract, adapter, pair, base_dir)
    verdict = evaluate_swarm_contract(document, run.run, run.report.members)
    fired = {item.rule_id for item in [*verdict.violations, *verdict.topology_incidents]}
    return ClauseStatus.failed.value if case.clause_id in fired else ClauseStatus.passed.value


def evaluate_canary(case: CanaryCase, root: str | Path, *, fixture: str) -> str:
    """Re-run one fixture and return the clause's status as a string."""

    base = Path(root)
    if case.kind == "trajectory":
        return _trajectory_status(case, base, fixture)
    if case.kind == "sandbox":
        return _sandbox_status(case, base, fixture)
    return _swarm_status(case, base, fixture)


def load_registry(path: str | Path) -> CanaryRegistry:
    registry_path = Path(path)
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanaryError(f"cannot read the canary registry {registry_path}: {exc}") from exc
    try:
        return CanaryRegistry.model_validate(payload)
    except ValidationError as exc:
        raise CanaryError(f"invalid canary registry {registry_path}: {exc}") from exc


def corpus_clause_ids(root: str | Path, corpus: str) -> list[str]:
    """The audited corpus, in the order the audit recorded it."""

    try:
        report = load_audit_csv(Path(root) / corpus)
    except ClauseCoverageInputError as exc:
        raise CanaryError(str(exc)) from exc
    return [entry.clause_id for entry in report.entries]


def declared_clauses(root: str | Path) -> dict[str, list[str]]:
    """Clause id to the committed contracts that declare it.

    Only ``examples/`` is scanned: those are the contracts the tree ships and
    the ones a reader can point at. A contract that parses as neither a clause
    contract nor a swarm contract is not a contract and is skipped, because the
    directory also holds trajectories, adapters and scenario files.
    """

    base = Path(root)
    declared: dict[str, list[str]] = {}
    for path in sorted((base / "examples").glob("*.yaml")):
        relative = path.relative_to(base).as_posix()
        try:
            document = load_contract(path)
        except Exception:  # noqa: BLE001 - the directory holds many non-contracts
            try:
                swarm = load_swarm_contract(path)
            except Exception:  # noqa: BLE001 - not a swarm contract either
                continue
            clause_ids = [clause.clause_id for clause in swarm.clauses]
        else:
            clause_ids = [clause.id for clause in document.clauses]
        for clause_id in clause_ids:
            declared.setdefault(clause_id, []).append(relative)
    return declared


def registry_clause_ids(root: str | Path, corpus: str) -> list[str]:
    """Clauses the registry must account for: the corpus, then contract extras.

    The extras are clauses a committed contract declares that the audit never
    counted. They still need evidence, so discovery must give them a case.
    """

    audited = corpus_clause_ids(root, corpus)
    return [*audited, *sorted(set(declared_clauses(root)) - set(audited))]


def check_registry(registry: CanaryRegistry, root: str | Path) -> CanaryReport:
    """Re-run every canary and compare the set against the audited corpus."""

    base = Path(root)
    corpus = corpus_clause_ids(base, registry.corpus)
    corpus_set = set(corpus)
    declared_by_contract = declared_clauses(base)
    problems: list[str] = []
    observations: list[CanaryObservation] = []

    seen: dict[str, int] = {}
    for case in registry.canaries:
        seen[case.clause_id] = seen.get(case.clause_id, 0) + 1
    for clause_id, count in sorted(seen.items()):
        if count > 1:
            problems.append(f"{clause_id}: {count} canaries for one clause")
    unproven_counts: dict[str, int] = {}
    for item in registry.unproven:
        unproven_counts[item.clause_id] = unproven_counts.get(item.clause_id, 0) + 1
    for clause_id, count in sorted(unproven_counts.items()):
        if count > 1:
            problems.append(f"{clause_id}: {count} unproven entries for one clause")
    for item in registry.unproven:
        if item.clause_id in seen:
            problems.append(f"{item.clause_id}: declared unproven and given a canary")
    for case in registry.canaries:
        if case.clause_id not in declared_by_contract:
            problems.append(
                f"{case.clause_id}: canary names a clause no committed contract declares"
            )
    for item in registry.unproven:
        if item.clause_id not in declared_by_contract:
            problems.append(
                f"{item.clause_id}: unproven entry names a clause no committed contract"
                " declares"
            )

    declared = set(seen) | {item.clause_id for item in registry.unproven}
    for clause_id in corpus:
        if clause_id not in declared:
            problems.append(
                f"{clause_id}: corpus clause has neither a canary nor an unproven entry"
            )

    # A clause added to a contract after the audit was recorded has no card, so
    # nothing else in the gate would notice it. It must arrive with evidence.
    for clause_id, contracts in sorted(declared_by_contract.items()):
        if clause_id in corpus_set or clause_id in declared:
            continue
        problems.append(
            f"{clause_id}: declared by {', '.join(contracts)} with no detector card"
            " and no registry entry"
        )

    for case in registry.canaries:
        try:
            status = evaluate_canary(case, base, fixture=case.fixture)
        except CanaryError as exc:
            problems.append(f"{case.clause_id}: {exc}")
            continue
        observations.append(
            CanaryObservation(
                clause_id=case.clause_id,
                role="canary",
                fixture=case.fixture,
                status=status,
            )
        )
        if status != ClauseStatus.failed.value:
            problems.append(
                f"{case.clause_id}: stopped firing — {case.fixture} now reports"
                f" {status}, so the clause no longer detects what this canary covers"
            )
        if case.control is None:
            continue
        try:
            control_status = evaluate_canary(case, base, fixture=case.control)
        except CanaryError as exc:
            problems.append(f"{case.clause_id}: control {case.control}: {exc}")
            continue
        observations.append(
            CanaryObservation(
                clause_id=case.clause_id,
                role="control",
                fixture=case.control,
                status=control_status,
            )
        )
        if control_status == ClauseStatus.failed.value:
            problems.append(
                f"{case.clause_id}: control stopped discriminating — {case.control}"
                " now fails, so the canary no longer shows the clause distinguishing"
                " a compliant run from a violating one"
            )

    return CanaryReport(
        clause_count=len(corpus),
        declared_count=len(declared_by_contract),
        canary_count=len(registry.canaries),
        discriminating_count=sum(1 for case in registry.canaries if case.discriminating),
        unproven_count=len(registry.unproven),
        observations=observations,
        problems=problems,
    )


def card_canary_states(
    registry: CanaryRegistry, root: str | Path
) -> dict[str, CardCanary]:
    """The live verdict per clause, shaped for the detector cards.

    A card that only repeats the recorded snapshot cannot show that a detector
    still works. These states are the gate's own observations, so the document
    and the build check cannot disagree: both come from the same re-run.
    """

    report = check_registry(registry, root)
    observed = {(item.clause_id, item.role): item for item in report.observations}
    states: dict[str, CardCanary] = {}

    for case in registry.canaries:
        canary = observed.get((case.clause_id, "canary"))
        if canary is None:
            states[case.clause_id] = CardCanary(
                state="blocked",
                detail=(
                    f"**Cannot evaluate** the canary `{case.fixture}`; the gate"
                    " reports why and fails until the fixture runs again."
                ),
            )
            continue
        if canary.status != ClauseStatus.failed.value:
            states[case.clause_id] = CardCanary(
                state="stale",
                detail=(
                    f"**Stopped firing**: `{case.fixture}` now reports"
                    f" {canary.status}, so this clause no longer detects what the"
                    " canary covers. The gate fails until this is repaired."
                ),
            )
            continue
        control = observed.get((case.clause_id, "control"))
        if case.control is None:
            states[case.clause_id] = CardCanary(
                state="fires",
                detail=(
                    f"**Fires** on `{case.fixture}`. No control fixture meets the"
                    " same trigger under this setup, so only the failing direction"
                    " is demonstrated."
                ),
            )
            continue
        if control is not None and control.status == ClauseStatus.failed.value:
            states[case.clause_id] = CardCanary(
                state="stale",
                detail=(
                    f"**Stopped discriminating**: the control `{case.control}` now"
                    " fails too, so the canary no longer separates a compliant run"
                    " from a violating one. The gate fails until this is repaired."
                ),
            )
            continue
        states[case.clause_id] = CardCanary(
            state="fires",
            detail=(
                f"**Fires** on `{case.fixture}`; the control `{case.control}`"
                " passes, so the clause distinguishes a compliant run from a"
                " violating one."
            ),
        )

    for item in registry.unproven:
        states[item.clause_id] = CardCanary(
            state="unproven",
            detail=f"**No canary**: {item.reason}.",
        )
    return states


def render_corpus_cards(registry: CanaryRegistry, root: str | Path) -> str:
    """The published detector-card document, with every canary's live verdict."""

    base = Path(root)
    try:
        report = load_audit_csv(base / registry.corpus)
    except ClauseCoverageInputError as exc:
        raise CanaryError(str(exc)) from exc
    return render_detector_cards_document(
        report, canary_states=card_canary_states(registry, base)
    )


def render_report(report: CanaryReport) -> str:
    lines = [
        f"CLAUSE CANARIES: {report.canary_count} canary(ies) over "
        f"{report.clause_count} corpus clause(s) and {report.declared_count}"
        f" clause(s) a contract declares; "
        f"{report.discriminating_count} with a control; "
        f"{report.unproven_count} declared unproven"
    ]
    for observation in report.observations:
        if observation.role == "canary" and observation.status != ClauseStatus.failed.value:
            continue
        lines.append(
            f"  {observation.clause_id:<40} {observation.role:<7} "
            f"{observation.status:<16} {observation.fixture}"
        )
    if report.problems:
        lines.append("")
        lines.extend(f"  FAIL {problem}" for problem in report.problems)
    lines.append(f"GATE: {'PASS' if report.gate_passed else 'FAIL'}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Discovery: rebuild the registry from the fixtures already committed.
# ---------------------------------------------------------------------------


class _Candidate(BaseModel):
    """A fixture that fired (or met) one clause, with how cleanly it did so."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case: CanaryCase
    #: How many clauses the fixture fails at all; one is the cleanest canary.
    fixture_failures: int
    #: The run's own verdict, so a control can prefer a run that passed.
    overall: str

    @property
    def clause_id(self) -> str:
        return self.case.clause_id

    def canary_key(self) -> tuple[bool, int, str]:
        """Published examples first, then the most specific negative fixture."""

        return (
            not self.case.fixture.startswith("examples/"),
            self.fixture_failures,
            self.case.fixture,
        )

    def control_key(self) -> tuple[bool, bool, str]:
        """A control should be a run that did not fail at all, preferably published."""

        return (
            self.overall != ClauseStatus.passed.value,
            not self.case.fixture.startswith("examples/"),
            self.case.fixture,
        )


def _committable_files(root: Path) -> set[str]:
    """Repository-relative paths a commit can contain; discovery may only pick those.

    ``data/`` is ignored except for named exceptions, so a local nightly run can
    sit next to a fixture that looks authoritative and would vanish on a clean
    checkout. A canary has to be runnable by the gate, so ignored files are not
    candidates. Untracked-but-not-ignored files are, so a new fixture can be
    discovered before it is staged.
    """

    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - no git binary
        raise CanaryError(f"git is required to discover canaries: {exc}") from exc
    if completed.returncode != 0:
        raise CanaryError("git ls-files failed: discovery needs a work tree")
    listed = completed.stdout.decode("utf-8", "replace").split("\0")
    return {item for item in listed if item}


def _contract_index(root: Path) -> dict[tuple[str, str], str]:
    """Contract id+version to the committed contract file that declares it."""

    index: dict[tuple[str, str], str] = {}
    for path in sorted((root / "examples").glob("*.yaml")):
        try:
            document = load_contract(path)
        except Exception:  # noqa: BLE001 - a non-contract YAML is expected here
            continue
        key = (document.contract.id, document.contract.version)
        index.setdefault(key, path.relative_to(root).as_posix())
    return index


def _trajectory_candidates(
    root: Path, failures: dict[str, list[_Candidate]], passes: dict[str, list[_Candidate]]
) -> None:
    contracts = _contract_index(root)
    documents: dict[str, object] = {}
    tracked = _committable_files(root)
    paths = sorted(
        path
        for path in list((root / "examples").glob("*.yaml"))
        + list((root / "data").rglob("*.yaml"))
        if path.relative_to(root).as_posix() in tracked
    )
    for path in paths:
        try:
            trajectory = load_trajectory(path)
        except Exception:  # noqa: BLE001 - most YAML in the tree is not a trajectory
            continue
        key = (trajectory.contract_id, trajectory.contract_version)
        if key not in contracts:
            continue
        contract_path = contracts[key]
        document = documents.get(contract_path)
        if document is None:
            document = load_contract(root / contract_path)
            documents[contract_path] = document
        evaluation = evaluate_contract(document, trajectory)  # type: ignore[arg-type]
        relative = path.relative_to(root).as_posix()
        failed = sum(1 for item in evaluation.clause_results if item.status is ClauseStatus.failed)
        overall = evaluation.status.value
        for result in evaluation.clause_results:
            if result.status not in (ClauseStatus.failed, ClauseStatus.passed):
                continue
            case = CanaryCase(
                clause_id=result.clause_id,
                kind="trajectory",
                contract=contract_path,
                fixture=relative,
            )
            bucket = failures if result.status is ClauseStatus.failed else passes
            bucket.setdefault(result.clause_id, []).append(
                _Candidate(case=case, fixture_failures=failed, overall=overall)
            )


def _sandbox_candidates(
    root: Path, failures: dict[str, list[_Candidate]], passes: dict[str, list[_Candidate]]
) -> None:
    for contract_path, adapter_path, sandbox_path, pattern in SANDBOX_BUNDLES:
        document = load_contract(root / contract_path)
        adapter = load_adapter_config(root / adapter_path)
        sandbox = load_declarative_sandbox_config(root / sandbox_path)
        for scenario_path in sorted((root / "examples").glob(pattern)):
            scenario = load_agent_scenario(scenario_path)
            run = run_declarative_sandbox_evaluation(document, adapter, sandbox, scenario)
            relative = scenario_path.relative_to(root).as_posix()
            evaluation = run.agent_evaluation.evaluation
            failed = sum(
                1 for item in evaluation.clause_results if item.status is ClauseStatus.failed
            )
            overall = evaluation.status.value
            for result in evaluation.clause_results:
                if result.status not in (ClauseStatus.failed, ClauseStatus.passed):
                    continue
                case = CanaryCase(
                    clause_id=result.clause_id,
                    kind="sandbox",
                    contract=contract_path,
                    fixture=relative,
                    adapter=adapter_path,
                    sandbox=sandbox_path,
                )
                bucket = failures if result.status is ClauseStatus.failed else passes
                bucket.setdefault(result.clause_id, []).append(
                    _Candidate(case=case, fixture_failures=failed, overall=overall)
                )


def _swarm_candidates(
    root: Path, failures: dict[str, list[_Candidate]], passes: dict[str, list[_Candidate]]
) -> None:
    contract_path, base_path, adapter_path, fail_pair, control_pair = SWARM_BUNDLE
    document = load_swarm_contract(root / contract_path)
    base_contract = load_contract(root / base_path)
    adapter = load_adapter_config(root / adapter_path)
    for pair_path in (fail_pair, control_pair):
        pair, base_dir = load_swarm_pair(root / pair_path)
        run = run_swarm_pair(base_contract, adapter, pair, base_dir)
        verdict = evaluate_swarm_contract(document, run.run, run.report.members)
        fired = {
            item.rule_id
            for item in [*verdict.violations, *verdict.topology_incidents]
        }
        for clause in document.clauses:
            case = CanaryCase(
                clause_id=clause.clause_id,
                kind="swarm",
                contract=contract_path,
                fixture=pair_path,
                adapter=adapter_path,
                base_contract=base_path,
            )
            bucket = failures if clause.clause_id in fired else passes
            bucket.setdefault(clause.clause_id, []).append(
                _Candidate(
                    case=case,
                    fixture_failures=len(fired),
                    overall=verdict.status,
                )
            )


def discover_registry(root: str | Path, *, corpus: str = DEFAULT_CORPUS) -> CanaryRegistry:
    """Pick, per clause, the first fixture of each kind that fires and that passes."""

    base = Path(root)
    failures: dict[str, list[_Candidate]] = {}
    passes: dict[str, list[_Candidate]] = {}
    _trajectory_candidates(base, failures, passes)
    _sandbox_candidates(base, failures, passes)
    _swarm_candidates(base, failures, passes)

    canaries: list[CanaryCase] = []
    unproven: list[UnprovenClause] = []
    for clause_id in registry_clause_ids(base, corpus):
        candidates = sorted(failures.get(clause_id, []), key=_Candidate.canary_key)
        if not candidates:
            unproven.append(
                UnprovenClause(
                    clause_id=clause_id,
                    reason=(
                        "no committed fixture makes this clause fire; add a negative"
                        " fixture before claiming the detector works"
                    ),
                )
            )
            continue
        chosen = candidates[0].case
        controls = sorted(passes.get(clause_id, []), key=_Candidate.control_key)
        # A control is only a control under the same evaluation setup: the
        # unguarded sandbox that makes the canary fire would make a guarded
        # scenario look compliant for the wrong reason.
        setup = (chosen.kind, chosen.contract, chosen.adapter, chosen.sandbox, chosen.base_contract)
        same_setup = [
            item.case.fixture
            for item in controls
            if (
                item.case.kind,
                item.case.contract,
                item.case.adapter,
                item.case.sandbox,
                item.case.base_contract,
            )
            == setup
            and item.case.fixture != chosen.fixture
        ]
        # Without a fixture that meets the trigger under the same setup there is
        # no control: a pass observed elsewhere would be re-evaluated here with
        # the canary's setup and mean something else.
        control = same_setup[0] if same_setup else None
        canaries.append(chosen.model_copy(update={"control": control}))

    canaries.sort(key=lambda case: case.clause_id)
    unproven.sort(key=lambda item: item.clause_id)
    return CanaryRegistry(
        corpus=corpus,
        canaries=canaries,
        unproven=unproven,
    )


def render_registry(registry: CanaryRegistry) -> str:
    """Stable JSON for the committed registry; a diff means the set changed."""

    payload = registry.model_dump(mode="json", exclude_none=True)
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rebuild the registry from the committed fixtures and write it",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    registry_path = args.registry or (root / DEFAULT_REGISTRY)
    try:
        if args.write:
            registry = discover_registry(root)
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text(render_registry(registry), encoding="utf-8")
            print(
                f"CANARY REGISTRY: {len(registry.canaries)} canary(ies), "
                f"{len(registry.unproven)} unproven -> {registry_path}"
            )
            return 0
        report = check_registry(load_registry(registry_path), root)
    except CanaryError as exc:
        print(f"CANARY ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(render_report(report), end="")
    return 0 if report.gate_passed else 1


if __name__ == "__main__":  # pragma: no cover - exercised through the script
    raise SystemExit(main())
