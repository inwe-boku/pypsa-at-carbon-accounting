# SPDX-FileCopyrightText: 2023-2026 Austrian Gas Grid Management AG
#
# SPDX-License-Identifier: MIT
# For license information, see the LICENSE.txt file in the project root.
"""
Publication figure (German) of the national balance bridge between two runs.

Takes the waterfall of section 2 of ``evals/notebooks/carbon_accounting.py``
and renders it for one horizon and one pair of scenarios, without the split
into physical and accounting steps. The claims on oil, kerosene and methanol
are merged into one step. Run it from the repository root with

    pixi run python evals/scripts/carbon_accounting_waterfall.py
"""

import argparse
import sys
import textwrap
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import pypsa

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from evals import carbon_accounting as ca  # noqa: E402

TITLE = "Unterschied technologiebasierter Ansatz → energieträgerbasierter Ansatz"
TOTAL_LABELS = (
    "Nationales Budget<br>technologiebasiert",
    "Nationales Budget<br>energieträgerbasiert",
)
FUEL_CLAIMS = "Claims Fuels"
MERGED_CLAIMS = ("Claims oil", "Claims kerosene", "Claims methanol")
# bridge category -> (axis label, explanation)
STEPS: dict[str, tuple[str, str]] = {
    ca.FUEL_USE: (
        "Einsatz fossiler<br>Energieträger",
        "weniger Einsatz fossiler Energieträger → weniger Emissionen",
    ),
    ca.PROCESS_AND_WASTE: ("Prozess- und<br>Abfallemissionen", ""),
    ca.CAPTURE_CREDIT: (
        "Carbon Capture",
        "weniger Carbon Capture in AT → mehr Emissionen",
    ),
    ca.CREATION_CREDIT: (
        "Gutschrift<br>Herstellung",
        "Synthetische Fuels/Gase haben keine negativen Emissionen in der "
        "Herstellung → mehr Emissionen",
    ),
    ca.CCU_CHARGE: ("CCU", "Unterschied in CCU"),
    "Claims gas": (
        "Claims Gas",
        "Synthetisches Gas wird inkl. der negativen Emissionen importiert und "
        "ergibt damit bei der Verbrennung in AT netto 0 Emissionen",
    ),
    FUEL_CLAIMS: (
        "Claims Fuels<br>(Öl, Kerosin, Methanol)",
        "Synthetische Fuels werden inkl. der negativen Emissionen importiert und "
        "ergeben so bei Verbrennung netto 0 Emissionen",
    ),
}
COLOURS = {"total": "#8a8984", "up": "#e34948", "down": "#1baf7a"}
FONT = "Arial, Helvetica, sans-serif"
WRAP = 24  # characters per line of an explanation


def bridge(root: Path, horizon: str, scenarios: tuple[str, str], country: str):
    """Return the balance bridge of two scenarios, claims on fuels merged."""
    files = ca.find_scenarios(root)
    networks = {s: {horizon: pypsa.Network(files[s][horizon])} for s in scenarios}
    frame = ca.balance_bridge(networks, country)
    frame.insert(
        frame.columns.get_loc(MERGED_CLAIMS[0]),
        FUEL_CLAIMS,
        frame[list(MERGED_CLAIMS)].sum(axis=1),
    )
    return frame.drop(columns=list(MERGED_CLAIMS))


def steps(frame: pd.DataFrame, horizon: str, a: str, b: str, cutoff: float):
    """Return the waterfall from ``a`` to ``b`` (see ``ca.waterfall``)."""
    start, end = frame.loc[(a, horizon)], frame.loc[(b, horizon)]
    rows = [{"category": "start", "value": start["Total"]}]
    for category in STEPS:
        delta = end[category] - start[category]
        if abs(delta) > cutoff:
            rows.append({"category": category, "value": delta})
    rows.append({"category": "end", "value": end["Total"]})
    out = pd.DataFrame(rows)
    is_total = out.category.isin(["start", "end"])
    level = out.value.where(~is_total, 0.0).cumsum() + start["Total"]
    out["end"] = level.where(~is_total, out.value)
    out["base"] = (out.end - out.value).where(~is_total, 0.0)
    labels = {"start": TOTAL_LABELS[0], "end": TOTAL_LABELS[1]}
    out["label"] = [labels.get(c) or STEPS[c][0] for c in out.category]
    return out


def number(value: float, sign: bool = True) -> str:
    """Format a number with a German decimal comma, without a negative zero."""
    value = round(value, 1) + 0.0
    return f"{value:{'+' if sign else ''}.1f}".replace(".", ",").replace("-", "−")


