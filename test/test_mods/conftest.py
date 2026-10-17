# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""Shared fixtures for the ``mods`` tests."""

from types import SimpleNamespace

import pandas as pd
import pypsa
import pytest

# emission factors in t/MWh, as in the cost data
EF = {"gas": 0.198, "oil": 0.2571, "methanol": 0.248}


def carbon_accounting_snakemake(
    method: str = "carrier", enable: bool = True
) -> SimpleNamespace:
    """Return a minimal Snakemake stand-in with the carbon accounting config."""
    return SimpleNamespace(
        config={"mods": {"carbon_accounting": {"enable": enable, "method": method}}}
    )


def carbon_accounting_costs() -> pd.DataFrame:
    """Return the cost entries that define the emission factors."""
    return pd.DataFrame(
        {
            "CO2 intensity": {"gas": EF["gas"], "oil": EF["oil"]},
            "carbondioxide-input": {"methanolisation": EF["methanol"]},
        }
    )


def _add_node(n: pypsa.Network, node: str) -> None:
    for suffix, carrier in [
        ("", "AC"),
        (" gas", "gas"),
        (" biogas", "biogas"),
        (" H2", "H2"),
        (" co2 stored", "co2 stored"),
        (" co2 sequestered", "co2 sequestered"),
    ]:
        unit = "t_co2" if carrier.startswith("co2") else "MWh"
        n.add("Bus", f"{node}{suffix}", carrier=carrier, location=node, unit=unit)


def build_carbon_network(nodes: tuple[str, ...] = ("AT1", "DE1")) -> pypsa.Network:
    """
    Build a small network with one link per carbon accounting class.

    Every node gets AC, gas, biogas, H2 and CO2 buses. Oil, methanol and the
    atmosphere are EU-wide buses. The efficiencies follow the 2050 cost data.
    Without loads or generators, the network is not meant to be solved.

    Parameters
    ----------
    nodes
        Node names; the first two characters are the country code.

    Returns
    -------
    :
        The network.
    """
    n = pypsa.Network()
    n.set_snapshots(range(2))
    n.add("Bus", "co2 atmosphere", carrier="co2", location="EU", unit="t_co2")
    n.add("Bus", "EU oil", carrier="oil", location="EU")
    n.add("Bus", "EU methanol", carrier="methanol", location="EU")
    for node in nodes:
        _add_node(n, node)

    for node in nodes:
        links = {
            "biogas to gas": dict(
                bus0=f"{node} biogas",
                bus1=f"{node} gas",
                bus2="co2 atmosphere",
                efficiency2=-EF["gas"],
            ),
            "biogas to gas CC": dict(
                bus0=f"{node} biogas",
                bus1=f"{node} gas",
                bus2=f"{node} co2 stored",
                bus3="co2 atmosphere",
                efficiency2=0.13023,
                efficiency3=-0.32823,
            ),
            "biomass to liquid": dict(
                bus0=f"{node} biogas",
                bus1="EU oil",
                bus2="co2 atmosphere",
                efficiency=0.45,
                efficiency2=-0.1157,
            ),
            "biomass-to-methanol": dict(
                bus0=f"{node} biogas",
                bus1="EU methanol",
                bus2="co2 atmosphere",
                efficiency=0.65,
                efficiency2=-0.1614,
            ),
            "Sabatier": dict(
                bus0=f"{node} H2",
                bus1=f"{node} gas",
                bus2=f"{node} co2 stored",
                efficiency=0.8,
                efficiency2=-0.8 * EF["gas"],
            ),
            "Fischer-Tropsch": dict(
                bus0=f"{node} H2",
                bus1="EU oil",
                bus2=f"{node} co2 stored",
                efficiency=0.734,
                efficiency2=-0.734 * EF["oil"],
            ),
            "DAC": dict(
                bus0=node,
                bus1=f"{node} co2 stored",
                bus2="co2 atmosphere",
                efficiency=1.0,
                efficiency2=-1.0,
            ),
            "SMR CC": dict(
                bus0=f"{node} gas",
                bus1=f"{node} H2",
                bus2="co2 atmosphere",
                bus3=f"{node} co2 stored",
                efficiency=0.7,
                efficiency2=0.0198,
                efficiency3=0.1782,
            ),
            "co2 sequestered": dict(
                bus0=f"{node} co2 stored",
                bus1=f"{node} co2 sequestered",
            ),
            "co2 vent": dict(
                bus0=f"{node} co2 stored",
                bus1="co2 atmosphere",
            ),
            "CCGT": dict(
                bus0=f"{node} gas",
                bus1=node,
                bus2="co2 atmosphere",
                efficiency=0.6,
                efficiency2=EF["gas"],
            ),
            "kerosene for aviation": dict(
                bus0="EU oil",
                bus1=node,
                bus2="co2 atmosphere",
                efficiency2=EF["oil"],
            ),
            "shipping oil": dict(
                bus0="EU oil",
                bus1=node,
                bus2="co2 atmosphere",
                efficiency2=EF["oil"],
            ),
            "industry methanol": dict(
                bus0="EU methanol",
                bus1=node,
                bus2="co2 atmosphere",
                efficiency2=EF["methanol"],
            ),
        }
        for carrier, attrs in links.items():
            n.add("Link", f"{node} {carrier}", carrier=carrier, **attrs)

    n.add(
        "Link",
        f"{nodes[0]} gas import",
        carrier="import gas",
        bus0="co2 atmosphere",
        bus1=f"{nodes[0]} gas",
        efficiency=1 / EF["gas"],
    )
    for fuel in ("oil", "methanol"):
        n.add(
            "Link",
            f"EU {fuel} import",
            carrier=f"import {fuel}",
            bus0="co2 atmosphere",
            bus1=f"EU {fuel}",
            efficiency=1 / EF[fuel],
        )
    for bus0, bus1 in zip(nodes[:-1], nodes[1:]):
        n.add(
            "Link",
            f"gas pipeline {bus0} -> {bus1}",
            carrier="gas pipeline",
            bus0=f"{bus0} gas",
            bus1=f"{bus1} gas",
        )
    for c in ("bus2", "bus3"):
        n.links[c] = n.links[c].fillna("")
    for c in ("efficiency2", "efficiency3"):
        n.links[c] = n.links[c].fillna(1.0)
    return n


