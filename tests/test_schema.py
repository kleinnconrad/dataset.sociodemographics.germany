"""Checks of the record models that keep the schema consistent over the years."""
import pytest

from conftest import FIXTURES_DIR
from schema import DATASET_MODELS, RecordBase, describe_models


@pytest.mark.parametrize("dataset", list(DATASET_MODELS))
def test_no_model_redefines_a_common_field(dataset: str) -> None:
    model = DATASET_MODELS[dataset]
    for name, field in RecordBase.model_fields.items():
        assert model.model_fields[name].annotation == field.annotation, f"{dataset}.{name} redefines a common field"
        assert model.model_fields[name].description == field.description, f"{dataset}.{name} redefines a common field"


@pytest.mark.parametrize("dataset", list(DATASET_MODELS))
def test_every_field_and_model_is_described(dataset: str) -> None:
    model = DATASET_MODELS[dataset]
    assert (model.__doc__ or "").strip(), f"{dataset} has no docstring"
    assert all(field.description for field in model.model_fields.values())


def test_dataset_names_are_snake_case() -> None:
    assert all(name.replace("_", "").isalnum() and name == name.lower() for name in DATASET_MODELS)


def test_record_schema_matches_the_snapshot() -> None:
    """Fails when a field of a record model is added, removed or retyped.

    Schema changes must be additive (CLAUDE.md, section 9). After an intended change, regenerate
    tests/fixtures/schema_snapshot.txt with describe_models and, if the meaning of a field changed,
    raise SCHEMA_VERSION.
    """
    snapshot = (FIXTURES_DIR / "schema_snapshot.txt").read_text(encoding="utf-8")
    assert describe_models([RecordBase, *DATASET_MODELS.values()], include_descriptions=False) + "\n" == snapshot
