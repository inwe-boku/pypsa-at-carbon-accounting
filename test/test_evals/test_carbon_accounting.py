# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""Unit tests for evals/carbon_accounting.py on toy networks."""

import numpy as np
import pandas as pd
import pypsa
import pytest

import mods.constants
import mods.network.carbon_accounting
from evals import carbon_accounting as ca
from mods.constraints.carbon_accounting import constraint_carbon_accounting
from mods.network.carbon_accounting import apply_carbon_accounting
from test.test_mods.conftest import (
    EF,
    build_gas_toy,
    build_oil_toy,
    carbon_accounting_costs,
    carbon_accounting_snakemake,
)

# domestic aviation share of AT in the toy metadata
ALPHA = 0.25


def solve(
    n: pypsa.Network,
    method: str,
    budgets: tuple[str, ...] = (),
    fair_share: float | None = None,
    rewards: dict[str, float] | None = None,
) -> pypsa.Network:
    """
    Apply the accounting method, solve with HiGHS and store the run config.

    ``rewards`` puts a negative marginal cost on claim links, to steer claims.
    """
    snakemake = carbon_accounting_snakemake(
        method, budgets=budgets, fair_share=fair_share
    )
    apply_carbon_accounting(n, snakemake, carbon_accounting_costs())
    for link, reward in (rewards or {}).items():
        n.links.loc[link, "marginal_cost"] = -reward
    status, _ = n.optimize(
        solver_name="highs",
        extra_functionality=lambda n, _: constraint_carbon_accounting(
            n, snakemake, 2050
        ),
    )
    assert status == "ok"
    energy_totals = pd.DataFrame(
        {
            "total domestic aviation": {"AT": 1.0, "DE": 1.0},
            "total international aviation": {"AT": 3.0, "DE": 0.0},
        }
    )
    n.meta = snakemake.config | {
        "resources": {"energy_totals": energy_totals.to_dict(orient="tight")}
    }
    return n


def add_budget(n: pypsa.Network, country: str, mu: float, constant: float = 0.0):
    """Add a solved national budget constraint with a given dual."""
    n.add("GlobalConstraint", f"co2_limit-{country}", constant=constant, sense="<=")
    n.global_constraints.loc[f"co2_limit-{country}", "mu"] = mu


def oil_toy() -> pypsa.Network:
    """Return the oil toy with idle green imports, which carry the emission factors."""
    n = build_oil_toy()
    for fuel, bus in [
        ("gas", "AT1 gas"),
        ("oil", "EU oil"),
        ("methanol", "EU methanol"),
    ]:
        n.add(
            "Link",
            f"{bus} import",
            carrier=f"import {fuel}",
            bus0="co2 atmosphere",
            bus1=bus,
            efficiency=1 / EF[fuel],
            p_nom=0.0,
        )
    return n


@pytest.fixture(scope="module")
def oil_technology() -> pypsa.Network:
    return solve(build_oil_toy(), "technology")


@pytest.fixture(scope="module")
def oil_carrier() -> pypsa.Network:
    n = solve(build_oil_toy(), "carrier", budgets=("AT",))
    add_budget(n, "AT", mu=-1.0, constant=5.0)
    return n


@pytest.mark.parametrize(
    "name",
    [
        "GREEN_BIO_PRODUCERS",
        "GREEN_SYNTHETIC_PRODUCERS",
        "GREEN_IMPORTS",
        "CAPTURE",
        "VENT",
    ],
)
def test_carriers_mirror_mods_constants(name):
    assert getattr(ca, name) == getattr(mods.constants, name)


def test_accounting_carriers_mirror_mods_constants():
    assert ca.CLAIM_KINDS == {v: k for k, v in mods.constants.CLAIM_CARRIERS.items()}
    assert set(ca.ATTRIBUTE_CARRIERS) == set(mods.constants.ATTRIBUTE_CARRIERS.values())


