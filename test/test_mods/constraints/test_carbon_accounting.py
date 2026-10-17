# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""Toy solves for mods/constraints/carbon_accounting.py (spec section 6, U7-U9)."""

import pandas as pd
import pypsa
import pytest

from mods.constraints.carbon_accounting import constraint_carbon_accounting
from mods.network.carbon_accounting import apply_carbon_accounting, link_ports
from test.test_mods.conftest import (
    build_gas_toy,
    carbon_accounting_costs,
    carbon_accounting_snakemake,
)


def solve(n: pypsa.Network, method: str) -> pypsa.Network:
    """Apply the accounting method and solve the network with HiGHS."""
    snakemake = carbon_accounting_snakemake(method)
    apply_carbon_accounting(n, snakemake, carbon_accounting_costs())
    status, _ = n.optimize(
        solver_name="highs",
        extra_functionality=lambda n, _: constraint_carbon_accounting(
            n, snakemake, 2050
        ),
    )
    assert status == "ok"
    return n


def booked(n: pypsa.Network) -> pd.Series:
    """Return the annual emissions at ``co2 atmosphere`` per link."""
    weightings = n.snapshot_weightings.generators
    total = pd.Series(0.0, index=n.links.index)
    for port in link_ports(n):
        at_atm = n.links.index[n.links[f"bus{port}"] == "co2 atmosphere"]
        flow = -n.links_t[f"p{port}"][at_atm].mul(weightings, axis=0).sum()
        total = total.add(flow, fill_value=0.0)
    return total


def national(n: pypsa.Network, ct: str) -> float:
    """Return the annual emissions booked by the links of a country."""
    emissions = booked(n)
    return emissions[emissions.index.str.startswith(ct)].sum()


@pytest.fixture(scope="module")
def solved() -> pypsa.Network:
    """Return the solved U7 toy: biogas in AT, consumed in DE."""
    return solve(build_gas_toy(), "carrier")


class TestGasClaims:
    """U7: biogas in AT, consumed in DE."""

    def test_both_countries_book_zero(self, solved):
        assert national(solved, "AT") == pytest.approx(0.0, abs=1e-6)
        assert national(solved, "DE") == pytest.approx(0.0, abs=1e-6)

    def test_claim_is_in_consuming_country(self, solved):
        p0 = solved.links_t.p0.sum()
        assert p0["DE renewable gas claim"] == pytest.approx(20.0)
        assert p0["AT renewable gas claim"] == pytest.approx(0.0, abs=1e-6)

    def test_same_total_as_technology(self, solved):
        technology = solve(build_gas_toy(), "technology")
        assert booked(solved).sum() == pytest.approx(booked(technology).sum(), abs=1e-6)


def test_transfer_needs_gas_flow():
    """U8: without gas flow from AT to DE, DE cannot claim AT's attributes."""
    n = build_gas_toy(consumer_in_origin=True, pipeline_capacity=0.0)
    apply_carbon_accounting(n, carbon_accounting_snakemake(), carbon_accounting_costs())
    # reward claims in DE, so the solver would move the attributes if it could
    n.links.loc["DE renewable gas claim", "marginal_cost"] = -1.0
    snakemake = carbon_accounting_snakemake()
    n.optimize(
        solver_name="highs",
        extra_functionality=lambda n, _: constraint_carbon_accounting(
            n, snakemake, 2050
        ),
    )
    p0 = n.links_t.p0.sum()
    assert p0["DE renewable gas claim"] == pytest.approx(0.0, abs=1e-6)
    assert p0["AT renewable gas attribute -> DE"] == pytest.approx(0.0, abs=1e-6)
    assert p0["AT renewable gas claim"] == pytest.approx(20.0)


def test_disabled_adds_no_constraints():
    n = build_gas_toy()
    n.optimize.create_model()
    before = set(n.model.constraints)
    constraint_carbon_accounting(n, carbon_accounting_snakemake("technology"), 2050)
    assert set(n.model.constraints) == before
