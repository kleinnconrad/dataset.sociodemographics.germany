"""Checks that the data model diagram, the example records and the variable tables of the README match the schema.

The entity relationship diagram lists every field of every dataset, the README
shows one example record per dataset, its variables table lists the active
concepts of the catalog, and its variable overview names fields of the
datasets and the topics of the series and the snapshots. These tests fail when
a field is added, renamed or removed in ``schema.py``, or a concept in
``config/series.toml`` or an item in ``config/snapshots.toml``, without
updating the README.
"""
import json
import re
from pathlib import Path
from typing import get_args

import pytest

from catalog import load_catalog
from schema import DATASET_MODELS, RecordBase, SeriesTopic
from snapshots import active_snapshots

README = Path(__file__).resolve().parents[1] / "README.md"
COMMON_ENTITY = "common_fields"
# Text of the variable cell of the rows of the variable overview that stand for the series or snapshots of a topic
TOPIC_ROWS = {"sociodemographic_series": "Series of topic", "source_snapshots": "Snapshot tables of topic"}


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def diagram_entities(text: str) -> dict[str, list[str]]:
    """Returns the attribute names of every entity of the erDiagram in the README.

    Args:
        text: The README.

    Returns:
        dict[str, list[str]]: Attribute names in diagram order, by entity name.
    """
    match = re.search(r"```mermaid\s*\nerDiagram\n(.*?)```", text, flags=re.DOTALL)
    assert match, "README has no erDiagram"
    entities: dict[str, list[str]] = {}
    for name, body in re.findall(r"^\s*(\w+)\s*\{\n(.*?)^\s*\}", match.group(1), flags=re.DOTALL | re.MULTILINE):
        entities[name] = [line.split()[1] for line in body.strip().splitlines() if line.strip()]
    return entities


def example_records(text: str) -> dict[str, list[dict]]:
    """Returns the example records of the README by dataset.

    Args:
        text: The README.

    Returns:
        dict[str, list[dict]]: Parsed JSON examples by dataset name.
    """
    section = text.split("## Example Records", 1)[1].split("\n## ", 1)[0]
    examples: dict[str, list[dict]] = {}
    for dataset, body in re.findall(r"<summary><code>(\w+)</code>.*?</summary>\s*```json\n(.*?)\n```", section,
                                    flags=re.DOTALL):
        examples.setdefault(dataset, []).append(json.loads(body))
    return examples


def test_common_fields_entity_lists_the_fields_of_every_record() -> None:
    entities = diagram_entities(_readme())
    assert entities[COMMON_ENTITY] == list(RecordBase.model_fields)


@pytest.mark.parametrize("dataset", list(DATASET_MODELS))
def test_dataset_entity_lists_all_fields_of_its_model(dataset: str) -> None:
    entities = diagram_entities(_readme())
    expected = [name for name in DATASET_MODELS[dataset].model_fields if name not in RecordBase.model_fields]
    assert sorted(entities.get(dataset, [])) == sorted(expected)


def test_every_diagram_attribute_has_a_comment() -> None:
    match = re.search(r"```mermaid\s*\nerDiagram\n(.*?)```", _readme(), flags=re.DOTALL)
    assert match
    attribute_lines = [line.strip() for line in match.group(1).splitlines()
                       if line.strip() and not line.strip().endswith(("{", "}")) and "--" not in line
                       and ".." not in line]
    assert attribute_lines
    assert all(re.search(r'"[^"]+"$', line) for line in attribute_lines), \
        [line for line in attribute_lines if not re.search(r'"[^"]+"$', line)]


@pytest.mark.parametrize("dataset", list(DATASET_MODELS))
def test_example_record_validates_against_its_model(dataset: str) -> None:
    examples = example_records(_readme())
    assert len(examples.get(dataset, [])) == 1, f"README needs exactly one example of {dataset}"
    DATASET_MODELS[dataset].model_validate(examples[dataset][0])


def test_variables_table_lists_every_active_concept() -> None:
    section = _readme().split("## Variables", 1)[1].split("\n## ", 1)[0]
    listed = re.findall(r"^\| `(\w+)` \|", section, flags=re.MULTILINE)
    active = [entry.concept for entry in load_catalog() if entry.active]
    assert listed == active


def variable_overview_rows(text: str) -> list[tuple[str, str]]:
    """Returns the variable and the dataset of every row of the variable overview of the README.

    Args:
        text: The README.

    Returns:
        list[tuple[str, str]]: Text of the variable cell and dataset name, in table order.
    """
    section = text.split("## Variable Overview", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^\| (.+?) \| `(\w+)` \|", section, flags=re.MULTILINE)


def overview_topics(text: str, dataset: str) -> list[str]:
    """Returns the topics of the topic rows of a dataset in the variable overview of the README.

    Args:
        text: The README.
        dataset: ``sociodemographic_series`` or ``source_snapshots``.

    Returns:
        list[str]: Topics in table order.
    """
    prefix = TOPIC_ROWS[dataset]
    topics = []
    for variable, row_dataset in variable_overview_rows(text):
        if variable.startswith(prefix):
            match = re.fullmatch(rf"{prefix} `(\w+)`", variable)
            assert match and row_dataset == dataset, (variable, row_dataset)
            topics.append(match.group(1))
    return topics


def test_variable_overview_names_content_fields_of_the_datasets() -> None:
    rows = [(variable, dataset) for variable, dataset in variable_overview_rows(_readme())
            if not variable.startswith(tuple(TOPIC_ROWS.values()))]
    assert rows
    for variable, dataset in rows:
        name = variable.strip("`")
        assert name in DATASET_MODELS[dataset].model_fields, (variable, dataset)
        assert name not in RecordBase.model_fields, (variable, dataset)


def test_variable_overview_lists_every_series_topic() -> None:
    active = {entry.topic for entry in load_catalog() if entry.active}
    expected = [topic for topic in get_args(SeriesTopic) if topic in active]
    assert overview_topics(_readme(), "sociodemographic_series") == expected


def test_variable_overview_lists_every_snapshot_topic() -> None:
    active = {source.topic for source in active_snapshots()}
    expected = [topic for topic in get_args(SeriesTopic) if topic in active]
    assert overview_topics(_readme(), "source_snapshots") == expected
