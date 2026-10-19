# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""
Carbon accounting comparison notebook (marimo).

Compares any number of runs of the carbon accounting configuration for one
country. All computations live in ``evals/carbon_accounting.py``; this file
only does UI, caching and plotting. Run it from the repository root with

    pixi run marimo edit evals/notebooks/carbon_accounting.py

Specification: ``docs-at/explanations/carbon-accounting-eval-spec.md``.
"""

import marimo

__generated_with = "0.25.0"
app = marimo.App(width="full", app_title="Carbon accounting comparison")


@app.cell
def _():
    import sys
    from pathlib import Path

    import marimo as mo
    import pandas as pd
    import plotly.express as px
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    REPO = Path(__file__).resolve().parents[2]
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    from evals import carbon_accounting as ca

    return REPO, ca, go, make_subplots, mo, pd, px


@app.cell
def _(go, mo):
    # categorical slots in fixed order; a scenario keeps its colour across selections
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
    GROUP_COLOURS = {"total": "#8a8984", "physical": "#2a78d6", "accounting": "#eb6834"}
    ICONS = {"ok": "✓", "warn": "⚠", "fail": "✗"}
    KINDS = {"ok": "success", "warn": "warn", "fail": "danger"}

    def badge(check) -> mo.Html:
        """Return a callout with the status icon, name and message of a check."""
        text = mo.md(f"**{ICONS[check.status]} {check.name}** — {check.message}")
        return mo.callout(text, kind=KINDS[check.status])

    def step_status(checks) -> str:
        """Return the worst status of several checks."""
        statuses = {c.status for c in checks}
        return next((s for s in ("fail", "warn") if s in statuses), "ok")

    def add_waterfall(fig, steps, row=1, col=1, legend=True):
        """Add the bars of a waterfall (see ``ca.waterfall``) to a figure."""
        for group, colour in GROUP_COLOURS.items():
            part = steps[steps.group == group]
            fig.add_trace(
                go.Bar(
                    x=part.step,
                    y=part.end - part.base,
                    base=part.base,
                    name=group,
                    legendgroup=group,
                    showlegend=legend,
                    marker=dict(color=colour, line=dict(width=0)),
                    customdata=part.value,
                    hovertemplate="%{x}<br>%{customdata:+.2f} Mt<extra></extra>",
                ),
                row=row,
                col=col,
            )
        fig.add_hline(
            y=steps.attrs["budget"],
            line=dict(color="#0b0b0b", dash="dash", width=1),
            annotation_text="budget",
            annotation_position="top left",
            row=row,
            col=col,
        )
        fig.update_xaxes(
            categoryorder="array", categoryarray=list(steps.step), row=row, col=col
        )

    def layout(fig, title: str, yaxis: str):
        fig.update_layout(
            title=title,
            template="plotly_white",
            bargap=0.3,
            legend=dict(orientation="h", y=-0.25),
            hovermode="closest",
            margin=dict(t=60, b=40),
        )
        fig.update_yaxes(title_text=yaxis, zeroline=True, zerolinecolor="#c3c2b7")
        return fig

    return PALETTE, add_waterfall, badge, layout, step_status


@app.cell
def _(mo):
    mo.md(r"""
    # Carbon accounting comparison

    Compares runs of the carbon accounting configuration for one country. Every Δ is
    measured against the **reference** scenario. Steps that fail a check stop the
    steps after them. Specification: `docs-at/explanations/carbon-accounting-eval-spec.md`.
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
    countries = sorted({c for b in info.budgets for c in b.split(", ") if c} | {"AT"})
    country_select = mo.ui.dropdown(options=countries, value="AT", label="Country")
    return colours, country_select, files, info, scenario_select


@app.cell
def _(ca, info, mo, scenario_select):
    _selected = list(scenario_select.value) or list(info.index.unique("scenario"))
    _default = ca.default_reference(info.loc[_selected])
    reference_select = mo.ui.dropdown(
        options=_selected, value=_default, label="Reference"
    )
    return (reference_select,)


