# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""Tests for mods/network/carbon_accounting.py (spec section 6, U1-U6, U12, U14)."""

from itertools import combinations

import pandas as pd
import pypsa
import pytest
from add_brownfield import add_brownfield

from evals.utils import get_location
from mods.constants import (
    CARBON_ACCOUNTING_MARKER,
    FAIR_SHARE_KEY,
    GREEN_BIO_PRODUCERS,
    GREEN_IMPORTS,
    GREEN_SYNTHETIC_PRODUCERS,
)
from mods.network.carbon_accounting import (
    TRANSFORMATION_CLASSES,
    apply_carbon_accounting,
    carbon_accounting_method,
    emission_factors,
    link_ports,
    port_bus_carrier,
    port_efficiency,
    unclassified_carriers,
)
from test.test_mods.conftest import (
    EF,
    build_carbon_network,
    build_oil_toy,
    carbon_accounting_costs,
    carbon_accounting_snakemake,
)


def _apply(n, method="carrier", enable=True) -> None:
    snakemake = carbon_accounting_snakemake(method, enable)
    apply_carbon_accounting(n, snakemake, carbon_accounting_costs())


class TestGuard:
    """U1 and U6: the guard on ``enable`` and ``method``."""

    @pytest.mark.parametrize(
        ("method", "enable"), [("technology", True), ("carrier", False)]
    )
    def test_network_unchanged(self, carbon_network, method, enable):
        before = carbon_network.copy()
        _apply(carbon_network, method, enable)
        for c in ("links", "buses", "stores", "generators"):
            assert getattr(carbon_network, c).equals(getattr(before, c))

    def test_unknown_method_raises(self, carbon_network):
        with pytest.raises(ValueError, match="Unknown carbon accounting method"):
            _apply(carbon_network, method="mass")

    def test_unknown_method_raises_when_disabled(self):
        with pytest.raises(ValueError, match="Unknown carbon accounting method"):
            carbon_accounting_method({"enable": False, "method": "mass"})

    def test_disabled_returns_none(self):
        assert carbon_accounting_method({"enable": False, "method": "carrier"}) is None


class TestClassification:
    """U5: every link at a CO2 or fuel bus is in exactly one class."""

    @pytest.mark.parametrize(("a", "b"), list(combinations(TRANSFORMATION_CLASSES, 2)))
    def test_classes_are_disjoint(self, a, b):
        overlap = set(TRANSFORMATION_CLASSES[a]) & set(TRANSFORMATION_CLASSES[b])
        assert not overlap

    def test_toy_network_is_classified(self, carbon_network):
        assert unclassified_carriers(carbon_network) == []

    def test_unclassified_carrier_raises(self, carbon_network):
        carbon_network.add(
            "Link", "AT1 new gas user", carrier="new gas user", bus0="AT1 gas"
        )
        assert unclassified_carriers(carbon_network) == ["new gas user"]
        with pytest.raises(ValueError, match="new gas user"):
            _apply(carbon_network)

    def test_link_ports(self, carbon_network):
        assert link_ports(carbon_network) == ["0", "1", "2", "3"]


def test_solved_networks_are_classified(nc):
    """U5 on the solved networks: fail early on upstream changes."""
    for n in nc:
        assert unclassified_carriers(n) == []


def _atmosphere(n) -> pd.Series:
    """Return the summed efficiency at the ``co2 atmosphere`` bus per link."""
    atm = pd.Series(0.0, index=n.links.index)
    for port in link_ports(n):
        at_atm = n.links[f"bus{port}"] == "co2 atmosphere"
        atm += port_efficiency(n, port).where(at_atm, 0.0)
    return atm


def test_emission_factors():
    assert emission_factors(carbon_accounting_costs()) == EF


