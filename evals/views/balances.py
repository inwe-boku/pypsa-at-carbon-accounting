# SPDX-FileCopyrightText: 2023-2025 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
import logging
from pathlib import Path

import pandas as pd
from pypsa import NetworkCollection

from evals.constants import DataModel as DM
from evals.fileio import Exporter
from evals.stats import collect_myopic_statistics
from evals.utils import (
    carbon_accounting_claims,
    get_energy_totals_domestic_share,
    get_heat_loss_factor,
    insert_index_level,
    rename_aggregate,
    scale_domestic_aviation,
    split_urban_central_heat_losses_and_consumption,
)
from evals.views.common import get_energy_for_heat_production, simple_bus_balance

logger = logging.getLogger(__name__)


def view_balance_carbon(
    result_path: str | Path,
    nc: NetworkCollection,
    config: dict,
) -> None:
    """
    Evaluate and export the carbon balance showing CO2 flows to and from atmosphere.

    This function analyzes CO2 emissions and deductions from the atmosphere bus only.
    It corresponds shows the national CO2 budget per year.

    Parameters
    ----------
    result_path
        Path where the evaluation results will be saved.
    nc
        Dictionary containing PyPSA network objects, typically keyed by year or scenario.
    config
        Configuration dictionary containing view settings including bus_carrier specification,
        chart type, and export parameters.
    """
    bus_carrier = config["view"]["bus_carrier"]

    co2_balance = collect_myopic_statistics(
        nc, "energy_balance", bus_carrier=bus_carrier
    )

    # need to deduct emission from international aviation.
    first_year = nc.index[0]
    energy_totals = pd.DataFrame.from_dict(
        nc[first_year].meta["resources"]["energy_totals"], orient="tight"
    )
    domestic_aviation_factors = get_energy_totals_domestic_share(
        energy_totals, kind="aviation"
    )

    # The domestic aviation factor reduces co2 emissions for aviation per country
    co2_balance = scale_domestic_aviation(co2_balance, domestic_aviation_factors)

    exporter = Exporter(statistics=[co2_balance], view_config=config["view"])
    exporter.export(result_path, config["global"]["subdir"])


def view_balance_carbon_claims(
    result_path: str | Path,
    nc: NetworkCollection,
    config: dict,
) -> None:
    """
    Evaluate and export the CO2 credits claimed per country (carbon accounting).

    Under the carrier-based carbon accounting method, countries claim green
    fuel credits up to their own consumption. The view shows the claimed
    credits next to a reference: the credit at the EU-average green share of
    each fuel. Skipped for the technology-based method, which has no claims.

    Parameters
    ----------
    result_path
        Path where the evaluation results will be saved.
    nc
        Collection of solved networks, indexed by year.
    config
        Configuration dictionary containing the view settings.
    """
    credits = [
        insert_index_level(carbon_accounting_claims(n), year, DM.YEAR)
        for year, n in nc.networks.items()
    ]
    credits = [c for c in credits if not c.empty]
    if not credits:
        logger.info("No carbon accounting claims found, skipping the view.")
        return

    statistic = pd.concat(credits)
    statistic = rename_aggregate(
        statistic, config["view"]["bus_carrier_groups"], level=DM.BUS_CARRIER
    )
    statistic.attrs["unit"] = "t_co2"
    exporter = Exporter(statistics=[statistic], view_config=config["view"])
    exporter.export(result_path, config["global"]["subdir"])


def view_balance_electricity(
    result_path: str | Path,
    nc: NetworkCollection,
    config: dict,
) -> None:
    """
    Evaluate and export electricity production and demand by country and year.

    This function calculates the electricity balance showing generation sources and
    consumption patterns across the network. It delegates to the simple_bus_balance
    function to collect and export supply and withdrawal statistics for electricity buses.

    Parameters
    ----------
    result_path
        Path where the evaluation results will be saved.
    nc
        Dictionary containing PyPSA network objects, typically keyed by year or scenario.
    config
        Configuration dictionary containing view settings including bus_carrier specification,
        chart type, and export parameters.

    Notes
    -----
    Balances do not add up to zero, because of domestic transmission losses.
    """
    simple_bus_balance(nc, config, result_path)


