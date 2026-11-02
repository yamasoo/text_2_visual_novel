import copy
import json

from t2vn import audio_scan as m

ASSETS = [
    {"asset_id": "bg_yard_dawn", "kind": "bg", "status": "planned", "license": ""},
    {"asset_id": "bgm_tension", "kind": "bgm", "status": "planned", "license": ""},
    {"asset_id": "bgm_mystery", "kind": "bgm", "status": "generated", "license": "mine"},
    {"asset_id": "amb_scrapyard", "kind": "ambience", "status": "selected", "license": ""},
    {"asset_id": "amb_dock", "kind": "ambience", "status": "planned"},
    {"kind": "bgm", "status": "planned"},
    "junk",
]


def touch(root, rel, text="x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_scan_finds_files_by_priority(tmp_path):
    touch(tmp_path, "bgm/bgm_tension.wav")
    touch(tmp_path, "bgm/bgm_tension.opus")
    touch(tmp_path, "bgm/bgm_tension.mp3")
    touch(tmp_path, "ambience/amb_scrapyard.ogg")
    results = {r["asset_id"]: r for r in m.scan_audio(tmp_path, ASSETS)}
    assert list(results) == ["bgm_tension", "bgm_mystery", "amb_scrapyard", "amb_dock"]
    tension = results["bgm_tension"]
    assert tension["present"] and tension["file"] == "bgm/bgm_tension.opus" and tension["extras"] == ["bgm/bgm_tension.mp3", "bgm/bgm_tension.wav"]
    assert results["amb_scrapyard"]["file"] == "ambience/amb_scrapyard.ogg" and results["amb_scrapyard"]["extras"] == []
    assert results["bgm_mystery"]["present"] is False and results["bgm_mystery"]["file"] is None
    assert results["amb_dock"]["present"] is False


def test_kind_folders_are_separate(tmp_path):
    touch(tmp_path, "ambience/bgm_tension.opus")
    touch(tmp_path, "bgm/amb_dock.opus")
    results = {r["asset_id"]: r for r in m.scan_audio(tmp_path, ASSETS)}
    assert not results["bgm_tension"]["present"] and not results["amb_dock"]["present"]


def test_missing_folders_ok(tmp_path):
    results = m.scan_audio(tmp_path / "nowhere", ASSETS)
    assert len(results) == 4 and not any(r["present"] for r in results)
    assert m.scan_audio(tmp_path, []) == []


def test_sidecar(tmp_path):
    touch(tmp_path, "bgm/bgm_tension.opus")
    touch(tmp_path, "bgm/bgm_tension.json", json.dumps({"license": " CC0 1.0 ", "source_url": "https://example.com/a", "extra": 1}))
    touch(tmp_path, "ambience/amb_dock.json", "{bad json")
    touch(tmp_path, "ambience/amb_scrapyard.json", json.dumps({"license": 5, "source_url": None}))
    results = {r["asset_id"]: r for r in m.scan_audio(tmp_path, ASSETS)}
    assert results["bgm_tension"]["license"] == "CC0 1.0" and results["bgm_tension"]["source_url"] == "https://example.com/a"
    assert results["amb_dock"]["license"] == "" and results["amb_scrapyard"]["license"] == ""
    assert m.read_sidecar(tmp_path / "bgm", "nothing") == {"license": "", "source_url": ""}
    touch(tmp_path, "bgm/list.json", "[1]")
    assert m.read_sidecar(tmp_path / "bgm", "list") == {"license": "", "source_url": ""}


def test_find_orphans(tmp_path):
    touch(tmp_path, "bgm/bgm_tension.opus")
    touch(tmp_path, "bgm/old_theme.opus")
    touch(tmp_path, "bgm/bgm_tension.json")
    touch(tmp_path, "bgm/notes.txt")
    touch(tmp_path, "ambience/amb_dock.ogg")
    touch(tmp_path, "ambience/bgm_tension.wav")  # 種類不對也算孤兒
    (tmp_path / "bgm" / "sub").mkdir()
    assert m.find_orphans(tmp_path, ASSETS) == ["ambience/bgm_tension.wav", "bgm/old_theme.opus"]
    assert m.find_orphans(tmp_path / "nowhere", ASSETS) == []


def test_apply_scan_status_and_license(tmp_path):
    touch(tmp_path, "bgm/bgm_tension.opus")
    touch(tmp_path, "bgm/bgm_tension.json", json.dumps({"license": "CC0 1.0"}))
    touch(tmp_path, "ambience/amb_scrapyard.opus")
    touch(tmp_path, "ambience/amb_scrapyard.json", json.dumps({"license": "CC0 1.0"}))
    results = m.scan_audio(tmp_path, ASSETS)
    before = copy.deepcopy(ASSETS)
    updated = m.apply_scan(ASSETS, results)
    assert ASSETS == before  # 不改傳入的物件
    by_id = {a["asset_id"]: a for a in updated if isinstance(a, dict) and "asset_id" in a}
    assert by_id["bgm_tension"]["status"] == "generated" and by_id["bgm_tension"]["license"] == "CC0 1.0"
    assert by_id["bgm_mystery"]["status"] == "planned" and by_id["bgm_mystery"]["license"] == "mine"  # 檔案不見 → 退回；授權不動
    assert by_id["amb_scrapyard"]["status"] == "selected" and by_id["amb_scrapyard"]["license"] == "CC0 1.0"
    assert by_id["amb_dock"]["status"] == "planned" and "license" not in by_id["amb_dock"]
    assert by_id["bg_yard_dawn"] == before[0]
    assert updated[5] == before[5] and updated[6] == "junk"


def test_apply_scan_does_not_overwrite_existing_license(tmp_path):
    touch(tmp_path, "bgm/bgm_mystery.opus")
    touch(tmp_path, "bgm/bgm_mystery.json", json.dumps({"license": "other"}))
    updated = m.apply_scan(ASSETS, m.scan_audio(tmp_path, ASSETS))
    mystery = next(a for a in updated if a.get("asset_id") == "bgm_mystery")
    assert mystery["license"] == "mine" and mystery["status"] == "generated"


def test_apply_scan_ignores_results_with_other_kind():
    results = [{"asset_id": "bg_yard_dawn", "kind": "bgm", "present": True, "file": "bgm/x.opus", "extras": [], "license": "z", "source_url": ""}]
    updated = m.apply_scan(ASSETS, results)
    assert updated[0] == ASSETS[0]
