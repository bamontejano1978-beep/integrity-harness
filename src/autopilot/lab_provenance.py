"""Which artifacts a lab instance is willing to seal.

A seal already declares, in ``LabPackage.limits``, that it attests *that this
artifact was sealed by that key at that moment* and not who produced it. That is
honest for a local workbench, where the only person signing is the researcher
who ran the simulation. It stops being enough as soon as an instance is offered
to another organisation: ``POST /api/lab/sign`` and ``POST /api/lab/package``
accept any artifact in the request body, so a hosted instance would hand out
``sealed_by_lab`` documents for content it never produced. The seal would then
mean "someone asked us to stamp this", which is exactly the meaning the
laboratory exists to avoid.

This module keeps the answer to that question in one place: the instance
registers the digest of every artifact it produced, plus the artifacts shipped
in its own bundle (built from the same code), and signs nothing else.

Three rules keep the registry from becoming a rubber stamp:

1. **Only the lab registers.** Endpoints that receive artifacts (digest, sign,
   package, verify) never add to the registry. Otherwise asking for a digest
   would be enough to make an arbitrary document eligible.
2. **A comparison pair is a composition, not a result.** A pair is accepted when
   both members are registered, because the interface builds that pair from runs
   the lab produced; any other pair is refused.
3. **Registration is a digest, not a name.** Two artifacts with the same name and
   different numbers are different artifacts, and the second one is not eligible.

This is an allowlist of content, not of users: it says nothing about who may run
the lab. Access control is a separate concern.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autopilot.lab_evidence import artifact_digest

PAIR_MODE = "comparison_ab"

#: Where a registered digest came from. Free text, shown in refusals and tests.
ORIGIN_BUNDLE = "lab_bundle"
ORIGIN_BUNDLE_VERIFIED = "lab_bundle_verified"
ORIGIN_LIVE_RUN = "live_run"
ORIGIN_LIVE_EVALUATION = "live_evaluation"
ORIGIN_LIVE_COMPARISON = "live_comparison"
ORIGIN_RECOMPOSED_PAIR = "recomposed_pair"


@dataclass(frozen=True)
class RegisteredArtifact:
    """One artifact this instance can attest, and where it came from."""

    digest: str
    experiment: str
    origin: str

    def as_dict(self) -> dict[str, str]:
        return {
            "artifact_sha256": self.digest,
            "experiment": self.experiment,
            "origin": self.origin,
        }


class LabArtifactRegistry:
    """Thread-safe set of artifact digests an instance is allowed to seal."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, RegisteredArtifact] = {}

    def register(self, artifact: Any, *, experiment: str, origin: str) -> str | None:
        """Register one artifact by digest. Returns the digest, or None if it
        cannot be canonicalised (which also means it cannot be signed)."""

        try:
            digest = artifact_digest(artifact)
        except (TypeError, ValueError):
            return None
        entry = RegisteredArtifact(
            digest=digest, experiment=str(experiment or ""), origin=origin
        )
        with self._lock:
            # The first origin wins: a run produced here stays a run produced
            # here even if the same artifact also ships in the bundle.
            self._items.setdefault(digest, entry)
        return digest

    def describe(self, digest: str) -> RegisteredArtifact | None:
        with self._lock:
            return self._items.get(digest)

    def digests(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def covers(self, artifact: Any) -> tuple[bool, str]:
        """Can this instance attest this artifact?

        Returns ``(True, "")`` or ``(False, reason)``. The reason is written to
        be shown to the caller: it names the digest and says what to do instead.
        """

        try:
            digest = artifact_digest(artifact)
        except (TypeError, ValueError) as exc:
            return False, f"artifact is not canonicalisable JSON: {exc}"

        if digest in self.digests():
            return True, ""

        if isinstance(artifact, dict) and str(artifact.get("mode") or "") == PAIR_MODE:
            members = (artifact.get("run_a"), artifact.get("run_b"))
            member_digests: list[str] = []
            for slot, member in zip(("run_a", "run_b"), members):
                try:
                    member_digest = artifact_digest(member)
                except (TypeError, ValueError):
                    return False, f"{slot} of the pair is not canonicalisable JSON"
                if member_digest not in self.digests():
                    return (
                        False,
                        (
                            f"{slot} of the pair ({member_digest[:16]}…) was not "
                            "produced by this instance either"
                        ),
                    )
                member_digests.append(member_digest)
            experiment = str(artifact.get("experiment") or "")
            with self._lock:
                self._items.setdefault(
                    digest,
                    RegisteredArtifact(
                        digest=digest,
                        experiment=experiment,
                        origin=ORIGIN_RECOMPOSED_PAIR,
                    ),
                )
            return True, ""

        return (
            False,
            (
                f"artifact {digest[:16]}… was not produced by this instance; run it "
                "here first, or export it as integrity-only from an instance "
                "without attributable signing"
            ),
        )


def register_bundle_artifacts(registry: LabArtifactRegistry, bundle_path: Path) -> int:
    """Register the artifacts the lab ships in its own static bundle.

    The bundle is built from this code by ``scripts/build_web_lab.py``, so its
    artifacts are the lab's own. Registering them keeps the interface usable on
    a fresh instance: the evidence bar signs the run it is showing, and that run
    came from the bundle, not from a request.

    Returns the number of artifacts registered.
    """

    path = Path(bundle_path)
    if not path.is_file():
        return 0
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(bundle, dict):
        return 0

    registered = 0
    experiments = bundle.get("experiments")
    if isinstance(experiments, dict):
        for name, artifact in experiments.items():
            if not isinstance(artifact, dict):
                continue
            if registry.register(artifact, experiment=name, origin=ORIGIN_BUNDLE):
                registered += 1
            verified = artifact.get("verified_1000_seeds")
            if isinstance(verified, dict):
                reconstructed = _verified_view(name, verified)
                if registry.register(
                    reconstructed, experiment=name, origin=ORIGIN_BUNDLE_VERIFIED
                ):
                    registered += 1

    evaluator = bundle.get("services")
    if isinstance(evaluator, dict):
        cases = (evaluator.get("evaluator") or {}).get("cases")
        if isinstance(cases, dict):
            for profile, profile_cases in cases.items():
                if not isinstance(profile_cases, dict):
                    continue
                for _kind, artifact in profile_cases.items():
                    if isinstance(artifact, dict) and registry.register(
                        artifact, experiment="evaluator", origin=ORIGIN_BUNDLE
                    ):
                        registered += 1
        canaries = (evaluator.get("canaries") or {}).get("sample_traces")
        if isinstance(canaries, dict):
            for name, artifact in canaries.items():
                if isinstance(artifact, dict) and registry.register(
                    artifact, experiment="canaries", origin=ORIGIN_BUNDLE
                ):
                    registered += 1

    return registered


def _verified_view(experiment: str, verified: dict[str, Any]) -> dict[str, Any]:
    """Mirror the artifact the interface builds from ``verified_1000_seeds``.

    ``lab/assets/js/lab.js`` replaces the current run with a three-field view of
    the verified sweep. It is the artifact the bar would then sign, so it has to
    be registrable here; keeping the shape in one function makes the coupling
    visible if either side changes.
    """

    manifest = verified.get("manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    view: dict[str, Any] = {"experiment": experiment}
    if manifest.get("parameters") is not None:
        view["parameters"] = manifest["parameters"]
    if verified.get("summaries") is not None:
        view["summaries"] = verified["summaries"]
    if manifest.get("parameters_sha256") is not None:
        view["parameters_sha256"] = manifest["parameters_sha256"]
    return view


__all__ = [
    "ORIGIN_BUNDLE",
    "ORIGIN_BUNDLE_VERIFIED",
    "ORIGIN_LIVE_COMPARISON",
    "ORIGIN_LIVE_EVALUATION",
    "ORIGIN_LIVE_RUN",
    "ORIGIN_RECOMPOSED_PAIR",
    "PAIR_MODE",
    "LabArtifactRegistry",
    "RegisteredArtifact",
    "register_bundle_artifacts",
]
