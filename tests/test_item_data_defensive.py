"""Tests for item_data.defensive_items — SR/consumable filtering.

Stubs the in-memory catalog so no network / Data Dragon is required.
"""
import item_data


_STUB = [
    {"id": 3075, "name": "Thornmail", "image": "a.png", "gold": 2700,
     "tags": ["Armor"], "armor": 70, "mr": 0, "hp": 0, "depth": 3, "sr": True},
    {"id": 3110, "name": "Frozen Heart", "image": "b.png", "gold": 2500,
     "tags": ["Armor"], "armor": 70, "mr": 0, "hp": 0, "depth": 2, "sr": True},
    # Arena-only variant: high armor but not Summoner's Rift -> must be excluded.
    {"id": 663058, "name": "Shield of Molten Stone", "image": "c.png",
     "gold": 2500, "tags": ["Armor"], "armor": 200, "mr": 0, "hp": 300,
     "depth": 0, "sr": False},
    # Cheap consumable with an armor stat -> excluded regardless of price gate.
    {"id": 2033, "name": "Refillable Potion", "image": "d.png", "gold": 150,
     "tags": ["Consumable", "Armor"], "armor": 30, "mr": 0, "hp": 0,
     "depth": 0, "sr": True},
    {"id": 3065, "name": "Spirit Visage", "image": "e.png", "gold": 2900,
     "tags": ["SpellBlock"], "armor": 0, "mr": 55, "hp": 0, "depth": 2,
     "sr": True},
]


def _with_stub(fn):
    saved = item_data._ITEM_CATALOG
    was_set = item_data._catalog_loaded.is_set()
    item_data._ITEM_CATALOG = list(_STUB)
    item_data._catalog_loaded.set()
    try:
        return fn()
    finally:
        item_data._ITEM_CATALOG = saved
        if not was_set:
            item_data._catalog_loaded.clear()


def test_defensive_items_excludes_arena_items():
    names = _with_stub(lambda: [i["name"] for i in item_data.defensive_items("armor", 800)])
    assert "Shield of Molten Stone" not in names
    assert names[0] in ("Thornmail", "Frozen Heart")


def test_defensive_items_excludes_consumables():
    names = _with_stub(lambda: [i["name"] for i in item_data.defensive_items("armor", 100)])
    assert "Refillable Potion" not in names


def test_defensive_items_sorted_by_resist_value():
    items = _with_stub(lambda: item_data.defensive_items("armor", 800))
    values = [i["value"] for i in items]
    assert values == sorted(values, reverse=True)


def test_defensive_items_mr_kind():
    names = _with_stub(lambda: [i["name"] for i in item_data.defensive_items("mr", 800)])
    assert names == ["Spirit Visage"]
