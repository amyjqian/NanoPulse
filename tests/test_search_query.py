"""``SearchQuery`` — the one parser behind the Positions / Trades
search boxes: AND-ed substring terms, ``not x`` / ``-x`` exclusions."""
from __future__ import annotations

import pytest

from src.ui.widgets.search_query import SearchQuery as Q


class TestParse:
    @pytest.mark.parametrize("text,include,exclude", [
        ("sgov", ("sgov",), ()),
        ("  SGOV  ", ("sgov",), ()),
        ("not sgov", (), ("sgov",)),
        ("NOT SGOV", (), ("sgov",)),
        ("-sgov", (), ("sgov",)),
        ("qqq not opt", ("qqq",), ("opt",)),
        ("qqq -opt", ("qqq",), ("opt",)),
        ("not sgov not qqq", (), ("sgov", "qqq")),
        ("qqq oct30", ("qqq", "oct30"), ()),
        ("not", (), ()),
        ("qqq not", ("qqq",), ()),
        ("", (), ()),
        (None, (), ()),
        ("q-ib", ("q-ib",), ()),      # a dash inside a term is literal
        ("-", ("-",), ()),            # a lone dash is literal too
    ])
    def test_terms(self, text, include, exclude):
        assert Q.parse(text) == Q(include, exclude)

    def test_truthiness(self):
        assert not Q.parse("")
        assert not Q.parse("not")
        assert Q.parse("not sgov")
        assert Q.parse("sgov")

    def test_equality_detects_a_changed_filter(self):
        assert Q.parse("not sgov") == Q.parse("-sgov")
        assert Q.parse("sgov") != Q.parse("not sgov")


class TestMatches:
    def test_empty_query_matches_everything(self):
        assert Q.parse("").matches([])
        assert Q.parse("not").matches(["SGOV"])

    def test_positive_term_any_field(self):
        assert Q.parse("sgov").matches(["ACC1", None, "", "SGOV"])
        assert not Q.parse("sgov").matches(["ACC1", "QQQ"])

    def test_exclusion_hides_a_hit_in_any_field(self):
        q = Q.parse("not sgov")
        assert q.matches(["ACC1", "QQQ"])
        assert not q.matches(["ACC1", "SGOV"])
        assert not q.matches(["SGOV-alias", "QQQ"])

    def test_positives_are_anded(self):
        q = Q.parse("qqq 2510")
        assert q.matches(["QQQ", "QQQ   251030P00680000"])
        assert not q.matches(["QQQ", "QQQ"])

    def test_mixed(self):
        q = Q.parse("acc1 not sgov")
        assert q.matches(["ACC1", "QQQ"])
        assert not q.matches(["ACC1", "SGOV"])
        assert not q.matches(["ACC2", "QQQ"])

    def test_set_valued_fields_are_flattened(self):
        assert Q.parse("cme").matches([{"CME", "GLOBEX"}])
        assert not Q.parse("not cme").matches([("NASDAQ", "CME")])
        assert Q.parse("nasdaq").matches([["NASDAQ"]])

    def test_accepts_a_generator(self):
        d = {"symbol": "SGOV", "account": "ACC1"}
        assert not Q.parse("not sgov").matches(d.get(k) for k in d)
