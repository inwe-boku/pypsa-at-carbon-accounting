# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""
Emissions of carbon accounting runs under every accounting method (marimo).

Evaluates every run of the carbon accounting configuration for all planning
horizons with the technology method and the carrier method, and answers:

1. Are different evaluation methods needed for the two accounting methods?
2. How do they differ, and what happens if they are applied vice versa?
3. How do the decarbonisation pathways of the approaches differ?

All computations live in ``evals/carbon_accounting_pathways.py``. Run it from
the repository root with

    pixi run marimo edit evals/notebooks/carbon_accounting_pathways.py
"""

import marimo

__generated_with = "0.25.0"
app = marimo.App(width="full", app_title="Carbon accounting pathways")


@app.cell
def _():
    import sys
    from pathlib import Path

    import marimo as mo
    import pandas as pd
    import plotly.graph_objects as go

    REPO = Path(__file__).resolve().parents[2]
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    from evals import carbon_accounting as ca
    from evals import carbon_accounting_pathways as cp

    return REPO, ca, cp, go, mo, pd


@app.cell
def _(cp, mo):
    # categorical slots in fixed order (validated palette); grey for "Other"
    PALETTE = [
        "#2a78d6",
        "#eb6834",
        "#1baf7a",
        "#eda100",
        "#e87ba4",
        "#008300",
        "#6250d6",
        "#e34948",
    ]
    EVALUATOR_COLOURS = dict(zip(cp.EVALUATORS, PALETTE))
    SECTOR_COLOURS = {name: PALETTE[i] for i, (name, _) in enumerate(cp.SECTORS)}
    SECTOR_COLOURS["Other"] = "#8a8984"
    ICONS = {"ok": "✓", "warn": "⚠", "fail": "✗"}
    KINDS = {"ok": "success", "warn": "warn", "fail": "danger"}

    def badge(check) -> mo.Html:
        """Return a callout with the status icon, name and message of a check."""
        text = mo.md(f"**{ICONS[check.status]} {check.name}** — {check.message}")
        return mo.callout(text, kind=KINDS[check.status])

    def layout(fig, title: str, yaxis: str = "Mt CO2", height: int = 420):
        fig.update_layout(
            title=title,
            template="plotly_white",
            bargap=0.3,
            legend=dict(orientation="h", y=-0.2),
            hovermode="x unified",
            height=height,
            margin=dict(t=60, b=40),
        )
        fig.update_yaxes(title_text=yaxis, zeroline=True, zerolinecolor="#c3c2b7")
        fig.update_xaxes(type="category")
        return fig

    return EVALUATOR_COLOURS, PALETTE, SECTOR_COLOURS, badge, layout


@app.cell
def _(mo):
    mo.md(r"""
    # Carbon accounting: emissions under every method

    A run is **optimised** under one accounting method (its *run method*), but its
    physical flows can be **evaluated** under any method (the *evaluator*):

    | Evaluator | Rule | Needs | Applies to |
    |---|---|---|---|
    | `technology` | the technology that releases CO2 books it; green producers book −EF at creation, green imports draw from the atmosphere | physical flows only | every run |
    | `carrier (optimised)` | green fuel books zero, fossil fuel +EF at the consumer, synthetic fuels are charged the CO2 they use; credits as **claimed by the optimiser** | the claims of the run | carrier runs |
    | `carrier (EU average)` | the same carrier rules; every consumer gets the **EU-wide green share** of its fuel | physical flows + an allocation rule | every run |

    National totals follow the national CO2 budget: links by name prefix, kerosene and
    kerosene claims scaled by the domestic aviation share. Claims are spread over the
    consuming links of the claiming country, so emissions can be shown by sector.
    """)
    return


@app.cell
def _(REPO, mo):
    root_input = mo.ui.text(
        value=str(REPO / "results" / "carbon-accounting"),
        label="Result root",
        full_width=True,
    )
    root_input
    return (root_input,)


@app.cell
def _(PALETTE, ca, mo, root_input):
    files = ca.find_scenarios(root_input.value)
    mo.stop(
        not files,
        mo.callout(
            mo.md(f"✗ No solved networks found in `{root_input.value}`."), kind="danger"
        ),
    )
    info = ca.run_info(files)
    colours = {s: PALETTE[i % len(PALETTE)] for i, s in enumerate(files)}
    scenario_select = mo.ui.multiselect(
        options=list(files), value=list(files), label="Scenarios"
    )
    budget_countries = sorted({c for b in info.budgets for c in b.split(", ") if c})
    country_select = mo.ui.dropdown(
        options=budget_countries,
        value="AT" if "AT" in budget_countries else budget_countries[0],
        label="Country",
    )
    return colours, country_select, files, info, scenario_select


@app.cell
def _(ca, country_select, info, mo, scenario_select):
    setup = ca.check_setup(info, list(scenario_select.value), country_select.value)
    _icon = {"ok": "✓", "warn": "⚠", "fail": "✗"}[setup.status]
    mo.output.replace(
        mo.vstack(
            [
                mo.md("## 0 Setup"),
                mo.hstack([scenario_select, country_select], justify="start", gap=2),
                mo.md(f"**{_icon}** {setup.message}"),
                mo.ui.table(
                    info.loc[list(scenario_select.value)]
                    .drop(columns="file")
                    .reset_index(),
                    selection=None,
                    label="Networks per scenario and horizon",
                ),
            ]
        )
    )
    mo.stop(setup.failed)
    selected = list(scenario_select.value)
    country = country_select.value
    return country, selected


@app.cell
def _(ca, cp, mo):
    @mo.cache
    def load(path: str) -> dict:
        """Load the solved networks of one scenario, once."""
        return ca.load_networks(path)

    @mo.cache
    def scenario_emissions(path: str, scenario: str):
        """Evaluate one scenario under every evaluator, once."""
        return cp.emissions({scenario: load(path)})

    @mo.cache
    def scenario_checks(path: str, scenario: str, countries: tuple) -> list:
        networks = {scenario: load(path)}
        return cp.checks(networks, cp.emissions(networks), list(countries))

    return load, scenario_checks, scenario_emissions


@app.cell
def _(
    badge,
    country,
    cp,
    files,
    info,
    load,
    mo,
    pd,
    scenario_checks,
    scenario_emissions,
    selected,
):
    _paths = {s: str(files[s][next(iter(files[s]))].parents[1]) for s in selected}
    _countries = tuple(sorted({c for b in info.budgets for c in b.split(", ") if c}))
    _frames, _checks = [], {}
    for _scenario in mo.status.progress_bar(
        selected, title="Loading and evaluating scenarios", remove_on_exit=True
    ):
        _frames.append(scenario_emissions(_paths[_scenario], _scenario))
        for _check in scenario_checks(_paths[_scenario], _scenario, _countries):
            _checks.setdefault(_check.name, []).append((_scenario, _check))
    frame = pd.concat(_frames)
    totals = cp.national_totals(frame, country)
    budget = cp.budgets({selected[0]: load(_paths[selected[0]])}, [country])[country]

    # one badge per check: a failed scenario if there is one
    _summary = [
        next((c for _, c in _items if c.failed), _items[0][1])
        for _items in _checks.values()
    ]
    _status = "fail" if any(c.failed for c in _summary) else "ok"
    mo.output.replace(
        mo.vstack(
            [
                mo.md(f"### Checks {dict(ok='✓', fail='✗')[_status]}"),
                *[badge(c) for c in _summary],
            ]
        )
    )
    mo.stop(_status == "fail")
    return budget, frame, totals


@app.cell
def _(budget, country, cp, mo, totals):
    _horizons = list(budget.index)
    _carrier = totals[cp.CARRIER_OPTIMISED].dropna()
    _gap = (_carrier - totals[cp.CARRIER_AVERAGE].reindex(_carrier.index)).abs()
    _gap_max = _gap.max() if len(_gap) else float("nan")
    _where = _gap.idxmax() if len(_gap) else ("–", "–", "–")
    _table = totals.copy()
    _table.insert(
        0, "budget", budget.reindex(totals.index.get_level_values("horizon")).to_numpy()
    )
    mo.vstack(
        [
            mo.md(
                f"""
    ## 1 Are different evaluation methods needed?

    **Yes, for the national split, not for the EU total.** All three evaluators give the
    same EU total for every run (check P5 above): both methods move CO2 between countries
    and between the moment a fuel is made and the moment it is burned, but they create or
    destroy no CO2.

    - **`technology`** needs nothing but the physical flows. It can be computed for every run,
      also ex post for carrier runs, by undoing the transformation of the green producers.
    - **`carrier`** needs, on top of the physical flows, an **allocation of the green
      attributes** to consuming countries. In a carrier run, this allocation is a decision
      variable (the claims). For a technology run it does not exist and must be set by a
      rule, here the EU-average green share.
    - The allocation is not a detail: for {country}, the optimised claims and the EU-average
      rule differ by up to **{_gap_max:.1f} Mt CO2** ({_where[0]}, {_where[2]}). Under the
      carrier method, the claims, not the physical system, decide most of the national total.

    National totals of {country} under every evaluator, with the national budget (Mt CO2):
    """
            ),
            mo.ui.table(_table.round(2).reset_index(), selection=None),
        ]
    )
    return


@app.cell
def _(EVALUATOR_COLOURS, budget, country, cp, go, layout, mo, totals):
    _gaps = cp.against_budget(totals, budget)

    def _run_figure(scenario: str):
        rows = totals.xs(scenario, level="scenario")
        method = rows.index.get_level_values("run method")[0]
        horizons = rows.index.get_level_values("horizon")
        fig = go.Figure()
        for evaluator, colour in EVALUATOR_COLOURS.items():
            if rows[evaluator].isna().all():
                continue
            own = (evaluator, method) in [
                (cp.TECHNOLOGY, "technology"),
                (cp.CARRIER_OPTIMISED, "carrier"),
            ]
            fig.add_trace(
                go.Bar(
                    x=horizons,
                    y=rows[evaluator],
                    name=evaluator + (" (own method)" if own else ""),
                    marker=dict(color=colour, line=dict(width=0)),
                    hovertemplate="%{y:.2f} Mt",
                )
            )
        fig.add_trace(
            go.Scatter(
                x=horizons,
                y=budget.reindex(horizons),
                name="budget",
                mode="lines+markers",
                line=dict(color="#0b0b0b", dash="dash", width=2),
                marker=dict(size=8),
                hovertemplate="%{y:.2f} Mt",
            )
        )
        return mo.ui.plotly(
            layout(fig, f"{country}: {scenario} (optimised with method {method})")
        )

    _scenarios = list(totals.index.unique("scenario"))
    mo.vstack(
        [
            mo.md(
                f"""
    ## 2 How do they differ, and what if they are applied vice versa?

    Each run under every evaluator, against the national budget of {country}. The bar of the
    run's own method meets the budget by construction (where the budget binds). The other
    bars show how the same physical system reads under the other method:

    - **A technology run read with carrier rules** loses the creation credits of the green
      fuels it produces (they go to whoever consumes them) and gains credits for the green
      fuels it consumes. A country that exports green fuels or CCU feedstock misses its
      budget under carrier accounting.
    - **A carrier run read with technology rules** loses all claims and gets the creation
      credits of its own producers back. A country that met its budget by claiming credits
      shows its physical emissions, typically far above the budget.

    Distance to the budget (Mt CO2, positive = above budget):
    """
            ),
            mo.ui.table(_gaps.round(2).reset_index(), selection=None),
            *[_run_figure(s) for s in _scenarios],
        ]
    )
    return


@app.cell
def _(country, cp, go, mo, totals):
    _year = "2040"
    _horizon = totals.index.get_level_values("horizon").astype(str)
    _rows = totals[_horizon == _year].droplevel(["run method", "horizon"])
    _z = _rows.T.reindex(list(cp.EVALUATORS))
    _heat = go.Figure(
        go.Heatmap(
            x=list(_z.columns),
            y=list(_z.index),
            z=_z.to_numpy(),
            text=_z.map(lambda v: "" if v != v else f"{v:.1f}").to_numpy(),
            texttemplate="%{text}",
            colorscale="RdBu_r",
            zmid=0,
            colorbar=dict(title="Mt CO2"),
            hovertemplate="%{y}<br>%{x}: %{z:.2f} Mt<extra></extra>",
        )
    )
    _heat.update_layout(
        title=f"{country} {_year}: emissions per run and evaluator (Mt CO2)",
        template="plotly_white",
        height=360,
        margin=dict(t=60, b=40),
    )
    _heat.update_xaxes(title_text="Run", type="category")
    _heat.update_yaxes(title_text="Evaluator", autorange="reversed")
    mo.vstack(
        [
            mo.md(
                f"### {country} in {_year}: every run under every evaluator\n\n"
                "Blank cells: the evaluator does not apply (no claims in a technology run)."
            ),
            mo.ui.plotly(_heat),
        ]
    )
    return


@app.cell
def _(cp, mo, selected):
    pathway_run = mo.ui.dropdown(options=selected, value=selected[0], label="Run")
    pathway_evaluator = mo.ui.dropdown(
        options=list(cp.EVALUATORS), value=cp.TECHNOLOGY, label="Evaluator"
    )
    return pathway_evaluator, pathway_run


@app.cell
def _(budget, colours, country, cp, go, layout, mo, totals):
    _own = cp.own_method(totals)
    _lines = go.Figure()
    for _scenario in totals.index.unique("scenario"):
        _rows = totals.xs(_scenario, level="scenario")
        _x = _rows.index.get_level_values("horizon")
        for _series, _dash, _label in [
            (_own.xs(_scenario, level="scenario"), "solid", "own method"),
            (_rows[cp.TECHNOLOGY], "dot", "technology"),
        ]:
            _lines.add_trace(
                go.Scatter(
                    x=_x,
                    y=_series,
                    name=f"{_scenario}: {_label}",
                    mode="lines+markers",
                    line=dict(color=colours[_scenario], dash=_dash, width=2),
                    marker=dict(size=8),
                    hovertemplate="%{y:.2f} Mt",
                )
            )
    _lines.add_trace(
        go.Scatter(
            x=list(budget.index),
            y=budget,
            name="budget",
            mode="lines",
            line=dict(color="#0b0b0b", dash="dash", width=1),
        )
    )
    layout(
        _lines, f"{country}: reported (own method) vs physical yardstick (technology)"
    )

    _yardstick = totals[cp.TECHNOLOGY].unstack("horizon").droplevel("run method")
    mo.vstack(
        [
            mo.md(
                f"""
    ## 3 Decarbonisation pathways

    The **reported** pathway (solid, each run under its own method) follows the budget in
    every run, so it cannot tell the approaches apart. The **physical** pathway needs one
    yardstick for all runs; the technology evaluator is the natural one, because it depends on
    the physical flows only (dotted). The gap between solid and dotted lines of a carrier run
    is what {country} meets by claims instead of domestic abatement and domestic green fuel
    production.

    {country} under the technology yardstick (Mt CO2):
    """
            ),
            mo.ui.table(_yardstick.round(2).reset_index(), selection=None),
            mo.ui.plotly(_lines),
        ]
    )
    return


@app.cell
def _(
    SECTOR_COLOURS,
    country,
    cp,
    frame,
    go,
    layout,
    mo,
    pathway_evaluator,
    pathway_run,
):
    _pathway = cp.sector_pathway(
        frame, country, pathway_run.value, pathway_evaluator.value, cutoff=1e-3
    )
    if _pathway.empty:
        _chart = mo.callout(
            mo.md(
                f"`{pathway_evaluator.value}` does not apply to {pathway_run.value}: "
                "it has no claims."
            ),
            kind="warn",
        )
    else:
        _fig = go.Figure()
        for _sector in _pathway.columns:
            _fig.add_trace(
                go.Bar(
                    x=_pathway.index,
                    y=_pathway[_sector],
                    name=_sector,
                    marker=dict(color=SECTOR_COLOURS[_sector], line=dict(width=0)),
                    hovertemplate="%{y:.2f} Mt",
                )
            )
        _fig.add_trace(
            go.Scatter(
                x=_pathway.index,
                y=_pathway.sum(axis=1),
                name="total",
                mode="lines+markers",
                line=dict(color="#0b0b0b", width=2),
                marker=dict(size=8),
                hovertemplate="%{y:.2f} Mt",
            )
        )
        _fig.update_layout(barmode="relative")
        _chart = mo.ui.plotly(
            layout(
                _fig,
                f"{country}: {pathway_run.value} evaluated with {pathway_evaluator.value}",
                height=480,
            )
        )
    mo.vstack(
        [
            mo.md(
                "### Pathway by sector\n\n"
                "Claims are spread over the consuming sectors. *Fuel supply* holds green "
                "fuel producers (creation credits under `technology`, CCU charges and the "
                "stored CO2 of bio-CC plants under `carrier`) and oil refining."
            ),
            mo.hstack([pathway_run, pathway_evaluator], justify="start", gap=2),
            _chart,
        ]
    )
    return


@app.cell
def _(ca, country, cp, frame, mo, totals):
    _reference = cp.reference_run(totals)
    _sectors = (
        frame.xs((country, "technology"), level=["country", "evaluator"])
        .emissions.groupby(["scenario", "horizon", "sector"])
        .sum()
    )
    _table = ca.difference_table(_sectors, _reference, cutoff=1e-3)
    mo.vstack(
        [
            mo.md(
                f"### Physical pathway by sector against {_reference}\n\n"
                f"{country} under the technology yardstick, Δ against the technology run "
                "(Mt CO2). Positive Δ: the run emits more in that sector."
            ),
            mo.ui.table(_table.round(2).reset_index(), selection=None),
        ]
    )
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
