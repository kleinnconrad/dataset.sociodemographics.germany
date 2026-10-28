"""
Defines the Pydantic models of the German sociodemographics dataset.

Every record written to disk is a subclass of ``RecordBase``. The statistical
series of the SDMX providers are stored as vintages
(``SociodemographicSeriesRecord``); sources that overwrite or remove what they
published are stored as snapshots of their published content
(``SnapshotRecord``); news articles and press releases are stored as events
with their metadata, and their Gemini labels separately
(``TextAnnotationRecord``). Vocabularies are ``Literal`` aliases, so that the
stored values are validated and the README can list them; google-genai drops
the field descriptions of ``Enum`` classes when it builds the response schema.
The models that Gemini fills (``ItemAnnotation`` and its parts) are separate
from the stored records. All their fields are required; absence is expressed
with "Not stated", "Not applicable", an empty list or null.
"""
import types
import typing
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"

# ISO 3166-2 codes of the German states, the state keys of dataset.politics.germany and dataset.economics.germany
StateCode = Literal["DE-BW", "DE-BY", "DE-BE", "DE-BB", "DE-HB", "DE-HH", "DE-HE", "DE-MV", "DE-NI", "DE-NW",
                    "DE-RP", "DE-SL", "DE-SN", "DE-ST", "DE-SH", "DE-TH"]

Provider = Literal["eurostat", "ecb"]
# W weekly, M monthly, Q quarterly, A annual
Frequency = Literal["W", "M", "Q", "A"]
# eu: an aggregate of countries (EU27_2020); country: Germany; nuts1: states; nuts2: government regions
# (Regierungsbezirke and statistical regions); nuts3: districts and district-free cities (Kreise, kreisfreie Städte)
GeoLevel = Literal["eu", "country", "nuts1", "nuts2", "nuts3"]
SeriesTopic = Literal["population_structure", "population_change", "births_and_fertility", "mortality_and_health",
                      "migration_and_citizenship", "households_and_families", "education", "income_and_poverty",
                      "wealth", "housing", "labour_market", "regional_economy", "religion", "regional_structure",
                      "crime_and_justice"]
# Formats of the published content of a snapshot source: a table of a GENESIS database (flat CSV), a CSV, xlsx or
# JSON file, a table of an HTML page, or a PDF document that is archived without rows
SnapshotFormat = Literal["genesis", "csv", "xlsx", "json", "html", "pdf"]
# Region codes used in the content of a snapshot: NUTS codes, official keys of states, districts and municipalities
# (Amtlicher Gemeindeschlüssel or Regionalschlüssel), numbers of the Bundestag constituencies, names of the
# states, dioceses, regional churches of the EKD, or none (Germany only)
GeoScheme = Literal["nuts", "ags", "constituency", "state_name", "diocese", "regional_church", "none"]
# Level of an issuer of press releases: Germany, a state, the European Union or an international organization
Jurisdiction = Literal["DE", StateCode, "EU", "INT"]

# ---------------------------------------------------------------------------
# News, press releases and their annotation
# ---------------------------------------------------------------------------

OutletType = Literal["public_broadcaster_national", "public_broadcaster_regional", "national_daily",
                     "weekly_magazine", "regional_daily", "tabloid", "online_only", "specialist_press"]
PaywallModel = Literal["free", "mixed", "hard"]
ActorType = Literal["federal_government", "federal_ministry", "parliament", "state_government", "statistical_office",
                    "public_agency", "eu_institution", "international_organization", "research_institute",
                    "welfare_association", "religious_community", "civil_society_organization", "trade_union",
                    "municipal_association"]
ContentBasis = Literal["title_only", "title_teaser", "lead_text"]
Channel = Literal["rss", "news_sitemap"]
ItemDataset = Literal["news_items", "press_releases"]

