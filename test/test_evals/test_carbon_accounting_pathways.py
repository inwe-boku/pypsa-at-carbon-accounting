# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""Unit tests for evals/carbon_accounting_pathways.py on toy networks."""

import pandas as pd
import pypsa
import pytest

from evals import carbon_accounting_pathways as cp
from test.test_evals.test_carbon_accounting import ALPHA, add_budget, solve
from test.test_mods.conftest import EF, build_oil_toy

COUNTRIES = ("AT", "DE")


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
def technology_run() -> pypsa.Network:
    return solve(oil_toy(), "technology")


@pytest.fixture(scope="module")
def carrier_run() -> pypsa.Network:
    # DE claims all green oil, so the optimised claims differ from the EU average
    return solve(oil_toy(), "carrier", rewards={"DE renewable oil claim": 1.0})


@pytest.fixture(scope="module")
def networks(technology_run, carrier_run) -> dict:
    return {"base": {"2050": technology_run}, "carrier": {"2050": carrier_run}}


@pytest.fixture(scope="module")
def frame(networks) -> pd.DataFrame:
    return cp.emissions(networks)


def national(n: pypsa.Network, evaluator: str, country: str) -> float:
    values = cp.evaluate(n, evaluator)
    return values[values.index.str.startswith(country)].sum()


def test_emission_factors(technology_run):
    assert cp.emission_factors(technology_run) == pytest.approx(EF)


def test_emission_factors_need_imports():
    with pytest.raises(ValueError, match="emission factor"):
        cp.emission_factors(build_oil_toy())


@pytest.mark.parametrize(
    ("carrier", "expected"),
    [
        ("biogas to gas", "Fuel supply"),
        ("Fischer-Tropsch", "Fuel supply"),
        ("DAC", "DAC & vent"),
        ("kerosene for aviation", "Transport"),
        ("urban central gas CHP", "Heat"),
        ("gas for industry CC", "Industry"),
        ("CCGT", "Power"),
        ("H2 Electrolysis", "Other"),
    ],
)
def test_sector(carrier, expected):
    assert cp.sector(carrier) == expected


class TestTechnologyEvaluator:
    def test_reproduces_technology_run(self, technology_run):
        table = cp.link_table(technology_run)
        values = cp.technology_emissions(table, EF)
        assert values.to_numpy() == pytest.approx(table.native.to_numpy())

    def test_inverts_carrier_run(self, technology_run, carrier_run):
        # the dispatch of the toy is fixed, so both runs have the same physical flows
        for ct in COUNTRIES:
            assert national(carrier_run, cp.TECHNOLOGY, ct) == pytest.approx(
                national(technology_run, cp.TECHNOLOGY, ct)
            )

    def test_values(self, technology_run):
        # AT: biogas creation credit, DAC capture, shipping oil
        at = national(technology_run, cp.TECHNOLOGY, "AT")
        assert at == pytest.approx(-20 * EF["gas"] - 20 * EF["oil"] + 20 * EF["oil"])


class TestCarrierEvaluators:
    def test_optimised_matches_booked_emissions(self, carrier_run):
        table = cp.link_table(carrier_run)
        for ct in COUNTRIES:
            booked = table.native[table.country == ct].sum()
            assert national(carrier_run, cp.CARRIER_OPTIMISED, ct) == pytest.approx(
                booked
            )

    def test_optimised_needs_claims(self, technology_run):
        assert cp.evaluate(technology_run, cp.CARRIER_OPTIMISED).isna().all()

    def test_average_on_technology_run(self, technology_run):
        # 20 MWh green oil on 40 MWh consumed: each country is half green;
        # all 20 MWh of gas are green and burned in DE
        at = national(technology_run, cp.CARRIER_AVERAGE, "AT")
        de = national(technology_run, cp.CARRIER_AVERAGE, "DE")
        # AT: CCU charge + capture credit + shipping at half green
        assert at == pytest.approx(10 * EF["oil"])
        assert de == pytest.approx(10 * EF["oil"])

    def test_average_is_independent_of_the_claims(self, technology_run, carrier_run):
        for ct in COUNTRIES:
            assert national(carrier_run, cp.CARRIER_AVERAGE, ct) == pytest.approx(
                national(technology_run, cp.CARRIER_AVERAGE, ct)
            )

    def test_unknown_rule(self, carrier_run):
        with pytest.raises(ValueError, match="allocation rule"):
            cp.green_shares(cp.link_table(carrier_run), "nearest")

    def test_unknown_evaluator(self, carrier_run):
        with pytest.raises(ValueError, match="evaluator"):
            cp.evaluate(carrier_run, "mass balance")


