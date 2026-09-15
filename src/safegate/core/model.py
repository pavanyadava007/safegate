"""
safegate.core.model
===================

The domain model is the product. Everything else is machinery around it.

An auditor at a Notified Body asks exactly one question, recursively:

    "Show me the evidence that this claim is true, and show me that the
     evidence was produced by the thing you say it was produced by."

That question is graph reachability over an immutable, content-addressed
DAG. So the model is a typed DAG, not a document set:

    Hazard ──covers──> SafetyRequirement ──implemented_by──> SafetyFunction
                                │                                │
                                │                        realised_by
                                │                                ▼
                                │                        SafetyArchitecture
                                │                         (ISO 13849 Cat)
                          verified_by
                                ▼
                           TestCase ──concretised_to──> ConcreteRun
                                                             │
                                                        produces
                                                             ▼
                                                        Evidence (CAS)

Design rules enforced here:

  R1. Every node carries a stable, deterministic ID derived from its
      semantic content: NOT a uuid4. Two engineers who author the same
      hazard independently get the same ID. Renaming a field does not
      orphan history.
  R2. Nodes are frozen. Mutation is modelled as a new node plus a
      `supersedes` edge. The audit trail is never destroyed.
  R3. No node may claim a Performance Level. PL is *derived* by the
      ISO 13849 calculator from the architecture and always recomputed.
      A stored PL is a lie waiting to happen.
  R4. Requirement text is free-form for humans; the machine-checkable
      acceptance criterion is an STL formula. Prose is not testable.
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .ids import content_id, digest

# --------------------------------------------------------------------------
# Enumerations mandated by the standards
# --------------------------------------------------------------------------


class PerformanceLevel(str, Enum):
    """ISO 13849-1 Performance Level. Ordered a < b < c < d < e."""

    a = "a"
    b = "b"
    c = "c"
    d = "d"
    e = "e"

    @property
    def rank(self) -> int:
        return "abcde".index(self.value)

    def __lt__(self, other: PerformanceLevel) -> bool:  # type: ignore[override]
        return self.rank < other.rank

    def __ge__(self, other: PerformanceLevel) -> bool:  # type: ignore[override]
        return self.rank >= other.rank


class Category(str, Enum):
    """ISO 13849-1 Category (architecture class)."""

    B = "B"
    CAT_1 = "1"
    CAT_2 = "2"
    CAT_3 = "3"
    CAT_4 = "4"


class Severity(str, Enum):
    """ISO 13849-1 Annex A risk graph: severity of injury."""

    S1 = "S1"  # slight, normally reversible
    S2 = "S2"  # serious, normally irreversible, including death


class Frequency(str, Enum):
    """Frequency / duration of exposure to the hazard."""

    F1 = "F1"  # seldom-to-less-often and/or exposure time is short
    F2 = "F2"  # frequent-to-continuous and/or exposure time is long


class Avoidance(str, Enum):
    """Possibility of avoiding the hazard or limiting the harm."""

    P1 = "P1"  # possible under specific conditions
    P2 = "P2"  # scarcely possible


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"  # infrastructure failure: NOT a safety verdict
    FLAKY = "flaky"  # non-deterministic under identical pinning
    SKIPPED = "skipped"


class ExecutionTier(str, Enum):
    """Where a run executed. Evidence weight is tier-dependent.

    EN ISO 3691-4 verification is ultimately physical. Simulation buys
    coverage and falsification pressure; it does not by itself discharge
    a verification obligation for a safety function. The policy engine
    encodes that rule (see policy/gate.py: `min_tier`).
    """

    MIL = "mil"  # model in the loop
    SIL = "sil"  # software in the loop (Gazebo / Isaac / headless)
    HIL = "hil"  # hardware in the loop (real ECU + safety scanner)
    REPLAY = "replay"  # recorded field data, open loop
    FIELD = "field"  # physical vehicle, instrumented test track


TIER_WEIGHT: dict[ExecutionTier, int] = {
    ExecutionTier.MIL: 0,
    ExecutionTier.SIL: 1,
    ExecutionTier.REPLAY: 1,
    ExecutionTier.HIL: 2,
    ExecutionTier.FIELD: 3,
}


# --------------------------------------------------------------------------
# Base node
# --------------------------------------------------------------------------


class Node(BaseModel):
    """Immutable, content-addressed graph node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    created_at: _dt.datetime = Field(
        default_factory=lambda: _dt.datetime.now(_dt.UTC)
    )
    supersedes: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    # ---- identity -------------------------------------------------------
    def identity_fields(self) -> dict[str, Any]:
        """Subset of fields that define semantic identity.

        Deliberately excludes created_at and labels so that re-authoring
        the same content yields the same ID.
        """
        raise NotImplementedError

    @property
    def id(self) -> str:
        return content_id(self.kind, self.identity_fields())


