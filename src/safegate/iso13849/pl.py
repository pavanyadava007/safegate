"""
safegate.iso13849.pl
====================

ISO 13849-1 Performance Level determination, implemented as a pure
function of the architecture so that PL can never be asserted by hand.

Chain implemented:

  1. MTTFd per component
       - electronics: taken from the manufacturer's declaration, in years
       - wear-out parts: MTTFd = B10d / (0.1 * n_op)
         with n_op = (d_op * h_op * 3600) / t_cycle   [cycles per year]
  2. MTTFd per channel, parts-count method:
       1/MTTFd_ch = sum_i 1/MTTFd_i
  3. Capping: MTTFd is capped at 100 years per channel (the standard does
     not credit reliability beyond "high").
  4. Two-channel symmetrisation (ISO 13849-1, 4.5.2):
       MTTFd = (2/3) * [ MTTFd_C1 + MTTFd_C2 - 1/(1/MTTFd_C1 + 1/MTTFd_C2) ]
  5. DCavg = sum_i (DC_i / MTTFd_i) / sum_i (1 / MTTFd_i)
  6. CCF score (Annex F) must be >= 65 for Categories 2, 3 and 4.
  7. Category + MTTFd band + DCavg band -> PL, via the Annex K / Figure 7
     combination table.

Three engineering positions taken here, each of which a reviewer should
be able to challenge:

  P1. The result is a *report object*, not a bare enum. Every input band,
      every cap that bound, and every precondition that failed is carried
      out with the answer. A PL with no derivation is not usable in a
      technical file.
  P2. Preconditions that fail (CCF < 65 on Cat 3, MTTFd "low" on Cat 4)
      do not silently downgrade the PL — they make the determination
      *invalid*. Silently returning a lower PL would let a broken design
      pass a weaker requirement.
  P3. PFHd is reported as the band bound, not a point estimate, unless
      the caller supplies a certified PFHd for the subsystem. Inventing
      precision the standard does not give you is how safety arguments
      rot.

Caveat for the reader: this implements the simplified (Annex K / bar
chart) route. The full Markov-model route in Annex K.2 is out of scope
and the report flags when the simplified route is being relied upon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

from ..core.model import Category, PerformanceLevel, SafetyArchitecture, Subsystem

MTTFD_CAP_YEARS = 100.0
MIN_CCF_SCORE = 65


class MTTFdBand(str, Enum):
    LOW = "low"  # 3 <= MTTFd < 10 years
    MEDIUM = "medium"  # 10 <= MTTFd < 30 years
    HIGH = "high"  # 30 <= MTTFd <= 100 years
    INVALID = "invalid"  # < 3 years: not permitted


class DCBand(str, Enum):
    NONE = "none"  # DC < 60 %
    LOW = "low"  # 60 % <= DC < 90 %
    MEDIUM = "medium"  # 90 % <= DC < 99 %
    HIGH = "high"  # DC >= 99 %


# PFHd bands, ISO 13849-1 Table 3, in dangerous failures per hour.
PFHD_BANDS: dict[PerformanceLevel, tuple[float, float]] = {
    PerformanceLevel.a: (1e-5, 1e-4),
    PerformanceLevel.b: (3e-6, 1e-5),
    PerformanceLevel.c: (1e-6, 3e-6),
    PerformanceLevel.d: (1e-7, 1e-6),
    PerformanceLevel.e: (1e-8, 1e-7),
}


def mttfd_band(years: float) -> MTTFdBand:
    if years < 3.0:
        return MTTFdBand.INVALID
    if years < 10.0:
        return MTTFdBand.LOW
    if years < 30.0:
        return MTTFdBand.MEDIUM
    return MTTFdBand.HIGH


def dc_band(dc: float) -> DCBand:
    if dc < 0.60:
        return DCBand.NONE
    if dc < 0.90:
        return DCBand.LOW
    if dc < 0.99:
        return DCBand.MEDIUM
    return DCBand.HIGH


# ISO 13849-1 Figure 7 / Annex K combination table.
# key: (Category, MTTFdBand, DCBand) -> PL
_COMBINATION: dict[tuple[Category, MTTFdBand, DCBand], PerformanceLevel] = {
    (Category.B, MTTFdBand.LOW, DCBand.NONE): PerformanceLevel.a,
    (Category.B, MTTFdBand.MEDIUM, DCBand.NONE): PerformanceLevel.a,
    (Category.B, MTTFdBand.HIGH, DCBand.NONE): PerformanceLevel.b,
    (Category.CAT_1, MTTFdBand.HIGH, DCBand.NONE): PerformanceLevel.c,
    (Category.CAT_2, MTTFdBand.LOW, DCBand.LOW): PerformanceLevel.a,
    (Category.CAT_2, MTTFdBand.MEDIUM, DCBand.LOW): PerformanceLevel.b,
    (Category.CAT_2, MTTFdBand.HIGH, DCBand.LOW): PerformanceLevel.c,
    (Category.CAT_2, MTTFdBand.LOW, DCBand.MEDIUM): PerformanceLevel.b,
    (Category.CAT_2, MTTFdBand.MEDIUM, DCBand.MEDIUM): PerformanceLevel.c,
    (Category.CAT_2, MTTFdBand.HIGH, DCBand.MEDIUM): PerformanceLevel.d,
    (Category.CAT_3, MTTFdBand.LOW, DCBand.LOW): PerformanceLevel.b,
    (Category.CAT_3, MTTFdBand.MEDIUM, DCBand.LOW): PerformanceLevel.c,
    (Category.CAT_3, MTTFdBand.HIGH, DCBand.LOW): PerformanceLevel.d,
    (Category.CAT_3, MTTFdBand.LOW, DCBand.MEDIUM): PerformanceLevel.c,
    (Category.CAT_3, MTTFdBand.MEDIUM, DCBand.MEDIUM): PerformanceLevel.d,
    (Category.CAT_3, MTTFdBand.HIGH, DCBand.MEDIUM): PerformanceLevel.d,
    (Category.CAT_4, MTTFdBand.HIGH, DCBand.HIGH): PerformanceLevel.e,
}

# Which DC bands each Category admits. Anything else is a design error,
# not a lower PL.
_ADMISSIBLE_DC: dict[Category, set[DCBand]] = {
    Category.B: {DCBand.NONE},
    Category.CAT_1: {DCBand.NONE},
    Category.CAT_2: {DCBand.LOW, DCBand.MEDIUM},
    Category.CAT_3: {DCBand.LOW, DCBand.MEDIUM},
    Category.CAT_4: {DCBand.HIGH},
}

_ADMISSIBLE_MTTFD: dict[Category, set[MTTFdBand]] = {
    Category.B: {MTTFdBand.LOW, MTTFdBand.MEDIUM, MTTFdBand.HIGH},
    Category.CAT_1: {MTTFdBand.HIGH},
    Category.CAT_2: {MTTFdBand.LOW, MTTFdBand.MEDIUM, MTTFdBand.HIGH},
    Category.CAT_3: {MTTFdBand.LOW, MTTFdBand.MEDIUM, MTTFdBand.HIGH},
    Category.CAT_4: {MTTFdBand.HIGH},
}


# --------------------------------------------------------------------------
# Component-level reliability
# --------------------------------------------------------------------------


def component_mttfd_years(s: Subsystem) -> float:
    """MTTFd for one subsystem, in years.

    Wear-out parts use the B10d route; everything else uses the declared
    MTTFd. If a subsystem gives both, B10d wins, because a declared MTTFd
    for a wear part usually assumes a duty cycle that is not yours.
    """
    if s.b10d_cycles is not None:
        if not s.cycles_per_hour:
            raise ValueError(
                f"{s.name}: b10d_cycles given without cycles_per_hour; "
                "duty cycle is required for the B10d route"
            )
        n_op = s.cycles_per_hour * s.operating_hours_per_day * s.operating_days_per_year
        if n_op <= 0:
            raise ValueError(f"{s.name}: computed n_op <= 0")
        return s.b10d_cycles / (0.1 * n_op)
    if s.mttfd_years is None:
        raise ValueError(f"{s.name}: neither mttfd_years nor b10d_cycles supplied")
    return s.mttfd_years


# --------------------------------------------------------------------------
# Result object
# --------------------------------------------------------------------------


@dataclass
class ChannelResult:
    channel: int
    mttfd_years_raw: float
    mttfd_years_capped: float
    band: MTTFdBand
    components: list[tuple[str, float, float]] = field(default_factory=list)


@dataclass
class PLResult:
    """Everything needed to defend the number in front of an assessor."""

    architecture_ref: str
    category: Category
    channels: list[ChannelResult]
    mttfd_years: float
    mttfd_band: MTTFdBand
    dc_avg: float
    dc_band: DCBand
    ccf_score: int
    achieved_pl: PerformanceLevel | None
    pfhd_band: tuple[float, float] | None
    valid: bool
    violations: list[str]
    notes: list[str]

    def meets(self, required: PerformanceLevel) -> bool:
        return (
            self.valid
            and self.achieved_pl is not None
            and self.achieved_pl.rank >= required.rank
        )

    def explain(self) -> str:
        lines = [
            f"Architecture {self.architecture_ref}  Category {self.category.value}",
        ]
        for ch in self.channels:
            lines.append(
                f"  channel {ch.channel}: MTTFd = {ch.mttfd_years_raw:.1f} y"
                f" (capped {ch.mttfd_years_capped:.1f} y) -> {ch.band.value}"
            )
            for name, mttfd, dc in ch.components:
                lines.append(f"      {name:<28} MTTFd={mttfd:8.1f} y  DC={dc:5.1%}")
        lines.append(
            f"  combined MTTFd = {self.mttfd_years:.1f} y ({self.mttfd_band.value})"
        )
        lines.append(f"  DCavg = {self.dc_avg:.1%} ({self.dc_band.value})")
        lines.append(f"  CCF score = {self.ccf_score}")
        if self.valid and self.achieved_pl and self.pfhd_band:
            lo, hi = self.pfhd_band
            lines.append(
                f"  => PL {self.achieved_pl.value}  "
                f"(PFHd in [{lo:.1e}, {hi:.1e}) 1/h)"
            )
        else:
            lines.append("  => DETERMINATION INVALID")
        for v in self.violations:
            lines.append(f"  ! {v}")
        for n in self.notes:
            lines.append(f"  - {n}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------


def evaluate_architecture(arch: SafetyArchitecture) -> PLResult:
    violations: list[str] = []
    notes: list[str] = [
        "Determined via the simplified route (ISO 13849-1 Annex K / Figure 7). "
        "A full Markov analysis may yield a different PFHd."
    ]

    by_channel: dict[int, list[Subsystem]] = {}
    for s in arch.subsystems:
        by_channel.setdefault(s.channel, []).append(s)
    if not by_channel:
        violations.append("architecture has no subsystems")

    channels: list[ChannelResult] = []
    dc_num = 0.0
    dc_den = 0.0
    for ch_id in sorted(by_channel):
        subs = by_channel[ch_id]
        inv = 0.0
        comps: list[tuple[str, float, float]] = []
        for s in subs:
            m = component_mttfd_years(s)
            if m <= 0:
                violations.append(f"{s.name}: non-positive MTTFd")
                continue
            inv += 1.0 / m
            comps.append((s.name, m, s.dc))
            dc_num += s.dc / m
            dc_den += 1.0 / m
        raw = (1.0 / inv) if inv > 0 else 0.0
        capped = min(raw, MTTFD_CAP_YEARS)
        if raw > MTTFD_CAP_YEARS:
            notes.append(
                f"channel {ch_id}: MTTFd capped from {raw:.0f} y to "
                f"{MTTFD_CAP_YEARS:.0f} y per ISO 13849-1"
            )
        channels.append(
            ChannelResult(ch_id, raw, capped, mttfd_band(capped), comps)
        )

    # Channel combination
    if len(channels) == 1:
        mttfd = channels[0].mttfd_years_capped
    elif len(channels) == 2:
        c1, c2 = channels[0].mttfd_years_capped, channels[1].mttfd_years_capped
        if c1 <= 0 or c2 <= 0:
            mttfd = 0.0
        else:
            mttfd = (2.0 / 3.0) * (c1 + c2 - 1.0 / (1.0 / c1 + 1.0 / c2))
            if abs(c1 - c2) > 1e-9:
                notes.append(
                    "asymmetric channels symmetrised per ISO 13849-1 4.5.2"
                )
    else:
        violations.append(
            f"{len(channels)} channels declared; ISO 13849-1 simplified route "
            "supports 1 or 2"
        )
        mttfd = min(c.mttfd_years_capped for c in channels)
    mttfd = min(mttfd, MTTFD_CAP_YEARS)

    m_band = mttfd_band(mttfd)
    dc_avg = (dc_num / dc_den) if dc_den > 0 else 0.0
    d_band = dc_band(dc_avg)

    # ---- preconditions --------------------------------------------------
    if m_band is MTTFdBand.INVALID:
        violations.append(
            f"MTTFd = {mttfd:.1f} y is below the 3-year floor of ISO 13849-1"
        )
    if arch.category in (Category.CAT_2, Category.CAT_3, Category.CAT_4):
        if arch.ccf_score < MIN_CCF_SCORE:
            violations.append(
                f"CCF score {arch.ccf_score} < {MIN_CCF_SCORE} required for "
                f"Category {arch.category.value} (Annex F)"
            )
    if arch.category in (Category.CAT_3, Category.CAT_4):
        if len(channels) < 2:
            violations.append(
                f"Category {arch.category.value} requires two channels; "
                f"{len(channels)} declared"
            )
    if d_band not in _ADMISSIBLE_DC[arch.category]:
        violations.append(
            f"DCavg band '{d_band.value}' is not admissible for Category "
            f"{arch.category.value} (admissible: "
            f"{sorted(b.value for b in _ADMISSIBLE_DC[arch.category])})"
        )
    if m_band is not MTTFdBand.INVALID and m_band not in _ADMISSIBLE_MTTFD[arch.category]:
        violations.append(
            f"MTTFd band '{m_band.value}' is not admissible for Category "
            f"{arch.category.value}"
        )
    if arch.uses_ml_in_safety_path:
        notes.append(
            "Architecture declares machine learning in the safety path. Under "
            "Regulation (EU) 2023/1230 Annex I, safety components with fully or "
            "partially self-evolving behaviour using machine-learning approaches "
            "require third-party conformity assessment by a Notified Body; "
            "self-certification is not available. ISO 13849-1 provides no "
            "quantification route for such elements."
        )

    lookup_key = (arch.category, m_band, d_band)
    achieved = _COMBINATION.get(lookup_key)
    valid = not violations and achieved is not None
    if achieved is None and not violations:
        violations.append(
            f"no Figure 7 cell for (Cat {arch.category.value}, "
            f"MTTFd {m_band.value}, DC {d_band.value})"
        )

    return PLResult(
        architecture_ref=arch.ref,
        category=arch.category,
        channels=channels,
        mttfd_years=mttfd,
        mttfd_band=m_band,
        dc_avg=dc_avg,
        dc_band=d_band,
        ccf_score=arch.ccf_score,
        achieved_pl=achieved if valid else None,
        pfhd_band=PFHD_BANDS.get(achieved) if valid and achieved else None,
        valid=valid,
        violations=violations,
        notes=notes,
    )


__all__ = [
    "ChannelResult",
    "DCBand",
    "MTTFdBand",
    "PFHD_BANDS",
    "PLResult",
    "component_mttfd_years",
    "dc_band",
    "evaluate_architecture",
    "mttfd_band",
]
