# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""Toy solves for mods/constraints/carbon_accounting.py (spec section 6, U7-U14)."""

import warnings
from types import SimpleNamespace

import pandas as pd
import pypsa
import pytest

from evals.constants import DataModel
from evals.stats import collect_myopic_statistics
from evals.utils import get_energy_totals_domestic_share, scale_domestic_aviation
from mods.constants import (
    ATTRIBUTE_CARRIERS,
    AVIATION_CARRIERS,
    CAPTURE,
    CLAIM_CARRIERS,
    FAIR_SHARE_KEY,
    GREEN_BIO_PRODUCERS,
    GREEN_IMPORTS,
)
from mods.constraints.carbon_accounting import (
    claimable_consumers,
    constraint_carbon_accounting,
)
from mods.constraints.co2_budget import constraint_national_co2_budgets
from mods.network.carbon_accounting import (
    TRANSFORMATION_CLASSES,
    apply_carbon_accounting,
    gas_borders,
    link_ports,
    port_bus_carrier,
    port_efficiency,
)
from test.test_mods.conftest import (
    EF,
    build_carbon_network,
    build_gas_toy,
    build_oil_toy,
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


def build_capture_toy() -> pypsa.Network:
    """
    Build a toy with DAC and Sabatier in AT and storage in NL.

    AT1 captures 5 t/h with DAC. Sabatier uses part of it for synthetic gas,
    which a CCGT in AT1 burns. The rest is piped to NL1 and sequestered.
    """
    n = pypsa.Network()
    n.set_snapshots(range(2))
    n.add("Bus", "co2 atmosphere", carrier="co2", location="EU", unit="t_co2")
    n.add(
        "Store",
        "co2 atmosphere",
        bus="co2 atmosphere",
        carrier="co2",
        e_nom_extendable=True,
        e_min_pu=-1,
    )
    for node in ("AT1", "NL1"):
        n.add("Bus", node, carrier="AC", location=node)
        n.add("Generator", node, bus=node, carrier="AC", p_nom=100, marginal_cost=1)
        n.add("Bus", f"{node} co2 stored", carrier="co2 stored", location=node)
    for suffix, carrier in ((" gas", "gas"), (" H2", "H2")):
        n.add("Bus", f"AT1{suffix}", carrier=carrier, location="AT1")
    n.add("Generator", "AT1 H2", bus="AT1 H2", carrier="H2", p_nom=100)
    n.add(
        "Link",
        "AT1 DAC",
        carrier="DAC",
        bus0="AT1",
        bus1="AT1 co2 stored",
        bus2="co2 atmosphere",
        efficiency2=-1.0,
        p_nom=5.0,
        p_min_pu=1,
    )
    n.add(
        "Link",
        "AT1 Sabatier",
        carrier="Sabatier",
        bus0="AT1 H2",
        bus1="AT1 gas",
        bus2="AT1 co2 stored",
        efficiency=0.8,
        efficiency2=-0.8 * EF["gas"],
        p_nom=1.0,
        p_min_pu=1,
    )
    n.add(
        "Link",
        "AT1 CCGT",
        carrier="CCGT",
        bus0="AT1 gas",
        bus1="AT1",
        bus2="co2 atmosphere",
        efficiency=0.6,
        efficiency2=EF["gas"],
        p_nom=100,
    )
    n.add(
        "Link",
        "CO2 pipeline AT1 -> NL1",
        carrier="CO2 pipeline",
        bus0="AT1 co2 stored",
        bus1="NL1 co2 stored",
        p_nom=100,
    )
    n.add("Bus", "NL1 co2 sequestered", carrier="co2 sequestered", location="NL1")
    n.add(
        "Store",
        "NL1 co2 sequestered",
        bus="NL1 co2 sequestered",
        carrier="co2 sequestered",
        e_nom_extendable=True,
    )
    n.add(
        "Link",
        "NL1 co2 sequestered",
        carrier="co2 sequestered",
        bus0="NL1 co2 stored",
        bus1="NL1 co2 sequestered",
        p_nom=100,
    )
    return n


class TestCaptureAndUse:
    """U9: captured CO2 counts as removed in AT unless AT uses it for synthetic gas."""

    @pytest.fixture(scope="class")
    def n(self) -> pypsa.Network:
        return solve(build_capture_toy(), "carrier")

    def test_credit_stays_with_capturing_country(self, n):
        used = 2 * 0.8 * EF["gas"]
        assert national(n, "AT") == pytest.approx(-10.0 + used)
        assert national(n, "NL") == pytest.approx(0.0, abs=1e-6)

    def test_credit_equals_exported_co2(self, n):
        exported = n.links_t.p0["CO2 pipeline AT1 -> NL1"].sum()
        assert national(n, "AT") == pytest.approx(-exported)

    def test_ccu_charged_at_the_ccu_plant(self, n):
        assert booked(n)["AT1 Sabatier"] == pytest.approx(2 * 0.8 * EF["gas"])

    def test_same_total_as_technology(self, n):
        technology = solve(build_capture_toy(), "technology")
        assert booked(n).sum() == pytest.approx(booked(technology).sum())


def _solve_fair_share(tolerance: float | None) -> pypsa.Network:
    """Solve the oil toy with AT holding a quarter of the shipping demand."""
    n = build_oil_toy()
    n.loads.loc["AT1 shipping", "p_set"] = 5.0
    n.loads.loc["DE1 shipping", "p_set"] = 15.0
    snakemake = carbon_accounting_snakemake(budgets=("AT",), fair_share=tolerance)
    apply_carbon_accounting(n, snakemake, carbon_accounting_costs())
    # reward claims in AT, so AT would take the whole pool if it could
    n.links.loc["AT renewable oil claim", "marginal_cost"] = -1.0
    n.optimize(
        solver_name="highs",
        extra_functionality=lambda n, _: constraint_carbon_accounting(
            n, snakemake, 2050
        ),
    )
    return n


class TestFairShare:
    """U13 and U14: AT claims at most its key times the EU green production."""

    @pytest.mark.parametrize(
        ("tolerance", "expected"), [(None, 10.0), (1.0, 5.0), (2.0, 10.0)]
    )
    def test_at_claim(self, tolerance, expected):
        # 20 MWh green oil; AT consumes 10 MWh and has a key of 0.25
        p0 = _solve_fair_share(tolerance).links_t.p0.sum()
        assert p0["AT renewable oil claim"] == pytest.approx(expected)
        assert p0["DE renewable oil claim"] == pytest.approx(20.0 - expected)

    def test_constraint_names(self):
        n = build_oil_toy()
        snakemake = carbon_accounting_snakemake(budgets=("AT",), fair_share=1.0)
        apply_carbon_accounting(n, snakemake, carbon_accounting_costs())
        n.optimize.create_model()
        constraint_carbon_accounting(n, snakemake, 2050)
        names = {c for c in n.model.constraints if "fair-share" in c}
        assert names == {
            "carbon_accounting-fair-share-AT-oil",
            "carbon_accounting-fair-share-AT-oil-aviation",
            "carbon_accounting-fair-share-AT-methanol",
        }

    def test_disabled_adds_no_constraint(self):
        n = build_oil_toy()
        snakemake = carbon_accounting_snakemake(budgets=("AT",))
        apply_carbon_accounting(n, snakemake, carbon_accounting_costs())
        n.optimize.create_model()
        constraint_carbon_accounting(n, snakemake, 2050)
        assert not any("fair-share" in c for c in n.model.constraints)


def _require_carrier(nc) -> None:
    """Skip the integration tests for runs without carrier-based accounting."""
    methods = {
        n.meta["mods"].get("carbon_accounting", {}).get("method", "technology")
        for n in nc
    }
    if methods != {"carrier"}:
        pytest.skip(f"integration tests need method 'carrier', got {methods}")


def _annual_p0(n: pypsa.Network) -> pd.Series:
    weightings = n.snapshot_weightings.generators
    return (
        n.links_t.p0.mul(weightings, axis=0).sum().reindex(n.links.index, fill_value=0)
    )


def _carrier_delta(n: pypsa.Network) -> pd.Series:
    """Return ``EF_out * out + used`` of the transformed classes (section 3.2)."""
    ef = {
        fuel: -n.links.efficiency[n.links.carrier == CLAIM_CARRIERS[fuel]].iloc[0]
        for fuel in ("gas", "oil", "methanol")
    }
    carriers = [c for k in ("G1", "G2") for c in TRANSFORMATION_CLASSES[k]]
    idx = n.links.index[n.links.carrier.isin(carriers)]
    delta = pd.Series(0.0, index=idx)
    producer = n.links.loc[idx, "carrier"].isin(GREEN_BIO_PRODUCERS)
    for port in link_ports(n):
        carrier = port_bus_carrier(n, port).loc[idx]
        efficiency = port_efficiency(n, port).loc[idx]
        used = -efficiency.clip(upper=0.0).where(~producer, 0.0)
        delta += used.where(carrier == "co2 stored", 0.0)
        if port != "0":
            output = efficiency.clip(lower=0).where(producer, 0.0)
            delta += carrier.map(ef).fillna(0.0) * output
    return delta


class TestSolvedNetworks:
    """Integration tests I1-I5 on a run with method 'carrier'."""

    @pytest.fixture(autouse=True)
    def carrier_run(self, nc):
        _require_carrier(nc)

    def test_same_total_as_technology(self, nc):
        """I1: the EU total equals its technology equivalent (inverse rule)."""
        for n in nc:
            p0 = _annual_p0(n)
            emissions = booked(n)
            claims = n.links.carrier.isin(CLAIM_CARRIERS.values())
            imports = n.links.carrier.isin(GREEN_IMPORTS)
            delta = _carrier_delta(n)
            # carrier total = technology total + delta - imports + claims
            difference = (
                emissions[claims].sum()
                + (delta * p0[delta.index]).sum()
                + p0[imports].sum()
            )
            assert abs(difference) <= 1e-6 * emissions.abs().sum()

    def test_attributes_fully_retired(self, nc):
        """I2: every attribute store is empty again at the end of the year."""
        for n in nc:
            stores = n.stores.index[n.stores.carrier.isin(ATTRIBUTE_CARRIERS.values())]
            weightings = n.snapshot_weightings.stores
            net = n.stores_t.p[stores].mul(weightings, axis=0).sum()
            assert net.abs().max() <= 1e-3

    def test_only_claims_book_negative(self, nc):
        """I3: negative annual emissions only from claims, capture and municipal waste."""
        bio_cc = {c for c in GREEN_BIO_PRODUCERS if c.endswith(" CC")}
        allowed = {*CLAIM_CARRIERS.values(), *CAPTURE, *bio_cc, "municipal solid waste"}
        for n in nc:
            emissions = booked(n)
            p0 = _annual_p0(n).abs()
            negative = emissions[emissions < 0]
            negative = negative[~n.links.loc[negative.index, "carrier"].isin(allowed)]
            # tolerated: G1 residuals of at most 1e-3 t/MWh and solver noise
            assert (negative >= -1e-3 * p0[negative.index] - 1e-3).all(), negative

    def test_gas_circulation(self, nc):
        """I4: report the smaller directional gas flow per border."""
        for year, n in nc.networks.items():
            p0 = _annual_p0(n)
            borders = gas_borders(n)
            flows = p0[borders.index].groupby([borders["from"], borders["to"]]).sum()
            for (x, y), forward in flows.items():
                backward = flows.get((y, x), 0.0)
                if x < y and min(forward, backward) > 0.05 * max(forward, backward):
                    warnings.warn(
                        f"{year}: gas flows {x}->{y} {forward:.3g} MWh and "
                        f"{y}->{x} {backward:.3g} MWh in both directions.",
                        stacklevel=1,
                    )

    @pytest.mark.parametrize("ct", ["AT", "DE"])
    def test_national_budget_matches_evals(self, nc, ct):
        """I5: the national LHS equals the evals CO2 balance of the country."""
        first = nc[nc.index[0]]
        energy_totals = pd.DataFrame.from_dict(
            first.meta["resources"]["energy_totals"], orient="tight"
        )
        alpha = get_energy_totals_domestic_share(energy_totals, kind="aviation")
        balance = collect_myopic_statistics(
            nc, "energy_balance", bus_carrier="co2", aggregate_components=None
        )
        balance = scale_domestic_aviation(balance, alpha)
        for year, n in nc.networks.items():
            emissions = booked(n)
            emissions = emissions[emissions.index.str.startswith(ct)]
            aviation = n.links.loc[emissions.index, "carrier"].isin(AVIATION_CARRIERS)
            lhs = emissions[~aviation].sum() + alpha[ct] * emissions[aviation].sum()

            evals = balance.xs(year, level=DataModel.YEAR)
            location = evals.index.get_level_values(DataModel.LOCATION)
            # the evals book from the atmosphere's point of view: supply is positive
            in_country = location.str.startswith(ct)
            components = evals.index.get_level_values("component")
            evals_total = evals[in_country & (components == "Link")].sum()
            assert evals_total == pytest.approx(lhs, rel=1e-6, abs=1.0)


class TestClaimCost:
    """Countries with a national budget only claim what the budget needs."""

    def test_marginal_cost_per_t(self, carbon_network):
        snakemake = carbon_accounting_snakemake(budgets=("AT",))
        apply_carbon_accounting(carbon_network, snakemake, carbon_accounting_costs())
        cost = carbon_network.links.marginal_cost
        assert cost["AT renewable oil claim"] == pytest.approx(0.01 * EF["oil"])
        assert cost["DE renewable oil claim"] == 0.0

    def test_slack_budget_country_does_not_claim(self):
        n = build_oil_toy()
        snakemake = carbon_accounting_snakemake(budgets=("DE",))
        apply_carbon_accounting(n, snakemake, carbon_accounting_costs())
        n.optimize(
            solver_name="highs",
            extra_functionality=lambda n, _: constraint_carbon_accounting(
                n, snakemake, 2050
            ),
        )
        p0 = n.links_t.p0.sum()
        assert p0["DE renewable oil claim"] == pytest.approx(0.0, abs=1e-6)
        assert p0["AT renewable oil claim"] == pytest.approx(20.0)


class TestSolvedFairShare:
    """I6: on a run with the fair share, budget countries stay within their cap."""

    def test_claims_within_cap(self, nc):
        _require_carrier(nc)
        for n in nc:
            accounting = n.meta["mods"]["carbon_accounting"]
            if not accounting.get("fair_share", {}).get("enable", False):
                pytest.skip("integration test needs fair_share.enable")
            tolerance = accounting["fair_share"]["tolerance"]
            budgets = n.meta["solving"]["constraints"]["co2_budget_national"]
            p0 = _annual_p0(n)
            keys = n.links[FAIR_SHARE_KEY].dropna()
            keys = keys[keys.index.str[:2].isin(list(budgets))]
            for claim, key in keys.items():
                fuel = n.links.at[claim, "carrier"].split()[1]
                pool = f"EU renewable {fuel} attribute"
                production = sum(
                    (port_efficiency(n, port) * p0)[n.links[f"bus{port}"] == pool].sum()
                    for port in link_ports(n)[1:]
                )
                cap = tolerance * key * production
                assert p0[claim] <= cap * (1 + 1e-6) + 1e-3, claim