# Topics of the press coverage. project_architecture.md maps them to the topic groups of the series (SeriesTopic).
SocialTopic = Literal["Population and demographic change", "Births and fertility", "Mortality and life expectancy",
                      "Health and care", "Migration and asylum", "Integration and citizenship",
                      "Families and households", "Children and youth", "Older people and ageing",
                      "Gender and equality", "Education and schools", "Higher education and vocational training",
                      "Income, poverty and social benefits", "Wealth and inequality", "Pensions", "Housing and rents",
                      "Labour market and employment", "Religion and churches", "Crime and justice",
                      "Regional disparities and rural areas", "Other"]
# Groups of the population; the annotation labels groups, never single persons
PopulationGroup = Literal["Children", "Young people", "Students", "Families", "Single parents", "Women", "Men",
                          "Older people", "Pensioners", "Persons in need of care", "Persons with disabilities",
                          "Migrants", "Refugees and asylum seekers", "Foreign nationals", "Naturalised citizens",
                          "Ethnic minorities", "Religious minorities", "LGBTQ people", "Low-income households",
                          "Recipients of social benefits", "Unemployed persons", "Employees", "Homeless people",
                          "Tenants", "Rural population", "Urban population", "Victims of crime",
                          "Suspects and offenders", "Other"]
SocialIndicator = Literal["Population size", "Births", "Fertility rate", "Deaths", "Life expectancy", "Immigration",
                          "Emigration", "Net migration", "Asylum applications", "Persons seeking protection",
                          "Deportations", "Naturalisations", "Foreign population", "Marriages", "Divorces",
                          "Households", "Childcare places", "School leavers", "Students", "Teachers",
                          "Educational attainment", "Poverty", "Income", "Inequality", "Wealth",
                          "Recipients of social benefits", "Pensions", "Rents", "House prices",
                          "Housing construction", "Homelessness", "Unemployment", "Employment",
                          "Shortage of skilled workers", "Church membership", "Church leavings", "Crime", "Prisoners",
                          "Persons in need of care", "Illness", "Other"]
Direction = Literal["Rising", "Falling", "Unchanged", "Not stated"]
# Institutions by function, so that the values stay valid when ministries are renamed or merged
Institution = Literal["Federal government", "Federal ministry of the interior",
                      "Federal ministry of family, education or youth", "Federal ministry of labour and social affairs",
                      "Federal ministry of health", "Federal ministry of housing", "Federal ministry of justice",
                      "Other federal ministry", "Bundestag or Bundesrat", "Federal Constitutional Court",
                      "Other court", "Federal Statistical Office", "State statistical office",
                      "Federal Office for Migration and Refugees", "Federal Employment Agency",
                      "Federal Criminal Police Office", "Police", "State government", "Municipality",
                      "Eurostat", "European Commission", "Other EU institution", "UN agency", "OECD",
                      "Research institute", "Welfare association", "Church or religious community", "Trade union",
                      "Employers' association", "Political party", "Other"]
EventType = Literal["Statistical release", "Study or report", "Survey or poll", "Legislation or regulation",
                    "Government decision", "Court ruling", "Budget or funding decision", "Benefit change",
                    "Policy proposal or demand", "Parliamentary debate or vote", "Election or campaign",
                    "Protest or demonstration", "Strike or labour dispute", "Crime or violent incident",
                    "Accident or disaster", "Arrival or reception of refugees", "Deportation", "Border control",
                    "Epidemic or health crisis", "Church event or decision", "Commemoration or anniversary", "Other"]
EventStatus = Literal["Announced or planned", "Ongoing", "Concluded", "Not applicable"]
PolicyLevel = Literal["Municipal", "State", "Federal", "European Union", "International", "Not applicable"]
ArticleType = Literal["News", "Opinion", "Interview", "Analysis", "Live blog", "Explainer", "Press release",
                      "Other"]
Tone = Literal["Positive", "Neutral", "Negative", "Mixed"]
# Generic news frames of Semetko and Valkenburg (2000), as in dataset.politics.germany and dataset.economics.germany
Frame = Literal["Conflict", "Economic consequences", "Responsibility", "Human interest", "Morality",
                "No dominant frame"]