class TestEmissionPorts:
    """U2: the emission rule of section 3.2 per class."""

    @pytest.mark.parametrize(
        ("carrier", "expected"),
        [
            ("biogas to gas", 0.0),
            # the stored share keeps its credit
            ("biogas to gas CC", -0.13023),
            ("biomass to liquid", -0.1157 + 0.45 * EF["oil"]),
            ("biomass-to-methanol", -0.1614 + 0.65 * EF["methanol"]),
            # CO2 used for synthetic fuels is charged at the CCU plant
            ("Sabatier", 0.8 * EF["gas"]),
            ("Fischer-Tropsch", 0.734 * EF["oil"]),
        ],
    )
    def test_emissions(self, carbon_network, carrier, expected):
        _apply(carbon_network)
        atm = _atmosphere(carbon_network)
        assert atm[f"AT1 {carrier}"] == pytest.approx(expected, abs=1e-12)

    @pytest.mark.parametrize(
        "carrier",
        ["DAC", "SMR CC", "co2 vent", "CCGT", "kerosene for aviation"],
    )
    def test_other_links_keep_their_emissions(self, carbon_network, carrier):
        before = _atmosphere(carbon_network)
        _apply(carbon_network)
        name = f"AT1 {carrier}"
        assert _atmosphere(carbon_network)[name] == before[name]

    def test_ccu_charge_gets_its_own_port(self, carbon_network):
        """Synthetic fuel producers have no atmosphere port in the technology method."""
        assert (carbon_network.links.loc["AT1 Sabatier"] != "co2 atmosphere").all()
        _apply(carbon_network)
        assert _port_to(carbon_network, "AT1 Sabatier", "co2 atmosphere") == (
            pytest.approx(0.8 * EF["gas"])
        )

    def test_transformed_links_are_marked(self, carbon_network):
        _apply(carbon_network)
        marker = carbon_network.links[CARBON_ACCOUNTING_MARKER]
        assert marker["AT1 Sabatier"] == "carrier"
        assert pd.isna(marker["AT1 DAC"])
        assert pd.isna(marker["AT1 CCGT"])


def _normalised(links: pd.DataFrame) -> pd.DataFrame:
    """Fill unconnected extra ports, which re-added links leave as NaN."""
    out = links.astype(object)
    for col in out.columns[out.columns.str.fullmatch(r"bus\d+")]:
        out[col] = out[col].fillna("")
    for col in out.columns[out.columns.str.fullmatch(r"efficiency\d+")]:
        out[col] = out[col].fillna(1.0)
    return out


def test_idempotent(carbon_network):
    """U3: a second run changes nothing, including the CCU charge."""
    _apply(carbon_network)
    once = _normalised(carbon_network.links)
    _apply(carbon_network)
    assert _normalised(carbon_network.links).equals(once)
    assert _atmosphere(carbon_network)[["AT1 DAC", "AT1 Sabatier"]].tolist() == [
        -1.0,
        pytest.approx(0.8 * EF["gas"]),
    ]


def test_port_bus_carrier(carbon_network):
    assert port_bus_carrier(carbon_network, "2")["AT1 DAC"] == "co2"


def _co2_stored(n) -> pd.Series:
    """Return the summed efficiency at ``co2 stored`` buses per link."""
    stored = pd.Series(0.0, index=n.links.index)
    for port in link_ports(n):
        at_stored = port_bus_carrier(n, port) == "co2 stored"
        stored += port_efficiency(n, port).where(at_stored, 0.0)
    return stored


def test_solved_networks_emissions(nc):
    """U2 on the solved networks: bio-fuels book their stored CO2, CCU its input."""
    for n in nc:
        m = n.copy()
        _apply(m)
        atm, stored = _atmosphere(m), _co2_stored(m)
        bio = m.links.carrier.isin(GREEN_BIO_PRODUCERS)
        assert (atm[bio] + stored[bio]).abs().max() <= 2e-4 + 1e-12
        ccu = m.links.carrier.isin(GREEN_SYNTHETIC_PRODUCERS)
        assert (atm[ccu] + stored[ccu]).abs().max() <= 1e-12
        dac = m.links.carrier == "DAC"
        assert atm[dac].eq(_atmosphere(n)[dac]).all()


def _port_to(n, link: str, bus: str) -> float:
    """Return the efficiency of the port of a link at a bus."""
    for port in link_ports(n):
        if n.links.at[link, f"bus{port}"] == bus:
            return port_efficiency(n, port)[link]
    raise KeyError(f"{link} has no port at {bus}")


class TestGasLayer:
    """Section 3.3: the gas accounting layer."""

    @pytest.fixture
    def n(self, carbon_network):
        _apply(carbon_network)
        return carbon_network

    def test_attribute_buses(self, n):
        buses = n.buses.loc[
            ["AT renewable gas attribute", "DE renewable gas attribute"]
        ]
        assert buses.carrier.eq("renewable gas attribute").all()
        assert buses.location.tolist() == ["AT", "DE"]

    def test_attribute_stores_are_cyclic(self, n):
        store = n.stores.loc["AT renewable gas attribute"]
        assert store.e_cyclic
        assert store.e_nom_extendable

    def test_claim_links(self, n):
        claim = n.links.loc["DE renewable gas claim"]
        assert claim.bus0 == "DE renewable gas attribute"
        assert claim.bus1 == "co2 atmosphere"
        assert claim.efficiency == -EF["gas"]

    def test_transfer_follows_pipelines(self, n):
        transfers = n.links.index[n.links.carrier == "renewable gas attribute transfer"]
        assert transfers.tolist() == ["AT renewable gas attribute -> DE"]

    @pytest.mark.parametrize(
        ("link", "efficiency"),
        [
            ("DE1 biogas to gas", 1.0),
            ("DE1 Sabatier", 0.8),
            ("AT1 gas import", 1 / EF["gas"]),
        ],
    )
    def test_attribute_ports(self, n, link, efficiency):
        bus = f"{link[:2]} renewable gas attribute"
        assert _port_to(n, link, bus) == pytest.approx(efficiency)

    def test_import_draws_from_virtual_source(self, n):
        assert n.links.at["AT1 gas import", "bus0"] == "co2 import"
        assert n.generators.at["co2 import", "bus"] == "co2 import"


