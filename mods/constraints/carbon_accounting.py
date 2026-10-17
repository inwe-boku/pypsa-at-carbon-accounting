# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""
Annual bounds of the carrier-based carbon accounting method.

Countries can claim green fuel attributes up to their own consumption, and gas
attributes only travel as far as the gas does. See section 3.3 of
``docs-at/explanations/carbon-accounting-spec.md``.
"""

import logging

import pandas as pd
import pypsa
from linopy import LinearExpression

from mods.constants import FUEL_TRANSPORT
from mods.network.carbon_accounting import (
    bus_country,
    carbon_accounting_method,
    claim_link,
    gas_borders,
    transfer_link,
)

logger = logging.getLogger(__name__)


def annual_flow(
    n: pypsa.Network, idx: pd.Index, efficiency: pd.Series | float = 1.0
) -> LinearExpression:
    """
    Return the weighted annual flow of links at bus0, times an efficiency.

    Parameters
    ----------
    n
        The network with an optimisation model.
    idx
        Links to sum.
    efficiency
        Factor per link, or one factor for all.

    Returns
    -------
    :
        ``sum_t w_t * efficiency * p0(l, t)`` over all links.
    """
    weightings = n.snapshot_weightings.generators
    return n.model["Link-p"].loc[:, idx].mul(efficiency).mul(weightings).sum()


def fuel_consumers(n: pypsa.Network, ct: str, fuel: str) -> pd.Index:
    """
    Return the links of a country that consume a fuel, without fuel transport.

    Parameters
    ----------
    n
        The network.
    ct
        Country code; links are selected by name prefix.
    fuel
        Bus carrier at ``bus0``, e.g. ``gas``.

    Returns
    -------
    :
        The consuming links.
    """
    links = n.links
    consumes = links.bus0.map(n.buses.carrier) == fuel
    in_country = links.index.str.startswith(ct)
    return links.index[consumes & in_country & ~links.carrier.isin(FUEL_TRANSPORT)]


def _add_claim_bounds(n: pypsa.Network) -> None:
    """Bound the claims of each country by its consumption (D4, D7)."""
    claims = n.links.index[n.links.carrier == "renewable gas claim"]
    for ct in bus_country(n, n.links.loc[claims, "bus0"]):
        consumers = fuel_consumers(n, ct, "gas")
        lhs = annual_flow(n, pd.Index([claim_link(ct, "gas")])) - annual_flow(
            n, consumers
        )
        n.model.add_constraints(lhs <= 0, name=f"carbon_accounting-claim-{ct}-gas")


def _add_transfer_bounds(n: pypsa.Network) -> None:
    """Bound the gas attribute transfers by the annual gas flow across a border (D1)."""
    borders = gas_borders(n)
    for (x, y), pipes in borders.groupby(["from", "to"]).groups.items():
        efficiency = n.links.loc[pipes, "efficiency"]
        lhs = annual_flow(n, pd.Index([transfer_link(x, y)])) - annual_flow(
            n, pipes, efficiency
        )
        n.model.add_constraints(lhs <= 0, name=f"carbon_accounting-transfer-{x}-{y}")


def constraint_carbon_accounting(
    n: pypsa.Network, snakemake, investment_year: int
) -> None:
    """
    Add the annual bounds of the carrier-based carbon accounting method.

    Returns early unless the ``carrier`` method is active.

    Parameters
    ----------
    n
        The network with an optimisation model.
    snakemake
        The Snakemake workflow object of the solve rule.
    investment_year
        The myopic planning horizon.

    Returns
    -------
    :
        Adds constraints to ``n.model``.
    """
    method = carbon_accounting_method(snakemake.config["mods"]["carbon_accounting"])
    if method != "carrier":
        return
    logger.info(f"Adding carbon accounting bounds for {investment_year}.")
    _add_claim_bounds(n)
    _add_transfer_bounds(n)
