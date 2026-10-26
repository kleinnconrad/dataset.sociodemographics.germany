# German Sociodemographics Dataset: Architecture

This document describes the structure of the pipeline: scope and decisions, the design rules for a long collection period, the datasets and their variables, how the dataset is correlated with other datasets, the prediction targets, the sources, storage and data volume, the run, failure handling, the measurement log and the implementation status. The pipeline adopts the structure of [dataset.economics.germany](https://github.com/kleinnconrad/dataset.economics.germany) and [dataset.politics.germany](https://github.com/kleinnconrad/dataset.politics.germany). Usage and configuration are described in the [README](README.md).

## Table of Contents
- [1. Scope and Decisions](#1-scope-and-decisions)
- [2. Design Rules](#2-design-rules)
- [3. Datasets and Variables](#3-datasets-and-variables)
  - [3.1 Common Fields](#31-common-fields)
  - [3.2 Statistical Series](#32-statistical-series)
  - [3.3 Regions](#33-regions)
  - [3.4 Concepts by Topic](#34-concepts-by-topic)
- [4. Correlation with Other Datasets](#4-correlation-with-other-datasets)
- [5. Prediction Targets](#5-prediction-targets)
- [6. Sources and Fetch Rules](#6-sources-and-fetch-rules)
- [7. Storage, Partitioning and Data Volume](#7-storage-partitioning-and-data-volume)
- [8. Repository Structure](#8-repository-structure)
- [9. Pipeline Run](#9-pipeline-run)
- [10. Failure Handling](#10-failure-handling)
- [11. Measurement Log](#11-measurement-log)
- [12. Implementation Status](#12-implementation-status)

---

## 1. Scope and Decisions

- **Scope:** the population of Germany and its structure: age and sex, births and fertility, mortality, migration and citizenship, households and families, education, income distribution and poverty, wealth distribution, housing, and the regional labour market and economy as context. Germany is described as a whole, by state (NUTS 1), by government region (NUTS 2) and by district (NUTS 3), as far as the sources publish the variables at these levels; national indicators are compared with the European Union (`EU27_2020`).
- **Horizon:** the data is collected over many years and analyzed as long time series. The design favors stable definitions, durable sources and a moderate data volume over the completeness of single tables.
- **Repository:** public. No license is granted for the code or the data of this repository. Every record names the license or terms of its source in the field `license`; the sources and their attribution requirements are listed in the [README](README.md#sources-and-attribution).
- **Schedule:** one workflow runs the collectors on Tuesdays and Fridays at 02:07 Europe/Berlin, the schedule of dataset.politics.germany and dataset.economics.germany. A run takes at most 30 minutes. A second workflow reviews the data sources on the first day of every month.
- **No paid access, no secrets:** every source is free and needs no key. The pipeline uses no language model; all variables are published statistics.
- **Aggregates only:** the dataset stores published aggregates of official statistics. It stores no microdata and nothing about individual persons.

**Frequency of the data.** Most sociodemographic statistics are annual and published with a lag of one to three years (population on 1 January within about six months, regional accounts after two to three years). Monthly and quarterly series (asylum applications, temporary protection, asylum decisions, wealth shares) and weekly deaths are the high-frequency part of the dataset. Because the runs are twice a week, `observed_at` marks the publication of every new value within a few days.

---

## 2. Design Rules

1. **Point-in-time correctness.** Every record has `observed_at`, the UTC time at which the pipeline observed it. Filtering `observed_at <= t` reproduces what was known at time t. The history that a series has at its first observation has `backfilled: true`.
2. **Vintages of statistical series.** Statistical offices revise published values, e.g. the population after a census. `sociodemographic_series` stores the complete series at its first observation (revision 0) and afterwards, with the next revision, only the periods that were added, revised or removed. Replaying the revisions in the order of `observed_at` gives the real-time data set.
3. **Two kinds of datasets.**
   - *Vintage:* `sociodemographic_series` (rule 2).
   - *Change:* `series_catalog` and `regions` store an entry when it appears and again when a field changes.
4. **Declarative registries.** The concepts (`config/series.toml`), the regions (`config/regions.toml`) and the providers (`config/providers.toml`) are configuration files with `added` and `removed` dates. The catalog and the regions are written to datasets, so that the coverage of every concept and region is known from the data alone.
5. **Permanent identifiers.** Concepts, series ids (`<concept>.<geo>[.<code>...]`), region codes, dataset names, field names and `record_id` formats are never reused for a different meaning.
6. **Additive schema changes.** New fields get a description and are added without changing existing ones; a changed meaning requires a new field and a new `SCHEMA_VERSION`. A snapshot test guards the record schema.
7. **Raw values, derived variables later.** The datasets store values as the sources report them. Shares, rates by citizenship, growth rates and other features are computed in the analysis (README, section Variables).
8. **One region key.** All regional series use the NUTS code of Eurostat as `geo` and carry the ISO 3166-2 code of their state, so that they join with the other datasets without a mapping table.
9. **Logged measurement changes.** Every change of a source, key, code, unit or regional classification is recorded in the measurement log (Section 11), so that structural breaks can be identified.
10. **Reviewed sources.** The sources are checked every month (Section 6): keys, coverage, discontinued and new datasets, regions and terms of use.

---

## 3. Datasets and Variables

All fields are defined with descriptions in [src/schema.py](src/schema.py); the README shows the data model and one example record per dataset.

### 3.1 Common Fields

Every record has: `record_id`, `revision`, `dataset`, `schema_version`, `observed_at`, `source_id`, `source_url`, `license`, `backfilled`.

### 3.2 Statistical Series

**`sociodemographic_series`** (vintage): series_id, concept, topic, geo, geo_level, state, breakdown {dimension: code}, frequency, unit, provider, provider_flow, provider_key, values {period: value}, removed_periods, first_period, last_period, observations, source_updated.

**`series_catalog`** (change): the entries of [config/series.toml](config/series.toml) with concept, name, topic, frequency, unit, provider, provider_flow, key_template, geo_levels, extra_geos, breakdown {dimension: [codes]}, added, removed, removed_reason.

A catalog entry describes one concept at one provider. Its SDMX key template has a placeholder for the region (`{geo}`) and one per breakdown dimension, e.g. `A.NR.{sex}.{age}.{geo}`; the other positions are fixed codes. Every combination of a region of the entry's levels and of the breakdown codes is a series with the id `<concept>.<geo>[.<code>...]`. One request fetches the series of up to 50 regions with SDMX OR-lists (`A.NR.M+F.Y_LT5+Y5-9.DE1+DE2`); the dimension columns of the SDMX-CSV response are matched with the positions of the template, so that the parser does not depend on the column names of a provider.

Periods are stored as `YYYY`, `YYYY-Qn`, `YYYY-MM` and `YYYY-Www`.

### 3.3 Regions

**`regions`** (change): the entries of [config/regions.toml](config/regions.toml): geo, name, level, parent, state, nuts_versions, added, removed, removed_reason.

The registry holds the 462 German codes of the NUTS versions 2016, 2021 and 2024 (taken from the NUTS files that Eurostat publishes in github.com/eurostat/Nuts2json): Germany, 16 states, 38 government regions and statistical regions, and 407 district codes, 400 of them in NUTS 2024. NUTS 2024 gave six districts of Thüringen new codes because their territories changed; the seven codes of the earlier versions stay in the registry, because datasets that still use NUTS 2021 publish their data under them. The level, the parent and the state follow from the code.

| Level | Codes | Example | Typical variables |
|---|---|---|---|
| `country` | `DE` | Germany | all |
| `nuts1` | 16 | `DE2` Bayern, state `DE-BY` | all regional variables; the join level of the other datasets |
| `nuts2` | 38 | `DE21` Oberbayern | life expectancy, causes of death, education, income, poverty, labour market |
| `nuts3` | 400 (+7 earlier codes) | `DE212` München, Kreisfreie Stadt | population by age and sex, demographic balance, fertility, density, GDP, employment |
| `eu` | `EU27_2020` | European Union | national comparisons |

### 3.4 Concepts by Topic

The initial catalog has 58 concepts that expand to about 34,000 series:

| Topic | Concepts | Levels | Provider |
|---|---|---|---|
| population_structure | population by broad age group and sex; population by five-year age group and sex (age pyramid); population by single year of age and sex; median age; age dependency ratios; population density | districts (single years: Germany) | Eurostat |
| population_change | demographic balance (population, live births, deaths, natural change, net migration, total change) and crude rates | districts | Eurostat |
| births_and_fertility | total fertility rate; mean age at childbirth; live births by age and citizenship of the mother; women by age and citizenship | districts; citizenship: Germany | Eurostat |
| mortality_and_health | life expectancy at birth and at 65 by sex; infant mortality; weekly deaths by sex; weekly deaths by region; standardised death rates by main cause | government regions; weekly: Germany and regions | Eurostat |
| migration_and_citizenship | population by citizenship and by country of birth; immigration and emigration by citizenship; acquisitions of citizenship; monthly asylum applicants; quarterly asylum decisions; monthly beneficiaries of temporary protection | Germany | Eurostat |
| households_and_families | average household size; young adults living with their parents; age of leaving the parental home; crude marriage and divorce rates | Germany, EU | Eurostat |
| education | educational attainment by level and sex; early leavers; NEET rate; adult learning | government regions, EU | Eurostat |
| income_and_poverty | disposable and primary income of households per inhabitant; income decile shares and cut-offs (income classes); mean and median equivalised income; Gini coefficient; S80/S20 (also by region); at-risk-of-poverty rates by age and by region; poverty or social exclusion by region and by citizenship; severe material and social deprivation (also by region) | government regions; distributions: Germany, EU | Eurostat |
| wealth | shares of net wealth of the wealthiest 10 and 5 percent and the poorest 50 percent of households (quarterly) | Germany | ECB |
| housing | tenure status; housing cost overburden (also by region); overcrowding | Germany, EU; overburden: government regions | Eurostat |
| labour_market | employment, unemployment and activity rates by sex | government regions | Eurostat |
| regional_economy | GDP per inhabitant; employment at the place of work | districts | Eurostat |

Derived variables, e.g. the shares of men and women, the fertility rate by citizenship and the income classes, are described in the README (section Variables).

---

## 4. Correlation with Other Datasets

The dataset is built to be joined with [dataset.politics.germany](https://github.com/kleinnconrad/dataset.politics.germany), [dataset.economics.germany](https://github.com/kleinnconrad/dataset.economics.germany), [dataset.technology](https://github.com/kleinnconrad/dataset.technology) and [dataset.fashion](https://github.com/kleinnconrad/dataset.fashion). The datasets share these conventions:

| Key | Use |
|---|---|
| Periods and `observed_at` | Time is the main join key. The period formats equal those of `economic_series` of dataset.economics.germany (`YYYY`, `YYYY-Qn`, `YYYY-MM`, `YYYY-Www`); event data of the other datasets (polls, votes, news, posts) is aggregated to these periods. `observed_at` keeps the joins point-in-time correct: an annual value is known only from its publication, often a year or more after the period. |
| `geo` `DE`, `EU27_2020` | The country and EU codes of the series of dataset.economics.germany and of the Eurostat indicators of dataset.technology. |
| `state` (ISO 3166-2) | Equals `jurisdiction` of the polls for the state parliaments and `states_concerned` and `home_state` of dataset.politics.germany, and `states_concerned` and `home_state` of dataset.economics.germany. Series of government regions and districts aggregate to their state. |
| ISO 3166-1 | `DE` is the country code of dataset.fashion (`source_country`) and of most datasets of dataset.technology; the World Bank indicators of dataset.technology use `DEU`. |
| NUTS codes | The keys of the regional statistics of Eurostat and of the GISCO boundaries; further regional sources and maps join on them. |

Examples of cross-dataset questions:
- **Politics and society:** do the age structure, the share of foreign citizens, net migration, poverty and unemployment of a state explain the level and the change of the party shares in the polls for its state parliament (`polls` with `parliament = Landtag`)? Do monthly asylum applications precede changes in the polls, in the parliamentary procedures on migration or in the press coverage of migration?
- **Economy and society:** how do regional income and poverty move with the national business cycle, inflation and the labour market of dataset.economics.germany? Are the wealth shares related to house prices and interest rates? Population series are the denominators of per-capita measures.
- **Technology and society:** do the Eurostat indicators of digital adoption and AI use of dataset.technology follow the educational attainment and the age structure?
- **Fashion and society:** the age structure, household income and urbanization of Germany as context for the posts of German fashion sources in dataset.fashion.

---

## 5. Prediction Targets

The dataset is designed for questions that need long series of comparable measurements and point-in-time features. Examples:

| Target | Labels | Main features |
|---|---|---|
| Population of a district in the next years | `population`, `population_by_age_group` (NUTS 3) | births, deaths and net migration of the demographic balance, age structure, median age, GDP and employment per district |
| Fertility rate by region | `total_fertility_rate` (NUTS 3) | age structure of women, mean age at childbirth, income, education, employment of women, density |
| Fertility of German and foreign women | births by citizenship of the mother and women by age and citizenship (derived rate) | population by citizenship, immigration, asylum applications |
| Ageing and dependency | `age_dependency_ratio`, `median_age` | age pyramid, net migration, life expectancy |
| Asylum applications in the next months | `asylum_applicants` (monthly) | past applications, decisions, temporary protection; political and economic news of the other datasets |
| Excess mortality | `deaths_weekly` | seasonal pattern of past years, population by age |
| Regional poverty and income | `at_risk_of_poverty_rate_regional`, `disposable_income_per_inhabitant` | unemployment, employment and activity rates, education, GDP per inhabitant |
| Inequality | `gini_coefficient`, `income_decile_shares`, `net_wealth_share` | labour market, house prices and interest rates of dataset.economics.germany |
| Election results and polls by state | polls of dataset.politics.germany | state profiles of this dataset (age, citizenship, migration, poverty, education, unemployment) |
| Revision behavior of statistics | revisions in `sociodemographic_series` | first release, census revisions, provider |

---

## 6. Sources and Fetch Rules

| Source | Access | Rate limit used | License or terms | Datasets |
|---|---|---|---|---|
| Eurostat dissemination API (SDMX 2.1) | SDMX-CSV, one request per catalog entry and group of up to 50 regions | 1 s | reuse authorised with acknowledgement of the source | `sociodemographic_series` |
| Eurostat table of contents | tab-separated text, one request per run | 1 s | as above | last update of the datasets (skipping unchanged entries), source review |
| ECB Data Portal (SDMX 2.1) | SDMX-CSV (`csvdata`) | 1 s | reuse free of charge with acknowledgement of the source | `sociodemographic_series` |
| Terms pages of the providers | HTML, monthly | 1 s, robots.txt | as stated on the pages | source review |

**Fetch rules** ([src/web.py](src/web.py)), the same as in the other datasets:
- Every request identifies the pipeline with the User-Agent `GermanSociodemographicsDataset/0.1 (+https://github.com/kleinnconrad/dataset.sociodemographics.germany)`. No personal data is sent.
- The SDMX interfaces and the table of contents are documented APIs offered for programmatic use; they are called under their terms and rate limits (`respect_robots=False`). Website requests (the terms pages) are checked against robots.txt with the longest-match rule of RFC 9309 and against TDMRep reservations.
- **Bot protection:** HTTP 401 or 403, a challenge page or a CAPTCHA stops the collector with an error. No headless browser is used.
- **Pacing:** one request per second per host, longer if robots.txt sets a crawl delay. Server errors, rate limits and network errors are retried twice.

**Unchanged datasets.** The Eurostat collector reads the table of contents first. A catalog entry whose dataset has the same last update as at its last download is skipped, unless the entry changed or its last download is older than `FULL_CHECK_DAYS` (28). Without the table of contents every entry is downloaded. The ECB entries are downloaded in every run.

**Source review.** On the first day of every month, `source_review.yml` runs [src/source_review.py](src/source_review.py), stores the report in `reviews/YYYY-MM.md` and opens a pull request:
- **Live check:** every active catalog entry is fetched for the last three years: entries without data or with a failing key, levels and regions without data, missing series, and newest periods older than expected (weekly 180 days, because Germany reports weekly deaths with a lag of several months; monthly 120 days, quarterly 270 days, annual 1,100 days after the end of the period). For an entry without data, the report shows the dimensions of the dataset (from its data structure definition) and the codes it has for Germany.
- **Datasets:** catalog datasets that the table of contents no longer lists, with possible successors (same code family, similar title); new or restructured datasets of the code families of the catalog (`demo_`, `migr_`, `edat_`, `ilc_`, `lfst_r_`, `hlth_`, `nama_10r_`, `cens_`, `proj_` and others) as candidates.
- **Regions:** German NUTS codes in a district dataset of Eurostat that the registry lacks.
- **State and runs:** entries without stored series, stale stored series, and entries without data or failed in the runs of the last 35 days.
- **Terms:** the text of every terms page is compared with the hash in `config/providers.toml`; a change is reported and the new hash is part of the pull request.

Changes of the catalog and the registry need a verified key and a measurement log entry and are made by hand; the review only reports them.

**Sources that were checked and are not collected:**

| Source | Reason |
|---|---|
| Destatis GENESIS-Online, Regionaldatenbank Deutschland, Zensus 2022 database | The web service requires a registered account, and its table codes could not be verified when the catalog was written. Variables that only they publish (fertility rate by citizenship and state, household net income classes of the Mikrozensus, population with a migration background, foreign population by district) are planned with an optional token (Section 12). Most Destatis statistics reach the dataset through Eurostat. |
| Bundesagentur für Arbeit, INKAR (BBSR) | No documented API for programmatic use; planned after a check of their terms. |
| Bundeswahlleiterin, structural data of the constituencies | Data files per election; planned after a check of their terms and file addresses. |
| Eurostat census hub (Census 2021) | Not available through the dissemination API. |

---

## 7. Storage, Partitioning and Data Volume

```text
data/
├── sociodemographic_series/year=2026/month=10/sociodemographic_series_2026-10-27.json
├── series_catalog/...
├── regions/...
└── state/
    ├── seen/series_catalog.json, seen/regions.json      # written entries with content hash and revision
    ├── seen/sociodemographic_series/<concept>/<shard>.json  # current values per concept and state
    ├── collectors.json                                   # first run, last run, failures, cursor per collector
    └── run_log.jsonl                                     # one line per run
```

- **Partition key:** the UTC day of the run that observed the record. A run writes at most one file per dataset; records are merged by `record_id` and `revision`.
- **Folder names:** `year=` and `month=` let DuckDB, Polars and pyarrow read a dataset as one table with partition pruning.
- **Format:** one JSON list per file with one record per line.
- **Seen shards:** the current values of the series are kept per concept and state (`DE1` to `DEG`), with Germany and the EU in shards of their own, so that a run rewrites only the shards of the concepts whose data changed (about 480 files).
- **Expected volume:** an offline test of the whole catalog with synthetic responses (31 years for every annual series, which is more than most datasets have) wrote 40 MB of records and 19 MB of state on the first run and nothing on a second run with unchanged data. The real first run is expected to write 20 to 40 MB. Afterwards, every annual release adds one period to each series of a concept: about 34,000 small records per year (an estimated 15 to 25 MB), plus revisions; git compresses the files to a fraction of their size.
- **Long-term plan:** when the repository approaches 1 GB, the records of completed years are archived as compressed Parquet files per year and dataset, or move to a separate data repository with the same layout.

---

## 8. Repository Structure

```text
dataset.sociodemographics.germany/
├── .github/
│   ├── dependabot.yml
│   └── workflows/
│       ├── ci.yml              # lint, tests, commit messages
│       ├── notify.yml          # run summary as a commit comment that mentions the owner
│       ├── pipeline.yml        # collectors, Tuesdays and Fridays 02:07 Europe/Berlin
│       └── source_review.yml   # monthly review of the data sources as a pull request
├── config/
│   ├── series.toml             # catalog of the concepts
│   ├── regions.toml            # registry of the German NUTS regions
│   └── providers.toml          # providers, terms of use and their hashes
├── data/                       # datasets and state, committed by the workflow
├── reviews/                    # reports of the monthly source review
├── src/
│   ├── main.py                 # entry point
│   ├── pipeline.py             # collector order, isolation, run log
│   ├── settings.py             # environment variables
│   ├── web.py                  # HTTP client with the fetch rules
│   ├── schema.py               # record models
│   ├── catalog.py              # series catalog and its expansion to series
│   ├── regions.py              # region registry
│   ├── providers.py            # provider configuration and terms hashes
│   ├── storage.py, state.py
│   ├── generate_dataset_overview.py
│   ├── source_review.py        # monthly review of the data sources
│   └── collectors/
│       ├── base.py             # collector interface and run context
│       ├── series.py           # vintages of statistical series
│       ├── sdmx.py             # Eurostat and ECB, Eurostat table of contents
│       └── registries.py       # series catalog and regions
├── scripts/
│   └── run_summary.py          # run summary for notify.yml
├── tests/                      # offline tests with a fake web
├── pyproject.toml
├── uv.lock
└── README.md
```

---

## 9. Pipeline Run

`src/main.py` starts the run; `src/pipeline.py` runs the collectors in the order of `COLLECTOR_ORDER`:

1. **series_catalog, regions:** write new and changed entries of the registries (no requests).
2. **ecb:** fetch the ECB entries and write the new and revised vintages.
3. **eurostat:** read the table of contents; fetch every catalog entry whose dataset changed or whose last download is older than 28 days, in groups of up to 50 regions; write the new and revised vintages entry by entry.
4. The workflow updates the README overview and commits `data/` and `README.md`. `notify.yml` then posts a summary of the run as a commit comment that mentions the repository owner.

**Run modes.** A regular run (`normal`) has a deadline of 25 minutes (`RUN_DEADLINE_MINUTES`) and a job timeout of 30 minutes. A `backfill` run downloads every entry, also those whose dataset is unchanged (deadline 85 minutes, timeout 90 minutes). `shadow` and `dry-run` work on a copy of the state and commit nothing.

**Requests and duration.** A full download of the catalog takes about 170 requests (the district concepts need nine groups of regions each). An offline test with synthetic responses processed the 34,000 series of a first run in under two minutes; the run time is set by the responses of the providers. Later runs skip the unchanged datasets and send a few requests.

---

## 10. Failure Handling

- **A collector fails:** the error is logged, recorded in `collectors.json` and the run log, and the next collector starts. Data already written stays. The run exits with code 1.
- **A catalog entry fails** (HTTP error, invalid response, other dimensions than the key template): the other entries continue; the failure is counted and listed in the run log (`failed_entries`). An entry without data in any group of regions (HTTP 404) is counted as `not_found` (`not_found_entries`); the monthly review reports both with a probe of the dataset.
- **Groups of regions without data** (e.g. a level that a dataset does not publish) are counted and do not fail the entry.
- **Region codes that a dataset does not know** (HTTP 400, e.g. codes of another NUTS version): the collector reads the regions of the dataset with one request, fetches the known ones and counts the others as `unknown_geos`; the review lists them. A 400 that is not caused by region codes fails the entry.
- **Incomplete series response:** a response that would remove more than 5 % of the stored periods of a series (and more than three) is not written for that series; the next run tries again. Stored series that a response no longer contains are counted as `absent` and are not changed.
- **Table of contents unavailable:** every entry is downloaded.
- **Run deadline:** after `RUN_DEADLINE_MINUTES` the collectors start no new work; a collector that stops early has the status `partial` and continues in the next run.
- **Concurrent pushes:** the workflow pulls with rebase and retries the push up to three times.

---

## 11. Measurement Log

Changes of sources, keys, codes, units, regional classifications and counting rules, and known breaks in the sources. Add a line for every change.

| Date | Dataset | Change |
|---|---|---|
| 2026-10-26 | all | Design of the pipeline; catalog of 54 concepts, registry of 462 regions (NUTS 2016, 2021 and 2024), providers Eurostat and ECB. |
| 2026-10-26 | `series_catalog` | First live check (source review in report mode) before the first collection: the keys of 20 entries were corrected to the dimension order and codes of their datasets (e.g. `demo_r_pjangrp3` freq.sex.unit.age.geo, `demo_r_find3` freq.indic_de.unit.geo, EU-SILC indicators with `statinfo` MED_EI and MEAN_EI and the poverty threshold `rskpovth` B_60 instead of the former codes MED_E, MEI_E and LI_R_MD60, NEET rate with `training` NO_FE_NO_NFE and `wstatus` NEMP). Four regional entries were added from the candidates of the review: `income_quintile_share_ratio_regional`, `severe_material_social_deprivation_regional`, `housing_cost_overburden_rate_regional`, `infant_mortality_rate`. |
| 2026-10-26 | `series_catalog` | Second live check: `migr_pop1ctz` has no citizenship code `FOR` (foreign citizens are `TOTAL` minus `NAT`); the asylum datasets use the applicant codes `TOTAL`, `FRST`, `SSEQ` and the decision codes `TOTAL`, `POS`, `NEG`, `POS_RFG`, `POS_SPROT`, `POS_HUM`; `ilc_di11_r` uses the unit code `INX`; weekly deaths are published for Germany by state only (`deaths_weekly_regional` restricted to NUTS 1). |
| 2026-10-26 | `sociodemographic_series` | Datasets reject requests that list a region code they do not know (HTTP 400), e.g. the regional accounts (`nama_10r_3gdp`, `nama_10r_3empers`) the NUTS 2024 codes of Thüringen. The collector then reads the regions of the dataset with one request and fetches the known ones; the unknown codes are counted (`unknown_geos`). |
| 2026-10-26 | `regions` | NUTS 2024 recoded six districts of Thüringen (DEG04, DEG0B, DEG0F, DEG0H, DEG0I, DEG0P became DEG0S, DEG0Q, DEG0T, DEG0V, DEG0U, DEG0R; DEG0N Eisenach merged into the Wartburgkreis). Both code sets are requested; a dataset publishes the codes of the NUTS version it uses, so a district series can change its code when Eurostat moves a dataset to NUTS 2024. |
| 2026-10-26 | `sociodemographic_series` | Series of a catalog entry are fetched in groups of up to 50 regions with SDMX OR-lists; rows with codes that were not requested are ignored. Status flags of the providers (e.g. `p` provisional, `b` break in series, `e` estimated) are not stored. |

---

## 12. Implementation Status

| Part | Status |
|---|---|
| Statistical series of Eurostat and the ECB as vintages, with skipping of unchanged datasets | implemented |
| Series catalog, region registry and provider configuration with their datasets | implemented |
| Monthly source review with live checks, probes, table of contents, region check and terms hashes, as a pull request | implemented |
| Live verification of the keys of the initial catalog (source review in report mode) | pending: the keys were written from the documentation of the providers and are verified with the first review run |
| Destatis GENESIS-Online and Regionaldatenbank with an optional free token (fertility rate by citizenship and state, Mikrozensus income classes and migration background, foreigners by district) | planned |
| Structural data of the Bundestag constituencies (Bundeswahlleiterin), to join with the constituency of the members of parliament in dataset.politics.germany | planned |
| Mapping of the districts to their official municipality keys (Kreisschlüssel) | planned |
| Population projections (EUROPOP2023, regional projections) as vintages | planned |
| Urban-rural, metropolitan and coastal typologies of the districts | planned |
| Feature tables with point-in-time joins of the vintages, also across the datasets (`scripts/build_features.py`) | planned |
