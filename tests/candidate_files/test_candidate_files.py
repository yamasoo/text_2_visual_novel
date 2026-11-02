import pytest

from t2vn import candidate_files as m


def make(folder, *stems, meta=True):
    folder.mkdir(parents=True, exist_ok=True)
    for stem in stems:
        (folder / f"{stem}.png").write_bytes(b"png")
        if meta:
            (folder / f"{stem}.json").write_text("{}", encoding="utf-8")


def ok(path):
    return path


def test_check_names_accepts_dedupes_and_rejects():
    assert m.check_names(["1.png", "a_2.png", "1.png"]) == ["1.png", "a_2.png"]
    assert m.check_names([]) == []
    for bad in ("../1.png", "a/1.png", "1.PNG", "1.json", "1", ".png", "x y.png", 5, None, "1.png\\"):
        with pytest.raises(ValueError):
            m.check_names(["1.png", bad])


def test_delete_removes_png_and_json_and_reports(tmp_path):
    folder = tmp_path / "cands"
    make(folder, "1", "2", "3")
    report = m.delete_candidates(ok, folder, ["1.png", "3.png"])
    assert report == {"deleted": ["1.png", "3.png"], "missing": [], "protected": []}
    assert sorted(p.name for p in folder.iterdir()) == ["2.json", "2.png"]


def test_missing_and_protected(tmp_path):
    folder = tmp_path / "cands"
    make(folder, "1", "2")
    report = m.delete_candidates(ok, folder, ["9.png", "1.png", "2.png"], protect=["1.png"])
    assert report == {"deleted": ["2.png"], "missing": ["9.png"], "protected": ["1.png"]}
    assert (folder / "1.png").is_file() and (folder / "1.json").is_file()


def test_png_without_json_is_fine(tmp_path):
    folder = tmp_path / "cands"
    make(folder, "1", meta=False)
    assert m.delete_candidates(ok, folder, ["1.png"])["deleted"] == ["1.png"]


def test_one_bad_name_deletes_nothing(tmp_path):
    folder = tmp_path / "cands"
    make(folder, "1")
    with pytest.raises(ValueError):
        m.delete_candidates(ok, folder, ["1.png", "../x.png"])
    assert (folder / "1.png").is_file()


def test_folder_missing_means_all_missing(tmp_path):
    assert m.delete_candidates(ok, tmp_path / "nope", ["1.png"]) == {"deleted": [], "missing": ["1.png"], "protected": []}


def test_assert_writable_called_for_png_and_json_and_errors_propagate(tmp_path):
    folder = tmp_path / "cands"
    make(folder, "1", "2")
    seen = []
    m.delete_candidates(lambda p: seen.append(p.name), folder, ["1.png"])
    assert seen == ["1.png", "1.json"]

    def refuse(path):
        raise PermissionError("no")

    with pytest.raises(PermissionError):
        m.delete_candidates(refuse, folder, ["2.png"])
    assert (folder / "2.png").is_file() and (folder / "2.json").is_file()


def test_does_not_touch_other_files(tmp_path):
    folder = tmp_path / "cands"
    make(folder, "1")
    (folder / "selected.json").write_text("{}", encoding="utf-8")
    m.delete_candidates(ok, folder, ["1.png"])
    assert (folder / "selected.json").is_file()


def test_trailing_newline_name_is_rejected():
    with pytest.raises(ValueError):
        m.check_names(["a.png\n"])