CommunicationType = Literal["Statistical release", "Study or report", "Policy proposal or position",
                            "Legislation or government decision", "Funding or programme", "Statement or reaction",
                            "Event or service information", "Personnel", "Other", "Not applicable"]


# ---------------------------------------------------------------------------
# Models filled by Gemini
# ---------------------------------------------------------------------------
# Long vocabularies are plain strings in these models, with the allowed values in the description:
# a response schema with many enumerations exceeds the limit of constrained decoding of the Gemini API.
# The annotation keeps only values of the vocabulary (annotation.clean_annotation); the stored
# records validate them against the Literal types.

def allowed(vocabulary: object) -> str:
    """Returns the values of a Literal vocabulary as text for a field description.

    Args:
        vocabulary: A ``Literal`` alias.

    Returns:
        str: For example ``Allowed values: Positive; Neutral; Negative; Mixed.``
    """
    return "Allowed values: " + "; ".join(str(value) for value in typing.get_args(vocabulary)) + "."


class GroupMention(BaseModel):
    """A group of the population that the text is about, as filled by the model."""
    group: str = Field(description="The group. Only groups, never single persons. " + allowed(PopulationGroup))
    tone: Tone = Field(description="How the text presents this group.")


class IndicatorMention(BaseModel):
    """A sociodemographic indicator whose value or development the text reports."""
    indicator: str = Field(description="The indicator. " + allowed(SocialIndicator))
    direction: Direction = Field(description="Development of the indicator as reported in the text.")


class UpcomingEvent(BaseModel):
    """A future event that the text names with a date."""
    event_type: str = Field(description="Type of the upcoming event. " + allowed(EventType))
    date: str = Field(description="Date of the event (YYYY-MM-DD). Only events with a stated day.")


class ItemAnnotation(BaseModel):
    """The labels of one article or press release."""
    item_number: int = Field(description="Number of the item in the request, starting at 1.")
    is_sociodemographic: bool = Field(description="True if the item is about the population of Germany, its structure or its living conditions: population and demographic change, births, deaths and health, migration and integration, families and households, children, youth and older people, education, income, poverty and wealth, social benefits and pensions, housing, employment, religion, and crime and justice as social phenomena. False for reports on a single crime, accident or court case about individuals.")
    topic: SocialTopic = Field(description="Main topic of the item. 'Other' if the item is not sociodemographic.")
    topics_secondary: list[str] = Field(description="Further clearly addressed topics, at most two, with the values of topic. Empty list if none.")
    issue_detail: Optional[str] = Field(description="The specific issue in one to five words, e.g. 'Kita places' or 'Deportations to Afghanistan'. Null if not sociodemographic.")
    article_type: ArticleType = Field(description="Form of the item, judged from the title and text, e.g. 'Opinion' for commentaries, 'Interview' for interviews.")
    population_groups: list[GroupMention] = Field(description="Groups of the population the item is mainly about, each once, at most five. Empty list if the item concerns no particular group.")
    indicators: list[IndicatorMention] = Field(description="Sociodemographic indicators whose current value or development the item reports, each once.")
    reports_statistics: bool = Field(description="True if the item reports figures of an official statistic, a register, a census or a survey, e.g. of the Statistisches Bundesamt, a state statistical office, Eurostat, the BAMF, the Bundesagentur für Arbeit or the police crime statistics.")
    institutions: list[str] = Field(description="Public institutions and organizations that act, decide, publish or comment in the item, each once. " + allowed(Institution))
    countries: list[str] = Field(description="Countries the item is mainly about, as ISO 3166-1 alpha-2 codes, e.g. DE, SY, UA; for migration also the countries of origin. Empty list if no country is concerned.")
    states_concerned: list[str] = Field(description="German states the item is mainly about, as ISO 3166-2 codes (DE-BW, DE-BY, DE-BE, DE-BB, DE-HB, DE-HH, DE-HE, DE-MV, DE-NI, DE-NW, DE-RP, DE-SL, DE-SN, DE-ST, DE-SH, DE-TH). Empty list for national or international items.")
    event_types: list[str] = Field(description="Types of events the item reports. Empty list if none. " + allowed(EventType))
    event_status: EventStatus = Field(description="Whether the main event is announced or planned, ongoing or concluded. 'Not applicable' without an event.")
    overall_tone: Tone = Field(description="Overall tone of the item.")
    mentions_demographic_change: bool = Field(description="True if the text refers to demographic change: ageing, a shrinking or growing population, low birth rates, or migration as a driver of the population (e.g. 'demografischer Wandel', 'Überalterung', 'Geburtenrückgang').")
    mentions_inequality: bool = Field(description="True if the text refers to social inequality or a social divide (e.g. 'soziale Ungleichheit', 'Schere zwischen Arm und Reich', 'Chancengleichheit', 'soziale Spaltung').")
    policy_level: PolicyLevel = Field(description="Main level of policy the item addresses. 'Not applicable' if no policy is addressed.")
    government_involved: bool = Field(description="True if the federal government, a state government or one of their members acts or is addressed.")
    frame: Frame = Field(description="Dominant frame of the item.")
    upcoming_events: list[UpcomingEvent] = Field(description="Future events named with a date (statistical releases, laws taking effect, benefit changes, court hearings, elections, deadlines, school or semester start). Empty list if none.")
    communication_type: CommunicationType = Field(description="For press releases: the main purpose of the release. 'Not applicable' for news articles.")