@pytest.fixture
def carbon_network() -> pypsa.Network:
    """Return a fresh synthetic network for the carbon accounting tests."""
    return build_carbon_network()


def build_gas_toy(
    consumer_in_origin: bool = False, pipeline_capacity: float = 100.0
) -> pypsa.Network:
    """
    Build a solvable two-country gas network: biogas in AT, consumers in DE.

    AT1 upgrades 10 MW of biogas into its gas bus. DE1 burns gas in a boiler
    for a 10 MW heat load and can buy fossil gas. A pipeline connects AT1 to
    DE1. Emissions go to an unbounded ``co2 atmosphere`` store.

    Parameters
    ----------
    consumer_in_origin
        Add the same boiler, heat load and fossil gas supply to AT1.
    pipeline_capacity
        Capacity of the pipeline from AT1 to DE1.

    Returns
    -------
    :
        The network with two snapshots of weight 1.
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
    n.add("Bus", "EU oil", carrier="oil", location="EU")
    n.add("Bus", "EU methanol", carrier="methanol", location="EU")
    for node in ("AT1", "DE1"):
        for suffix, carrier in [("", "AC"), (" gas", "gas"), (" heat", "heat")]:
            n.add("Bus", f"{node}{suffix}", carrier=carrier, location=node)

    n.add("Bus", "AT1 biogas", carrier="biogas", location="AT1")
    n.add("Generator", "AT1 biogas", bus="AT1 biogas", carrier="biogas", p_nom=10)
    n.add(
        "Link",
        "AT1 biogas to gas",
        carrier="biogas to gas",
        bus0="AT1 biogas",
        bus1="AT1 gas",
        bus2="co2 atmosphere",
        efficiency2=-EF["gas"],
        p_nom=10,
        p_min_pu=1,
    )
    n.add(
        "Link",
        "gas pipeline AT1 -> DE1",
        carrier="gas pipeline",
        bus0="AT1 gas",
        bus1="DE1 gas",
        p_nom=pipeline_capacity,
    )
    for node in ("AT1", "DE1") if consumer_in_origin else ("DE1",):
        n.add(
            "Generator",
            f"{node} gas",
            bus=f"{node} gas",
            carrier="gas",
            p_nom=100,
            marginal_cost=50,
        )
        n.add(
            "Link",
            f"{node} rural gas boiler",
            carrier="rural gas boiler",
            bus0=f"{node} gas",
            bus1=f"{node} heat",
            bus2="co2 atmosphere",
            efficiency2=EF["gas"],
            p_nom=100,
        )
        n.add("Load", f"{node} heat", bus=f"{node} heat", p_set=10)
    return n
