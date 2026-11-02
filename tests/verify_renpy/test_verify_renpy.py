import pytest

from t2vn import verify_renpy as m


class Cfg:
    def __init__(self, protected=False):
        self.protected = protected

    def is_write_protected(self, path):
        return self.protected


def make_project(tmp_path, scenes=3):
    game = tmp_path / "proj" / "game"
    game.mkdir(parents=True)
    (game / "script.rpy").write_text("label start:\n    jump ch001\n", encoding="utf-8")
    body = "label ch001:\n    scene black\n" + "".join(f"\nlabel ch001_sc{i:02d}:\n    scene black\n" for i in range(1, scenes + 1))
    (game / "vn_ch001.rpy").write_text(body, encoding="utf-8")
    (game / "vn_end.rpy").write_text("label vn_end:\n    return\n", encoding="utf-8")
    return tmp_path / "proj"


def test_scene_labels_in_order(tmp_path):
    game = make_project(tmp_path) / "game"
    assert m.scene_labels(game) == ["ch001", "ch001_sc01", "ch001_sc02", "ch001_sc03"]


def test_build_testsuite_covers_the_checks():
    text = m.build_testsuite(["ch001", "ch001_sc01", "ch001_sc02"])
    for case in ("v1_launch", "v2_screens", "v3_input", "v4_main_loop_and_ending", "v5_restart"):
        assert f"testcase {case}:" in text
    assert text.startswith("# ") and "testsuite vn_verify:" in text and "teardown:" in text
    assert "advance repeat 50" in text and 'skip until screen "main_menu" timeout' in text and "seen_label" in text and "vn_end" in text
    assert 'label "' not in text   # label 後面接不加引號的名稱（Ren'Py 實測）
    assert 'run Start()' in text and "\t" not in text


def test_build_testsuite_thins_screenshots():
    labels = [f"ch001_sc{i:03d}" for i in range(200)]
    text = m.build_testsuite(labels, max_shots=10)
    assert text.count('screenshot "verify/progress_') == 10 and "run MainMenu(confirm=False)" in text


def test_build_testsuite_needs_scenes():
    with pytest.raises(m.VerifyError):
        m.build_testsuite(["ch001"])


def test_find_runner_prefers_console_python(tmp_path):
    sdk = tmp_path / "sdk"
    (sdk / "lib" / "py3-windows-x86_64").mkdir(parents=True)
    (sdk / "lib" / "py3-windows-x86_64" / "python.exe").write_bytes(b"")
    (sdk / "renpy.py").write_text("", encoding="utf-8")
    (sdk / "renpy.exe").write_bytes(b"")
    prefix, warning = m.find_runner(sdk)
    assert prefix[0].endswith("python.exe") and prefix[1].endswith("renpy.py") and warning is None


def test_find_runner_falls_back_and_errors(tmp_path):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    with pytest.raises(m.VerifyError):
        m.find_runner(sdk)
    (sdk / "renpy.exe").write_bytes(b"")
    prefix, warning = m.find_runner(sdk)
    assert prefix[0].endswith("renpy.exe") and warning


def test_parse_test_output():
    text = ("[rpytest] [exc]     - v1_launch\n[rpytest] [exc]     - v1_launch (1.255 s)\n[rpytest] [exc]     - v2_screens\n"
            "renpy.test.types.RenpyTestTimeoutError: Until Statement timed out after 5.0 seconds.\n")
    parsed = m.parse_test_output(text)
    assert parsed["cases"] == {"launch": True, "screens": False, "input": None, "main_loop_ending": None, "restart": None}
    assert parsed["has_traceback"] is True and parsed["errors"]


def fake_runner(project):
    calls = []

    def run(command, timeout):
        calls.append(command)
        if command[-1] == "lint":
            return 0, "Statistics: 541 blocks\n"
        shots = project / "game" / "verify"
        shots.mkdir(exist_ok=True)
        (shots / "01_main_menu.png").write_bytes(b"png")
        return 0, "".join(f"[rpytest] [exc]     - {c}\n[rpytest] [exc]     - {c} (1.0 s)\n" for _k, _n, c in m.CHECKS)

    return run, calls