class AnnotationBatch(BaseModel):
    """The model response for a batch of items."""
    items: list[ItemAnnotation] = Field(description="One entry per item of the request, in the order of the request.")


# ---------------------------------------------------------------------------
# Stored records
# ---------------------------------------------------------------------------

class RecordBase(BaseModel):
    """Fields that every record of the dataset has."""
    model_config = ConfigDict(extra="forbid")

    record_id: str = Field(description="Stable id of the item. Together with revision it identifies a record.")
    revision: int = Field(default=0, description="0 for the first observation; incremented each time the source changes the item.")
    dataset: str = Field(description="Name of the dataset.")
    schema_version: str = Field(default=SCHEMA_VERSION, description="Version of the record schema.")
    observed_at: datetime = Field(description="UTC time at which the pipeline observed this version of the item. Filtering observed_at <= t gives the state of knowledge at time t.")
    source_id: str = Field(description="Id of the source (API, configuration file, outlet or press source).")
    source_url: Optional[str] = Field(description="URL of the item or of the API resource it was read from.")
    license: str = Field(description="License or terms of the source.")
    backfilled: bool = Field(description="True if the item is older than the start of collection for its dataset, so that observed_at does not reflect when it became public.")


class SeriesCatalogRecord(RecordBase):
    """An entry of the series catalog, recorded when it appears or changes."""
    concept: str = Field(description="Stable id of the entry: what its series measure, e.g. 'population_by_age_group'. The first part of every series_id of the entry.")
    name: str = Field(description="Name of the concept.")
    topic: SeriesTopic = Field(description="Topic group of the concept.")
    frequency: Frequency = Field(description="Frequency of the periods: W weekly, M monthly, Q quarterly, A annual.")
    unit: str = Field(description="Unit of the values, e.g. 'persons on 1 January'.")
    provider: Provider = Field(description="Organization and interface the series are read from.")
    provider_flow: str = Field(description="Dataset or dataflow of the provider, e.g. 'demo_r_pjangrp3'.")
    key_template: str = Field(description="SDMX key of the series with the placeholders {geo} and one placeholder per breakdown dimension, e.g. 'A.NR.{sex}.{age}.{geo}'.")
    geo_levels: list[GeoLevel] = Field(description="Levels of the German regions the entry is collected for.")
    extra_geos: list[str] = Field(description="Further areas the entry is collected for, e.g. EU27_2020 for comparisons.")
    breakdown: dict[str, list[str]] = Field(description="Codes of the provider per breakdown dimension, e.g. {'sex': ['M', 'F']}; every combination is a series.")
    added: date = Field(description="Day on which the entry was added to the catalog; start of collection.")
    removed: Optional[date] = Field(description="Day on which the entry was removed from the catalog; end of collection.")
    removed_reason: Optional[str] = Field(description="Why the entry was removed.")


