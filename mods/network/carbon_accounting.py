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


def link_ports(n: pypsa.Network) -> list[str]:
    """Return the port numbers of all link buses, e.g. ``["0", "1", "2"]``."""
    ports = n.links.columns[n.links.columns.str.fullmatch(r"bus\d+")].str[3:]
    return sorted(ports, key=int)


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
