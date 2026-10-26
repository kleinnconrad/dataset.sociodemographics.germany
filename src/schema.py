"""
Defines the Pydantic models of the German sociodemographics dataset.

Every record written to disk is a subclass of ``RecordBase``. Vocabularies are
``Literal`` aliases, so that the stored values are validated and the README
can list them.
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
                      "wealth", "housing", "labour_market", "regional_economy"]


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
    source_id: str = Field(description="Id of the source (API or configuration file).")
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


DATASET_MODELS: dict[str, type[RecordBase]] = {
    "sociodemographic_series": SociodemographicSeriesRecord,
    "series_catalog": SeriesCatalogRecord,
    "regions": RegionRecord,
}


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
    used for the schema snapshot test (all models, without descriptions).

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