class SociodemographicSeriesRecord(RecordBase):
    """A vintage of a statistical series: all values at the first observation, then the added, revised and removed periods."""
    series_id: str = Field(description="Stable id of the series: concept, geo and the breakdown codes, separated by dots, e.g. 'population_by_age_group.DE212.F.Y20-24'.")
    concept: str = Field(description="Catalog entry of the series.")
    topic: SeriesTopic = Field(description="Topic group of the series.")
    geo: str = Field(description="Region or area: NUTS code of the German region (DE, DE2, DE21, DE212) or an aggregate such as EU27_2020.")
    geo_level: GeoLevel = Field(description="Level of the region: eu, country, nuts1 (state), nuts2 (government region) or nuts3 (district).")
    state: Optional[StateCode] = Field(description="ISO 3166-2 code of the state that contains the region; null for Germany and aggregates.")
    breakdown: dict[str, str] = Field(description="Code of the provider per breakdown dimension, e.g. {'sex': 'F', 'age': 'Y20-24'}; empty for a series without breakdown.")
    frequency: Frequency = Field(description="Frequency of the periods.")
    unit: str = Field(description="Unit of the values.")
    provider: Provider = Field(description="Organization and interface the series is read from.")
    provider_flow: str = Field(description="Dataset or dataflow of the provider.")
    provider_key: str = Field(description="SDMX key of the series at the provider.")
    values: dict[str, float] = Field(description="Values by period (YYYY-Www, YYYY-MM, YYYY-Qn or YYYY). Revision 0 holds all periods of the series; a later revision only the periods that were added or whose value changed.")
    removed_periods: list[str] = Field(description="Periods whose value disappeared at the provider since the previous revision.")
    first_period: Optional[str] = Field(description="First period of the complete series after this revision.")
    last_period: Optional[str] = Field(description="Last period of the complete series after this revision.")
    observations: int = Field(description="Number of periods of the complete series after this revision.")
    source_updated: Optional[datetime] = Field(description="Last update of the dataset at the provider (UTC), if the provider states it.")


class RegionRecord(RecordBase):
    """An entry of the region registry, recorded when it appears or changes."""
    geo: str = Field(description="NUTS code of the region, e.g. 'DE212'; the join key geo of sociodemographic_series.")
    name: str = Field(description="Name of the region in the NUTS classification.")
    level: GeoLevel = Field(description="Level of the region: country, nuts1 (state), nuts2 or nuts3 (district).")
    parent: Optional[str] = Field(description="NUTS code of the region one level above; null for Germany.")
    state: Optional[StateCode] = Field(description="ISO 3166-2 code of the state that contains the region; null for Germany.")
    nuts_versions: list[str] = Field(description="Versions of the NUTS classification that contain the code with this territory, e.g. ['2021', '2024'].")
    added: date = Field(description="Day on which the region was added to the registry.")
    removed: Optional[date] = Field(description="Day on which the region was removed from the registry.")
    removed_reason: Optional[str] = Field(description="Why the region was removed.")


