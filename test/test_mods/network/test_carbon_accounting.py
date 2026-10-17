# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""Tests for mods/network/carbon_accounting.py (spec section 6, U1-U6)."""

from itertools import combinations

import pandas as pd
import pytest

from mods.constants import (
    CAPTURE,
    CAPTURE_ELIGIBLE,
    CARBON_ACCOUNTING_MARKER,
    GREEN_BIO_PRODUCERS,
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

    def test_capture_eligible(self):
        bio_cc = {c for c in GREEN_BIO_PRODUCERS if c.endswith(" CC")}
        assert set(CAPTURE_ELIGIBLE) == set(CAPTURE) | bio_cc

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


def _co2_stored_carriers(n) -> set[str]:
    """Return carriers of links with a port at a ``co2 stored`` bus."""
    at_stored = pd.Series(False, index=n.links.index)
    for port in link_ports(n):
        at_stored |= n.links[f"bus{port}"].map(n.buses.carrier) == "co2 stored"
    return set(n.links.carrier[at_stored])


def test_solved_networks_are_classified(nc):
    """U5 on the solved networks: fail early on upstream changes."""
    for n in nc:
        assert unclassified_carriers(n) == []
        bio_cc = _co2_stored_carriers(n) & set(GREEN_BIO_PRODUCERS)
        assert bio_cc <= set(CAPTURE_ELIGIBLE)


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
    """U2: the gross emission rule of section 3.2 per class."""

    @pytest.mark.parametrize(
        ("carrier", "expected"),
        [
            ("biogas to gas", 0.0),
            ("biogas to gas CC", 0.0),
            ("biomass to liquid", -0.1157 + 0.45 * EF["oil"]),
            ("biomass-to-methanol", -0.1614 + 0.65 * EF["methanol"]),
            ("DAC", 0.0),
            ("SMR CC", 0.198),
            ("co2 vent", 0.0),
        ],
    )
    def test_gross_emissions(self, carbon_network, carrier, expected):
        _apply(carbon_network)
        atm = _atmosphere(carbon_network)
        assert atm[f"AT1 {carrier}"] == pytest.approx(expected, abs=1e-12)

    @pytest.mark.parametrize(
        "carrier",
        ["Sabatier", "Fischer-Tropsch", "CCGT", "kerosene for aviation"],
    )
    def test_other_links_keep_their_emissions(self, carbon_network, carrier):
        before = _atmosphere(carbon_network)
        _apply(carbon_network)
        name = f"AT1 {carrier}"
        assert _atmosphere(carbon_network)[name] == before[name]

    def test_capture_without_atmosphere_port_gets_one(self, carbon_network):
        carbon_network.add(
            "Link",
            "AT1 waste CHP CC",
            carrier="waste CHP CC",
            bus0="AT1",
            bus1="AT1 co2 stored",
            efficiency=0.23,
        )
        _apply(carbon_network)
        assert _atmosphere(carbon_network)["AT1 waste CHP CC"] == pytest.approx(0.23)

    def test_transformed_links_are_marked(self, carbon_network):
        _apply(carbon_network)
        marker = carbon_network.links[CARBON_ACCOUNTING_MARKER]
        assert marker["AT1 DAC"] == "carrier"
        assert marker["AT1 Sabatier"] == "carrier"
        assert pd.isna(marker["AT1 CCGT"])


def test_idempotent(carbon_network):
    """U3: a second run changes nothing, including capture-only links."""
    _apply(carbon_network)
    once = carbon_network.links.copy()
    _apply(carbon_network)
    assert carbon_network.links.equals(once)
    assert _atmosphere(carbon_network)[["AT1 DAC", "AT1 SMR CC"]].tolist() == [
        0.0,
        pytest.approx(0.198),
    ]


def test_port_bus_carrier(carbon_network):
    assert port_bus_carrier(carbon_network, "2")["AT1 DAC"] == "co2"


def test_solved_networks_gross_emissions(nc):
    """U2 on the solved networks: bio-fuel residuals are tiny, capture is gross."""
    for n in nc:
        m = n.copy()
        _apply(m)
        atm = _atmosphere(m)
        bio = m.links.carrier.isin(GREEN_BIO_PRODUCERS)
        assert atm[bio].abs().max() <= 2e-4 + 1e-12
        dac = m.links.carrier == "DAC"
        assert (atm[dac] == 0).all()
        capture = m.links.carrier.isin(CAPTURE)
        assert (atm[capture] >= 0).all()