class TestPooledLayer:
    """Section 3.3: the EU pools of oil and methanol attributes."""

    @pytest.fixture
    def n(self, carbon_network):
        _apply(carbon_network)
        return carbon_network

    @pytest.mark.parametrize("fuel", ["oil", "methanol"])
    def test_pool_feeds_every_country(self, n, fuel):
        pool = f"EU renewable {fuel} attribute"
        assert n.buses.at[pool, "location"] == "EU"
        assert n.stores.at[pool, "e_cyclic"]
        for ct in ("AT", "DE"):
            link = n.links.loc[f"{pool} -> {ct}"]
            assert (link.bus0, link.bus1) == (pool, f"{ct} renewable {fuel} attribute")
            assert n.buses.at[link.bus1, "location"] == ct

    @pytest.mark.parametrize(
        ("claim", "bus", "factor"),
        [
            ("renewable oil claim", "renewable oil attribute", EF["oil"]),
            ("renewable oil claim aviation", "renewable oil attribute", EF["oil"]),
            (
                "renewable methanol claim",
                "renewable methanol attribute",
                EF["methanol"],
            ),
        ],
    )
    def test_claim_links(self, n, claim, bus, factor):
        link = n.links.loc[f"AT {claim}"]
        assert (link.bus0, link.bus1) == (f"AT {bus}", "co2 atmosphere")
        assert link.efficiency == -factor

    @pytest.mark.parametrize(
        ("link", "bus", "efficiency"),
        [
            ("AT1 biomass to liquid", "EU renewable oil attribute", 0.45),
            ("AT1 Fischer-Tropsch", "EU renewable oil attribute", 0.734),
            ("AT1 biomass-to-methanol", "EU renewable methanol attribute", 0.65),
            ("EU oil import", "EU renewable oil attribute", 1 / EF["oil"]),
            (
                "EU methanol import",
                "EU renewable methanol attribute",
                1 / EF["methanol"],
            ),
        ],
    )
    def test_attribute_ports(self, n, link, bus, efficiency):
        assert _port_to(n, link, bus) == pytest.approx(efficiency)

    @pytest.mark.parametrize("link", ["EU oil import", "EU methanol import"])
    def test_imports_draw_from_virtual_source(self, n, link):
        assert n.links.at[link, "bus0"] == "co2 import"


def test_claims_are_located_in_their_country(carbon_network):
    """U11: the evals attribute every claim link to its country, not to EU."""
    _apply(carbon_network)
    location = get_location(carbon_network, "Link")
    claims = carbon_network.links.index[
        carbon_network.links.carrier.str.contains(" claim")
    ]
    assert not claims.empty
    assert (location[claims] == claims.str[:2]).all()


def test_sequestration_is_unchanged(carbon_network):
    """Section 3.2: stored CO2 is credited at capture, so there are no removal claims."""
    before = carbon_network.links.loc["AT1 co2 sequestered"].copy()
    _apply(carbon_network)
    after = carbon_network.links.loc["AT1 co2 sequestered", before.index]
    assert after.equals(before)
    assert not carbon_network.buses.carrier.str.contains("removal").any()


def _vintage(year: int) -> pypsa.Network:
    """Build the network with links of one build year, as the workflow does."""
    n = build_carbon_network(suffix=f"-{year}")
    # sequestration, imports and pipelines are rebuilt in every horizon
    infinite = n.links.carrier.isin(["co2 sequestered", "gas pipeline", *GREEN_IMPORTS])
    n.links["p_nom_extendable"] = True
    n.links["lifetime"] = 20.0
    n.links.loc[infinite, "lifetime"] = float("inf")
    n.links["build_year"] = year
    n.add(
        "Generator",
        f"AT1 biogas-{year}",
        bus="AT1 biogas",
        p_nom_extendable=True,
        lifetime=20.0,
        build_year=year,
    )
    n.add(
        "Store",
        f"AT1 gas-{year}",
        bus="AT1 gas",
        e_nom_extendable=True,
        lifetime=20.0,
        build_year=year,
    )
    return n


