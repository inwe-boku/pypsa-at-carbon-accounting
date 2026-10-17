# Carbon Dioxide Emission Tracking and Constraints in PyPSA-AT

PyPSA-AT implements multiple layers of CO2 tracking and constraints to model emission reduction pathways and climate policy targets. This document explains how emissions are tracked and how different constraints control the energy system's carbon footprint.

## CO2 Emission Accounting Architecture

PyPSA-AT uses two main types of CO2 constraints, each serving different modeling purposes:

### 1. National CO2 Budgets

National CO2 budgets allow **country-specific emission limits**, enabling differentiated emission reduction pathways for each country to account for country-specific climate targets (e.g., Austria's path to climate neutrality by 2040).
This feature was added in PyPSA-AT and consists of a reduced version of the PyPSA-DE function. 

```yaml
solving:
  constraints:
      co2_budget_national:
        AT:
          2020: 1.00  # 100% of 1990 levels
          2030: 0.64  # 64% of 1990 levels
          2040: 0.12  # 12% of 1990 levels
          2050: 0.00  # Net-zero
```

#### Regional Emission Tracking

The rules below describe the default, technology-based carbon accounting
(`mods.carbon_accounting.method: technology`). The carrier-based alternative is
described in [Carbon accounting methods](#carbon-accounting-methods).

- Emissions from fuels are balanced in the region where a technology causes the emission
- E-Fuels vs. Fossil Fuels: Whether E-Fuels (biomethane, synthetic oil, green methanol, etc.) or fossil fuels are burned does not make a difference in emission accounting. All emissions are booked where combustion occurs.
- Negative emissions from Carbon Capture technologies, Direct Air Capture (DAC), or CCU (e.g., bio-methanol production) are accounted for in the respective technology region
- ETS trade (Emission Trading System) is NOT explicitly modeled
- A constraint ensures that total emissions from all regions in a country are below a respective threshold (the national CO2 budget) 

#### Implications

- One country may not produce E-Fuels to compensate for emissions in another country
- In contrast, it is possible for a region to compensate for emissions in another region of the same country, because the constraint works on a per-country level. 
- It is assumed that all certificates resulting from E-Fuel generation are traded within the same country (not region!)
- CO2 deductions from CO2 buses by CCU technologies always count as negative emissions in the country where they occur
- It is possible for a country to transform imported CO2 into E-Fuels (e.g. synthetic oil) and export them to create a negative balance


**How it works**

1. Identifies all links connecting to `co2 atmosphere` bus for a given country
2. Accounts for emissions across all bus ports (bus0, bus1, bus2, etc.) for those links
3. Scales aviation emissions by domestic-to-total ratio calculated from respective model values to exclude international aviation
4. Creates a country-specific constraint: `Σ(emissions) ≤ country_budget`

**Mathematical formulation**

For each country $c$ and planning horizon $y$:

$$
E_c + \alpha_c \cdot E_c^{\text{avi}} \leq B_{c,y}
$$

<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem;" markdown>
<div markdown>

**Left-hand side — emissions**

| Symbol | Description | Unit |
|---|---|---|
| $E_c$ | Net sectoral CO₂ emissions in country $c$ — positive for emitters, negative for carbon removal (CCS, DAC, CCU) | tCO₂/yr |
| $E_c^{\text{avi}}$ | Gross aviation emissions in country $c$ | tCO₂/yr |
| $\alpha_c$ | Domestic aviation share $= E_{\text{avi},c}^{\text{dom}} / E_{\text{avi},c}^{\text{total}}$ | — |

</div>
<div markdown>

**Right-hand side — budget**

| Symbol | Description | Unit |
|---|---|---|
| $B_{c,y}$ | National CO₂ budget $= E_c^{1990} \cdot r_{c,y}$ | tCO₂/yr |
| $r_{c,y}$ | Reduction target from config (e.g. 0.64 = 64 % of 1990 levels) | — |

</div>
</div>

✅ **Included**

- All technology emissions in the country (combustion, industrial processes)
- Negative emissions from CCU, CCS, and DAC in the country
- Emissions from domestic aviation
- All energy sectors: electricity, heating, transport, industry
- All emissions from navigation (domestic and international)

❌ **Excluded**

- International aviation (also the green kerosene claims under the carrier-based method
  are scaled by $\alpha_c$)

**Key differences from PyPSA-DE**

- Simpler emission tracking: does not distinguish E-Fuels and fossil fuels

### Carbon accounting methods

The configuration key `mods.carbon_accounting.method` decides *where* a green fuel
counts as green. The choice applies to the whole model: the national CO2 budgets, the
EU-wide CO2 limit and the CO2 balances in the evaluations.

```yaml
mods:
  carbon_accounting:
    enable: true
    method: technology   # technology | carrier
    claim_cost: 0.01     # EUR/t CO2, carrier method only
```

| | `technology` (default) | `carrier` |
|---|---|---|
| Producing biomethane, bio-oil or e-fuels | negative emissions in the producing country | zero |
| Burning a green fuel | emissions in the burning country, like fossil fuel | zero, if the country claims the green fuel |
| Burning a fossil fuel | emissions in the burning country | emissions in the burning country |
| Green fuel imports from outside Europe | negative emissions at the entry point (oil and methanol: no country) | zero in the country that claims them |
| CO2 captured and stored (CCS, DAC) | negative emissions where the CO2 is captured | the capturing country claims the stored CO2 |
| CO2 captured and turned into e-fuels (CCU) | negative emissions at the capture plant | no credit; the e-fuel itself counts as green |

**How the carrier method works.** The gas, oil and methanol networks stay as they are:
green and fossil molecules still mix in the same pipelines and markets. In parallel, every
MWh of green fuel creates a *green certificate* (an attribute). A country can turn
certificates into avoided emissions by *claiming* them, at the emission factor of the fuel
(gas 0.198, oil 0.2571, methanol 0.248 t CO2/MWh). Three rules keep the bookkeeping honest:

1. A country can claim at most as much green fuel as it consumes in the year. Green
   kerosene is claimed against kerosene for aviation only, and it is scaled with the
   domestic aviation share, like kerosene itself.
2. Green gas certificates only travel along the gas pipelines: from one country to the
   next, at most as much as gas actually flowed across that border in the year.
   Certificates for oil and methanol, and for stored CO2, are traded freely across Europe.
3. Every certificate is claimed by the end of the year. The EU-wide CO2 balance is
   therefore exactly the same as under the technology method; only the split between
   countries changes.

Stored CO2 is credited to the countries that capture CO2 (point sources, DAC, biogenic
capture), each at most its own gross capture.

**Who claims what.** Within these limits, the model decides which country claims a
certificate. Countries with a national CO2 budget (Austria, Germany) claim certificates
only when their budget needs them: their claims carry a tiny cost (`claim_cost`) that
breaks the tie in favour of the other countries. Without it, the split between countries
would be arbitrary whenever no national budget binds. The evaluation
*Carbon Accounting Claims* shows the claimed credits per country next to the credit it
would receive at the EU-average green share of each fuel, so that this effect is visible.

**Example.** Austria upgrades 1 TWh of biogas, which is burned in German gas boilers.
Under `technology`, Austria books −0.198 Mt and Germany +0.198 Mt. Under `carrier`,
Austria books 0 and Germany books 0.198 Mt for burning the gas and −0.198 Mt for claiming
its green certificates, so 0 in total. Germany can only make that claim because the gas
physically flowed from Austria to Germany.

See [Carbon Accounting Methods](carbon-accounting-spec.md) for the full specification.

### 2. Global CO2 Limit (PyPSA-EUR default)

The `CO2Limit` constraint caps total system-wide emissions based on historical baseline years (typically 1990) to account for European emission reduction targets (e.g., "reduce emissions to 55% of 1990 levels by 2030").
This is the standard PyPSA-EUR emission tracking controlled in the `config.yaml`:

```yaml
electricity:
  co2limit_enable: true  # Enable/disable the constraint
  co2limit: 7.75e+7      # Absolute limit in tonnes CO2 (if used)
```