def figure(out: pd.DataFrame, subtitle: str) -> go.Figure:
    """Return the publication waterfall."""
    fig = go.Figure()
    is_total = out.category.isin(["start", "end"])
    rising = out.value > 0
    span = out[["base", "end"]].abs().max().max()

    for key, name, mask in (
        ("up", "Anstieg der Emissionen", ~is_total & rising),
        ("down", "Rückgang der Emissionen", ~is_total & ~rising),
    ):
        part = out[mask]
        fig.add_trace(
            go.Bar(
                x=part.label,
                y=part.end - part.base,
                base=part.base,
                name=name,
                marker=dict(color=COLOURS[key], line=dict(width=0)),
                width=0.55,
                hovertemplate="%{x}<br>%{y:+.2f} Mt CO₂<extra></extra>",
            )
        )
    # totals: a bar if it has a height, always a bold line at its value
    totals = out[is_total]
    fig.add_trace(
        go.Bar(
            x=totals.label,
            y=totals.value,
            name="Nationales Budget",
            marker=dict(color=COLOURS["total"], line=dict(width=0)),
            width=0.55,
            showlegend=False,
            hovertemplate="%{x}<br>%{y:.2f} Mt CO₂<extra></extra>",
        )
    )
    for _, row in totals.iterrows():
        fig.add_shape(
            type="line",
            xref="x",
            yref="y",
            x0=out.index[out.label == row.label][0] - 0.3,
            x1=out.index[out.label == row.label][0] + 0.3,
            y0=row.value,
            y1=row.value,
            line=dict(color="#0b0b0b", width=3),
        )
        fig.add_annotation(
            x=row.label,
            y=max(row.value, 0),
            text=f"<b>{number(row.value, sign=False)} Mt</b>",
            showarrow=False,
            yshift=14,
            font=dict(size=14),
        )

    # connectors between consecutive bars
    for i in range(len(out) - 1):
        fig.add_shape(
            type="line",
            xref="x",
            yref="y",
            x0=i + 0.275,
            x1=i + 1 - 0.275,
            y0=out.end[i],
            y1=out.end[i],
            line=dict(color="#8a8984", width=1, dash="dot"),
        )

    # arrows beside the steps and the size of each step
    for i, row in out[~is_total].iterrows():
        colour = COLOURS["up" if row.value > 0 else "down"]
        fig.add_annotation(
            x=i - 0.36,
            y=row.end,
            ax=i - 0.36,
            ay=row.base,
            xref="x",
            yref="y",
            axref="x",
            ayref="y",
            showarrow=True,
            arrowhead=2,
            arrowsize=1.2,
            arrowwidth=2.5,
            arrowcolor=colour,
            text="",
        )
        # fig.add_annotation(
        #     x=i,
        #     y=max(row.base, row.end),
        #     text=f"<b>{number(row.value)}</b>",
        #     showarrow=False,
        #     yshift=12,
        #     font=dict(size=14, color=colour),
        # )

    # explanations below the axis labels
    for i, row in out[~is_total].iterrows():
        text = STEPS[row.category][1]
        if text:
            fig.add_annotation(
                x=i,
                y=0,
                xref="x",
                yref="paper",
                yshift=-62,
                yanchor="top",
                text="<br>".join(textwrap.wrap(text, WRAP)),
                showarrow=False,
                align="center",
                font=dict(size=12, color="#4a4a46"),
            )

    fig.add_hline(y=0, line=dict(color="#0b0b0b", width=1))
    fig.update_layout(
        title=dict(
            text=f"<b>{TITLE}</b>",
            subtitle=dict(text=subtitle, font=dict(size=14, color="#4a4a46")),
            x=0.01,
            xanchor="left",
            font=dict(size=20),
        ),
        template="plotly_white",
        font=dict(family=FONT, size=13, color="#0b0b0b"),
        separators=",.",
        barmode="overlay",
        width=1300,
        height=760,
        margin=dict(t=110, b=230, l=80, r=30),
        legend=dict(orientation="h", x=1, xanchor="right", y=1.02, yanchor="bottom"),
        hovermode="closest",
    )
    fig.update_xaxes(
        categoryorder="array",
        categoryarray=list(out.label),
        tickfont=dict(size=13),
        showgrid=False,
    )
    fig.update_yaxes(
        title_text="Mt CO₂",
        range=[-0.12 * span, 1.15 * span],
        gridcolor="#e6e5df",
        zeroline=False,
    )
    return fig


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--root", default=REPO / "results" / "carbon-accounting")
    parser.add_argument("--horizon", default="2040")
    parser.add_argument("--a", default="CA_base", help="technology-based run")
    parser.add_argument("--b", default="CA_carrier_fair", help="carrier-based run")
    parser.add_argument("--country", default="AT")
    parser.add_argument("--cutoff", type=float, default=1e-3, help="Mt CO2")
    parser.add_argument("--output", default=None, help="HTML file")
    args = parser.parse_args()

    root = Path(args.root)
    frame = bridge(root, args.horizon, (args.a, args.b), args.country)
    out = steps(frame, args.horizon, args.a, args.b, args.cutoff)
    subtitle = f"Nationale CO₂-Bilanz {args.country} {args.horizon} in Mt CO₂ "
    fig = figure(out, subtitle)
    output = Path(
        args.output
        or root / f"waterfall_{args.country}_{args.horizon}_{args.a}_{args.b}.html"
    )
    fig.write_html(output, include_plotlyjs="cdn")
    print(f"Saved {output}")


if __name__ == "__main__":
    main()