class TestDiscovery:
    @pytest.fixture(scope="class")
    @classmethod
    def root(cls, tmp_path_factory, oil_technology, oil_carrier):
        root = tmp_path_factory.mktemp("results")
        for scenario, n in [("base", oil_technology), ("carrier", oil_carrier)]:
            (root / scenario / "networks").mkdir(parents=True)
            for horizon in ("2030", "2050"):
                n.export_to_netcdf(
                    root / scenario / "networks" / f"solved_{horizon}.nc"
                )
        (root / "empty").mkdir()
        return root

    def test_find_scenarios(self, root):
        files = ca.find_scenarios(root)
        assert list(files) == ["base", "carrier"]
        assert list(files["base"]) == ["2030", "2050"]

    def test_find_scenarios_missing_root(self, tmp_path):
        assert ca.find_scenarios(tmp_path / "missing") == {}

    def test_run_info(self, root):
        info = ca.run_info(ca.find_scenarios(root))
        assert info.xs("base", level="scenario").method.eq("technology").all()
        assert info.xs("carrier", level="scenario").method.eq("carrier").all()
        assert info.at[("carrier", "2030"), "claim_cost"] == pytest.approx(0.01)
        assert info.at[("carrier", "2030"), "budgets"] == "AT"
        assert ca.default_reference(info) == "base"

    def test_load_networks(self, root):
        networks = ca.load_networks(root / "carrier")
        assert list(networks) == ["2030", "2050"]


class TestCheckSetup:
    @pytest.fixture
    def info(self):
        index = pd.MultiIndex.from_tuples(
            [("a", "2030"), ("a", "2050"), ("b", "2030"), ("b", "2050"), ("c", "2030")],
            names=["scenario", "horizon"],
        )
        return pd.DataFrame({"budgets": ["AT, DE"] * 4 + ["DE"]}, index=index)

    def test_ok(self, info):
        assert ca.check_setup(info, ["a", "b"], "AT").status == "ok"

    def test_too_few_scenarios(self, info):
        assert ca.check_setup(info, ["a"], "AT").failed

    def test_different_horizons(self, info):
        assert ca.check_setup(info, ["a", "c"], "AT").failed

    def test_no_budget_warns(self, info):
        info.loc[("b", "2050"), "budgets"] = "DE"
        assert ca.check_setup(info, ["a", "b"], "AT").status == "warn"


def test_port_flows_signs(oil_technology):
    flows = ca.port_flows(oil_technology).set_index(["component", "name", "port"])
    # the load withdraws, the generator supplies, the link withdraws at bus0
    assert flows.at[("Load", "DE1 heat", "0"), "energy"] == pytest.approx(20.0)
    assert flows.at[("Generator", "AT1 biogas", "0"), "energy"] == pytest.approx(-20.0)
    assert flows.at[("Link", "AT1 biogas to gas", "0"), "energy"] == pytest.approx(20.0)
    assert flows.at[("Link", "AT1 biogas to gas", "1"), "energy"] == pytest.approx(
        -20.0
    )
    assert flows.at[("Link", "AT1 biogas to gas", "0"), "asset_country"] == "AT"
    assert flows.at[("Link", "AT1 shipping oil", "0"), "bus_country"] == "EU"


class TestNationalCategories:
    """The categories add up to the national budget LHS (name prefix, alpha)."""

    def test_technology(self, oil_technology):
        balance = ca.national_co2_categories(oil_technology, "AT")
        assert balance[ca.CREATION_CREDIT] == pytest.approx(-20 * EF["gas"])
        assert balance[ca.CAPTURE_CREDIT] == pytest.approx(-20 * EF["oil"])
        assert balance[ca.FUEL_USE] == pytest.approx(20 * EF["oil"])
        assert balance[ca.CCU_CHARGE] == pytest.approx(0.0)
        assert balance.sum() == pytest.approx(-20 * EF["gas"])

    def test_carrier(self, oil_carrier):
        balance = ca.national_co2_categories(oil_carrier, "AT")
        assert balance[ca.CREATION_CREDIT] == pytest.approx(0.0, abs=1e-9)
        assert balance[ca.CCU_CHARGE] == pytest.approx(20 * EF["oil"])
        assert balance[ca.CAPTURE_CREDIT] == pytest.approx(-20 * EF["oil"])
        assert balance[ca.FUEL_USE] == pytest.approx(20 * EF["oil"])

    def test_claims_retire_all_attributes(self, oil_carrier):
        claims = list(ca.CLAIM_CATEGORIES.values())
        total = sum(
            ca.national_co2_categories(oil_carrier, ct)[claims].sum()
            for ct in ("AT", "DE")
        )
        assert total == pytest.approx(-20 * EF["gas"] - 20 * EF["oil"])

    def test_same_eu_total(self, oil_technology, oil_carrier):
        technology, carrier = (
            sum(ca.national_co2_categories(n, ct).sum() for ct in ("AT", "DE"))
            for n in (oil_technology, oil_carrier)
        )
        assert technology == pytest.approx(carrier)

    def test_aviation_scaled(self):
        n = build_oil_toy()
        n.links.loc["AT1 shipping oil", "carrier"] = "kerosene for aviation"
        n = solve(n, "technology")
        balance = ca.national_co2_categories(n, "AT")
        assert balance[ca.FUEL_USE] == pytest.approx(ALPHA * 20 * EF["oil"])


