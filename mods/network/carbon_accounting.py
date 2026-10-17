# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""
Switchable carbon accounting method for the composed network.

The ``technology`` method is the status quo: the technology that releases CO2
books it, and green fuel producers book a negative credit at creation. The
``carrier`` method books green fuels at zero and fossil fuels at their emission
factor in the consuming country. It adds an accounting layer of attribute
buses, stores and claim links. See
``docs-at/explanations/carbon-accounting-spec.md``.
"""

from logging import getLogger

import pandas as pd
import pypsa
from snakemake.script import Snakemake

from mods.constants import (
    ACCOUNTING_FUELS,
    CAPTURE,
    CARBON_ACCOUNTING_MARKER,
    CARBON_ACCOUNTING_METHODS,
    CO2_BUS_CARRIERS,
    GREEN_BIO_PRODUCERS,
    GREEN_IMPORTS,
    GREEN_SYNTHETIC_PRODUCERS,
    SEQUESTRATION,
    UNCHANGED,
    VENT,
)

logger = getLogger(__name__)

TRANSFORMATION_CLASSES: dict[str, tuple[str, ...]] = {
    "G1": GREEN_BIO_PRODUCERS,
    "G2": GREEN_SYNTHETIC_PRODUCERS,
    "G3": GREEN_IMPORTS,
    "C1": CAPTURE,
    "S1": SEQUESTRATION,
    "V1": VENT,
    "U": UNCHANGED,
}


def carbon_accounting_method(config: dict) -> str | None:
    """
    Return the active carbon accounting method.

    Parameters
    ----------
    config
        The ``mods.carbon_accounting`` configuration section.

    Returns
    -------
    :
        The configured method, or ``None`` if the feature is disabled.

    Raises
    ------
    ValueError
        If the configured method is unknown.
    """
    method = config["method"]
    if method not in CARBON_ACCOUNTING_METHODS:
        raise ValueError(
            f"Unknown carbon accounting method '{method}'. "
            f"Choose one of {CARBON_ACCOUNTING_METHODS}."
        )
    return method if config["enable"] else None


# classes whose links get the carbon accounting marker
TRANSFORMED_CLASSES: tuple[str, ...] = ("G1", "G2", "G3", "C1", "S1", "V1")
ATMOSPHERE = "co2 atmosphere"


def emission_factors(costs: pd.DataFrame) -> dict[str, float]:
    """
    Return the emission factors of gas, oil and methanol in t/MWh.

    Parameters
    ----------
    costs
        Processed cost DataFrame for the current planning horizon.

    Returns
    -------
    :
        Emission factor per fuel.
    """
    return {
        "gas": costs.at["gas", "CO2 intensity"],
        "oil": costs.at["oil", "CO2 intensity"],
        "methanol": costs.at["methanolisation", "carbondioxide-input"],
    }


def link_ports(n: pypsa.Network) -> list[str]:
    """Return the port numbers of all link buses, e.g. ``["0", "1", "2"]``."""
    ports = n.links.columns[n.links.columns.str.fullmatch(r"bus\d+")].str[3:]
    return sorted(ports, key=int)


def _efficiency_column(port: str) -> str:
    return "efficiency" if port == "1" else f"efficiency{port}"


def port_efficiency(n: pypsa.Network, port: str) -> pd.Series:
    """Return the static efficiency of a link port, ``-1`` for ``bus0``."""
    if port == "0":
        return pd.Series(-1.0, index=n.links.index)
    return n.links[_efficiency_column(port)].astype(float)


def port_bus_carrier(n: pypsa.Network, port: str) -> pd.Series:
    """Return the carrier of the bus at a link port, empty if unconnected."""
    return n.links[f"bus{port}"].map(n.buses.carrier).fillna("")


def add_link_port(
    n: pypsa.Network, idx: pd.Index, bus: pd.Series, efficiency: pd.Series
) -> None:
    """
    Connect links to an extra bus at the next free port.

    Creates the ``bus{k}`` and ``efficiency{k}`` columns if they are missing.

    Parameters
    ----------
    n
        The network, modified in place.
    idx
        Links to connect.
    bus
        Bus per link, indexed like ``idx``.
    efficiency
        Efficiency at the new port per link, indexed like ``idx``.

    Returns
    -------
    :
        Modifies the network in place.
    """
    used = pd.DataFrame(
        {int(p): n.links.loc[idx, f"bus{p}"].fillna("") != "" for p in link_ports(n)}
    )
    next_port = used.apply(lambda row: row[row].index.max() + 1, axis=1)
    for port, links in next_port.groupby(next_port).groups.items():
        bus_col, eff_col = f"bus{port}", _efficiency_column(str(port))
        if bus_col not in n.links:
            n.links[bus_col] = ""
        if eff_col not in n.links:
            n.links[eff_col] = 1.0
        n.links.loc[links, bus_col] = bus.loc[links]
        n.links.loc[links, eff_col] = efficiency.loc[links]


def class_links(n: pypsa.Network, *classes: str) -> pd.Index:
    """Return the links of the given transformation classes without the marker."""
    carriers = [c for k in classes for c in TRANSFORMATION_CLASSES[k]]
    unmarked = (
        n.links[CARBON_ACCOUNTING_MARKER].isna()
        if CARBON_ACCOUNTING_MARKER in n.links
        else True
    )
    return n.links.index[n.links.carrier.isin(carriers) & unmarked]


def transform_emission_ports(n: pypsa.Network, ef: dict[str, float]) -> None:
    """
    Book bio-fuel producers, capture and vent links at their gross emissions.

    Applies ``atm_carrier = atm_technology + stored + EF_out * out`` to the
    unmarked links of the classes G1, C1 and V1 (spec section 3.2), with
    ``out = 0`` for capture and vent links. The rule is additive, so the EU
    sum at the ``co2 atmosphere`` bus stays the same once all attributes
    are claimed.

    Parameters
    ----------
    n
        The network, modified in place.
    ef
        Emission factor per fuel in t/MWh.

    Returns
    -------
    :
        Modifies the network in place.
    """
    idx = class_links(n, "G1", "C1", "V1")
    links = n.links.loc[idx]
    delta = pd.Series(0.0, index=idx)
    is_producer = links.carrier.isin(GREEN_BIO_PRODUCERS)
    for port in link_ports(n):
        carrier = port_bus_carrier(n, port).loc[idx]
        efficiency = port_efficiency(n, port).loc[idx]
        delta += efficiency.where(carrier == "co2 stored", 0.0)
        if port != "0":
            fuel_ef = carrier.map(ef).fillna(0.0)
            output = efficiency.clip(lower=0.0).where(is_producer, 0.0)
            delta += fuel_ef * output

    atm_port = pd.Series("", index=idx)
    for port in reversed(link_ports(n)[1:]):
        atm_port = atm_port.mask(links[f"bus{port}"] == ATMOSPHERE, port)
    for port, group in atm_port.groupby(atm_port).groups.items():
        if port == "":
            continue
        col = _efficiency_column(port)
        if col in n.links_t and n.links_t[col].columns.intersection(group).any():
            raise NotImplementedError(f"Time-varying '{col}' at {list(group)}.")
        n.links.loc[group, col] += delta.loc[group]

    missing = atm_port.index[(atm_port == "") & (delta != 0)]
    if not missing.empty:
        add_link_port(n, missing, pd.Series(ATMOSPHERE, index=missing), delta)
    logger.info(
        f"Booked {len(idx)} producer, capture and vent links at gross emissions."
    )


def mark_transformed(n: pypsa.Network) -> None:
    """Mark all links of the transformed classes, so they are skipped next time."""
    idx = class_links(n, *TRANSFORMED_CLASSES)
    n.links.loc[idx, CARBON_ACCOUNTING_MARKER] = "carrier"


def unclassified_carriers(n: pypsa.Network) -> list[str]:
    """
    Return link carriers at CO2 or fuel buses without a transformation class.

    Parameters
    ----------
    n
        The network to check.

    Returns
    -------
    :
        Sorted carriers of links with a port at a CO2 or fuel bus that are
        not listed in any class of :data:`TRANSFORMATION_CLASSES`.
    """
    bus_carriers = (*CO2_BUS_CARRIERS, *ACCOUNTING_FUELS)
    at_bus = pd.Series(False, index=n.links.index)
    for port in link_ports(n):
        at_bus |= n.links[f"bus{port}"].map(n.buses.carrier).isin(bus_carriers)
    classified = {c for carriers in TRANSFORMATION_CLASSES.values() for c in carriers}
    return sorted(set(n.links.carrier[at_bus]) - classified)


def apply_carbon_accounting(
    n: pypsa.Network, snakemake: Snakemake, costs: pd.DataFrame
) -> None:
    """
    Apply the configured carbon accounting method to the composed network.

    Returns early for the ``technology`` method or a disabled feature, so the
    network stays unchanged.

    Parameters
    ----------
    n
        The composed network, modified in place.
    snakemake
        The Snakemake workflow object of the ``compose_network`` rule.
    costs
        Processed cost DataFrame for the current planning horizon.

    Returns
    -------
    :
        Modifies the network in place.

    Raises
    ------
    ValueError
        If the method is unknown or a link carrier at a CO2 or fuel bus is
        not classified.
    """
    method = carbon_accounting_method(snakemake.config["mods"]["carbon_accounting"])
    if method != "carrier":
        return

    unclassified = unclassified_carriers(n)
    if unclassified:
        raise ValueError(
            f"Link carriers at CO2 or fuel buses without a carbon accounting "
            f"class: {unclassified}. Add them to mods/constants.py."
        )
    logger.info("Applying the carrier-based carbon accounting method.")

    ef = emission_factors(costs)
    transform_emission_ports(n, ef)
    mark_transformed(n)