# --------------------------------------------------------------------------
# Hazard layer  (ISO 12100 hazard identification, ISO 13849-1 Annex A)
# --------------------------------------------------------------------------


class Hazard(Node):
    """A hazard identified by the ISO 12100 risk assessment.

    `required_pl` is derived, not asserted, via the Annex A risk graph.
    """

    kind: Literal["hazard"] = "hazard"
    ref: str  # human handle, e.g. "HAZ-COLL-001"
    title: str
    description: str
    lifecycle_phase: str = "normal_operation"
    zone: str | None = None  # e.g. "aisle", "charging_bay", "pedestrian_crossing"
    severity: Severity
    frequency: Frequency
    avoidance: Avoidance

    def identity_fields(self) -> dict[str, Any]:
        return {"ref": self.ref, "title": self.title}

    @property
    def required_pl(self) -> PerformanceLevel:
        """ISO 13849-1 Annex A risk graph -> PLr.

            S1 F1 P1 -> a
            S1 F1 P2 -> b
            S1 F2 P1 -> b
            S1 F2 P2 -> c
            S2 F1 P1 -> c
            S2 F1 P2 -> d
            S2 F2 P1 -> d
            S2 F2 P2 -> e
        """
        table = {
            ("S1", "F1", "P1"): PerformanceLevel.a,
            ("S1", "F1", "P2"): PerformanceLevel.b,
            ("S1", "F2", "P1"): PerformanceLevel.b,
            ("S1", "F2", "P2"): PerformanceLevel.c,
            ("S2", "F1", "P1"): PerformanceLevel.c,
            ("S2", "F1", "P2"): PerformanceLevel.d,
            ("S2", "F2", "P1"): PerformanceLevel.d,
            ("S2", "F2", "P2"): PerformanceLevel.e,
        }
        return table[
            (self.severity.value, self.frequency.value, self.avoidance.value)
        ]


# --------------------------------------------------------------------------
# Requirement layer
# --------------------------------------------------------------------------


class SafetyRequirement(Node):
    """A safety requirement with a machine-checkable acceptance criterion.

    `criterion_stl` is the contract. Prose is for the reader; STL is for
    the gate. A requirement with no STL cannot be automatically verified
    and the policy engine will flag it as `unverifiable`.
    """

    kind: Literal["requirement"] = "requirement"
    ref: str  # "SR-COLL-001"
    statement: str
    hazard_refs: list[str]
    criterion_stl: str | None = None
    parameters: dict[str, float] = Field(default_factory=dict)
    standard_clauses: list[str] = Field(default_factory=list)
    allocated_to: list[str] = Field(default_factory=list)  # SafetyFunction refs

    def identity_fields(self) -> dict[str, Any]:
        return {"ref": self.ref, "statement": self.statement}


# --------------------------------------------------------------------------
# Design layer
# --------------------------------------------------------------------------