class TestBridge:
    @pytest.fixture(scope="class")
    @classmethod
    def bridge(cls, oil_technology, oil_carrier):
        networks = {"base": {"2050": oil_technology}, "carrier": {"2050": oil_carrier}}
        return ca.balance_bridge(networks, "AT")

    def test_columns(self, bridge):
        assert list(bridge.columns) == [*ca.CATEGORIES, "Total", "Budget"]
        assert bridge.index.names == ["scenario", "horizon"]

    def test_budget(self, bridge):
        assert bridge.at[("carrier", "2050"), "Budget"] == pytest.approx(5.0 / ca.MT)
        assert pd.isna(bridge.at[("base", "2050"), "Budget"])

    def test_waterfall_closes(self, bridge):
        steps = ca.waterfall(bridge, "2050", "base", "carrier")
        assert steps.step.iloc[0] == "base"
        assert steps.step.iloc[-1] == "carrier"
        deltas = steps[steps.group != "total"]
        assert deltas.end.iloc[-1] == pytest.approx(steps.value.iloc[-1])
        assert (deltas.end - deltas.base).to_numpy() == pytest.approx(
            deltas.value.to_numpy()
        )
        assert set(deltas.group) <= {"physical", "accounting"}

    def test_waterfall_cutoff(self, bridge):
        steps = ca.waterfall(bridge, "2050", "base", "carrier", cutoff=1e-9)
        assert "Δ Process & waste" not in steps.step.to_list()


class TestConsistency:
    @pytest.fixture(scope="class")
    @classmethod
    def table(cls, oil_technology, oil_carrier):
        networks = {"base": {"2050": oil_technology}, "carrier": {"2050": oil_carrier}}
        return ca.consistency(networks, "AT")

    def test_checks_pass(self, table):
        checks = ca.consistency_checks(table)
        assert [c.status for c in checks] == ["ok"] * 4

    def test_eu_total_differs(self, table):
        table = table.copy()
        table.loc[("carrier", "2050"), "eu_total"] += 1.0
        assert ca.consistency_checks(table)[0].failed

    def test_national_balance_differs(self, table):
        table = table.copy()
        table.loc[("carrier", "2050"), "evals_balance"] += 1.0
        assert ca.consistency_checks(table)[2].failed

    def test_fair_share_exceeded(self, table):
        table = table.copy()
        table.loc[("carrier", "2050"), "fair_share_excess"] = 0.1
        assert ca.consistency_checks(table)[3].failed


