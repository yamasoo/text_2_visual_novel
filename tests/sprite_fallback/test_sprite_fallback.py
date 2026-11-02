import pytest

from t2vn import sprite_fallback as m


def test_pick_expr():
    assert m.pick_expr("angry", ["neutral", "angry"]) == "angry"
    assert m.pick_expr("angry", ["neutral"]) == "neutral"
    assert m.pick_expr("angry", ["smile"]) is None
    assert m.pick_expr("angry", []) is None
    assert m.pick_expr("neutral", iter(["neutral"])) == "neutral"


def test_plan_exact_and_fallback():
    plan = m.plan_images([("ellie", "angry"), ("ellie", "neutral")], {"ellie": {"neutral"}}, {"ellie": {"neutral", "angry"}})
    assert plan == [
        {"char_id": "ellie", "expr": "angry", "sprite_expr": "neutral", "headshot_expr": "angry",
         "sprite_fallback": True, "headshot_fallback": False},
        {"char_id": "ellie", "expr": "neutral", "sprite_expr": "neutral", "headshot_expr": "neutral",
         "sprite_fallback": False, "headshot_fallback": False},
    ]


def test_plan_missing_everything():
    plan = m.plan_images([("ghost", "smile")], {}, {})
    assert plan == [{"char_id": "ghost", "expr": "smile", "sprite_expr": None, "headshot_expr": None,
                     "sprite_fallback": False, "headshot_fallback": False}]


def test_plan_dedupes_keeps_order_and_empty_expr_is_neutral():
    plan = m.plan_images([("b", ""), ("a", "neutral"), ("b", "neutral"), ("a", "neutral")], {}, {})
    assert [(p["char_id"], p["expr"]) for p in plan] == [("b", "neutral"), ("a", "neutral")]


def test_plan_headshot_only_and_sprite_only():
    plan = m.plan_images([("a", "sad")], {"a": {"neutral"}}, {})
    assert plan[0]["sprite_expr"] == "neutral" and plan[0]["headshot_expr"] is None
    plan = m.plan_images([("a", "sad")], {}, {"a": {"sad"}})
    assert plan[0]["sprite_expr"] is None and plan[0]["headshot_expr"] == "sad" and plan[0]["headshot_fallback"] is False


@pytest.mark.parametrize("pair", [("Ellie", "neutral"), ("mi ra", "neutral"), ("ellie", "Angry"), ("", "neutral"), ("ellie", "a-b"), ("a/b", "neutral")])
def test_plan_rejects_bad_ids(pair):
    with pytest.raises(ValueError):
        m.plan_images([pair], {}, {})


def test_plan_does_not_mutate_inputs():
    sprites = {"a": {"neutral"}}
    heads = {"a": ["neutral"]}
    m.plan_images([("a", "x")], sprites, heads)
    assert sprites == {"a": {"neutral"}} and heads == {"a": ["neutral"]}


def test_image_lines_with_fallback_and_side():
    plan = m.plan_images([("ellie", "angry")], {"ellie": {"neutral"}}, {"ellie": {"angry"}})
    assert m.image_lines(plan) == [
        'image ellie angry = "images/sprite/ellie_neutral.png"',
        'image side ellie angry = "images/headshot/ellie_angry.png"',
    ]


def test_image_lines_without_any_image_uses_original_name():
    plan = m.plan_images([("ghost", "smile")], {}, {})
    assert m.image_lines(plan) == ['image ghost smile = "images/sprite/ghost_smile.png"']


def test_image_lines_empty():
    assert m.image_lines([]) == []


def test_needed_files_dedupes_and_skips_missing():
    plan = m.plan_images([("a", "angry"), ("a", "sad"), ("a", "neutral"), ("ghost", "smile")], {"a": {"neutral"}}, {"a": {"neutral", "sad"}})
    assert m.needed_files(plan) == [
        "images/sprite/a_neutral.png", "images/headshot/a_neutral.png", "images/headshot/a_sad.png",
    ]


def test_needed_files_empty():
    assert m.needed_files([]) == []


def test_scan_available(tmp_path):
    (tmp_path / "sprite").mkdir()
    (tmp_path / "headshot").mkdir()
    for name in ("ellie_reed_neutral.png", "ellie_reed_angry.png", "a_ming_neutral.png", "notes.png", "x_unknown.png", "_neutral.png", "readme.txt"):
        (tmp_path / "sprite" / name).write_bytes(b"x")
    (tmp_path / "headshot" / "ellie_reed_smile.png").write_bytes(b"x")
    sprites, heads = m.scan_available(tmp_path)
    assert sprites == {"ellie_reed": {"neutral", "angry"}, "a_ming": {"neutral"}}
    assert heads == {"ellie_reed": {"smile"}}


def test_scan_available_missing_folders(tmp_path):
    assert m.scan_available(tmp_path) == ({}, {})
    assert m.scan_available(tmp_path / "nope") == ({}, {})


def test_scan_available_accepts_str_path(tmp_path):
    (tmp_path / "sprite").mkdir()
    (tmp_path / "sprite" / "a_neutral.png").write_bytes(b"x")
    assert m.scan_available(str(tmp_path))[0] == {"a": {"neutral"}}


def test_scan_then_plan_roundtrip(tmp_path):
    (tmp_path / "sprite").mkdir()
    (tmp_path / "sprite" / "a_neutral.png").write_bytes(b"x")
    sprites, heads = m.scan_available(tmp_path)
    plan = m.plan_images([("a", "worried")], sprites, heads)
    assert m.needed_files(plan) == ["images/sprite/a_neutral.png"]