def sdk_dir(tmp_path):
    sdk = tmp_path / "sdk"
    sdk.mkdir(exist_ok=True)
    (sdk / "renpy.sh").write_text("", encoding="utf-8")
    return sdk


def test_verify_end_to_end_with_fake_runner(tmp_path):
    project = make_project(tmp_path)
    run, calls = fake_runner(project)
    out = tmp_path / "out"
    summary = m.verify(Cfg(), sdk_dir(tmp_path), project, out, runner=run)
    assert summary["cases"] == {k: True for k, _n, _c in m.CHECKS} and summary["screenshots"] == 1
    assert [c[-1] for c in calls] == ["lint", "vn_verify"] and calls[1][-2] == "test"
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "啟動" in report and "重開" in report and "通過" in report
    assert (out / "lint.txt").read_text(encoding="utf-8").startswith("Statistics") and (out / "screens" / "01_main_menu.png").is_file()
    assert not (project / "game" / m.TEST_FILE).exists()   # 預設跑完刪掉
    assert "## Lint" in report and "## 劇本可達性" in report
    assert summary["lint_issues"] == 0 and summary["lint_crashed"] is False and "reach_errors" in summary and "reach_warnings" in summary


def test_verify_report_lists_lint_issues_and_dead_ends(tmp_path):
    project = make_project(tmp_path)
    (project / "game" / "vn_ch009.rpy").write_text("label ch009:\n    \"hi\"\n", encoding="utf-8")
    run, _ = fake_runner(project)

    def lint_run(command, timeout):
        if command[-1] == "lint":
            return 0, "game/vn_ch009.rpy:2 The image named 'x' was not found.\n"
        return run(command, timeout)

    summary = m.verify(Cfg(), sdk_dir(tmp_path), project, tmp_path / "out_issues", runner=lint_run)
    report = (tmp_path / "out_issues" / "report.md").read_text(encoding="utf-8")
    assert summary["lint_issues"] == 1 and summary["reach_errors"] >= 1
    assert "was not found" in report and "[錯誤]" in report


def test_verify_keep_test_and_protected(tmp_path):
    project = make_project(tmp_path)
    run, _ = fake_runner(project)
    m.verify(Cfg(), sdk_dir(tmp_path), project, tmp_path / "out", runner=run, keep_test=True)
    assert (project / "game" / m.TEST_FILE).is_file()
    with pytest.raises(m.VerifyError):
        m.verify(Cfg(protected=True), sdk_dir(tmp_path), project, tmp_path / "out2", runner=run)


def test_verify_needs_game_dir(tmp_path):
    (tmp_path / "p").mkdir()
    with pytest.raises(m.VerifyError):
        m.verify(Cfg(), sdk_dir(tmp_path), tmp_path / "p", tmp_path / "o")


def test_syntax_error_is_reported():
    text = 'File "game/vn_verify_test.rpy", line 12: expected \'label_name\' not found.\n    advance until label "x"\n    ^'
    parsed = m.parse_test_output(text)
    assert parsed["syntax_error"] and "line 12" in parsed["syntax_error"][0]
    assert all(v is None for v in parsed["cases"].values())
    summary = {"runner_warning": None, "preflight_errors": 0, "preflight_warnings": 0, "lint_code": 0, "test_code": 1,
               "cases": parsed["cases"], "traceback": False, "syntax_error": parsed["syntax_error"], "screenshots": 0}
    assert "語法錯誤" in m.render_report(summary, [])


def test_parse_marks_error_between_start_and_done_as_failed():
    text = ("[rpytest] [exc]     - v1_launch\n[rpytest] [exc]     - v1_launch (1.0 s)\n[rpytest] [exc]     - v5_restart\n"
            "renpy.test.types.RenpyTestTimeoutError: TestCase timed out after 5.0 seconds.\n"
            "[rpytest] [exc]     - v5_restart (4.8 s)\n[rpytest] [exc]     - v5_restart (4.8 s)\n")
    cases = m.parse_test_output(text)["cases"]
    assert cases["launch"] is True and cases["restart"] is False and cases["screens"] is None