@app.cell
def _(badge, ca, country_select, info, mo, reference_select, scenario_select):
    setup = ca.check_setup(info, list(scenario_select.value), country_select.value)
    mo.output.replace(
        mo.vstack(
            [
                mo.md("## 0 Setup"),
                mo.hstack(
                    [scenario_select, reference_select, country_select],
                    justify="start",
                    gap=2,
                ),
                badge(setup),
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
    reference = reference_select.value
    country = country_select.value
    return country, reference, selected


@app.cell
def _(ca, mo):
    @mo.cache
    def load(path: str) -> dict:
        """Load the solved networks of one scenario, once."""
        return ca.load_networks(path)

    @mo.cache
    def scenario_frames(path: str, scenario: str, country: str) -> dict:
        """Compute every statistic of one scenario, once per country."""
        networks = {scenario: load(path)}
        return {
            "bridge": ca.balance_bridge(networks, country),
            "consistency": ca.consistency(networks, country),
            "target": ca.target_cost(networks, country),
            "physical": ca.physical_response(networks, country),
        }

    return (scenario_frames,)


@app.cell
def _(country, files, mo, pd, scenario_frames, selected):
    _frames = {}
    for _scenario in mo.status.progress_bar(
        selected, title="Loading and evaluating scenarios", remove_on_exit=True
    ):
        _path = str(files[_scenario][next(iter(files[_scenario]))].parents[1])
        _frames[_scenario] = scenario_frames(_path, _scenario, country)
    bridge = pd.concat([f["bridge"] for f in _frames.values()])
    consistency = pd.concat([f["consistency"] for f in _frames.values()])
    target = pd.concat([f["target"] for f in _frames.values()])
    physical = pd.concat([f["physical"] for f in _frames.values()])
    horizons = sorted(bridge.index.unique("horizon"))
    return bridge, consistency, horizons, physical, target


@app.cell
def _(badge, ca, consistency, mo, step_status):
    checks = ca.consistency_checks(consistency)
    _status = step_status(checks)
    mo.output.replace(
        mo.vstack(
            [
                mo.md(f"## 1 Consistency {dict(ok='✓', warn='⚠', fail='✗')[_status]}"),
                *[badge(c) for c in checks],
                mo.accordion(
                    {
                        c.name: mo.ui.table(
                            c.details.reset_index(), selection=None, show_download=False
                        )
                        for c in checks
                        if not c.details.empty
                    }
                ),
            ]
        )
    )
    mo.stop(_status == "fail")
    consistent = True
    return (consistent,)


@app.cell
def _(consistent, horizons, mo, reference, selected):
    assert consistent
    horizon_select = mo.ui.dropdown(
        options=horizons, value=horizons[-1], label="Horizon"
    )
    _others = [s for s in selected if s != reference]
    a_select = mo.ui.dropdown(options=selected, value=reference, label="Scenario A")
    b_select = mo.ui.dropdown(options=selected, value=_others[0], label="Scenario B")
    return a_select, b_select, horizon_select


@app.cell
def _(
    a_select,
    add_waterfall,
    b_select,
    bridge,
    ca,
    country,
    horizon_select,
    layout,
    make_subplots,
    mo,
    reference,
    selected,
):
    _horizon = horizon_select.value
    _steps = ca.waterfall(bridge, _horizon, a_select.value, b_select.value, cutoff=1e-3)
    _main = make_subplots(rows=1, cols=1)
    add_waterfall(_main, _steps)
    layout(
        _main,
        f"{country} {_horizon}: {a_select.value} → {b_select.value}",
        "Mt CO2",
    )
    _main.add_vrect(
        x0=0.5,
        x1=0.5 + (_steps.group == "physical").sum(),
        fillcolor="#2a78d6",
        opacity=0.05,
        line_width=0,
        annotation_text="physical",
        annotation_position="top left",
    )

    _others = [s for s in selected if s != reference]
    _multiples = make_subplots(
        rows=1, cols=len(_others), subplot_titles=_others, shared_yaxes=True
    )
    for _i, _other in enumerate(_others, start=1):
        add_waterfall(
            _multiples,
            ca.waterfall(bridge, _horizon, reference, _other, cutoff=1e-3),
            col=_i,
            legend=_i == 1,
        )
    layout(_multiples, f"Every scenario against {reference}, {_horizon}", "Mt CO2")
    _multiples.update_layout(height=420)

    _table = bridge.xs(_horizon, level="horizon").T
    mo.vstack(
        [
            mo.md(
                "## 2 National balance bridge\n\n"
                "Physical steps (blue) change because the system changed; accounting "
                "steps (orange) change because the method changed. Kerosene and kerosene "
                "claims are scaled by the domestic aviation share, as in the national budget."
            ),
            mo.hstack([horizon_select, a_select, b_select], justify="start", gap=2),
            mo.ui.plotly(_main),
            mo.ui.plotly(_multiples),
            mo.accordion(
                {
                    "Categories (Mt CO2)": mo.ui.table(
                        _table.round(3).reset_index(names="category"), selection=None
                    )
                }
            ),
        ]
    )
    return


@app.cell
def _(ca, colours, consistent, go, layout, mo, reference, target):
    assert consistent
    cost = ca.add_cost_deltas(target, reference)

    _prices = go.Figure()
    for _scenario, _rows in cost.groupby(level="scenario", sort=False):
        _x = _rows.index.get_level_values("horizon")
        _prices.add_trace(
            go.Scatter(
                x=_x,
                y=_rows["national CO2 price"],
                name=_scenario,
                mode="lines+markers",
                line=dict(color=colours[_scenario], width=2),
                marker=dict(size=8),
                customdata=_rows.regime,
                hovertemplate="%{x}: %{y:.3f} €/t (%{customdata})<extra>"
                + _scenario
                + "</extra>",
            )
        )
    layout(
        _prices,
        "National CO2 price μ (an abatement cost only for physical and pool-scarce)",
        "€/t",
    )

    def _delta_bars(column: str, title: str):
        fig = go.Figure()
        for scenario, rows in cost.groupby(level="scenario", sort=False):
            if scenario == reference:
                continue
            fig.add_trace(
                go.Bar(
                    x=rows.index.get_level_values("horizon"),
                    y=rows[column],
                    name=scenario,
                    marker=dict(color=colours[scenario], line=dict(width=0)),
                    hovertemplate="%{x}: %{y:+.3f} bn €<extra>" + scenario + "</extra>",
                )
            )
        return layout(fig, title, "bn €")

    _columns = [
        "regime",
        "binding",
        "national CO2 price",
        "claim cost",
        "EU CO2 price",
        "credit-financed share",
        "pre-claim emissions",
        "claims",
        "budget",
        "system cost",
        "Δ system cost",
        "cost in country",
        "Δ cost in country",
        "capex in country",
        "opex in country",
        "trade balance",
    ]
    mo.vstack(
        [
            mo.md(
                "## 3 Cost of the national target\n\n"
                "The regime says how the country meets its budget: `slack` (under budget), "
                "`physical` (no more credits, μ is the marginal abatement cost), "
                "`pool-scarce` (budget countries compete for a pool), `paper` (spare credits, "
                "μ ≈ claim cost). Duals of the credit bounds are not stored in the solved "
                "networks; they are estimated from μ (see `ca.credit_bounds`). Emissions in "
                "Mt CO2, cost in bn €."
            ),
            mo.ui.table(cost[_columns].round(4).reset_index(), selection=None),
            mo.ui.plotly(_prices),
            mo.hstack(
                [
                    mo.ui.plotly(
                        _delta_bars("Δ system cost", f"EU system cost Δ vs {reference}")
                    ),
                    mo.ui.plotly(
                        _delta_bars(
                            "Δ cost in country", f"Cost in country Δ vs {reference}"
                        )
                    ),
                ],
                widths="equal",
            ),
        ]
    )
    return (cost,)


@app.cell
def _(consistent, mo, physical):
    assert consistent
    group_select = mo.ui.dropdown(
        options=list(physical.index.unique("group")),
        value="capacity",
        label="Group",
    )
    return (group_select,)


@app.cell
def _(ca, colours, country, group_select, mo, physical, px, reference):
    _group = physical.xs(group_select.value, level="group")
    _table = ca.difference_table(_group, reference, cutoff=1e-3)
    _long = _group.reset_index()
    _long = _long[_long.set_index(["horizon", "item", "unit"]).index.isin(_table.index)]
    _unit = ", ".join(_long.unit.unique())
    _items = list(_table.index.unique("item"))

    def _horizon_figure(horizon: str):
        fig = px.bar(
            _long[_long.horizon == horizon],
            x="item",
            y="value",
            color="scenario",
            barmode="group",
            color_discrete_map=colours,
            category_orders={"item": _items, "scenario": list(colours)},
            template="plotly_white",
            labels={"value": _unit, "item": ""},
            title=f"{country} {horizon}: {group_select.value} ({_unit})",
        )
        fig.update_traces(marker_line_width=0)
        fig.update_layout(
            bargap=0.25, legend=dict(orientation="h", y=-0.35), height=420
        )
        return mo.ui.plotly(fig)

    _figures = [_horizon_figure(h) for h in sorted(_long.horizon.unique())]
    mo.vstack(
        [
            mo.md(
                "## 7 Physical system response\n\n"
                "Values of every scenario and Δ against the reference. Rows that are zero "
                "in every scenario are hidden. Capacities are at the input (bus0) of the link."
            ),
            group_select,
            mo.ui.table(_table.round(3).reset_index(), selection=None),
            *_figures,
        ]
    )
    return


@app.cell
def _(bridge, ca, cost, mo):
    _summary = ca.summary(bridge, cost)
    mo.vstack(
        [
            mo.md("## Summary"),
            mo.ui.table(_summary.round(3).reset_index(), selection=None),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
