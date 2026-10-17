# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""Toy solves for mods/constraints/carbon_accounting.py (spec section 6, U7-U9)."""

from types import SimpleNamespace

import pandas as pd
import pypsa
import pytest

from mods.constraints.carbon_accounting import (
    claimable_consumers,
    constraint_carbon_accounting,
)
from mods.constraints.co2_budget import constraint_national_co2_budgets
from mods.network.carbon_accounting import apply_carbon_accounting, link_ports
from test.test_mods.conftest import (
    EF,
    build_carbon_network,
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


def _coefficient(n: pypsa.Network, constraint: str, link: str) -> float:
    """Return the summed LHS coefficient of a link's flow in a constraint."""
    labels = n.model["Link-p"].loc[:, link].labels.values
    lhs = n.model.constraints[constraint].lhs
    coeffs, variables = lhs.coeffs.values.ravel(), lhs.vars.values.ravel()
    return coeffs[pd.Series(variables).isin(labels).to_numpy()].sum()


def test_aviation_claims_scaled_in_national_budget(tmp_path):
    """U10: kerosene and its green claim are scaled by the domestic share."""
    n = build_carbon_network(("AT1",))
    apply_carbon_accounting(n, carbon_accounting_snakemake(), carbon_accounting_costs())
    n.links["marginal_cost"] = 1.0  # the model needs an objective
    n.meta = {
        "sector": {
            "transport": True,
            "heating": False,
            "industry": False,
            "agriculture": False,
        }
    }
    co2_totals = tmp_path / "co2_totals.csv"
    pd.DataFrame(
        {"electricity": [1.0], "rail non-elec": [1.0], "road non-elec": [1.0]},
        index=pd.Index(["AT"], name="country"),
    ).to_csv(co2_totals)
    energy_totals = tmp_path / "energy_totals.csv"
    pd.DataFrame(
        {"total domestic aviation": [1.0], "total international aviation": [3.0]},
        index=pd.MultiIndex.from_tuples([("AT", 2019)], names=["country", "year"]),
    ).to_csv(energy_totals)
    snakemake = SimpleNamespace(
        params=SimpleNamespace(
            solving={"constraints": {"co2_budget_national": {"AT": {2050: 0.5}}}},
            energy_totals_year=2019,
        ),
        input=SimpleNamespace(
            co2_totals_name=str(co2_totals), energy_totals=str(energy_totals)
        ),
    )
    n.optimize.create_model()
    constraint_national_co2_budgets(n, snakemake, 2050)

    name = "GlobalConstraint-co2_limit-AT"
    snapshots = len(n.snapshots)
    alpha = 0.25
    claim = _coefficient(n, name, "AT renewable oil claim aviation")
    kerosene = _coefficient(n, name, "AT1 kerosene for aviation")
    shipping = _coefficient(n, name, "AT1 shipping oil")
    assert claim == pytest.approx(-EF["oil"] * alpha * snapshots)
    assert kerosene == pytest.approx(EF["oil"] * alpha * snapshots)
    assert shipping == pytest.approx(EF["oil"] * snapshots)


def build_oil_toy() -> pypsa.Network:
    """
    Extend the gas toy with oil: Fischer-Tropsch in AT, oil users in both.

    AT1 makes 10 MW of synthetic oil from H2 and stored CO2. AT1 and DE1 each
    burn 10 MW in shipping and fossil oil covers the rest. A DE reward on
    claims shows that oil attributes reach any country (EU pool).
    """
    n = build_gas_toy()
    for node in ("AT1",):
        n.add("Bus", f"{node} H2", carrier="H2", location=node)
        n.add("Bus", f"{node} co2 stored", carrier="co2 stored", location=node)
        n.add("Generator", f"{node} H2", bus=f"{node} H2", carrier="H2", p_nom=100)
        n.add(
            "Generator",
            f"{node} co2 stored",
            bus=f"{node} co2 stored",
            carrier="co2 stored",
            p_nom=100,
        )
    n.add(
        "Link",
        "AT1 Fischer-Tropsch",
        carrier="Fischer-Tropsch",
        bus0="AT1 H2",
        bus1="EU oil",
        bus2="AT1 co2 stored",
        efficiency=1.0,
        efficiency2=-EF["oil"],
        p_nom=10,
        p_min_pu=1,
    )
    n.add(
        "Generator", "EU oil", bus="EU oil", carrier="oil", p_nom=100, marginal_cost=50
    )
    for node in ("AT1", "DE1"):
        n.add("Bus", f"{node} shipping", carrier="shipping oil", location=node)
        n.add("Load", f"{node} shipping", bus=f"{node} shipping", p_set=10)
        n.add(
            "Link",
            f"{node} shipping oil",
            carrier="shipping oil",
            bus0="EU oil",
            bus1=f"{node} shipping",
            bus2="co2 atmosphere",
            efficiency2=EF["oil"],
            p_nom=100,
        )
    return n


def test_oil_attributes_are_pooled():
    """Oil attributes made in AT can be claimed in DE without any physical link."""
    n = build_oil_toy()
    snakemake = carbon_accounting_snakemake()
    apply_carbon_accounting(n, snakemake, carbon_accounting_costs())
    n.links.loc["DE renewable oil claim", "marginal_cost"] = -1.0
    n.optimize(
        solver_name="highs",
        extra_functionality=lambda n, _: constraint_carbon_accounting(
            n, snakemake, 2050
        ),
    )
    p0 = n.links_t.p0.sum()
    assert p0["DE renewable oil claim"] == pytest.approx(20.0)
    assert p0["AT renewable oil claim"] == pytest.approx(0.0, abs=1e-6)
    assert national(n, "DE") == pytest.approx(0.0, abs=1e-6)
    assert national(n, "AT") == pytest.approx(20 * EF["oil"])


def test_oil_claims_bounded_by_consumption():
    """A country cannot claim more green oil than it consumes."""
    n = build_oil_toy()
    n.loads.loc["DE1 shipping", "p_set"] = 4.0
    snakemake = carbon_accounting_snakemake()
    apply_carbon_accounting(n, snakemake, carbon_accounting_costs())
    n.links.loc["DE renewable oil claim", "marginal_cost"] = -1.0
    n.optimize(
        solver_name="highs",
        extra_functionality=lambda n, _: constraint_carbon_accounting(
            n, snakemake, 2050
        ),
    )
    p0 = n.links_t.p0.sum()
    assert p0["DE renewable oil claim"] == pytest.approx(8.0)
    assert p0["AT renewable oil claim"] == pytest.approx(12.0)


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ("gas", ["AT1 SMR CC", "AT1 CCGT"]),
        ("oil", ["AT1 shipping oil"]),
        ("oil aviation", ["AT1 kerosene for aviation"]),
        ("methanol", ["AT1 industry methanol"]),
    ],
)
def test_claimable_consumers(carbon_network, claim, expected):
    """Claims are bounded by consumption, split for aviation; pipelines are excluded."""
    assert claimable_consumers(carbon_network, "AT", claim).tolist() == expected