class SnapshotRecord(RecordBase):
    """The content that a source without a reliable history published at one observation, stored again only when it changes."""
    snapshot_id: str = Field(description="Stable id of the published item: provider and item, e.g. 'zensus2022.religion_wahlkreise' or 'destatis.12612-0002'.")
    name: str = Field(description="Name of the item in the snapshot configuration.")
    topic: SeriesTopic = Field(description="Topic group of the item.")
    provider: str = Field(description="Provider id of config/providers.toml.")
    item: str = Field(description="Id of the item at the provider: the table code, or the name of the fetched file.")
    format: SnapshotFormat = Field(description="Format of the published content: genesis, csv, xlsx, json, html or pdf.")
    geo_scheme: GeoScheme = Field(description="Region codes used in the content: nuts, ags (official keys of states, districts and municipalities), constituency (numbers of the Bundestag constituencies), state_name (names of the states), diocese, regional_church or none (Germany only).")
    page_url: Optional[str] = Field(description="Page from which the address of the published file was read; null for a fixed address or an API.")
    source_title: Optional[str] = Field(description="Title of the item as the source states it, e.g. the title of a GENESIS table or the file name.")
    source_updated: Optional[datetime] = Field(description="Last change of the item as the source states it (UTC), e.g. the Last-Modified header; null if not stated.")
    content_hash: str = Field(description="Hash of the columns and rows (or of the file of an archived document). A record is written only when it differs from the previous observation.")
    previous_hash: Optional[str] = Field(description="content_hash of the previous record of the item; null for the first observation.")
    columns: list[str] = Field(description="Column names of the rows. For GENESIS tables: time and the codes of the variables and their attributes, value, value_unit, value_variable_code.")
    rows: list[list[Optional[str]]] = Field(description="The published rows as text, exactly as the source wrote the cells (numbers keep their notation, e.g. decimal commas); null for an empty cell. Empty for an archived document.")
    row_count: int = Field(description="Number of rows.")
    labels: dict[str, str] = Field(description="Labels of the codes in the rows, e.g. {'KREISE:08222': 'Mannheim, Stadtkreis'} for GENESIS tables; empty if the rows hold the labels.")
    archived_file: Optional[str] = Field(description="Path of the published file in this repository (data/files/...) if the file itself is archived, e.g. a PDF document.")
    byte_size: int = Field(description="Size of the published file in bytes.")


class SnapshotCatalogRecord(RecordBase):
    """An entry of the snapshot configuration, recorded when it appears or changes."""
    snapshot_id: str = Field(description="Stable id of the item; the join key of source_snapshots.")
    name: str = Field(description="Name of the item.")
    topic: SeriesTopic = Field(description="Topic group of the item.")
    provider: str = Field(description="Provider id of config/providers.toml.")
    format: SnapshotFormat = Field(description="Format of the published content.")
    address: str = Field(description="Where the item is read: the GENESIS database and table, the fixed file address, or the page and the pattern of the file link.")
    geo_scheme: GeoScheme = Field(description="Region codes used in the content.")
    archive: bool = Field(description="Whether the published file itself is archived in data/files/.")
    added: date = Field(description="Day on which the item was added to the configuration; start of collection.")
    removed: Optional[date] = Field(description="Day on which the item was removed from the configuration; end of collection.")
    removed_reason: Optional[str] = Field(description="Why the item was removed.")


class NewsItemRecord(RecordBase):
    """Metadata of one news article. The text is not stored."""
    outlet_id: str = Field(description="Id of the outlet in config/sources.toml.")
    outlet_type: OutletType = Field(description="Type of the outlet.")
    owner_group: str = Field(description="Publisher group of the outlet, or 'public' for public broadcasters.")
    home_state: Optional[StateCode] = Field(description="State of a regional outlet; null for national outlets.")
    outlet_topic: Optional[SocialTopic] = Field(description="Topic of a specialist outlet, e.g. 'Migration and asylum'; null for general outlets.")
    channel: Channel = Field(description="Where the article was found: RSS/Atom feed or news sitemap.")
    url: str = Field(description="URL of the article.")
    canonical_url: Optional[str] = Field(description="Canonical URL declared by the article page.")
    title: str = Field(description="Title of the article.")
    published_at: Optional[datetime] = Field(description="Publication time (UTC).")
    updated_at: Optional[datetime] = Field(description="Last modification time from the article page (UTC).")
    section: Optional[str] = Field(description="Section of the outlet, from the page or the feed.")
    feed_categories: list[str] = Field(description="Categories of the item in the feed.")
    keywords: list[str] = Field(description="Keywords from the article page or the news sitemap.")
    jsonld_type: Optional[str] = Field(description="schema.org type of the article page, e.g. NewsArticle or OpinionNewsArticle.")
    is_agency_copy: Optional[bool] = Field(description="True if the article is credited to a news agency (dpa, AFP, Reuters, epd, KNA, AP). Null without page data.")
    author_count: Optional[int] = Field(description="Number of named authors. Author names are not stored. Null without page data.")
    word_count: Optional[int] = Field(description="Word count from the article page. Null if not given.")
    is_paywalled: Optional[bool] = Field(description="True if the page marks the article as not accessible for free. Null without page data.")
    has_video: Optional[bool] = Field(description="True if the article page embeds a video. Null without page data.")
    content_basis: ContentBasis = Field(description="Text available for the annotation: title only, title and teaser, or lead text of the page.")


