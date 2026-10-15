"""Margin / Cash classification of an account.

IB exposes no margin-vs-cash flag on the API.  The TWS Account window
derives it, and so does ``AccountsTableModel.account_kind``: a cash
account's BuyingPower equals its AvailableFunds, a Reg T margin account's
is ~3.3-4×, portfolio margin ~6.7×.  ``AccountType`` carries the
ownership / registration structure; a registered plan (RRSP, TFSA, IRA…)
is always cash and is badged by its own name.  Both tags already arrive
in the account-summary batches, so no new IB request is involved.
"""
from __future__ import annotations

import pytest

from src.ui.widgets.accounts_model import AccountsTableModel, registered_plan


def _model(*rows):
    m = AccountsTableModel()
    m.update_batch(list(rows))
    return m


class TestAccountKind:
    def test_margin_from_buying_power_ratio(self):
        # The maya paper account, as probed: 4.6M BP on 1.38M available.
        m = _model(("L", "DU1", "NetLiquidation", "1408196", "USD"),
                   ("L", "DU1", "AccountType", "INDIVIDUAL", ""),
                   ("L", "DU1", "AvailableFunds", "1382479", "USD"),
                   ("L", "DU1", "BuyingPower", "4608262", "USD"))
        assert m.account_kind("DU1") == "Margin"
        assert m.account_type("DU1") == "INDIVIDUAL"

    def test_cash_when_buying_power_equals_available(self):
        m = _model(("L", "U1", "NetLiquidation", "50000", "USD"),
                   ("L", "U1", "AvailableFunds", "48000", "USD"),
                   ("L", "U1", "BuyingPower", "48000", "USD"))
        assert m.account_kind("U1") == "Cash"

    def test_unknown_until_both_inputs_arrive(self):
        m = _model(("L", "U1", "NetLiquidation", "50000", "USD"),
                   ("L", "U1", "BuyingPower", "160000", "USD"))
        assert m.account_kind("U1") == ""

    def test_registered_plan_wins_and_is_named(self):
        m = _model(("L", "U2", "NetLiquidation", "50000", "CAD"),
                   ("L", "U2", "AccountType", "TFSA", ""),
                   ("L", "U2", "AvailableFunds", "1000", "CAD"),
                   ("L", "U2", "BuyingPower", "4000", "CAD"))
        assert m.account_kind("U2") == "TFSA"

    def test_fully_committed_funds_keep_last_answer(self):
        m = _model(("L", "U1", "NetLiquidation", "50000", "USD"),
                   ("L", "U1", "AvailableFunds", "10000", "USD"),
                   ("L", "U1", "BuyingPower", "40000", "USD"))
        assert m.account_kind("U1") == "Margin"
        m.update_batch([("L", "U1", "AvailableFunds", "0", "USD"),
                        ("L", "U1", "BuyingPower", "0", "USD")])
        assert m.account_kind("U1") == "Margin"

    def test_forget_label_purges(self):
        m = _model(("L", "U1", "NetLiquidation", "50000", "USD"),
                   ("L", "U1", "AccountType", "INDIVIDUAL", ""),
                   ("L", "U1", "AvailableFunds", "10000", "USD"),
                   ("L", "U1", "BuyingPower", "40000", "USD"))
        m.forget_label("L")
        assert m.account_kind("U1") == ""
        assert m.account_type("U1") == ""


class TestRegisteredPlan:
    @pytest.mark.parametrize("atype,plan", [
        ("INDIVIDUAL", ""), ("JOINT", ""), ("CORPORATION", ""),
        ("IRA-ROTH-NEW", "IRA"), ("IRA-TRAD-NEW", "IRA"),
        ("RRSP", "RRSP"), ("TFSA", "TFSA"), ("", ""),
    ])
    def test_tokens(self, atype, plan):
        assert registered_plan(atype) == plan
