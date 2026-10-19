"""The query typed into a Positions / Trades search box.

One parser for every view that has a "Search..." field, so the syntax
is defined once:

  * whitespace separates terms; every term is a case-insensitive
    substring and the positive ones must ALL match (``qqq oct30``);
  * ``not sgov`` or ``-sgov`` EXCLUDES rows where any searched field
    contains "sgov";
  * a bare / trailing ``not`` is ignored, and ``-`` only negates at the
    start of a term (an alias like ``Q-ib`` searches normally).

Qt-free.  Each model keeps a ``SearchQuery`` where it used to keep the
lowercased text; the frozen dataclass compares by value, which is what
the "did the filter change?" guards rely on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class SearchQuery:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str | None) -> "SearchQuery":
        include: list[str] = []
        exclude: list[str] = []
        negate_next = False
        for word in (text or "").lower().split():
            if negate_next:
                exclude.append(word)
                negate_next = False
            elif word == "not":
                negate_next = True
            elif len(word) > 1 and word[0] == "-":
                exclude.append(word[1:])
            else:
                include.append(word)
        return cls(tuple(include), tuple(exclude))

    def __bool__(self) -> bool:
        return bool(self.include or self.exclude)

    def matches(self, fields: Iterable) -> bool:
        """True when every positive term appears in at least one field
        and no excluded term appears in any.  Fields may be ``None``,
        strings, or sets / lists / tuples (joined with spaces, so
        ``cme`` matches ``{"CME"}``).  An empty query matches all."""
        if not self.include and not self.exclude:
            return True
        hay: list[str] = []
        for v in fields:
            if v is None or v == "":
                continue
            if isinstance(v, (set, list, tuple, frozenset)):
                s = " ".join(str(x) for x in v).lower()
            else:
                s = str(v).lower()
            if s:
                hay.append(s)
        for term in self.exclude:
            if any(term in s for s in hay):
                return False
        for term in self.include:
            if not any(term in s for s in hay):
                return False
        return True