class TestBudgetRegime:
    def test_no_budget(self, oil_technology):
        assert ca.budget_regime(oil_technology, "AT")[0] == "none"

    def test_slack(self):
        n = solve(build_oil_toy(), "carrier", budgets=("AT",))
        add_budget(n, "AT", mu=0.0)
        assert ca.budget_regime(n, "AT")[0] == "slack"

    def test_technology_is_physical(self):
        n = solve(build_oil_toy(), "technology")
        add_budget(n, "AT", mu=-50.0)
        assert ca.budget_regime(n, "AT")[0] == "physical"

    def test_paper(self):
        # DE claims part of the oil pool, AT only what it needs at the claim cost
        n = solve(build_oil_toy(), "carrier", budgets=("AT",))
        add_budget(n, "AT", mu=-0.01)
        assert ca.budget_regime(n, "AT") == ("paper", "")

    def test_bound_binds(self):
        # AT claims all oil it consumes; at a high price the claim bound binds
        rewards = {"AT renewable oil claim": 1.0}
        n = solve(build_oil_toy(), "carrier", budgets=("AT",), rewards=rewards)
        add_budget(n, "AT", mu=-50.0)
        bounds = ca.credit_bounds(n, "AT")
        assert bounds.at["carbon_accounting-claim-AT-oil", "tight"]
        regime, binding = ca.budget_regime(n, "AT")
        assert regime == "physical"
        assert "carbon_accounting-claim-AT-oil" in binding

    def test_pool_scarce(self):
        # AT claims the whole pool, DE (no budget) none; the price is at the claim cost
        rewards = {"AT renewable oil claim": 1.0}
        n = solve(build_oil_toy(), "carrier", budgets=("AT",), rewards=rewards)
        n.links.loc["AT renewable oil claim", "marginal_cost"] = 0.01 * EF["oil"]
        add_budget(n, "AT", mu=-0.01)
        assert ca.pool_claims(n).at["oil", "other"] == pytest.approx(0.0, abs=1e-6)
        assert ca.budget_regime(n, "AT") == ("pool-scarce", "oil pool")


def test_credit_bounds_transfer():
    """The gas transfer into DE is bounded by the pipeline flow from AT."""
    n = solve(build_gas_toy(), "carrier", budgets=("DE",))
    add_budget(n, "DE", mu=-50.0)
    bounds = ca.credit_bounds(n, "DE")
    transfer = bounds.loc["carbon_accounting-transfer-AT-DE"]
    assert transfer.bound == pytest.approx(20.0)
    assert transfer.tight
    assert transfer.dual == pytest.approx(50 * EF["gas"] - 0.01 * EF["gas"])


class TestCost:
    @pytest.fixture(scope="class")
    @classmethod
    def solved(cls):
        # fossil gas in DE sets the gas price
        n = build_gas_toy()
        n.loads.loc["DE1 heat", "p_set"] = 15.0
        return solve(n, "technology")

    def test_trade_is_zero_sum(self, solved):
        at, de = ca.country_cost(solved, "AT"), ca.country_cost(solved, "DE")
        price = solved.buses_t.marginal_price["DE1 gas"]
        exported = -solved.links_t.p1["gas pipeline AT1 -> DE1"]
        assert at.trade == pytest.approx(-(exported * price).sum())
        assert at.trade < 0
        assert de.trade == pytest.approx(-at.trade)

    def test_total(self, solved):
        cost = ca.country_cost(solved, "DE")
        assert cost.total == pytest.approx(cost.capex + cost.opex + cost.trade)

    def test_fuel_and_attribute_valued_together(self):
        # DE burns only green gas from AT and claims all of it: a shift between the
        # gas and the attribute price (non-unique duals) leaves its cost unchanged
        rewards = {"DE renewable gas claim": 1.0}
        n = solve(build_gas_toy(), "carrier", budgets=("DE",), rewards=rewards)
        before = ca.country_cost(n, "DE").trade
        prices = n.buses_t.marginal_price
        prices["DE1 gas"] += 1000.0
        prices["DE renewable gas attribute"] -= 1000.0
        assert ca.country_cost(n, "DE").trade == pytest.approx(before)

    def test_target_cost_deltas(self, solved, oil_technology):
        table = ca.target_cost({"a": {"2050": solved}, "b": {"2050": solved}}, "AT")
        table = ca.add_cost_deltas(table, "a")
        assert table["Δ system cost"].to_numpy() == pytest.approx([0.0, 0.0])
        assert table.regime.eq("none").all()


