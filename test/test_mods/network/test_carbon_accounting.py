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
    GREEN_BIO_PRODUCERS,
)
from mods.network.carbon_accounting import (
    TRANSFORMATION_CLASSES,
    apply_carbon_accounting,
    carbon_accounting_method,
    link_ports,
    unclassified_carriers,
)
from test.test_mods.conftest import (
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