class Subsystem(BaseModel):
    """A block in the safety-related part of the control system (SRP/CS).

    Reliability data comes from the component manufacturer's declaration.
    Either give MTTFd directly (electronics) or B10d + duty cycle
    (electromechanical wear-out parts).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    channel: int = 1  # 1 or 2 for redundant architectures
    mttfd_years: float | None = None
    b10d_cycles: float | None = None
    cycles_per_hour: float | None = None
    operating_hours_per_day: float = 16.0
    operating_days_per_year: float = 250.0
    dc: float = 0.0  # diagnostic coverage, fraction 0..1
    role: str = "logic"  # "input" | "logic" | "output"
    part_number: str | None = None
    cert_reference: str | None = None  # e.g. TÜV certificate number

    @field_validator("dc")
    @classmethod
    def _dc_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("dc must be a fraction in [0, 1]")
        return v


class SafetyArchitecture(Node):
    """The SRP/CS realising one safety function."""

    kind: Literal["architecture"] = "architecture"
    ref: str
    category: Category
    subsystems: list[Subsystem]
    ccf_score: int = 0  # ISO 13849-1 Annex F, >= 65 required for Cat 2/3/4
    systematic_measures: list[str] = Field(default_factory=list)
    uses_ml_in_safety_path: bool = False

    def identity_fields(self) -> dict[str, Any]:
        return {"ref": self.ref, "category": self.category.value}


class SafetyFunction(Node):
    """A safety function in the ISO 13849-1 sense.

    Example: "Detect a person in the protective field and bring the truck
    to a standstill before contact."
    """

    kind: Literal["safety_function"] = "safety_function"
    ref: str  # "SF-PDS-01"
    name: str
    description: str
    architecture_ref: str
    requirement_refs: list[str]
    reaction: str = "safe_stop"  # "safe_stop" | "speed_reduction" | "steer_away"
    demand_rate_per_hour: float | None = None

    def identity_fields(self) -> dict[str, Any]:
        return {"ref": self.ref, "name": self.name}


# --------------------------------------------------------------------------
# Verification layer
# --------------------------------------------------------------------------


class ParameterRange(BaseModel):
    """One dimension of an abstract scenario's parameter space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    low: float | None = None
    high: float | None = None
    values: list[float] | None = None
    unit: str = ""

    @field_validator("values")
    @classmethod
    def _one_of(cls, v: list[float] | None, info: Any) -> list[float] | None:
        return v

    def is_discrete(self) -> bool:
        return self.values is not None


class TestCase(Node):
    """An ABSTRACT scenario: a parameter space plus an acceptance criterion.

    This is never executed directly. The scenario compiler concretises it
    into a set of ConcreteRun specs. That separation is what makes
    coverage claims meaningful: you cover a *space*, not a script.
    """

    kind: Literal["test_case"] = "test_case"
    ref: str  # "TC-CROSS-001"
    title: str
    requirement_refs: list[str]
    scenario_template: str  # path or inline OSC2 / YAML template
    parameter_space: list[ParameterRange] = Field(default_factory=list)
    criterion_stl: str
    required_tier: ExecutionTier = ExecutionTier.SIL
    scenario_class: str = "generic"  # for coverage bucketing

    def identity_fields(self) -> dict[str, Any]:
        return {"ref": self.ref, "template": self.scenario_template}