class TestPhysicalResponse:
    @pytest.fixture(scope="class")
    @classmethod
    def response(cls, oil_carrier):
        return ca.physical_response({"carrier": {"2050": oil_carrier}}, "AT")

    def get(self, response, group, item):
        return response.xs((group, item), level=["group", "item"]).iloc[0]

    def test_technologies(self, response):
        assert self.get(response, "output", "Fischer-Tropsch") == pytest.approx(
            20 / ca.TWH
        )
        assert self.get(response, "output", "DAC") == pytest.approx(
            20 * EF["oil"] / ca.MT
        )

    def test_co2(self, response):
        assert self.get(response, "co2", "captured") == pytest.approx(
            20 * EF["oil"] / ca.MT
        )
        assert self.get(response, "co2", "used for CCU") == pytest.approx(
            20 * EF["oil"] / ca.MT
        )

    def test_fuel_balance_closes(self, response):
        gas = response.xs("fuel gas", level="group")
        assert gas.sum() == pytest.approx(0.0, abs=1e-12)
        assert self.get(response, "fuel gas", "border flows") == pytest.approx(
            -20 / ca.TWH
        )

    def test_borders(self, response):
        assert self.get(response, "border gas", "export to DE") == pytest.approx(
            20 / ca.TWH
        )
        assert self.get(
            response, "border gas attributes", "export to DE"
        ) == pytest.approx(20 / ca.TWH)


def test_accounting_link_carriers_mirror_mods_constants():
    assert set(ca.ACCOUNTING_LINK_CARRIERS) == set(
        mods.network.carbon_accounting.ACCOUNTING_LINK_CARRIERS
    )


class TestImplicitTransfers:
    """Attributes are paid at the shadow price of their bus."""

    EU_PRICE, MU = 200.0, 50.0
    # attribute price per t CO2, between the EU price and the national value
    PRICE = 220.0

    @classmethod
    def priced_carrier(cls) -> pypsa.Network:
        """Return a carrier run with an EU and an AT CO2 price and set attribute prices."""
        n = solve(oil_toy(), "carrier", budgets=("AT",))
        add_budget(n, "AT", mu=-cls.MU)
        add_budget(n, "EU", mu=-cls.EU_PRICE)
        n.global_constraints = n.global_constraints.rename(
            index={"co2_limit-EU": ca.EU_CO2_LIMIT}
        )
        attributes = n.buses[n.buses.carrier.isin(ca.ATTRIBUTE_CARRIERS)]
        for bus, carrier in attributes.carrier.items():
            n.buses_t.marginal_price[bus] = EF[carrier.split()[1]] * cls.PRICE
        return n

    @pytest.fixture(scope="class")
    @classmethod
    def carrier(cls):
        return cls.priced_carrier()

    @pytest.fixture(scope="class")
    @classmethod
    def technology(cls):
        return solve(oil_toy(), "technology")

    def test_trade(self, carrier):
        # AT makes 20 MWh of green gas and oil, DE (no budget) claims them all
        trade = ca.attribute_trade(carrier)
        assert trade.at[("AT", "oil"), "produced"] == pytest.approx(20.0)
        price = EF["oil"] * self.PRICE
        assert trade.at[("AT", "oil"), "sales"] == pytest.approx(20.0 * price)
        assert trade.at[("DE", "oil"), "claimed"] == pytest.approx(20.0)
        assert trade.at[("DE", "oil"), "purchases"] == pytest.approx(20.0 * price)
        assert trade.unpriced.sum() == 0.0

    def test_trade_is_zero_sum(self, carrier):
        trade = ca.attribute_trade(carrier).sum()
        assert trade.produced == pytest.approx(trade.claimed)
        assert trade.sales == pytest.approx(trade.purchases)

    def test_no_trade_under_technology(self, technology):
        assert ca.attribute_trade(technology).empty

    def test_transfers(self, carrier, technology):
        table = ca.implicit_transfers(
            {"base": {"2050": technology}, "c": {"2050": carrier}}
        )
        assert list(table.index.unique("scenario")) == ["c"]
        sales = 20.0 * (EF["gas"] + EF["oil"]) * self.PRICE
        assert table.at[("c", "2050", "AT"), "net"] == pytest.approx(sales / ca.BN)
        assert table.net.sum() == pytest.approx(0.0)

    def test_prices(self, carrier):
        prices = ca.attribute_prices(carrier, "AT")
        oil = prices.loc["oil"]
        assert oil["EU value"] == pytest.approx(EF["oil"] * self.EU_PRICE)
        assert oil["national value"] == pytest.approx(
            EF["oil"] * (self.EU_PRICE + self.MU)
        )
        assert oil.price == pytest.approx(EF["oil"] * self.PRICE)
        assert oil["claimant rent"] == pytest.approx(EF["oil"] * (250.0 - 220.0 - 0.01))

    def test_price_not_unique(self):
        # below the EU value no country without a budget would leave it unclaimed
        n = self.priced_carrier()
        n.buses_t.marginal_price["EU renewable oil attribute"] = -1000.0
        n.buses_t.marginal_price["AT renewable oil attribute"] = -1000.0
        buses = ca.attribute_bus_prices(n)
        assert not buses.at["EU renewable oil attribute", "unique"]
        assert buses.at["AT renewable gas attribute", "unique"]
        assert pd.isna(ca.attribute_prices(n, "AT").at["oil", "price"])
        trade = ca.attribute_trade(n)
        assert trade.at[("AT", "oil"), "unpriced"] == pytest.approx(20.0)
        assert trade.at[("AT", "oil"), "sales"] == 0.0

    def test_green_surplus(self):
        # AT makes 20 MWh of synthetic oil and burns 8, the rest goes to DE
        n = oil_toy()
        n.loads.loc["AT1 shipping", "p_set"] = 4.0
        n = solve(n, "technology")
        n.add("GlobalConstraint", ca.EU_CO2_LIMIT, sense="<=")
        n.global_constraints.loc[ca.EU_CO2_LIMIT, "mu"] = -self.EU_PRICE
        oil = ca.green_surplus(n, "AT").loc["oil"]
        assert oil.produced == pytest.approx(20.0)
        assert oil.used == pytest.approx(8.0)
        assert oil.surplus == pytest.approx(12.0)
        assert oil.value == pytest.approx(12.0 * EF["oil"] * self.EU_PRICE)

    def test_no_green_surplus_without_production(self, technology):
        assert ca.green_surplus(technology, "DE").surplus.sum() == 0.0

    def test_prices_without_attributes(self, technology):
        prices = ca.attribute_prices(technology, "AT")
        assert prices.price.isna().all()
        assert prices["claimant rent"].isna().all()