class PressReleaseRecord(RecordBase):
    """Metadata of one press release. The text is not stored."""
    press_source_id: str = Field(description="Id of the press source in config/sources.toml.")
    actor_type: ActorType = Field(description="Type of the issuing actor.")
    jurisdiction: Jurisdiction = Field(description="Level of the issuer: DE, a state (ISO 3166-2), EU or INT.")
    url: str = Field(description="URL of the press release.")
    canonical_url: Optional[str] = Field(description="Canonical URL declared by the page.")
    title: str = Field(description="Title of the press release.")
    published_at: Optional[datetime] = Field(description="Publication time (UTC).")
    feed_categories: list[str] = Field(description="Categories of the item in the feed.")
    content_basis: ContentBasis = Field(description="Text available for the annotation.")


class TextAnnotationRecord(RecordBase):
    """Gemini labels of one news article or press release."""
    item_dataset: ItemDataset = Field(description="Dataset of the annotated item.")
    item_id: str = Field(description="record_id of the annotated item.")
    model: str = Field(description="Gemini model that produced the labels.")
    extraction_hash: str = Field(description="Hash of the prompt, response schema and model settings. Labels with the same hash were produced under the same conditions.")
    content_basis: ContentBasis = Field(description="Text the model saw: title only, title and teaser, or lead text.")
    is_sociodemographic: bool = Field(description="Whether the item is about the population, its structure or its living conditions.")
    topic: SocialTopic = Field(description="Main topic.")
    topics_secondary: list[SocialTopic] = Field(description="Further topics, at most two.")
    issue_detail: Optional[str] = Field(description="Specific issue in a few words.")
    article_type: ArticleType = Field(description="Form of the item.")
    population_groups: list[PopulationGroup] = Field(description="Groups of the population the item is mainly about.")
    group_tone: dict[PopulationGroup, Tone] = Field(description="How the item presents each of its population groups.")
    indicators: dict[SocialIndicator, Direction] = Field(description="Reported indicators with their reported direction.")
    reports_statistics: bool = Field(description="Whether the item reports figures of an official statistic, a register, a census or a survey.")
    institutions: list[Institution] = Field(description="Institutions and organizations that act, decide, publish or comment.")
    countries: list[str] = Field(description="Countries the item is mainly about (ISO 3166-1 alpha-2), for migration also the countries of origin.")
    states_concerned: list[StateCode] = Field(description="German states the item is mainly about.")
    event_types: list[EventType] = Field(description="Types of events reported.")
    event_status: EventStatus = Field(description="Stage of the main event.")
    overall_tone: Tone = Field(description="Overall tone of the item.")
    mentions_demographic_change: bool = Field(description="Whether the item refers to demographic change (ageing, shrinking or growing population, low birth rates).")
    mentions_inequality: bool = Field(description="Whether the item refers to social inequality or a social divide.")
    policy_level: PolicyLevel = Field(description="Main level of policy addressed.")
    government_involved: bool = Field(description="Whether a government or its members act or are addressed.")
    frame: Frame = Field(description="Dominant news frame (Semetko and Valkenburg).")
    upcoming_events: list[dict[str, str]] = Field(description="Future events with event_type and date (YYYY-MM-DD).")
    communication_type: CommunicationType = Field(description="Purpose of a press release; 'Not applicable' for news.")


