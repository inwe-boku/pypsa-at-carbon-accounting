# Run the Reduced Carbon Accounting Configuration

`config/config.carbon-accounting.yaml` is a reduced PyPSA-AT setup. It is the
common base for comparing carbon accounting methods and design choices, and it
keeps every solve under 30 minutes.

## Run it

```bash
pixi run snakemake -call --configfile config/config.carbon-accounting.yaml
```

Results are written to `results/carbon-accounting/<scenario>/`.

## What differs from the default PyPSA-AT configuration

| Setting | Default (`config.at.yaml`) | Carbon accounting |
|---|---|---|
| `mods.modify_nuts3_shapes` | `AT35DE5` | `AT10DE5` |
| `clustering.administrative` | AT 3 | AT 2 |
| `solving.solver` | HiGHS (`ipx`) | Gurobi (`gurobi-default`) |
| `run.scenarios.file` | `scenarios.manual.yaml` | `scenarios.carbon-accounting.yaml` |

Everything else is inherited: all countries, the planning horizons 2025, 2030,
2040 and 2050 (myopic), `365h` temporal averaging, and all `mods:` features.

!!! note "Why not one node for Austria and Germany?"
    The PyPSA-AT custom clustering (`mods.clustering.custom`) only supports
    `AT10DE5`, `AT10DE16`, `AT35DE5` and `AT35DE16`. Several modifications
    (the AGGM gas grid, KLIEN potentials, the TYNDP node mappings) rely on it.
    `AT10DE5` is therefore the smallest supported resolution.

!!! note "Why keep 2025?"
    The base-year demand calibration (heat, transport, electricity base load)
    uses the first planning horizon and `demand.source_years`. 2025 remains the
    calibrated starting point, so 2030 inherits a realistic brownfield.

## Add an accounting variant

Add an entry to `config/scenarios.carbon-accounting.yaml` and list it under
`run.name`. Only override accounting-related keys, and leave the reductions
above untouched so that runs stay comparable:

```yaml
CA_my_variant:
  description: Short description of the accounting choice
  solving:
    constraints:
      ...
```

## Need it faster?

If a solve exceeds the time budget, reduce the `countries` list first, keeping
Austria's neighbours so that cross-border flows stay represented.