def test_national_rent():
    index = pd.MultiIndex.from_tuples(
        [("base", "2050"), ("c", "2050")], names=["scenario", "horizon"]
    )
    bridge = pd.DataFrame(0.0, index=index, columns=list(ca.ACCOUNTING_CATEGORIES))
    bridge.loc[("base", "2050"), ca.CREATION_CREDIT] = -2.0
    bridge.loc[("c", "2050"), "Claims oil"] = -3.0
    cost = pd.DataFrame({"national CO2 price": [100.0, np.nan]}, index=index)
    rent = ca.national_rent(bridge, cost)
    assert rent.at[("base", "2050"), ca.CREATION_CREDIT] == pytest.approx(0.2)
    assert rent.loc[("c", "2050")].abs().sum() == 0.0


def test_difference_table():
    values = pd.Series(
        [1.0, 3.0, 0.0, 0.0],
        index=pd.MultiIndex.from_tuples(
            [("a", "x"), ("b", "x"), ("a", "y"), ("b", "y")], names=["scenario", "item"]
        ),
    )
    table = ca.difference_table(values, "b", cutoff=1e-9)
    assert list(table.columns) == ["b", "a", "Δ a"]
    assert table.loc["x"].to_list() == [3.0, 1.0, -2.0]
    assert "y" not in table.index


def test_summary():
    index = pd.MultiIndex.from_tuples([("a", "2050")], names=["scenario", "horizon"])
    bridge = pd.DataFrame(0.0, index=index, columns=[*ca.CATEGORIES, "Total", "Budget"])
    bridge.loc[:, ["Fuel use", "Claims gas", "Total", "Budget"]] = [5.0, -2.0, 3.0, 3.0]
    cost = pd.DataFrame(
        {"Δ system cost": [0.0], "Δ cost in country": [0.0]}, index=index
    )
    row = ca.summary(bridge, cost).iloc[0]
    assert row["pre-claim emissions [Mt]"] == 5.0
    assert row["claims [Mt]"] == 2.0


def test_implicit_transfers_without_attributes(oil_technology):
    table = ca.implicit_transfers({"base": {"2050": oil_technology}})
    assert table.empty
    assert table.index.names == ["scenario", "horizon", "country"]
    assert "net" in table.columns