def test_aviation_scaled():
    n = oil_toy()
    n.links.loc["AT1 shipping oil", "carrier"] = "kerosene for aviation"
    n = solve(n, "technology")
    values = cp.evaluate(n, cp.TECHNOLOGY)
    unscaled = cp.evaluate(n, cp.TECHNOLOGY, scaled=False)
    link = "AT1 shipping oil"
    assert values[link] == pytest.approx(ALPHA * unscaled[link])


class TestFrames:
    def test_emissions_index(self, frame):
        assert frame.index.names == [
            "scenario",
            "horizon",
            "evaluator",
            "country",
            "sector",
        ]
        evaluators = frame.xs("base", level="scenario").index.unique("evaluator")
        assert cp.CARRIER_OPTIMISED not in evaluators

    def test_national_totals(self, frame):
        totals = cp.national_totals(frame, "DE")
        assert list(totals.columns) == list(cp.EVALUATORS)
        assert (
            totals.loc[("base", "technology", "2050"), cp.CARRIER_OPTIMISED]
            != (totals.loc[("base", "technology", "2050"), cp.CARRIER_OPTIMISED])
        )  # NaN

    def test_own_method(self, frame):
        totals = cp.national_totals(frame, "DE")
        own = cp.own_method(totals)
        assert (
            own[("base", "technology", "2050")]
            == totals.at[("base", "technology", "2050"), cp.TECHNOLOGY]
        )
        assert (
            own[("carrier", "carrier", "2050")]
            == totals.at[("carrier", "carrier", "2050"), cp.CARRIER_OPTIMISED]
        )

    def test_against_budget(self, frame):
        totals = cp.national_totals(frame, "DE")
        gaps = cp.against_budget(totals, pd.Series({"2050": 1.0}))
        assert (gaps + 1.0).to_numpy() == pytest.approx(totals.to_numpy(), nan_ok=True)

    def test_budgets(self):
        n = solve(oil_toy(), "technology")
        add_budget(n, "AT", mu=0.0, constant=2e6)
        budgets = cp.budgets({"a": {"2050": n}}, ["AT"])
        assert budgets.at["2050", "AT"] == pytest.approx(2.0)

    def test_sector_pathway(self, frame):
        pathway = cp.sector_pathway(frame, "AT", "base", cp.TECHNOLOGY)
        assert list(pathway.index) == ["2050"]
        assert "Fuel supply" in pathway.columns
        assert cp.sector_pathway(frame, "AT", "base", cp.CARRIER_OPTIMISED).empty

    def test_reference_run(self, frame):
        assert cp.reference_run(cp.national_totals(frame, "AT")) == "base"


def test_checks_pass(networks, frame):
    checks = cp.checks(networks, frame, list(COUNTRIES))
    assert [c.status for c in checks] == ["ok"] * 3


def test_merge_checks_keeps_checked_scenarios():
    details = pd.DataFrame({"deviation": [0.5]}, index=[("carrier", "2050")])
    skipped = cp.Check("c", "ok", "Nothing to check: no carrier runs.")
    checked = cp.Check("c", "fail", "Largest deviation: 5.0e-01.", details)
    merged = cp.merge_checks([skipped, checked])
    assert merged.failed
    assert merged.details.compare(details).empty


def test_merge_checks_nothing_checked():
    skipped = cp.Check("c", "ok", "Nothing to check: no carrier runs.")
    assert cp.merge_checks([skipped, skipped]) is skipped
