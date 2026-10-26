# German Sociodemographics Dataset: Architecture

This document describes the structure of the pipeline: scope and decisions, the design rules for a long collection period, the datasets and their variables (statistical series, snapshots of volatile sources, regions, religious affiliation), how the dataset is correlated with other datasets, the prediction targets, the sources, storage and data volume, the run, failure handling, the measurement log and the implementation status. The pipeline adopts the structure of [dataset.economics.germany](https://github.com/kleinnconrad/dataset.economics.germany) and [dataset.politics.germany](https://github.com/kleinnconrad/dataset.politics.germany). Usage and configuration are described in the [README](README.md).

## Table of Contents
- [1. Scope and Decisions](#1-scope-and-decisions)
- [2. Design Rules](#2-design-rules)
- [3. Datasets and Variables](#3-datasets-and-variables)
  - [3.1 Common Fields](#31-common-fields)
  - [3.2 Statistical Series](#32-statistical-series)
  - [3.3 Regions](#33-regions)
  - [3.4 Concepts by Topic](#34-concepts-by-topic)
  - [3.5 Snapshots of Volatile Sources](#35-snapshots-of-volatile-sources)
  - [3.6 Religious Affiliation](#36-religious-affiliation)
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

- **Purpose:** the main purpose is to persist volatile sociodemographic data: tables and files that their sources overwrite, delete, publish only as the current state, keep online only for a short time or change without versions. The pipeline stores what such a source published at every observation (`source_snapshots`) and thereby builds the history that the source does not keep. The statistical series of Eurostat and the ECB, which keep their own history, are the stable backbone and are stored as vintages.
- **Scope:** the population of Germany and its structure: age and sex, births and fertility, mortality, migration and citizenship, households and families, education, income distribution and poverty, wealth distribution, housing, religious affiliation, crime and criminal justice, and the regional labour market and economy as context. Germany is described as a whole, by state (NUTS 1), by government region (NUTS 2) and by district (NUTS 3), as far as the sources publish the variables at these levels; national indicators are compared with the European Union (`EU27_2020`).
- **Horizon:** the data is collected over many years and analyzed as long time series. The design favors stable definitions, durable sources and a moderate data volume over the completeness of single tables.
- **Repository:** public. No license is granted for the code or the data of this repository. Every record names the license or terms of its source in the field `license`; the sources and their attribution requirements are listed in the [README](README.md#sources-and-attribution).
- **Schedule:** one workflow runs the collectors once a week, on Wednesdays at 02:07 Europe/Berlin. The source that changes most often is the weekly report on deaths of the Statistisches Bundesamt, whose edition is replaced every week (the edition observed on 2026-10-26 was published on a Tuesday morning); a run in the middle of the week sees every edition, and a failed run can be repeated by hand before the next edition replaces it. The other sources are checked at their own intervals, independent of the schedule: Eurostat datasets when the table of contents reports an update (at the latest every 28 days), snapshot items every 7 to 30 days. A run takes at most 30 minutes. Every run is reported by `notify.yml`. A second workflow reviews the data sources on the first day of every month.
- **No paid access:** every source is free. The GENESIS databases (GENESIS-Online, Regionaldatenbank, Zensus database) need free accounts, whose tokens are optional repository secrets; without them their tables are counted as `no_access` and every other source is collected. The pipeline uses no language model; all variables are published statistics.
- **Aggregates only:** the dataset stores published aggregates of official statistics. It stores no microdata and nothing about individual persons.

**Frequency of the data.** Most sociodemographic statistics are annual and published with a lag of one to three years (population on 1 January within about six months, regional accounts after two to three years). Monthly and quarterly series (asylum applications, temporary protection, asylum decisions, wealth shares) and weekly deaths are the high-frequency part of the dataset. Because the pipeline runs every week, `observed_at` marks the publication of every new value within seven days.

---

## 2. Design Rules

1. **Point-in-time correctness.** Every record has `observed_at`, the UTC time at which the pipeline observed it. Filtering `observed_at <= t` reproduces what was known at time t. The history that a series has at its first observation has `backfilled: true`.
2. **Vintages of statistical series.** Statistical offices revise published values, e.g. the population after a census. `sociodemographic_series` stores the complete series at its first observation (revision 0) and afterwards, with the next revision, only the periods that were added, revised or removed. Replaying the revisions in the order of `observed_at` gives the real-time data set.
3. **Three kinds of datasets.**
   - *Vintage:* `sociodemographic_series` (rule 2).
   - *Snapshot:* `source_snapshots` stores the complete content of a volatile item at its first observation and whenever its hash changes; unchanged content writes no record (Section 3.5).
   - *Change:* `series_catalog`, `regions` and `snapshot_catalog` store an entry when it appears and again when a field changes.
4. **Declarative registries.** The concepts (`config/series.toml`), the snapshot items (`config/snapshots.toml`), the regions (`config/regions.toml`) and the providers (`config/providers.toml`) are configuration files with `added` and `removed` dates. The catalogs and the regions are written to datasets, so that the coverage of every concept and region is known from the data alone.
5. **Permanent identifiers.** Concepts, series ids (`<concept>.<geo>[.<code>...]`), snapshot ids (`<provider>.<item>`), region codes, dataset names, field names and `record_id` formats are never reused for a different meaning.
6. **Additive schema changes.** New fields get a description and are added without changing existing ones; a changed meaning requires a new field and a new `SCHEMA_VERSION`. A snapshot test guards the record schema.
7. **Raw values, derived variables later.** The datasets store values as the sources report them. Shares, rates by citizenship, growth rates and other features are computed in the analysis (README, section Variables).
8. **One region key for the series.** All regional series use the NUTS code of Eurostat as `geo` and carry the ISO 3166-2 code of their state, so that they join with the other datasets without a mapping table. Snapshots keep the region codes of their sources (`geo_scheme`); the mapping of official keys and state names to the ISO codes is in Section 3.3.
9. **Logged measurement changes.** Every change of a source, key, code, unit or regional classification is recorded in the measurement log (Section 11), so that structural breaks can be identified.
10. **Reviewed sources.** The sources are checked every month (Section 6): keys, coverage, discontinued and new datasets, the snapshot items, regions and terms of use. A source that is checked and not admitted is recorded with the reason.

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

**Keys of the snapshots.** The snapshot items keep the region codes of their sources (`geo_scheme`): official keys (`ags`: Amtlicher Gemeindeschlüssel or Regionalschlüssel; two digits for the state, five for the district, twelve for the municipality in the Zensus files), the three-digit numbers of the Bundestag constituencies (`constituency`), or the names of the states (`state_name`). The first two digits of an official key and the state names map to the ISO 3166-2 codes of the series and the other datasets:

| Key | State | ISO | Key | State | ISO |
|---|---|---|---|---|---|
| 01 | Schleswig-Holstein | DE-SH | 09 | Bayern | DE-BY |
| 02 | Hamburg | DE-HH | 10 | Saarland | DE-SL |
| 03 | Niedersachsen | DE-NI | 11 | Berlin | DE-BE |
| 04 | Bremen | DE-HB | 12 | Brandenburg | DE-BB |
| 05 | Nordrhein-Westfalen | DE-NW | 13 | Mecklenburg-Vorpommern | DE-MV |
| 06 | Hessen | DE-HE | 14 | Sachsen | DE-SN |
| 07 | Rheinland-Pfalz | DE-RP | 15 | Sachsen-Anhalt | DE-ST |
| 08 | Baden-Württemberg | DE-BW | 16 | Thüringen | DE-TH |

A district key of five digits maps to one NUTS 3 code; the mapping table of the districts is planned (Section 12).

### 3.4 Concepts by Topic

The catalog has 61 concepts that expand to about 34,000 series:

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
| crime_and_justice | offences recorded by the police by offence category (ICCS); persons suspected and convicted by citizenship (German, foreign); prison population and capacity | Germany | Eurostat |

Derived variables, e.g. the shares of men and women, the fertility rate by citizenship and the income classes, are described in the README (section Variables).

### 3.5 Snapshots of Volatile Sources

**`source_snapshots`** (snapshot): snapshot_id, name, topic, provider, item, format, geo_scheme, page_url, source_title, source_updated, content_hash, previous_hash, columns, rows, row_count, labels, archived_file, byte_size.

**`snapshot_catalog`** (change): the entries of [config/snapshots.toml](config/snapshots.toml) with snapshot_id, name, topic, provider, format, address, geo_scheme, archive, added, removed, removed_reason.

A snapshot item is one published table or file. Two collectors fetch them ([src/collectors/snapshots.py](src/collectors/snapshots.py)):
- **`genesis`** reads tables of the GENESIS databases through their web service: first the table title (`metadata/table`), which must match the pattern `expect` of the entry, then the table as flat CSV in a zip archive (`data/tablefile`, `format=ffcsv`). The label columns of the flat CSV are moved into `labels`, so that every row holds the codes of the time and of the variables, the value, its unit and its variable. The token of an account is sent in the `username` header (`password` empty); tables of more than 40,000 values are restricted with `startyear`.
- **`files`** reads CSV, xlsx, JSON, HTML tables and PDF documents from websites with the fetch rules of every website request. A file whose address changes with each edition is found through the pattern `link` on its `page`. Conditional requests (ETag, Last-Modified) avoid downloading unchanged files; a file that several entries read (sheets of one workbook) is downloaded once per run. Format options select the sheet and the header (`sheet`, `header_pattern`, `header_rows`; a header of several rows is joined per column).

Cells are stored as text as the source wrote them. `content_hash` is the hash of the columns and rows (of the file for a PDF document); a record is written when it differs from the hash of the previous observation, which is kept in `data/state/seen/source_snapshots.json` together with the file address, ETag, Last-Modified and the day of the last change. `check_days` sets the interval of an item (0: every run), `update_days` the interval within which the source is expected to change the content (the review reports an item whose content did not change for longer).

**Items** (54 at the start):

| Topic | Items | Provider | Regions |
|---|---|---|---|
| religion | Zensus 2022 and Zensus 2011: population by religious affiliation per municipality, district and state (special evaluation, two sheets); per constituency (2022); Zensus database: 1000A-1018 religion per state, 1000A-1E18 per regional church of the EKD, 1000A-1K18 per Catholic diocese, and per regional church (2E) and diocese (2K) by migration history (22), citizenship groups (31), sex (28) and five-year age group (10) | zensus2022 | ags, constituency, regional_church, diocese |
| births_and_fertility | 12612-0002 live births by month and sex; 12612-0101 by state and month; 12612-0103 by state and citizenship of the child and the parents; 12612-0104 total fertility rate by state; 12612-0010 by citizenship of the mother | destatis | none, ags |
| mortality_and_health | 12613-0006 deaths by month and sex; 12613-0013 by state, month and sex; weekly report Sterbefälle nach Tagen, Wochen und Monaten: deaths by day (Germany), by calendar week and age group (Germany), by month, state and age group | destatis | none, state_name |
| migration_and_citizenship | 12211-0200 population by migration status (Mikrozensus); 12521-0002 foreigners by citizenship and 12521-0040 by district (Central Register of Foreigners); 12531-0001 persons seeking protection and 12531-0040 by district; share of foreigners per constituency (Zensus 2022) | destatis, zensus2022 | none, ags, constituency |
| income_and_poverty | 12211-0300 private households by monthly net income class (Mikrozensus) | destatis | none |
| labour_market | 13211-02-05-4 unemployed persons by district, annual average (Bundesagentur für Arbeit in the Regionaldatenbank) | regionalstatistik | ags |
| population_structure, households_and_families | German citizens in five age groups and population by marital status per constituency (Zensus 2022 special evaluations) | zensus2022 | constituency |
| population_structure, migration_and_citizenship, households_and_families, religion | Zensus database per Bundestag constituency (299): five-year age groups (1000A-1W04), migration history (1W11), marital status (1W12), country of birth (1W14), sex (1W17), religion (1W18), citizenship groups (1W20), most frequent citizenships (1W21) | zensus2022 | constituency |
| crime_and_justice | 24911-0001 offences and 24911-0002 suspects by nationality and sex (Polizeiliche Kriminalstatistik); 24311-0001 and 24311-0002 convicted persons (Strafverfolgungsstatistik); 24321-0001 prisoners by sex, age, nationality and type of detention and 24321-0002 by state (Strafvollzugsstatistik); persons with pre-trial detention by citizenship and state in 2014, 2017 and 2019 (special evaluation) | destatis | none, state_name |
| regional_structure | Structural data of the constituencies of the Bundestag elections 2017, 2021 and 2025 (48 indicators per constituency: area, population, citizenship, age, migration balance, housing, income, employment, unemployment, education, social benefits) | bundeswahlleiterin | constituency |

The table codes of GENESIS-Online were taken from the publications of the Statistisches Bundesamt and from the table lists of the observed statistics, and were verified with the token on 2026-10-26, as were the tables of the Regionaldatenbank and the Zensus database with their accounts. The pattern `expect` prevents that a wrong or reused code writes other content under an id. With a token, the review lists the other tables of every observed statistic as candidates. Without `startyear` the web service returns the default selection of a table, for several tables only the last years; `startyear` sets the first year of the stored window. The Polizeiliche Kriminalstatistik has only two tables in GENESIS-Online (24911-0001, 24911-0002), both for Germany.

### 3.6 Religious Affiliation

Religious affiliation is a required variable. What the sources publish:

| Source | Content | Regions | Status |
|---|---|---|---|
| Zensus 2011 and Zensus 2022 (special evaluation on www.destatis.de) | Population by affiliation: Roman Catholic Church (public-law), Protestant Church (EKD, public-law), other, none or not stated; number and share | municipalities, districts, states, Germany | collected (`zensus2022.religion_gemeinden_2022`, `zensus2022.religion_gemeinden_2011`) |
| Zensus 2022, special evaluation per constituency | the same three groups | 299 constituencies of the Bundestag election 2025 | collected (`zensus2022.religion_wahlkreise`, and from the Zensus database `zensus2022.1000A-1W18`) |
| Zensus database, table 1000A-1018 | persons by religion (the three groups) | states and Germany | collected with the token `ZENSUS2022_TOKEN` |
| Zensus database, tables 1000A-1E18 and 1000A-1K18, with the breakdowns 1000A-2E22, -2E31, -2E28, -2E10 and 1000A-2K22, -2K31, -2K28, -2K10 | persons by religion per regional church of the EKD (E) and per Catholic diocese (K), also by migration history, citizenship groups, sex and five-year age group | 19 regional churches, 27 dioceses | collected with the token `ZENSUS2022_TOKEN` |
| Deutsche Bischofskonferenz, Kirchliche Statistik | Catholics, joinings, re-admissions, leavings, baptisms by diocese (27) and by state | dioceses, states | not collected: HTTP 401 for automated requests; PDF only |
| EKD, Kirchenmitgliederzahlen | Protestants by regional church (20) and by state; Catholics by state | regional churches, states | not collected: the imprint forbids any further use without permission |
| Open data of cities (Düsseldorf, Dortmund/Ruhr) | church leavings by denomination | cities | not collected: portals not reachable with the fetch rules |

**Mapping to states.** The 27 Catholic dioceses and the 20 regional churches of the EKD do not follow the borders of the states: e.g. the archdiocese of Hamburg covers Hamburg, Schleswig-Holstein and Mecklenburg, the diocese of Limburg parts of Hessen and Rheinland-Pfalz, the Evangelische Kirche in Mitteldeutschland mainly Sachsen-Anhalt and Thüringen, and several regional churches share Niedersachsen. The Zensus 2022 database publishes the population by religious affiliation per regional church and per diocese (regional variables of the database, tables 1000A-1E18 and 1000A-1K18 and their breakdowns), so the census figures exist for the territories of the churches themselves, next to the official keys of states, districts and municipalities. No official table maps dioceses or regional churches to districts; the member statistics of the churches by state are excluded (above). The census groups of the special evaluation map to every level through the official key.

**Gaps.** Annual church members, joinings and leavings by state are not collected until the EKD or the Deutsche Bischofskonferenz permit the reuse or offer the data without bot protection; the annual figures between the censuses therefore come only from the census years 2011 and 2022. Other religious communities (Muslims, Orthodox churches, Jewish communities, free churches) are part of the group other, none or not stated; their member statistics are published by the communities themselves (e.g. the Zentralwohlfahrtsstelle der Juden in Deutschland for the Jewish communities) as PDF documents without stated terms that permit redistribution, and the Islam statistics of the Bundesamt für Migration und Flüchtlinge are survey estimates published as PDF.

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
| Constituency numbers | The structural data of the Bundeswahlleiterin and the constituency evaluations of the Zensus 2022 (`geo_scheme = constituency`) carry the number of the Bundestag constituency, the key of the constituency results and of the directly elected members in dataset.politics.germany. The constituencies are redrawn before most elections; every edition holds the constituencies of its election. |
| Official keys | The snapshots with `geo_scheme = ags` carry the official keys of states, districts and municipalities; the first two digits give the state (Section 3.3). |

Examples of cross-dataset questions:
- **Constituencies:** do the religious affiliation, the share of foreigners, the age structure, income and unemployment of a constituency (structural data and Zensus evaluations) explain the first and second votes and the party of its directly elected member?
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
| Revision behavior of statistics | revisions in `sociodemographic_series`; successive snapshots of GENESIS tables and of the weekly report on deaths | first release, census revisions, provider |
| Final weekly deaths from the provisional figures | the final values of the weekly report (after about a year) | the provisional and estimated values of earlier editions in `source_snapshots` |
| Crime and its regional context | `police_recorded_offences`, suspects of `criminal_justice_persons_by_citizenship`, the PKS tables in `source_snapshots` | age structure, citizenship, unemployment, poverty, urbanization |
| Religious affiliation by region | the census shares of the churches | age structure, migration, urbanization, East and West Germany |

---

## 6. Sources and Fetch Rules

| Source | Access | Rate limit used | License or terms | Datasets |
|---|---|---|---|---|
| Eurostat dissemination API (SDMX 2.1) | SDMX-CSV, one request per catalog entry and group of up to 50 regions | 1 s | reuse authorised with acknowledgement of the source | `sociodemographic_series` |
| Eurostat table of contents | tab-separated text, one request per run | 1 s | as above | last update of the datasets (skipping unchanged entries), source review |
| ECB Data Portal (SDMX 2.1) | SDMX-CSV (`csvdata`) | 1 s | reuse free of charge with acknowledgement of the source | `sociodemographic_series` |
| GENESIS-Online (Destatis) web service | POST `metadata/table` and `data/tablefile` (flat CSV in a zip archive), account token in the header | 1 s | Datenlizenz Deutschland – Namensnennung 2.0 | `source_snapshots` |
| Regionaldatenbank Deutschland web service | as GENESIS-Online | 1 s | Datenlizenz Deutschland – Namensnennung 2.0 | `source_snapshots` |
| Zensus database web service | as GENESIS-Online | 1 s | Datenlizenz Deutschland – Namensnennung 2.0 | `source_snapshots` |
| www.destatis.de (Statistische Berichte, special evaluations of the Zensus 2022 and of the Strafverfolgungsstatistik) | page with the file link, then the xlsx file; conditional requests | 30 s (crawl delay), robots.txt, TDMRep | reproduction and distribution with a reference to the source (Copyright allgemein; Zensus: Datenlizenz Deutschland 2.0) | `source_snapshots` |
| www.bundeswahlleiterin.de (structural data of the constituencies) | page with the file link, then the CSV file | 1 s, robots.txt, TDMRep | Datenlizenz Deutschland – Namensnennung 2.0 | `source_snapshots` |
| Terms pages of the providers | HTML, monthly | 1 s, robots.txt | as stated on the pages | source review |

**Fetch rules** ([src/web.py](src/web.py)), the same as in the other datasets:
- Every request identifies the pipeline with the User-Agent `GermanSociodemographicsDataset/0.1 (+https://github.com/kleinnconrad/dataset.sociodemographics.germany)`. No personal data is sent.
- The SDMX interfaces, the table of contents and the web services of the GENESIS databases are documented APIs offered for programmatic use; they are called under their terms and rate limits (`respect_robots=False`). The robots.txt of www.regionalstatistik.de disallows every path, including the web service; the open-data page of the statistical offices names the base URL of this web service for programmatic use, so it is treated as a documented API. Website requests (the pages and files of the snapshot sources, the terms pages) are checked against robots.txt with the longest-match rule of RFC 9309 and against TDMRep reservations.
- **Bot protection:** HTTP 401 or 403, a challenge page or a CAPTCHA stops a series collector with an error; a snapshot item fails and is not requested again for 30 days. HTTP 401 of a GENESIS web service means a missing or invalid account and is counted as `no_access`. No headless browser is used.
- **Pacing:** one request per second per host, longer if robots.txt sets a crawl delay; a host with a crawl delay of more than 120 seconds is not fetched. Server errors, rate limits and network errors are retried twice.

**Unchanged datasets.** The Eurostat collector reads the table of contents first. A catalog entry whose dataset has the same last update as at its last download is skipped, unless the entry changed or its last download is older than `FULL_CHECK_DAYS` (28). Without the table of contents every entry is downloaded. The ECB entries are downloaded in every run.

**Source review.** On the first day of every month, `source_review.yml` runs [src/source_review.py](src/source_review.py), stores the report in `reviews/YYYY-MM.md` and opens a pull request:
- **Live check:** every active catalog entry is fetched for the last three years: entries without data or with a failing key, levels and regions without data, missing series, and newest periods older than expected (weekly 180 days, because Germany reports weekly deaths with a lag of several months; monthly 120 days, quarterly 270 days, annual 1,100 days after the end of the period). For an entry without data, the report shows the dimensions of the dataset (from its data structure definition) and the codes it has for Germany.
- **Datasets:** catalog datasets that the table of contents no longer lists, with possible successors (same code family, similar title); new or restructured datasets of the code families of the catalog (`demo_`, `migr_`, `edat_`, `ilc_`, `lfst_r_`, `hlth_`, `nama_10r_`, `cens_`, `proj_` and others) as candidates.
- **Regions:** German NUTS codes in a district dataset of Eurostat that the registry lacks.
- **State and runs:** entries without stored series, stale stored series, and entries without data or failed in the runs of the last 35 days.
- **Snapshot items:** every active item is fetched live: unavailable items, titles or columns that no longer match `expect`, GENESIS tables without access (with the name of the missing secret), changed file addresses, content without rows, content changed since the last run, items never stored, and items whose content did not change for longer than `update_days`. The report shows the title, columns and first row of every item. With a token, the other tables of every observed GENESIS statistic are listed as candidates. The excluded sources are listed; a source checked more than 365 days ago is due for a new check.
- **Terms:** the text of every terms page is compared with the hash in `config/providers.toml`; a change is reported and the new hash is part of the pull request.
- **Probe:** started by hand with URLs, the workflow checks candidate sources with the fetch rules and describes them (robots.txt rules and crawl delay, TDM reservations, response, links and text passages of pages, sheets and first rows of workbooks). Every source of the table below was checked this way.

Changes of the catalog, the snapshot configuration and the registry need a verified key or address and a measurement log entry and are made by hand; the review only reports them.

**Sources that were checked and are not collected:**

The snapshot sources are recorded with the day of the check in the `[[excluded]]` entries of `config/snapshots.toml`, which the monthly review lists.

| Source | Checked | Reason |
|---|---|---|
| Bundeskriminalamt, Polizeiliche Kriminalstatistik on www.bka.de (tables by state and district: cases, clearance rates, suspects by citizenship) | 2026-10-26 | The robots.txt sets a crawl delay of 180 seconds, longer than the maximum of 120 seconds: robots.txt, the TDM file, the page and the tables would take 10 to 15 minutes of a run. The terms of reuse could not be read for the same reason; a secondary source names the Datenlizenz Deutschland 2.0 for the PKS yearbook. The PKS tables of GENESIS-Online (statistic 24911) are collected instead; tables by state and district are not collected. |
| Deutsche Bischofskonferenz, Kirchliche Statistik | 2026-10-26 | HTTP 401 for automated requests (bot protection); PDF only. |
| Evangelische Kirche in Deutschland, Kirchenmitgliederzahlen (xlsx by regional church and state) | 2026-10-26 | Fetchable (robots.txt allows the xlsx files, PDF files are disallowed), but the imprint states: "Alle Urheber- und Nutzungsrechte an Texten und Bildern liegen bei der EKD oder bei Dritten. Eine weitere Verwendung ist ohne ausdrückliche Genehmigung der Rechteinhaber untersagt." |
| Statistik der Bundesagentur für Arbeit (monthly labour market by district) | 2026-10-26 | HTTP 403 for automated requests on statistik.arbeitsagentur.de; the robots.txt of the data API statistik-dr.arbeitsagentur.de disallows it. Unemployment by district comes from the Regionaldatenbank (annual) and the structural data of the constituencies. |
| Bundesamt für Migration und Flüchtlinge, Asylgeschäftsstatistik | 2026-10-26 | The robots.txt of www.bamf.de could not be fetched from GitHub-hosted runners in three checks (connection error), so every path counts as disallowed (RFC 9309); the monthly figures are PDF. Asylum figures come from Eurostat, persons seeking protection from GENESIS-Online (12531). |
| BBSR, INKAR | 2026-10-26 | The robots.txt of www.inkar.de could not be fetched in three checks (connection error). |
| Open data Ruhr (church leavings in Dortmund) | 2026-10-26 | The robots.txt returned a server error or timed out in three checks. |
| Open data Düsseldorf (church leavings since 1988) | 2026-10-26 | ckan.open.nrw.de not reachable; opendata.duesseldorf.de has no CKAN API at /api/3 (HTTP 404). |
| Eurostat, crimes recorded by the police by NUTS 3 region (`crim_gen_reg`) | 2026-10-26 | Discontinued in 2010. |
| Eurostat, prisoners by citizenship (`crim_pris_ctz`) | 2026-10-26 | Germany reports only the total, which equals `prison_population`. |
| Eurostat census hub (Census 2021) | 2026-10-26 | Not available through the dissemination API; the census tables of Eurostat (`cens_21*`) are listed as candidates by the review. |

---

## 7. Storage, Partitioning and Data Volume

```text
data/
├── sociodemographic_series/year=2026/month=10/sociodemographic_series_2026-10-27.json
├── series_catalog/...
├── regions/...
├── source_snapshots/...
├── snapshot_catalog/...
├── files/<snapshot_id>/YYYY-MM-DD_<hash>.pdf           # archived documents of snapshot items
└── state/
    ├── seen/series_catalog.json, seen/regions.json      # written entries with content hash and revision
    ├── seen/snapshot_catalog.json, seen/source_snapshots.json  # snapshot items: hash, file address, ETag, last change
    ├── seen/sociodemographic_series/<concept>/<shard>.json  # current values per concept and state
    ├── collectors.json                                   # first run, last run, failures, cursor per collector
    └── run_log.jsonl                                     # one line per run
```

- **Partition key:** the UTC day of the run that observed the record. A run writes at most one file per dataset; records are merged by `record_id` and `revision`.
- **Folder names:** `year=` and `month=` let DuckDB, Polars and pyarrow read a dataset as one table with partition pruning.
- **Format:** one JSON list per file with one record per line.
- **Seen shards:** the current values of the series are kept per concept and state (`DE1` to `DEG`), with Germany and the EU in shards of their own, so that a run rewrites only the shards of the concepts whose data changed (about 480 files).
- **Expected volume:** an offline test of the whole catalog with synthetic responses (31 years for every annual series, which is more than most datasets have) wrote 40 MB of records and 19 MB of state on the first run and nothing on a second run with unchanged data. The real first run is expected to write 20 to 40 MB. Afterwards, every annual release adds one period to each series of a concept: about 34,000 small records per year (an estimated 15 to 25 MB), plus revisions; git compresses the files to a fraction of their size.
- **Snapshots:** an item writes its complete content at every change. The one-time special evaluations and the structural data change rarely (a few hundred rows each). The weekly report on deaths changes every week: the deaths by day (about 2,200 rows) are stored in every run, about 10 MB per year; the tables by week and by month and state (5,000 to 6,500 rows) are checked every 28 days, about 8 MB per year each. GENESIS tables are checked every 7 or 30 days and change with their releases. In total the snapshots are expected to add 30 to 60 MB per year before compression.
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
│       ├── pipeline.yml        # collectors, Wednesdays 02:07 Europe/Berlin
│       └── source_review.yml   # monthly review of the data sources as a pull request
├── config/
│   ├── series.toml             # catalog of the concepts
│   ├── snapshots.toml          # snapshot items and excluded sources
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
│   ├── snapshots.py            # snapshot configuration
│   ├── probe.py                # description of candidate sources with the fetch rules
│   ├── storage.py, state.py
│   ├── generate_dataset_overview.py
│   ├── source_review.py        # monthly review of the data sources
│   └── collectors/
│       ├── base.py             # collector interface and run context
│       ├── series.py           # vintages of statistical series
│       ├── sdmx.py             # Eurostat and ECB, Eurostat table of contents
│       ├── snapshots.py        # GENESIS tables and files as snapshots
│       └── registries.py       # series catalog, regions and snapshot catalog
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

1. **series_catalog, regions, snapshot_catalog:** write new and changed entries of the registries (no requests).
2. **files:** fetch the file items that are due (page, then file; one download per file and run) and write a snapshot for new or changed content.
3. **genesis:** fetch the GENESIS tables that are due (title, then table) and write a snapshot for new or changed content; without a token the tables count as `no_access`.
4. **ecb:** fetch the ECB entries and write the new and revised vintages.
5. **eurostat:** read the table of contents; fetch every catalog entry whose dataset changed or whose last download is older than 28 days, in groups of up to 50 regions; write the new and revised vintages entry by entry.
6. The workflow updates the README overview and commits `data/` and `README.md`. `notify.yml` then posts a summary of the run as a commit comment that mentions the repository owner.

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
- **A snapshot item fails** (page or file unavailable, no link matches, the title or columns do not match `expect`, unreadable file): the other items continue; the failure is listed in the run log (`failed_entries`) and reported by the review. An item whose source refuses requests (HTTP 401/403, challenge) is not requested for 30 days (`blocked`). GENESIS tables without access are counted as `no_access` and do not fail the collector; the collector fails only if every attempted item failed.
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
| 2026-10-26 | all | Change of direction: the main purpose of the dataset is to persist volatile sources. New dataset kind *snapshot* with the datasets `source_snapshots` and `snapshot_catalog` (additive; `SCHEMA_VERSION` stays 1.0) and the topic groups `religion`, `regional_structure` and `crime_and_justice`. Snapshot cells are stored as text as the source wrote them; numbers that a workbook stores as floating-point values are written with 15 significant digits, as spreadsheet programs show them. |
| 2026-10-26 | `providers` | New providers with their terms, read with the probe: `destatis` (GENESIS-Online and www.destatis.de; Datenlizenz Deutschland – Namensnennung 2.0, Copyright allgemein permits reproduction and distribution with a reference to the source), `regionalstatistik` and `zensus2022` (open-data page of the statistical offices: Datenlizenz Deutschland 2.0), `bundeswahlleiterin` (imprint: reproduction and distribution, also commercial, with a reference to the source; the CSV files state the Datenlizenz Deutschland – Namensnennung 2.0). |
| 2026-10-26 | `source_snapshots` | GENESIS-Online tables (provider `destatis`): 12612-0002, 12612-0101, 12612-0104, 12612-0010, 12613-0006, 12211-0300, 12211-0200, 12521-0002, 12521-0040 (from 2018), 12531-0001, 24911-0001, 24911-0002, 24311-0001 (from 2015), 24311-0002, 24321-0001. Regionaldatenbank: 13211-02-05-4. Zensus database: 1000A-1018. The web services answer HTTP 401 without an account (checked live); the codes and titles are verified with the first review after the secrets are set, and every table has an `expect` pattern of its title. The robots.txt of www.regionalstatistik.de disallows every path; its web service is used as a documented API. |
| 2026-10-26 | `source_snapshots` | GENESIS-Online verified with the token (POST requests to the RESTful/JSON interface; the GET methods and the SOAP interface were switched off on 15 July 2025): all tables exist and match their pattern. 12211-0200 is titled with `Migrationsstatus` (the pattern was extended). Without `startyear` several tables return only their last years (12612-0002 five years, 12613-0006 two years, 12521-0002 one reference date, 12211-0300 and 24311-0002 one year); the windows start in 2000 (12612-0002, 12613-0006, 12521-0002), 2005 (12211-0200, 12211-0300), 2007 (12531-0001), 2010 (12612-0101), 2015 (24311-0001, 24311-0002) and 2018 (12521-0040). The two tables of the Polizeiliche Kriminalstatistik keep their default selection, which reaches back further. Added from the table lists of the observed statistics: 12612-0103 live births by state and citizenship of the child and the parents (from 2010), 12613-0013 deaths by state, month and sex (from 2010), 12531-0040 persons seeking protection by district (from 2018), 24321-0002 prisoners by state (from 2022). GENESIS-Online has no tables of the police crime statistics by state or district. |
| 2026-10-26 | `source_snapshots` | Regionaldatenbank verified with user name and password: 13211-02-05-4 (unemployed persons and unemployment rates by district, annual average, from 2009) matches its pattern; without `startyear` the web service returns the latest year (7,163 rows for 2025), which the annual snapshots keep. Credentials with characters outside ASCII are sent in the request headers as ISO-8859-1 bytes (UTF-8 for other characters); before, such a password failed before the request was sent. |
| 2026-10-26 | `source_snapshots` | Zensus database verified with the token: 1000A-1018 (Personen: Religion) matches its pattern; without `regionalvariable` the web service returns the 16 states and Germany (regional variable `GEOBL1`, 136 rows: number and share of the three groups). The municipalities and districts are collected from the special evaluation of the Zensus 2011 and 2022. |
| 2026-10-26 | `source_snapshots` | Zensus database, religion by the territories of the churches, verified with the token: 1000A-1E18 (religion per regional church of the EKD, regional variable `GEOEV1`, 19 regional churches) and 1000A-1K18 (per Catholic diocese, 27 dioceses), and per regional church and diocese by migration history (2E22, 2K22), citizenship groups (2E31, 2K31), sex (2E28, 2K28) and five-year age group (2E10, 2K10). These tables have no breakdown by state (the web service rejects `regionalvariable = GEOBL1` with status 25). The review shows the labels of the first row of every GENESIS table. |
| 2026-10-26 | `source_snapshots` | Zensus database per Bundestag constituency (regional variable `Bundestagswahlkreise`, 299 constituencies), verified with the token: 1000A-1W04 (five-year age groups, 11,960 rows), 1W11 (migration history), 1W12 (marital status), 1W14 (country of birth), 1W17 (sex), 1W18 (religion), 1W20 (citizenship groups), 1W21 (most frequent citizenships, 25,116 rows). The other tables of the series (further age groupings, single countries of birth and citizenship) are not collected. |
| 2026-10-26 | all | Schedule changed from Tuesdays and Fridays to Wednesdays (one run per week, 02:07 Europe/Berlin). `observed_at` of a new value is at most seven days after its publication (before: three to four days). Records observed before the change keep their timestamps. |
| 2026-10-26 | `source_snapshots` | Destatis, Statistischer Bericht Sterbefälle nach Tagen, Wochen und Monaten (weekly edition, file version 175 on 2026-10-26, found on its page through the pattern of its address): sheets `csv-12613-b01` (deaths by day, Germany, 2,202 rows, every run), `csv-12613-01` (by calendar week and age group, 5,088 rows, every 28 days) and `csv-12613-11` (by month, state and age group, 6,240 rows, every 28 days), verified live. Data from 1 January 2026 are provisional and estimated for the last nine weeks; from the reporting year 2025 the Cell-Key method perturbs the counts, so sums of a table can differ from its totals (break against earlier years). |
| 2026-10-26 | `source_snapshots` | Zensus 2022 special evaluations on www.destatis.de, found on the Zensus page through the pattern of their address, verified live: religious affiliation per municipality, district, state and Germany in the Zensus 2022 and the Zensus 2011 (file `religionszugehoerigkeit_zenus2022_und_zensus2011_bundesland.xlsx`, sheets `Religion_Zensus2022` and `Religion_Zensus2011`, 10,793 rows each, header of three rows); per Bundestag constituency of 2025: religious affiliation, share of foreigners, German citizens in five age groups, marital status (299 rows each). The 2011 sheet stores the shares of Germany as fractions (0.3) and the other shares as percent text (25,1%), as the source formats them. |
| 2026-10-26 | `source_snapshots` | Destatis, special evaluation of the Strafverfolgungsstatistik (10 November 2021): persons with pre-trial detention by citizenship (German, not German) and state for 2014, 2017 and 2019 (sheets `U-Haft 2014`, `U-Haft 2017`, `U-Haft 2019`, 18 rows each), verified live. |
| 2026-10-26 | `source_snapshots` | Die Bundeswahlleiterin, structural data of the constituencies of the Bundestag elections 2017, 2021 and 2025 (CSV, 316 rows each: constituencies plus state and federal totals, 48 indicators), found on the pages of the elections through the pattern of their address, verified live. The editions differ in notation: 2021 writes constituency numbers with leading zeros (`001`) and thousands separators (`2.124,3`), 2017 and 2025 do not; the share of foreigners is named `Ausländer (%)` in 2017 and `Ausländer/-innen (%)` from 2021. |
| 2026-10-26 | `series_catalog` | Crime and criminal justice (Eurostat, Germany), verified live: `police_recorded_offences` (`crim_off_cat`, `A.{iccs}.NR.{geo}`, 20 ICCS codes; Germany reports serious assault as `ICCS020111` and the theft of motor vehicles as `ICCS05021`, not `ICCS02011` and `ICCS050211`), `criminal_justice_persons_by_citizenship` (`crim_just_ctz` with the dimensions freq, leg_stat, citizen, unit, geo; Germany reports suspected and convicted persons, `PER_SUSP` and `PER_CNV`, as `TOTAL`, `NAT` and `FOR`, without prosecuted persons and without an offence breakdown), `prison_population` (`crim_pris_cap`, `PRIS_ACT_CAP` and `PRIS_OFF_CAP`). Not added: `crim_pris_ctz` (Germany reports only the total) and `crim_gen_reg` (regional data end in 2010). |
| 2026-10-26 | `source_snapshots` | Checked and not admitted (Section 6): Polizeiliche Kriminalstatistik on www.bka.de (crawl delay 180 s), Kirchliche Statistik of the Deutsche Bischofskonferenz (HTTP 401), Kirchenmitgliederzahlen of the EKD (terms forbid further use), Statistik der Bundesagentur für Arbeit (HTTP 403, robots.txt of the data API), BAMF (robots.txt not reachable), INKAR (robots.txt not reachable), open data Ruhr and Düsseldorf (not reachable). |
| 2026-10-26 | `source_snapshots` | Fetch rules of the snapshot items: a host with a crawl delay of more than 120 seconds is not fetched; an item whose source answers with HTTP 401 or 403 or a challenge is not requested again for 30 days; HTTP 401 of a GENESIS web service counts as `no_access`. |

---

## 12. Implementation Status

| Part | Status |
|---|---|
| Statistical series of Eurostat and the ECB as vintages, with skipping of unchanged datasets | implemented |
| Series catalog, region registry and provider configuration with their datasets | implemented |
| Monthly source review with live checks, probes, table of contents, region check and terms hashes, as a pull request | implemented |
| Live verification of the keys of the catalog (source review in report mode) | implemented: all 61 entries verified on 2026-10-26 |
| Snapshots of volatile sources (`source_snapshots`, `snapshot_catalog`), collectors `files` and `genesis`, review of the snapshot items, probe of candidate sources | implemented |
| GENESIS-Online, Regionaldatenbank and Zensus database with optional free accounts (fertility by citizenship and state, Mikrozensus income classes and migration status, foreigners and persons seeking protection by district, police crime statistics, convictions, prisoners by state, unemployment by district, religion) | implemented; GENESIS-Online, Regionaldatenbank and Zensus database verified with their accounts on 2026-10-26 |
| Structural data of the Bundestag constituencies (Bundeswahlleiterin) and Zensus 2022 evaluations per constituency, to join with dataset.politics.germany | implemented |
| Religious affiliation (Zensus 2011 and 2022 by municipality, district, state and constituency) | implemented; annual church statistics excluded (Section 3.6) |
| Crime and criminal justice (Eurostat for Germany; PKS, Strafverfolgung and Strafvollzug of GENESIS-Online) | implemented; PKS tables by state and district of the BKA excluded (crawl delay) |
| Monthly labour market by district (Bundesagentur für Arbeit) | excluded (HTTP 403, robots.txt); annual unemployment by district from the Regionaldatenbank |
| Mapping of the districts to their official keys (Kreisschlüssel) and of the constituencies of every election to districts | planned |
| Population projections (EUROPOP2023, regional projections) as vintages | planned |
| Urban-rural, metropolitan and coastal typologies of the districts | planned |
| Feature tables with point-in-time joins of the vintages, also across the datasets (`scripts/build_features.py`) | planned |
