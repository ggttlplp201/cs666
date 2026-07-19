from pathlib import Path

from system_a.collections import load_collection_map

MAP = Path(__file__).resolve().parents[1] / "config" / "trade_up_collections.yaml"


def test_verified_reds_map_to_gold_type():
    cmap = load_collection_map(MAP, verified_only=True)
    # MP9 Starlight (Dreams & Nightmares) → knife; MP7 Bloodsport (Clutch) → glove
    assert cmap.gold_type_for("MP9 | Starlight Protector (Field-Tested)") == "knife"
    assert cmap.gold_type_for("MP7 | Bloodsport (Factory New)") == "glove"
    assert cmap.is_gold_case_covert("AK-47 | Nightwish (Minimal Wear)")


def test_wear_agnostic_matching():
    cmap = load_collection_map(MAP, verified_only=True)
    for wear in ("Factory New", "Field-Tested", "Battle-Scarred"):
        assert cmap.gold_type_for(f"MP9 | Starlight Protector ({wear})") == "knife"


def test_unverified_reds_excluded_by_default():
    # Recoil Case reds are covert_verified:false → not in verified map
    cmap = load_collection_map(MAP, verified_only=True)
    # a non-gold-case item returns None
    assert cmap.gold_type_for("AK-47 | Redline (Field-Tested)") is None
    # cases_with_gold still lists every case (type backbone is high-confidence)
    assert "Clutch Case" in cmap.cases_with_gold
    assert "Dreams & Nightmares Case" in cmap.cases_with_gold


def test_map_collections_excluded_from_gold():
    cmap = load_collection_map(MAP, verified_only=True)
    assert "The Cache Collection" in cmap.map_collections_no_gold
    assert cmap.verified   # independently corroborated 2026-07-19


def test_clutch_neo_noir_is_glove_fuel():
    # Leon's correction: M4A4 Neo-Noir is a Clutch Case covert (glove fuel);
    # USP-S Cortex (Classified) is NOT present.
    cmap = load_collection_map(MAP, verified_only=True)
    assert cmap.gold_type_for("M4A4 | Neo-Noir (Field-Tested)") == "glove"
    assert cmap.gold_type_for("USP-S | Cortex (Field-Tested)") is None