class Pinning(BaseModel):
    """The five hashes that make a run reproducible.

    If any of these is absent, the run is not evidence. This is the single
    most important invariant in the system: an auditor must be able to
    re-run the exact test three years later and get the same verdict.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_hash: str
    sut_build_hash: str  # firmware / ROS workspace digest
    backend_hash: str  # simulator or HIL rig image digest
    config_hash: str  # vehicle + safety-scanner configuration
    seed: int


class ConcreteRun(Node):
    """One executable instance: abstract test case + a point in its space."""

    kind: Literal["concrete_run"] = "concrete_run"
    test_case_ref: str
    # Scenario template reference, so a run in the manifest is enough to
    # re-execute it without the project checkout that produced it.
    scenario: str = ""
    assignment: dict[str, float]
    tier: ExecutionTier
    pinning: Pinning

    def identity_fields(self) -> dict[str, Any]:
        return {
            "test_case_ref": self.test_case_ref,
            "scenario": self.scenario,
            "assignment": dict(sorted(self.assignment.items())),
            "tier": self.tier.value,
            "pinning": self.pinning.model_dump(),
        }


class RunResult(Node):
    """Outcome of one ConcreteRun.

    `robustness` is the quantitative STL semantics value. Its sign gives
    the verdict; its magnitude gives the *margin*, which is far more
    useful than a boolean: it tells the falsifier which direction to push
    and it tells the safety engineer how close the design is to the edge.
    """

    kind: Literal["run_result"] = "run_result"
    run_id: str
    verdict: Verdict
    robustness: float | None = None
    duration_s: float = 0.0
    artifacts: dict[str, str] = Field(default_factory=dict)  # name -> CAS digest
    metrics: dict[str, float] = Field(default_factory=dict)
    message: str = ""
    executed_at: _dt.datetime = Field(
        default_factory=lambda: _dt.datetime.now(_dt.UTC)
    )

    def identity_fields(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "executed_at": self.executed_at.isoformat()}


class Finding(Node):
    """A conformity gap raised by the policy engine.

    Findings are first-class nodes because the technical file must show
    both the gaps and their disposition. Hiding a failed test is fraud;
    showing it with a justified disposition is engineering.
    """

    kind: Literal["finding"] = "finding"
    ref: str
    severity: Literal["blocker", "major", "minor", "info"]
    rule: str
    subject: str  # node id or ref the finding attaches to
    detail: str
    disposition: Literal["open", "accepted", "mitigated", "waived"] = "open"
    waiver_justification: str | None = None
    waiver_approver: str | None = None

    def identity_fields(self) -> dict[str, Any]:
        return {"rule": self.rule, "subject": self.subject}


# --------------------------------------------------------------------------
# Project aggregate
# --------------------------------------------------------------------------


class Project(BaseModel):
    """The whole conformity dataset for one machine variant."""

    model_config = ConfigDict(extra="forbid")

    name: str
    machine_type: str = "driverless industrial truck"
    variant: str = "default"
    standards: list[str] = Field(
        default_factory=lambda: [
            "EN ISO 3691-4:2023",
            "EN ISO 13849-1:2023",
            "EN ISO 12100:2010",
            "Regulation (EU) 2023/1230",
        ]
    )
    hazards: list[Hazard] = Field(default_factory=list)
    requirements: list[SafetyRequirement] = Field(default_factory=list)
    architectures: list[SafetyArchitecture] = Field(default_factory=list)
    safety_functions: list[SafetyFunction] = Field(default_factory=list)
    test_cases: list[TestCase] = Field(default_factory=list)

    def content_digest(self) -> str:
        """Digest of the design data, independent of when it was loaded.

        Recorded with every campaign so the evidence commits to the exact
        hazards, requirements, architectures and test cases it verified.
        """

        def strip(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: strip(v) for k, v in obj.items() if k != "created_at"}
            if isinstance(obj, list):
                return [strip(v) for v in obj]
            return obj

        return digest(strip(self.model_dump(mode="json")))

    def by_ref(self, ref: str) -> Node | None:
        for coll in (
            self.hazards,
            self.requirements,
            self.architectures,
            self.safety_functions,
            self.test_cases,
        ):
            for n in coll:
                if getattr(n, "ref", None) == ref:
                    return n
        return None


__all__ = [
    "TIER_WEIGHT",
    "Avoidance",
    "Category",
    "ConcreteRun",
    "ExecutionTier",
    "Finding",
    "Frequency",
    "Hazard",
    "Node",
    "ParameterRange",
    "PerformanceLevel",
    "Pinning",
    "Project",
    "RunResult",
    "SafetyArchitecture",
    "SafetyFunction",
    "SafetyRequirement",
    "Severity",
    "Subsystem",
    "TestCase",
    "Verdict",
]