class TestBrownfield:
    """U4: links copied from the previous horizon are not transformed twice."""

    @pytest.fixture
    def n(self, tmp_path):
        previous = _vintage(2030)
        _apply(previous)
        previous.links["p_nom_opt"] = 1.0
        previous.generators["p_nom_opt"] = 1.0
        previous.stores["e_nom_opt"] = 1.0
        # the workflow reads the previous horizon from its netCDF file
        previous.export_to_netcdf(tmp_path / "solved_2030.nc")
        previous = pypsa.Network(tmp_path / "solved_2030.nc")

        n = _vintage(2040)
        add_brownfield(n, previous, 2040, capacity_threshold=0.1)
        _apply(n)
        return n

    @pytest.mark.parametrize("year", [2030, 2040])
    def test_coefficients(self, n, year):
        atm = _atmosphere(n)
        assert atm[f"AT1 DAC-{year}"] == -1.0
        assert atm[f"AT1 SMR CC-{year}"] == pytest.approx(0.0198)
        assert atm[f"AT1 biogas to gas-{year}"] == pytest.approx(0.0)
        assert atm[f"AT1 Sabatier-{year}"] == pytest.approx(0.8 * EF["gas"])
        marker = n.links.at[f"AT1 Sabatier-{year}", CARBON_ACCOUNTING_MARKER]
        assert marker == "carrier"

    @pytest.mark.parametrize("year", [2030, 2040])
    def test_one_attribute_port(self, n, year):
        link = n.links.loc[f"AT1 biogas to gas-{year}"]
        buses = [link[f"bus{p}"] for p in link_ports(n)]
        assert buses.count("AT renewable gas attribute") == 1

    def test_layer_exists_once(self, n):
        claims = n.links.index[n.links.carrier == "renewable gas claim"]
        assert sorted(claims) == ["AT renewable gas claim", "DE renewable gas claim"]
        assert n.generators.index.tolist().count("co2 import") == 1
        assert n.links.at["AT1 gas import", "bus0"] == "co2 import"
        assert n.links.at["AT1 co2 sequestered-2040", "bus0"] == "AT1 co2 stored"


def test_empty_marker_counts_as_unmarked(carbon_network):
    """A netCDF round trip turns the NaN marker of untransformed links into ""."""
    carbon_network.links[CARBON_ACCOUNTING_MARKER] = ""
    _apply(carbon_network)
    assert _atmosphere(carbon_network)["AT1 Sabatier"] == pytest.approx(0.8 * EF["gas"])


class TestFairShareKeys:
    """U12 and U14: keys from the fixed demand, guard on ``fair_share``."""

    @pytest.fixture
    def n(self):
        n = build_oil_toy()
        n.loads.loc["AT1 shipping", "p_set"] = 5.0
        n.loads.loc["DE1 shipping", "p_set"] = 15.0
        snakemake = carbon_accounting_snakemake(fair_share=1.0)
        apply_carbon_accounting(n, snakemake, carbon_accounting_costs())
        return n

    def test_keys_are_demand_shares(self, n):
        keys = n.links[FAIR_SHARE_KEY]
        assert keys["AT renewable oil claim"] == pytest.approx(0.25)
        assert keys["DE renewable oil claim"] == pytest.approx(0.75)

    def test_keys_of_a_fuel_sum_to_one(self, n):
        keys = n.links[FAIR_SHARE_KEY]
        assert keys[keys.index.str.contains(" oil claim")].sum() == pytest.approx(1.0)

    def test_without_demand_the_key_is_zero(self, n):
        keys = n.links[FAIR_SHARE_KEY]
        assert keys["AT renewable oil claim aviation"] == 0.0
        assert keys["AT renewable methanol claim"] == 0.0

    def test_gas_claims_have_no_key(self, n):
        assert pd.isna(n.links.at["AT renewable gas claim", FAIR_SHARE_KEY])

    def test_disabled_adds_no_keys(self):
        n = build_oil_toy()
        apply_carbon_accounting(
            n, carbon_accounting_snakemake(), carbon_accounting_costs()
        )
        assert FAIR_SHARE_KEY not in n.links

    @pytest.mark.parametrize("tolerance", [0.0, -1.0])
    def test_non_positive_tolerance_raises(self, carbon_network, tolerance):
        snakemake = carbon_accounting_snakemake(fair_share=tolerance)
        with pytest.raises(ValueError, match="tolerance must be positive"):
            apply_carbon_accounting(
                carbon_network, snakemake, carbon_accounting_costs()
            )
