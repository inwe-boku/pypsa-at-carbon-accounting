# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""
Annual bounds of the carrier-based carbon accounting method.

Countries can claim green fuel attributes up to their own consumption, and gas
attributes only travel as far as the gas does. Optionally, the pooled claims
of countries with a national budget are capped at their fair share. See
sections 3.3 and 3.6 of ``docs-at/explanations/carbon-accounting-spec.md``.
"""

import logging

import pandas as pd
import pypsa
from linopy import LinearExpression

from mods.constants import CLAIM_CARRIERS, FAIR_SHARE_KEY, FUEL_TRANSPORT
from mods.network.carbon_accounting import (
    CLAIMS,
    POOL,
    attribute_bus,
    carbon_accounting_method,
    claim_link,
    fair_share_tolerance,
    gas_borders,
    link_ports,
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
    for claim in (c for claims in CLAIMS.values() for c in claims):
        links = n.links.index[n.links.carrier == CLAIM_CARRIERS[claim]]
        for ct in links.str[:2]:
            lhs = annual_flow(n, pd.Index([claim_link(ct, claim)])) - annual_flow(
                n, claimable_consumers(n, ct, claim)
            )
            name = f"carbon_accounting-claim-{ct}-{claim.replace(' ', '-')}"
            n.model.add_constraints(lhs <= 0, name=name)


def _add_transfer_bounds(n: pypsa.Network) -> None:
    """Bound the gas attribute transfers by the annual gas flow across a border (D1)."""
    borders = gas_borders(n)
    for (x, y), pipes in borders.groupby(["from", "to"]).groups.items():
        efficiency = n.links.loc[pipes, "efficiency"]
        lhs = annual_flow(n, pd.Index([transfer_link(x, y)])) - annual_flow(
            n, pipes, efficiency
        )
        n.model.add_constraints(lhs <= 0, name=f"carbon_accounting-transfer-{x}-{y}")


def green_production(n: pypsa.Network, fuel: str) -> LinearExpression:
    """
    Return the annual attributes that flow into the EU pool of a fuel.

    Parameters
    ----------
    n
        The network with an optimisation model.
    fuel
        Pooled fuel, ``oil`` or ``methanol``.

    Returns
    -------
    :
        ``sum_t w_t * efficiency * p0(l, t)`` over the attribute ports of
        all producers and imports of the fuel.
    """
    pool = attribute_bus(POOL, fuel)
    efficiency = pd.Series(0.0, index=n.links.index)
    for port in link_ports(n)[1:]:
        efficiency += port_efficiency(n, port).where(n.links[f"bus{port}"] == pool, 0.0)
    producers = efficiency.index[efficiency != 0]
    return annual_flow(n, producers, efficiency[producers])


def _add_fair_share_bounds(n: pypsa.Network, cts: list[str], tolerance: float) -> None:
    """Cap the pooled claims of countries with a national budget at their fair share (D18)."""
    keys = n.links[FAIR_SHARE_KEY].dropna()
    keys = keys[keys.index.str[:2].isin(cts)]
    production = {}
    for claim, key in keys.items():
        kind = n.links.at[claim, "carrier"]
        kind = next(k for k, c in CLAIM_CARRIERS.items() if c == kind)
        fuel = kind.split()[0]
        if fuel not in production:
            production[fuel] = green_production(n, fuel)
        lhs = annual_flow(n, pd.Index([claim])) - tolerance * key * production[fuel]
        name = f"carbon_accounting-fair-share-{claim[:2]}-{kind.replace(' ', '-')}"
        n.model.add_constraints(lhs <= 0, name=name)


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
    config = snakemake.config["mods"]["carbon_accounting"]
    if carbon_accounting_method(config) != "carrier":
        return
    logger.info(f"Adding carbon accounting bounds for {investment_year}.")
    _add_claim_bounds(n)
    _add_transfer_bounds(n)
    tolerance = fair_share_tolerance(config)
    if tolerance is not None:
        budgets = snakemake.config["solving"]["constraints"]["co2_budget_national"]
        _add_fair_share_bounds(n, list(budgets or {}), tolerance)
