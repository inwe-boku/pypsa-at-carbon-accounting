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

from mods.constants import CAPTURE_ELIGIBLE, CLAIM_CARRIERS, FUEL_TRANSPORT
from mods.network.carbon_accounting import (
    CLAIMS,
    carbon_accounting_method,
    claim_link,
    gas_borders,
    link_ports,
    port_bus_carrier,
    port_efficiency,
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


def claimable_consumers(n: pypsa.Network, ct: str, claim: str) -> pd.Index:
    """
    Return the links whose consumption bounds a claim of a country.

    Aviation claims are bounded by ``kerosene for aviation`` alone, the other
    oil claims by all other oil consumption.

    Parameters
    ----------
    n
        The network.
    ct
        Country code.
    claim
        Claim kind, a key of :data:`mods.constants.CLAIM_CARRIERS`.

    Returns
    -------
    :
        The consuming links.
    """
    fuel = claim.split()[0]
    consumers = fuel_consumers(n, ct, fuel)
    if fuel != "oil":
        return consumers
    aviation = n.links.loc[consumers, "carrier"] == "kerosene for aviation"
    return consumers[aviation] if claim == "oil aviation" else consumers[~aviation]


def _add_claim_bounds(n: pypsa.Network) -> None:
    """Bound the claims of each country by its consumption (D4, D7)."""
    fuel_claims = (c for k, claims in CLAIMS.items() if k != "removal" for c in claims)
    for claim in fuel_claims:
        links = n.links.index[n.links.carrier == CLAIM_CARRIERS[claim]]
        for ct in links.str[:2]:
            lhs = annual_flow(n, pd.Index([claim_link(ct, claim)])) - annual_flow(
                n, claimable_consumers(n, ct, claim)
            )
            name = f"carbon_accounting-claim-{ct}-{claim.replace(' ', '-')}"
            n.model.add_constraints(lhs <= 0, name=name)


def gross_capture(n: pypsa.Network) -> pd.Series:
    """
    Return the CO2 captured per unit of ``p0`` of the capture-eligible links.

    Parameters
    ----------
    n
        The network.

    Returns
    -------
    :
        Sum of the efficiencies at ``co2 stored`` ports, per eligible link.
    """
    idx = n.links.index[n.links.carrier.isin(CAPTURE_ELIGIBLE)]
    captured = pd.Series(0.0, index=idx)
    for port in link_ports(n)[1:]:
        at_store = port_bus_carrier(n, port).loc[idx] == "co2 stored"
        captured += port_efficiency(n, port).loc[idx].where(at_store, 0.0)
    return captured


def _add_removal_bounds(n: pypsa.Network) -> None:
    """Bound the removal claims of each country by its gross capture (D9, D15)."""
    captured = gross_capture(n)
    claims = n.links.index[n.links.carrier == CLAIM_CARRIERS["removal"]]
    for ct in claims.str[:2]:
        capture = captured[captured.index.str.startswith(ct)]
        lhs = annual_flow(n, pd.Index([claim_link(ct, "removal")])) - annual_flow(
            n, capture.index, capture
        )
        n.model.add_constraints(lhs <= 0, name=f"carbon_accounting-removal-{ct}")


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
    _add_removal_bounds(n)
