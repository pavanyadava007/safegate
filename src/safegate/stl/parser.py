"""
safegate.stl.parser
===================

A small, total, recursive-descent parser for the STL surface syntax that
appears in requirement and test-case YAML.

Deliberate non-goal: expressive power. Safety requirements that need a
Turing-complete oracle are requirements nobody can assess. The grammar is
kept small enough that a TÜV assessor can be taught it in fifteen minutes,
which is a real acceptance criterion for this product.

Grammar (lowest precedence first)::

    formula   := implication
    implication := disjunction ( "->" implication )?
    disjunction := conjunction ( "or" conjunction )*
    conjunction := unary ( "and" unary )*
    unary     := "not" unary
               | ("always" | "G") interval? unary
               | ("eventually" | "F") interval? unary
               | ("once" | "O") interval? unary
               | atom ( ("until" | "U") interval? unary )?
    atom      := "(" formula ")" | comparison
    comparison := expr op expr | IDENT
    expr      := operand ( ("+" | "-") operand )*
    operand   := "-"? ( NUMBER | IDENT )
    interval  := "[" NUMBER "," (NUMBER | "inf") "]"
    op        := ">=" | "<=" | ">" | "<"

Expressions are linear (sums and differences of signals, parameters and
constants), which keeps robustness in physical units: `speed <= limit +
0.05` has robustness `limit + 0.05 - speed` in m/s. A bare identifier means
`x > 0.5`, for boolean signals.

Units: bare numbers are SI base units (metres, seconds, m/s). Intervals
are in seconds. A trailing unit suffix (``0.2s``, ``150ms``, ``50mm``) is
accepted and normalised, because safety engineers write ``150ms`` and
silently dropping the unit is how you ship a 1000x error.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .robustness import (
    Always,
    And,
    Comparison,
    Const,
    Eventually,
    Formula,
    Implies,
    LinearExpr,
    Not,
    Once,
    Or,
    Until,
)

_UNIT_SCALE: dict[str, float] = {
    "": 1.0,
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
    "m": 1.0,
    "mm": 1e-3,
    "cm": 1e-2,
    "km": 1e3,
    "deg": math.pi / 180.0,
    "rad": 1.0,
    "hz": 1.0,
}

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<num>\d+(?:\.\d*)?(?:[eE][+-]?\d+)?(?P<unit>ms|us|mm|cm|km|deg|rad|hz|s|m)?\b)
      | (?P<inf>\binf\b)
      | (?P<op>>=|<=|->|>|<|\+|-)
      | (?P<punc>[\[\],()])
      | (?P<ident>[A-Za-z_][A-Za-z_0-9./]*)
    )
    """,
    re.VERBOSE,
)

_KEYWORDS = {
    "and",
    "or",
    "not",
    "always",
    "eventually",
    "until",
    "once",
    "G",
    "F",
    "U",
    "O",
    "implies",
}


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    value: Any = None
    pos: int = 0


class STLSyntaxError(ValueError):
    """Raised with the offending column, because error messages are UX."""

    def __init__(self, msg: str, src: str, pos: int) -> None:
        pointer = " " * pos + "^"
        super().__init__(f"{msg}\n  {src}\n  {pointer}")


def tokenize(src: str) -> list[Token]:
    toks: list[Token] = []
    i = 0
    while i < len(src):
        if src[i].isspace():
            i += 1
            continue
        m = _TOKEN_RE.match(src, i)
        if not m or m.end() == i:
            raise STLSyntaxError("unexpected character", src, i)
        start = m.start(m.lastgroup or "num")
        if m.group("num"):
            unit = m.group("unit") or ""
            raw = m.group("num")
            if unit:
                raw = raw[: -len(unit)]
            toks.append(
                Token("num", m.group("num"), float(raw) * _UNIT_SCALE[unit], start)
            )
        elif m.group("inf"):
            toks.append(Token("num", "inf", math.inf, start))
        elif m.group("op"):
            toks.append(Token("op", m.group("op"), None, start))
        elif m.group("punc"):
            toks.append(Token("punc", m.group("punc"), None, start))
        else:
            ident = m.group("ident")
            kind = "kw" if ident in _KEYWORDS else "ident"
            toks.append(Token(kind, ident, None, start))
        i = m.end()
    toks.append(Token("eof", "", None, len(src)))
    return toks


