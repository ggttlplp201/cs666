from system_a.concentration import Row, class_of, rank_classes


def test_class_of():
    assert class_of("★ Karambit | Doppler (Factory New)") == "Knife"
    assert class_of("★ Sport Gloves | Vice (Field-Tested)") == "Gloves"
    assert class_of("★ Specialist Gloves | Hand Wraps") == "Gloves"
    assert class_of("AWP | Dragon Lore (Field-Tested)") == "AWP"
    assert class_of("AK-47 | Redline (Field-Tested)") == "Rifle"
    assert class_of("Desert Eagle | Blaze (Factory New)") == "Pistol"
    assert class_of("MP9 | Starlight Protector (Field-Tested)") == "SMG"
    assert class_of("Sticker | Foo") == "Other"


def test_high_barrier_thin_supply_scores_top():
    # knives: expensive + thin listings; pistols: cheap + deep listings
    rows = (
        [Row(f"★ Knife {i}", price=1000.0 + i, listings=20) for i in range(6)] +
        [Row(f"Glock-18 | S{i}", price=5.0 + i, listings=500) for i in range(6)]
    )
    ranking = rank_classes(rows, min_items=5)
    assert ranking[0].cls == "Knife"
    assert ranking[-1].cls == "Pistol"
    assert ranking[0].score > ranking[-1].score
    assert 0.0 <= ranking[0].score <= 1.0


def test_min_items_filter():
    rows = ([Row(f"★ Knife {i}", 900.0, 10) for i in range(3)] +
            [Row(f"AK-47 | S{i}", 20.0, 200) for i in range(6)])
    ranking = rank_classes(rows, min_items=5)
    assert {c.cls for c in ranking} == {"Rifle"}   # Knife dropped (only 3)
