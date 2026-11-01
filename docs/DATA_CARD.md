# Data card: Tarbela and Mangla daily reservoir history

A daily record of water level, inflow, outflow and live storage at Pakistan's
two largest reservoirs, rebuilt from news reports of the official WAPDA/IRSA
daily release, from January 2015 to today, and extended every morning.

| | |
| --- | --- |
| **Maintainer** | Arooj Zehra ([GitHub](https://github.com/arooj-zehra/pakistan-water-watch)) |
| **Coverage** | 2015-01-23 to today (updated daily); dense from 2020 |
| **Size** | ~4,500 dam-days across two dams (one row per dam per reported day) |
| **Get it** | CSV from the [Data page](https://pakistan-water-watch.vercel.app/data); JSON in [`web/public/data/history.json`](../web/public/data/history.json); the API's `/api/export.csv` when run locally |
| **Status** | Live; corrections recorded in [`data/review/decisions.csv`](../data/review/decisions.csv) |
| **Code licence** | MIT. The figures themselves are WAPDA/IRSA's; see [Terms and credit](#terms-and-credit) |

## What a row means

| Field | Unit | Meaning |
| --- | --- | --- |
| `date` | – | The day the figures describe. Flows are means over the 24 hours to 06:00 PKT on this date; the level is the reading at 06:00 PKT. |
| `station` | – | `tarbela` (Indus) or `mangla` (Jhelum) |
| `level_ft` | feet above mean sea level | Lake level |
| `inflow_cusecs` | cubic feet per second | Mean inflow into the reservoir. This is close to the natural river flow, since there is little regulation upstream |
| `outflow_cusecs` | cubic feet per second | Mean release, an operator decision |
| `live_storage_maf` | million acre-feet | Usable storage above dead level, when the story reports it |
| `source` | – | The outlet whose story supplied the figures (see below), or `review` for a value added by a reviewer |

Missing values are empty, never zero or interpolated. The models fill gaps of
3 days or less linearly for their *inputs* only; targets and scores always use
reported values.

## Where the figures come from

The PMD Flood Forecasting Division's monthly sheets were meant to be the main
source. Probing every month from January 2010 found only four sheets, all from
September to December 2015, and the Internet Archive held no more. The history
was therefore rebuilt from the daily IRSA/WAPDA figures that news agencies
republish. Eighteen candidate sources were probed; these contributed:

| Source | Dam-days | First | Last | How it's collected |
| --- | --- | --- | --- | --- |
| Associated Press of Pakistan (APP) | 1,685 | 2017-07-22 | today | WordPress REST API |
| UrduPoint | 1,609 | 2016-07-22 | today | XML sitemaps + article pages |
| Pakistan Observer | 783 | 2019-07-29 | 2026-08 | WordPress REST API |
| Customs Today | 210 | 2015-01-23 | 2018-07 | WordPress REST API |
| PMD Flood Forecasting Division | 182 | 2015-09-01 | 2015-12-02 | Monthly HTML sheets |
| Daily Times | 9 | 2017-01-30 | 2022-08 | XML sitemaps |
| Express Tribune | 8 | 2018-03-23 | 2025-08 | XML sitemaps |
| Energy Update | 3 | 2021-09-07 | 2025-04 | WordPress REST API |
| Daily Ausaf | 1 | 2025-05-17 | 2025-05-17 | WordPress REST API |
| Profit (Pakistan Today) | 1 | 2018-08-21 | 2018-08-21 | XML sitemaps |

Counts are per dam-day chosen for the history (the best story for each day);
the figures on the [Data page](https://pakistan-water-watch.vercel.app/data)
are always current. Every figure is traceable to its article URL in
[`data/inventory/news_parsed.csv`](../data/inventory/news_parsed.csv), and raw
pages are committed under `data/raw/` so everything can be re-parsed.

**Weather** used by the forecasts comes from Open-Meteo: ERA5 reanalysis
(`models=era5`, pinned because the default blend shifts by several °C around
2017) at ten points, five in the upper Indus (Gilgit, Skardu, Astore, Hunza,
Leh) and five in the upper Jhelum (Srinagar, Pahalgam, Muzaffarabad, Naran,
Kel), plus the forecast API for the days ERA5 hasn't reached yet.

## How it was built

1. **Collect.** WordPress sites expose every post through `/wp-json/wp/v2/posts`
   (with a `?rest_route=` fallback); other sites were found through their XML
   sitemaps, keeping articles whose titles match whole words such as "Tarbela",
   "IRSA" and "inflow". Requests are 2 seconds apart, carry a User-Agent naming
   the project, and respect `robots.txt`.
2. **Parse.** A windowed parser reads the text around each dam's name for the
   level, inflow, outflow and storage, handling paired ("inflows and outflows
   28,800 and 60,000 cusecs respectively"), split ("224, 700") and reversed
   phrasing. Malformed numbers such as `4,8461` are rejected rather than
   guessed.
3. **Date.** A story's date comes from its dateline or title, or from the
   weekday it names for next-day reprints (at most 3 days back). Dating
   Pakistan Observer by publication time alone had put 295 days at odds with
   other outlets; with the weekday rule 402 of 408 shared days match exactly.
4. **Combine.** One figure per dam per day: the best story wins, with APP
   preferred. Disagreements over 1% are logged in
   [`data/inventory/news_conflicts.csv`](../data/inventory/news_conflicts.csv)
   (73 dam-days, most under 5%: different reporting times or rounding).
5. **Validate.** Rows failing a plausibility rule go to a `quarantine` table
   and never reach the history: flows outside physical limits (an inflow floor
   of 5,000 cusecs for Tarbela and 500 for Mangla catches dropped digits), a
   level jump too large for one day, or a level below dead level (flagged
   rather than rejected, since it does happen in spring). 11 stories have been
   quarantined.
6. **Dead levels.** Tarbela's dead level has risen with sedimentation. Rather
   than hard-coding it, the pipeline reads it from stories that state it,
   accepting a new value only if it is higher than the last:

   | Dam | Dead level | From |
   | --- | --- | --- |
   | Tarbela | 1,380 ft | 2015-01-23 |
   | Tarbela | 1,386 ft | 2018-07-11 |
   | Tarbela | 1,392 ft | 2021-08-03 |
   | Tarbela | 1,398 ft | 2022-08-04 |
   | Tarbela | 1,402 ft | 2023-08-10 |
   | Mangla | 1,040 ft | 2015-01-23 |
   | Mangla | 1,050 ft | 2019-07-18 |

   These are the first dates each value appears in the stories, not official
   change dates.
7. **Check and review.** Four anomaly checks (trend-aware local z-score,
   seasonal STL residuals, Isolation Forest, water balance) queue suspicious
   values; a reviewer marks each wrong, right, or corrected. Decisions live in
   [`data/review/decisions.csv`](../data/review/decisions.csv) and are applied
   whenever the data is read, so the original values stay in the database and
   every correction is visible and reversible. How well the checks work is in
   the [anomaly report](ANOMALY_REPORT.md).
8. **Extend daily.** The GitHub Actions job repeats steps 1 to 7 for the last
   few days each morning at 10:00 PKT and opens an issue if a parser breaks or
   the newest figures are more than 3 days old.

## Coverage

Share of days with a figure, both dams, after filling gaps of 3 days or less:

| Year | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026* |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Reported | 26% | 4% | 41% | 45% | 31% | 64% | 74% | 47% | 84% | 72% | 56% | 64% |
| Gaps ≤3 days filled | 26% | 4% | 61% | 66% | 49% | 79% | 92% | 57% | 96% | 75% | 75% | 90% |

\*2026 to date. A month-by-month heatmap is in the
[data inventory](data_inventory.md).

**Known gaps:** January–March 2024, February 2025 and October–December 2025
for both dams; February–July 2022 for Tarbela; sparse coverage before 2020.

**The gate.** The original plan required five complete years with under 10%
missing for both dams. Only 2021 and 2023 meet that, so before any modelling
the gate was revised and recorded: at least two years under 10% missing to
test on, and at least five years with 75% or more present to train on (2020,
2021, 2023, 2024 and 2025). `python -m pipeline.coverage_report` checks both.

## Known biases and caveats

- **Second-hand figures.** These are WAPDA's numbers as retyped by journalists.
  Typing errors are the main risk; the checks catch most large ones (76% of
  planted errors big enough to matter) but not small ones.
- **Uneven over time.** Early years come mostly from one or two outlets with
  their own habits (such as rounding), so 2015–2019 are both thinner and
  noisier than recent years.
- **Missing is not random.** Several of the longest gaps fall in the low-flow
  months (January–March 2024, February 2025, October–December 2025), so winter
  is somewhat under-represented.
- **Outflow and storage are reported less often than inflow and level.**
- **Rounded levels.** Some stories round the level to the nearest foot or report
  the maximum (e.g. 1,550 ft) loosely; the water-balance check flags the worst.
- **Reviewer judgement.** Corrections are one person's decisions, recorded with
  the original value so anyone can disagree.

## Intended use

Suitable for: understanding seasonal and year-to-year patterns, training and
testing forecasting models (with the gaps in mind), journalism and teaching
about the Indus basin, and checking claims about reservoir levels on past dates.

Not suitable for: operational water management or flood warning (use WAPDA,
IRSA and PMD directly), or any purpose that needs certified measurements.

## Terms and credit

The reservoir figures are published by WAPDA and IRSA and reported by the
outlets above; credit them when using the data. The dataset itself is the
numbers, each with a link to its source article. Article text is cached under
`data/raw/` only so the figures can be re-parsed when the parser improves. Weather data © Open-Meteo.com, CC BY 4.0, from Copernicus ERA5.
The code that builds the dataset is MIT-licensed.

To report an error, open an issue on GitHub with the date, dam and source link.
