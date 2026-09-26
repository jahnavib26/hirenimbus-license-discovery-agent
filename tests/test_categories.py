from pathlib import Path

from app.categories import CategoryMapper


TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "data" / "category_taxonomy.json"


def test_supplied_taxonomy_maps_google_types_to_library_ids() -> None:
    mapper = CategoryMapper.from_json(TAXONOMY_PATH)

    assert mapper.map(
        [
            "plumber",
            "air_conditioning_repair_service",
            "general_contractor",
            "point_of_interest",
        ]
    ) == ["plumbing", "hvac", "renovation"]


def test_taxonomy_mapping_keeps_unknown_types_unmapped() -> None:
    mapper = CategoryMapper.from_json(TAXONOMY_PATH)

    assert mapper.map(["point_of_interest", "establishment", "some_other_service"]) == []
