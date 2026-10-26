"""Checks that the data model diagram and the example records of the README match the schema.

The entity relationship diagram lists every field of every dataset, the README
shows one example record per dataset, and its variables table lists the active
concepts of the catalog. These tests fail when a field is added, renamed or
removed in ``schema.py``, or a concept in ``config/series.toml``, without
updating the README.
"""
import json
import re
from pathlib import Path

import pytest

from catalog import load_catalog
from schema import DATASET_MODELS, RecordBase

README = Path(__file__).resolve().parents[1] / "README.md"
COMMON_ENTITY = "common_fields"


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
