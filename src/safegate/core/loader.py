"""
safegate.core.loader
====================

Loads a project from YAML. The YAML is the source of truth that lives in
git next to the firmware, which is the whole point: safety data that lives
in a document-management system diverges from the code within one sprint.

Validation happens here and is strict. A typo in a hazard reference must
fail loudly at load time, not silently produce an uncovered hazard that
nobody notices until an assessor does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..stl.parser import STLSyntaxError, parse_stl
from .model import (
    Avoidance,
    Category,
    ExecutionTier,
    Frequency,
    Hazard,
    ParameterRange,
    Project,
    SafetyArchitecture,
    SafetyFunction,
    SafetyRequirement,
    Severity,
    Subsystem,
    TestCase,
)


class ProjectLoadError(ValueError):
    pass


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_project(root: str | Path) -> Project:
    root = Path(root)
    meta = _read(root / "project.yaml")
    proj = Project(
        name=meta.get("name", root.name),
        machine_type=meta.get("machine_type", "driverless industrial truck"),
        variant=meta.get("variant", "default"),
        standards=meta.get("standards")
        or Project.model_fields["standards"].default_factory(),  # type: ignore[union-attr]
    )

    for h in _read(root / "hazards.yaml").get("hazards", []):
        proj.hazards.append(
            Hazard(
                ref=h["ref"],
                title=h["title"],
                description=h.get("description", ""),
                zone=h.get("zone"),
                lifecycle_phase=h.get("lifecycle_phase", "normal_operation"),
                severity=Severity(h["severity"]),
                frequency=Frequency(h["frequency"]),
                avoidance=Avoidance(h["avoidance"]),
            )
        )

    for r in _read(root / "requirements.yaml").get("requirements", []):
        proj.requirements.append(
            SafetyRequirement(
                ref=r["ref"],
                statement=r["statement"],
                hazard_refs=list(r.get("hazards", [])),
                criterion_stl=r.get("criterion_stl"),
                parameters=dict(r.get("parameters", {})),
                standard_clauses=list(r.get("clauses", [])),
                allocated_to=list(r.get("allocated_to", [])),
            )
        )

    arch_doc = _read(root / "architectures.yaml")
    for a in arch_doc.get("architectures", []):
        subs = [
            Subsystem(
                name=s["name"],
                channel=int(s.get("channel", 1)),
                mttfd_years=s.get("mttfd_years"),
                b10d_cycles=s.get("b10d_cycles"),
                cycles_per_hour=s.get("cycles_per_hour"),
                operating_hours_per_day=float(s.get("operating_hours_per_day", 16.0)),
                operating_days_per_year=float(s.get("operating_days_per_year", 250.0)),
                dc=float(s.get("dc", 0.0)),
                role=s.get("role", "logic"),
                part_number=s.get("part_number"),
                cert_reference=s.get("cert_reference"),
            )
            for s in a.get("subsystems", [])
        ]
        proj.architectures.append(
            SafetyArchitecture(
                ref=a["ref"],
                category=Category(str(a["category"])),
                subsystems=subs,
                ccf_score=int(a.get("ccf_score", 0)),
                systematic_measures=list(a.get("systematic_measures", [])),
                uses_ml_in_safety_path=bool(a.get("uses_ml_in_safety_path", False)),
            )
        )

    for f in _read(root / "safety_functions.yaml").get("safety_functions", []):
        proj.safety_functions.append(
            SafetyFunction(
                ref=f["ref"],
                name=f["name"],
                description=f.get("description", ""),
                architecture_ref=f["architecture"],
                requirement_refs=list(f.get("requirements", [])),
                reaction=f.get("reaction", "safe_stop"),
                demand_rate_per_hour=f.get("demand_rate_per_hour"),
            )
        )

    for t in _read(root / "test_cases.yaml").get("test_cases", []):
        space = [
            ParameterRange(
                name=p["name"],
                low=p.get("low"),
                high=p.get("high"),
                values=p.get("values"),
                unit=p.get("unit", ""),
            )
            for p in t.get("parameters", [])
        ]
        proj.test_cases.append(
            TestCase(
                ref=t["ref"],
                title=t["title"],
                requirement_refs=list(t.get("requirements", [])),
                scenario_template=t.get("scenario", ""),
                parameter_space=space,
                criterion_stl=t["criterion_stl"],
                required_tier=ExecutionTier(t.get("tier", "sil")),
                scenario_class=t.get("scenario_class", "generic"),
            )
        )

    _validate(proj, root)
    return proj


def _validate(p: Project, root: Path | None = None) -> None:
    """Referential integrity. Fail at load, not at audit."""
    errs: list[str] = []
    haz = {h.ref for h in p.hazards}
    req = {r.ref for r in p.requirements}
    arch = {a.ref for a in p.architectures}

    for r in p.requirements:
        for h in r.hazard_refs:
            if h not in haz:
                errs.append(f"requirement {r.ref}: unknown hazard {h!r}")
    for sf in p.safety_functions:
        if sf.architecture_ref not in arch:
            errs.append(f"safety function {sf.ref}: unknown architecture {sf.architecture_ref!r}")
        for rr in sf.requirement_refs:
            if rr not in req:
                errs.append(f"safety function {sf.ref}: unknown requirement {rr!r}")
    for tc in p.test_cases:
        for rr in tc.requirement_refs:
            if rr not in req:
                errs.append(f"test case {tc.ref}: unknown requirement {rr!r}")
        if not tc.criterion_stl:
            errs.append(f"test case {tc.ref}: missing criterion_stl")
        else:
            try:
                parse_stl(tc.criterion_stl)
            except STLSyntaxError as exc:
                errs.append(f"test case {tc.ref}: criterion does not parse: {exc}")
        if (
            root is not None
            and tc.scenario_template
            and not (root / tc.scenario_template).exists()
        ):
            errs.append(
                f"test case {tc.ref}: scenario template {tc.scenario_template!r} not found"
            )
    for r in p.requirements:
        if r.criterion_stl:
            try:
                parse_stl(r.criterion_stl)
            except STLSyntaxError as exc:
                errs.append(f"requirement {r.ref}: criterion does not parse: {exc}")

    for coll, label in (
        (p.hazards, "hazard"),
        (p.requirements, "requirement"),
        (p.test_cases, "test case"),
        (p.safety_functions, "safety function"),
        (p.architectures, "architecture"),
    ):
        seen: set[str] = set()
        for n in coll:
            ref = n.ref
            if ref in seen:
                errs.append(f"duplicate {label} ref {ref!r}")
            seen.add(ref)

    if errs:
        raise ProjectLoadError(
            "project failed validation:\n  " + "\n  ".join(errs)
        )


__all__ = ["ProjectLoadError", "load_project"]