def view_balance_heat(
    result_path: str | Path,
    nc: NetworkCollection,
    config: dict,
) -> None:
    """
    Evaluate and export the heat balance showing heat production and consumption.

    This function calculates the heat balance for specified heat bus carriers (urban central,
    urban decentral, rural heat) by analyzing link energy flows and load withdrawals. Heat
    supply is determined from link energy balance data, showing the input energy carriers
    feeding heat production. Heat demand includes consumption loads, with central heat losses
    separated from actual consumption. The function exports both supply and demand data with
    appropriate chart formatting based on the configured chart type.

    Parameters
    ----------
    result_path
        Path where the evaluation results will be saved.
    nc
        Dictionary containing PyPSA network objects, typically keyed by year or scenario.
    config
        Configuration dictionary containing view settings including bus_carrier specification,
        chart type, and export parameters.

    Notes
    -----
    Heat supply is calculated from link energy balances, excluding CO2 flows. For urban
    central heat, distribution losses are separated from consumption using heat loss factors.
    The function supports both ESMGroupedBarChart (preserving individual bus carriers) and
    ESMBarChart (aggregating bus carriers) visualization modes.
    """
    bus_carrier = config["view"]["bus_carrier"]

    heat_mix = get_energy_for_heat_production(nc, drop_regex="")
    heat_mix = heat_mix.swaplevel(DM.CARRIER, DM.BUS_CARRIER)
    heat_mix.index.names = DM.YEAR_IDX_NAMES

    generator_supply = collect_myopic_statistics(
        nc,
        "supply",
        comps="Generator",
        bus_carrier=bus_carrier,
    )

    heat_loss_factor = get_heat_loss_factor(nc)
    demand = (
        collect_myopic_statistics(
            nc,
            "withdrawal",
            bus_carrier=bus_carrier,
        )
        .pipe(split_urban_central_heat_losses_and_consumption, heat_loss_factor)
        .mul(-1)
    )

    exporter = Exporter(
        statistics=[heat_mix, demand, generator_supply], view_config=config["view"]
    )
    exporter.export(result_path, config["global"]["subdir"])


def view_balance_hydrogen(
    result_path: str | Path,
    nc: NetworkCollection,
    config: dict,
) -> None:
    """
    Evaluate and export the hydrogen balance showing H2 production and consumption.

    This function analyzes hydrogen flows in the energy system, including production
    from electrolyzers and other sources, as well as consumption from fuel cells,
    industrial processes, and other hydrogen-using technologies. It delegates to the
    simple_bus_balance function to collect and export supply and withdrawal statistics
    for hydrogen buses.

    Parameters
    ----------
    result_path
        Path where the evaluation results will be saved.
    nc
        Dictionary containing PyPSA network objects, typically keyed by year or scenario.
    config
        Configuration dictionary containing view settings including bus_carrier specification,
        chart type, and export parameters.
    """
    simple_bus_balance(nc, config, result_path)


def view_balance_methane(
    result_path: str | Path,
    nc: NetworkCollection,
    config: dict,
) -> None:
    """
    Evaluate and export the methane balance showing natural gas and biogas flows.

    This function analyzes methane (CH4) flows in the energy system, including natural gas
    supply from pipelines and storage, biogas production, methanation processes, and
    consumption in gas boilers, combined heat and power plants, and other gas-consuming
    technologies. It delegates to the simple_bus_balance function to collect and export
    supply and withdrawal statistics for methane buses.

    Parameters
    ----------
    result_path
        Path where the evaluation results will be saved.
    nc
        Dictionary containing PyPSA network objects, typically keyed by year or scenario.
    config
        Configuration dictionary containing view settings including bus_carrier specification,
        chart type, and export parameters.
    """
    simple_bus_balance(nc, config, result_path)


def view_balance_biomass(
    result_path: str | Path,
    nc: NetworkCollection,
    config: dict,
) -> None:
    """
    Evaluate and export the solid biomass balance showing biomass supply and consumption.

    This function analyzes solid biomass flows in the energy system, including biomass
    supply from forestry and agriculture, biomass imports, and consumption in biomass
    boilers, combined heat and power plants, and other biomass-using technologies. It
    delegates to the simple_bus_balance function to collect and export supply and
    withdrawal statistics for solid biomass buses.

    Parameters
    ----------
    result_path
        Path where the evaluation results will be saved.
    nc
        Dictionary containing PyPSA network objects, typically keyed by year or scenario.
    config
        Configuration dictionary containing view settings including bus_carrier specification,
        chart type, and export parameters.
    """
    simple_bus_balance(nc, config, result_path)


def view_balance_fuels(
    result_path: str | Path,
    nc: NetworkCollection,
    config: dict,
) -> None:
    """
    Evaluate and export the fuel balance showing primary fuel supply and consumption.

    This function calculates the energy balance for primary fuel carriers including coal,
    lignite, oil, solid biomass, methanol, ammonia, waste, and uranium. It delegates to
    the simple_bus_balance function to collect and export supply and withdrawal statistics
    for specified fuel bus carriers. The view supports grouping of related fuels (e.g.,
    coal and lignite) through the bus_carrier_groups configuration.

    Parameters
    ----------
    result_path
        Path where the evaluation results will be saved.
    nc
        Dictionary containing PyPSA network objects, typically keyed by year or scenario.
    config
        Configuration dictionary containing view settings including bus_carrier specification,
        bus_carrier_groups for aggregating related fuels, chart type, and export parameters.

    Notes
    -----
    This view is particularly useful for analyzing primary energy supply and fuel consumption
    patterns across different sectors (industry, transport, heat, power generation). The
    bus_carrier_groups configuration allows aggregation of similar fuels for clearer
    visualization, such as combining coal and lignite into a single "Coal" category.
    """
    simple_bus_balance(nc, config, result_path)