class Parser:
    def __init__(self, src: str) -> None:
        self.src = src
        self.toks = tokenize(src)
        self.i = 0

    # ---- helpers --------------------------------------------------------
    @property
    def cur(self) -> Token:
        return self.toks[self.i]

    def eat(self, kind: str, text: str | None = None) -> Token:
        t = self.cur
        if t.kind != kind or (text is not None and t.text != text):
            want = text or kind
            raise STLSyntaxError(f"expected {want!r}, got {t.text!r}", self.src, t.pos)
        self.i += 1
        return t

    def accept(self, kind: str, text: str | None = None) -> Token | None:
        t = self.cur
        if t.kind == kind and (text is None or t.text == text):
            self.i += 1
            return t
        return None

    # ---- grammar --------------------------------------------------------
    def parse(self) -> Formula:
        f = self.implication()
        if self.cur.kind != "eof":
            raise STLSyntaxError("trailing input", self.src, self.cur.pos)
        return f

    def implication(self) -> Formula:
        left = self.disjunction()
        if self.accept("op", "->") or self.accept("kw", "implies"):
            return Implies(left, self.implication())
        return left

    def disjunction(self) -> Formula:
        f = self.conjunction()
        while self.accept("kw", "or"):
            f = Or(f, self.conjunction())
        return f

    def conjunction(self) -> Formula:
        f = self.unary()
        while self.accept("kw", "and"):
            f = And(f, self.unary())
        return f

    def interval(self) -> tuple[float, float]:
        if self.cur.kind == "punc" and self.cur.text == "[":
            start = self.eat("punc", "[")
            a = self.eat("num").value
            self.eat("punc", ",")
            b = self.eat("num").value
            self.eat("punc", "]")
            if not (0.0 <= a <= b) or math.isinf(a):
                # An empty or negative window makes `always` vacuously true,
                # which would pass every run.
                raise STLSyntaxError(
                    f"interval [{a}, {b}] must satisfy 0 <= a <= b with finite a",
                    self.src,
                    start.pos,
                )
            return float(a), float(b)
        return 0.0, math.inf

    def unary(self) -> Formula:
        t = self.cur
        if t.kind == "kw":
            if t.text == "not":
                self.i += 1
                return Not(self.unary())
            if t.text in ("always", "G"):
                self.i += 1
                a, b = self.interval()
                return Always(self.unary(), a, b)
            if t.text in ("eventually", "F"):
                self.i += 1
                a, b = self.interval()
                return Eventually(self.unary(), a, b)
            if t.text in ("once", "O"):
                self.i += 1
                a, b = self.interval()
                return Once(self.unary(), a, b)
        left = self.atom()
        if self.cur.kind == "kw" and self.cur.text in ("until", "U"):
            self.i += 1
            a, b = self.interval()
            return Until(left, self.unary(), a, b)
        return left

    def atom(self) -> Formula:
        if self.accept("punc", "("):
            f = self.implication()
            self.eat("punc", ")")
            return f
        return self.comparison()

    def operand(self) -> tuple[float, str | None]:
        sign = -1.0 if self.accept("op", "-") else 1.0
        t = self.cur
        if t.kind == "num":
            self.i += 1
            return sign * float(t.value), None
        if t.kind == "ident":
            self.i += 1
            return sign, t.text
        raise STLSyntaxError("expected a signal name or number", self.src, t.pos)

    def expr(self) -> str | float | LinearExpr:
        terms = [self.operand()]
        while self.cur.kind == "op" and self.cur.text in ("+", "-"):
            sign = 1.0 if self.eat("op").text == "+" else -1.0
            coef, name = self.operand()
            terms.append((sign * coef, name))
        if len(terms) == 1:
            coef, name = terms[0]
            if name is None:
                return coef
            if coef == 1.0:
                return name
        return LinearExpr(tuple(terms))

    def comparison(self) -> Formula:
        lhs = self.expr()
        t = self.cur
        if t.kind != "op" or t.text not in (">=", "<=", ">", "<"):
            # A bare identifier is allowed and treated as `x > 0.5`, which
            # covers boolean signals like `estop_engaged`.
            if isinstance(lhs, str):
                return Comparison(lhs, ">", 0.5)
            if isinstance(lhs, float):
                return Const(lhs)
            raise STLSyntaxError("expected a comparison operator", self.src, t.pos)
        self.i += 1
        rhs = self.expr()
        return Comparison(lhs, t.text, rhs)


def parse_stl(src: str) -> Formula:
    """Parse STL source into a Formula. Raises STLSyntaxError."""
    return Parser(src).parse()


__all__ = ["Parser", "STLSyntaxError", "Token", "parse_stl", "tokenize"]
