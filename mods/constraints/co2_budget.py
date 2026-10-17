# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""National CO2 budget constraints applied during the solve phase."""

import logging

import pandas as pd
import pypsa

from mods.constants import AVIATION_CARRIERS

logger = logging.getLogger(__name__)


def _atmosphere_efficiency(links: pd.DataFrame) -> pd.Series:
    """
    Return the efficiency of each link at its ``co2 atmosphere`` port.

    Parameters
    ----------
    links
        Links with exactly one output port at ``co2 atmosphere``.

    Returns
    -------
    :
        Efficiency per link.

    Raises
    ------
    ValueError
        If a link has no or several output ports at ``co2 atmosphere``.
    """
    ports = links.columns[links.columns.str.fullmatch(r"bus[1-9]\d*")].str[3:]
    at_atm = pd.DataFrame({p: links[f"bus{p}"] == "co2 atmosphere" for p in ports})
    if not at_atm.sum(axis=1).eq(1).all():
        raise ValueError(f"Expected one 'co2 atmosphere' port at {list(links.index)}.")
    efficiency = pd.DataFrame(
        {p: links["efficiency" if p == "1" else f"efficiency{p}"] for p in ports}
    )
    return efficiency.where(at_atm).sum(axis=1)


def constraint_national_co2_budgets(
    n: pypsa.Network, snakemake, investment_year: int
) -> None:
    """
    Replaces the PyPSA-DE national CO2 budget function.

    This function
      - adds CO2 national constraints based on a balance at
        the `co2 atmosphere` bus
      - scales emissions from kerosene for aviation with the
        domestic / total aviation ratio
      - emissions (positive and negative) are balanced at the
        respective model region

    Differences to PyPSA-DE
      - Does not distinguish E-Fuels like BioMethane or synthetic oil.

    Parameters
    ----------
    n
        The pypsa network to add the constraints to.
    snakemake
        The snakemake workflow object.
    investment_year
        The myopic planning horizon.

    Notes
    -----
    This function overwrites the PyPSA-DE function in
    scripts/pypsa-de/additional_functionality.py.
    """
    national_co2_budgets = snakemake.params.solving["constraints"][
        "co2_budget_national"
    ]
    logger.info(f"Adding national CO2 budgets to {n.name} for year {investment_year}")
    nhours = n.snapshot_weightings.generators.sum()
    nyears = nhours / 8760
    MtCO2_to_tCO2 = 1e6

    co2_totals = pd.read_csv(snakemake.input.co2_totals_name, index_col=0).mul(
        MtCO2_to_tCO2
    )
    from scripts.prepare_sector_network import determine_emission_sectors

    sectors = determine_emission_sectors(n.meta["sector"])
    co2_total_totals = co2_totals[sectors].sum(axis=1) * nyears

    for ct, budget in national_co2_budgets.items():
        limit = co2_total_totals[ct] * budget[investment_year]
        logger.info(
            f"Limiting emissions in country {ct} to {budget[investment_year]:.1%} of "
            f"1990 levels, i.e. {limit:,.2f} tCO2/a",
        )

        lhs = []
        weightings = n.snapshot_weightings.generators
        links = n.components.links.static
        link_ports = links.filter(like="bus").columns.str[3:]
        for port in link_ports:
            # exclude aviation here to multiply it with a domestic factor later
            idx = links.index[
                links.index.str.startswith(ct)
                & (links[f"bus{port}"] == "co2 atmosphere")
                & ~links.carrier.isin(AVIATION_CARRIERS)
            ]

            logger.info(
                f"For {ct} adding following link carriers to port {port} "
                f"CO2 constraint: {sorted(links.loc[idx, 'carrier'].unique())}"
            )

            if port == "0":
                efficiency = -1.0
            elif port == "1":
                efficiency = links.loc[idx, "efficiency"]
            else:
                efficiency = links.loc[idx, f"efficiency{port}"]

            port_emissions = (
                n.model["Link-p"].loc[:, idx].mul(efficiency).mul(weightings).sum()
            )
            lhs.append(port_emissions)

        # Aviation demand
        country_year = (ct, snakemake.params.energy_totals_year)
        energy_totals = pd.read_csv(snakemake.input.energy_totals, index_col=[0, 1])
        aviation_domestic = energy_totals.loc[country_year, "total domestic aviation"]
        aviation_international = energy_totals.loc[
            country_year, "total international aviation"
        ]
        aviation_total = aviation_domestic + aviation_international
        if aviation_total == 0:  # avoids division by zero errors
            domestic_aviation_factor = 0.0
        else:
            domestic_aviation_factor = aviation_domestic / aviation_total
        # kerosene and the claims of green kerosene (carbon accounting method
        # 'carrier') are scaled alike
        aviation_links = links[
            links.index.str.startswith(ct) & links.carrier.isin(AVIATION_CARRIERS)
        ]
        aviation_emissions = (
            n.model["Link-p"]
            .loc[:, aviation_links.index]
            .mul(_atmosphere_efficiency(aviation_links))
            .mul(weightings)
            .sum()
            .mul(domestic_aviation_factor)
        )
        lhs.append(aviation_emissions)
        logger.info(
            f"Adding domestic aviation emissions for {ct} with "
            f"a factor of {domestic_aviation_factor:.2f}"
        )

        # Navigation demand
        # todo: do we need to deduct emissions from internation navigation?

        # add total regional emissions to the model and make sure to drop existing
        # constraints from previous year
        lhs = sum(lhs)

        cname = f"co2_limit-{ct}"

        n.model.add_constraints(
            lhs <= limit,
            name=f"GlobalConstraint-{cname}",
        )

        if cname in n.global_constraints.index:
            logger.warning(
                f"Global constraint {cname} already exists. Dropping and adding it again."
            )
            n.global_constraints.drop(cname, inplace=True)

        n.add(
            "GlobalConstraint",
            cname,
            constant=limit,
            sense="<=",
            type="",
            carrier_attribute="",
        )