class PressCatalogRecord(RecordBase):
    """An entry of the press configuration (news outlets and issuers of press releases), recorded when it appears or changes."""
    press_id: str = Field(description="Stable id of the outlet or press source; the outlet_id of news_items and the press_source_id of press_releases.")
    kind: Literal["outlet", "press_source"] = Field(description="outlet for a news outlet, press_source for an issuer of press releases.")
    name: str = Field(description="Name of the outlet or issuer.")
    homepage: str = Field(description="Homepage URL.")
    outlet_type: Optional[OutletType] = Field(description="Type of an outlet; null for press sources.")
    owner_group: Optional[str] = Field(description="Publisher group of an outlet, or 'public'; null for press sources.")
    home_state: Optional[StateCode] = Field(description="State of a regional outlet; null otherwise.")
    outlet_topic: Optional[SocialTopic] = Field(description="Topic of a specialist outlet; null otherwise.")
    paywall_model: Optional[PaywallModel] = Field(description="Paywall model of an outlet (free, mixed, hard); null for press sources.")
    actor_type: Optional[ActorType] = Field(description="Type of the issuer of a press source; null for outlets.")
    jurisdiction: Optional[Jurisdiction] = Field(description="Level of the issuer of a press source (DE, a state, EU, INT); null for outlets.")
    feeds: list[str] = Field(description="RSS or Atom feeds that are read.")
    news_sitemap: Optional[str] = Field(description="News sitemap of an outlet that is read; null if none.")
    topic_filter: bool = Field(description="True if items need a sociodemographic section or term to be stored; false if every item of the source is stored.")
    fetch_pages: bool = Field(description="Whether article pages are fetched for metadata and lead text.")
    added: date = Field(description="Day on which the source was added to the configuration; start of collection.")
    removed: Optional[date] = Field(description="Day on which the source was removed from the configuration; end of collection.")
    removed_reason: Optional[str] = Field(description="Why the source was removed.")


DATASET_MODELS: dict[str, type[RecordBase]] = {
    "sociodemographic_series": SociodemographicSeriesRecord,
    "series_catalog": SeriesCatalogRecord,
    "regions": RegionRecord,
    "source_snapshots": SnapshotRecord,
    "snapshot_catalog": SnapshotCatalogRecord,
    "news_items": NewsItemRecord,
    "press_releases": PressReleaseRecord,
    "text_annotations": TextAnnotationRecord,
    "press_catalog": PressCatalogRecord,
}

# Models whose text form goes into the extraction hash
ANNOTATION_MODELS: tuple[type[BaseModel], ...] = (
    GroupMention, IndicatorMention, UpcomingEvent, ItemAnnotation, AnnotationBatch,
)


def _describe_type(annotation: object) -> str:
    """Returns a text form of a type annotation that is the same on all supported Python versions.

    Args:
        annotation: A type annotation of a model field.

    Returns:
        str: For example ``list[str]`` or ``Union[NoneType,str]``.
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is Literal:
        return "Literal[" + ",".join(repr(arg) for arg in args) + "]"
    if origin is typing.Union or origin is types.UnionType:
        return "Union[" + ",".join(sorted(_describe_type(arg) for arg in args)) + "]"
    if origin is list:
        return "list[" + _describe_type(args[0]) + "]"
    if origin is dict:
        return "dict[" + _describe_type(args[0]) + "," + _describe_type(args[1]) + "]"
    if isinstance(annotation, type):
        return annotation.__name__
    return repr(annotation)


def describe_models(models: typing.Iterable[type[BaseModel]], include_descriptions: bool) -> str:
    """Returns a canonical text form of models.

    The text lists every field with its type and whether it is required. It is
    used for the extraction hash (annotation models, with descriptions) and for
    the schema snapshot test (all models, without descriptions).

    Args:
        models: The models to describe.
        include_descriptions: Whether to include the field descriptions.

    Returns:
        str: One line per model and per field.
    """
    lines = []
    for model in models:
        lines.append(model.__name__)
        for name, field in model.model_fields.items():
            line = f"  {name}: {_describe_type(field.annotation)} required={field.is_required()}"
            if include_descriptions:
                line += f" description={field.description!r}"
            lines.append(line)
    return "\n".join(lines)
