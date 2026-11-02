"""S8：Ren'Py 專案驗收腳本（六項檢查：啟動、畫面、輸入、主循環、結局、重開）。

在**本機**執行（需要 Ren'Py SDK）：先跑前置檢查與 Lint，再產生自動測試（``game/vn_verify_test.rpy``），
用 Ren'Py 內建的 ``test`` 指令走一遍，最後把結果與截圖整理成一份報告。

用法::

    python tools/verify_renpy.py --sdk <renpy-sdk> --project <專案資料夾> [--out <報告資料夾>] [--timeout 900] [--keep-test]

Ren'Py 自動測試語法依官方文件（testcase／testsuite／advance until label／screenshot）；
第一次實跑若語法被拒，報告會附完整輸出，照訊息調整 :func:`build_testsuite` 即可。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .errors import T2vnError
from . import lint_parse, rpy_reach
from .renpy_preflight import check_project

TEST_FILE = "vn_verify_test.rpy"
SUITE = "vn_verify"
SHOT_DIR = "verify"
LABEL_RE = re.compile(r"^label\s+(ch[0-9]{3}(?:_sc[0-9]+)?)\s*:", re.M)
UNTIL_TIMEOUT = 300.0
ADVANCE_CHUNK = 50
BACK_TO_MENU = ["        run MainMenu(confirm=False)", '        pause until screen "main_menu" timeout 30.0']
SKIP_ALL = 'run Preference("skip", "all messages")'  # 驗收要連沒看過的對白也快轉
SKIP_STOP = 'run SetField(config, "skipping", None)'  # 到點後停止快轉，才能拍畫面
CHECKS = (
    ("launch", "啟動", "v1_launch"),
    ("screens", "畫面", "v2_screens"),
    ("input", "輸入", "v3_input"),
    ("main_loop_ending", "主循環與結局", "v4_main_loop_and_ending"),
    ("restart", "重開", "v5_restart"),
)
Runner = Callable[[list[str], int], "tuple[int, str]"]


class VerifyError(T2vnError):
    """驗收設定有問題（找不到 SDK、專案沒有場景…）。"""


def scene_labels(game: Path) -> list[str]:
    """``game/vn_ch*.rpy`` 裡的章與場景 label，依檔名與檔內順序。"""
    labels: list[str] = []
    for path in sorted(game.glob("vn_ch[0-9][0-9][0-9].rpy")):
        labels += LABEL_RE.findall(path.read_text(encoding="utf-8"))
    return labels


def build_testsuite(labels: list[str], max_shots: int = 10) -> str:
    """產生 Ren'Py 自動測試檔內容。``labels``：:func:`scene_labels` 的結果（至少要有一個場景）。"""
    scenes = [x for x in labels if "_sc" in x]
    if not scenes:
        raise VerifyError("專案裡找不到任何場景 label（game/vn_ch*.rpy），請先匯出")
    lines = [
        "# 由 t2vn.verify_renpy 產生；驗收用，可刪除。",
        f"testsuite {SUITE}:",
        "    teardown:",
        "        exit",
        "",
        "    testcase v1_launch:",
        "        pause 1.0",
        f'        screenshot "{SHOT_DIR}/01_main_menu.png"',
        "",
        "    testcase v2_screens:",
        '        run Start()',
        "        pause 1.0",
        f'        screenshot "{SHOT_DIR}/scene_{scenes[0]}_title.png"',
        "        advance repeat 4",
        "        pause 0.5",
        f'        screenshot "{SHOT_DIR}/scene_{scenes[0]}.png"',
    ]
    for i in range(1, max_shots + 1):  # 每按 ADVANCE_CHUNK 句拍一張，涵蓋整本的不同段落
        lines += [f"        advance repeat {ADVANCE_CHUNK}", "        pause 0.3", f'        screenshot "{SHOT_DIR}/progress_{i:02d}.png"']
    lines += [
        "",
        "    testcase v3_input:",
        *BACK_TO_MENU,
        '        run Start()',
        "        advance repeat 15",
        "        pause 0.5",
        f'        screenshot "{SHOT_DIR}/input_after_15_advances.png"',
        "",
        "    testcase v4_main_loop_and_ending:",
        *BACK_TO_MENU,
        '        run Start()',
        f"        {SKIP_ALL}",
        "        pause 2.0",
        f'        skip until screen "main_menu" timeout {UNTIL_TIMEOUT}',
        f"        {SKIP_STOP}",
        '        assert eval (renpy.seen_label("vn_end"))',
        f'        screenshot "{SHOT_DIR}/back_to_menu.png"',
        "",
        "    testcase v5_restart:",  # 接在 v4 之後，此時已在主選單，不必再回主選單（已在主選單時 MainMenu 會卡住）
        '        run Start()',
        "        pause 1.0",
        f'        screenshot "{SHOT_DIR}/restart_first_scene.png"',
        "",
    ]
    return "\n".join(lines)


def find_runner(sdk: Path) -> tuple[list[str], str | None]:
    """回傳 ``(指令前綴, 警告)``。優先用 SDK 內附的主控台 Python（才拿得到輸出），沒有再退到啟動檔。"""
    sdk = Path(sdk)
    script = sdk / "renpy.py"
    if script.is_file():
        for python in sorted(sdk.glob("lib/py3-*/python.exe")) + sorted(sdk.glob("lib/py3-*/python")):
            return [str(python), str(script)], None
    for name, warning in (("renpy.exe", "只找到 renpy.exe（視窗程式），主控台可能抓不到輸出"), ("renpy.sh", None)):
        if (sdk / name).is_file():
            return [str(sdk / name)], warning
    raise VerifyError(f"在 {sdk} 找不到 Ren'Py（renpy.py／renpy.exe／renpy.sh）")


def run_command(command: list[str], timeout: int) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "") + (exc.stderr or "") + f"\n[逾時：超過 {timeout} 秒，已停止]"
    except OSError as exc:
        return 127, f"無法執行：{exc}"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def parse_test_output(text: str) -> dict[str, Any]:
    """解析 ``[rpytest]`` 輸出。

    開始行 ``- name`` 之後、完成行 ``- name (1.2 s)`` 之前出現 ``Error``／``Exception`` 就是失敗；沒有開始行為 ``None``（沒跑到）。
    """
    names = {case: key for key, _name, case in CHECKS}
    results: dict[str, bool | None] = dict.fromkeys(names.values())
    current: str | None = None
    errored = False
    lines = text.splitlines()
    for line in lines:
        timed = re.search(r"-\s+(v\d_\w+)\s+\([\d.]+\s*s\)", line)
        bare = re.search(r"-\s+(v\d_\w+)\s*$", line)
        if bare and bare.group(1) in names:
            current, errored = bare.group(1), False
            results[names[current]] = False
        elif timed and timed.group(1) in names:
            if current == timed.group(1):
                results[names[current]] = not errored
            current = None
        elif current and re.search(r"(?:Error|Exception)\b", line):
            errored = True
    errors = [ln.strip() for ln in lines if "Error:" in ln and "renpy" in ln][:3]
    syntax = [ln for ln in lines if "expected" in ln or "is not a keyword" in ln][:3]
    return {"cases": results, "has_traceback": "Traceback" in text or bool(errors), "errors": errors, "syntax_error": syntax}


def collect_shots(project: Path, since: float, dest: Path) -> list[str]:
    """把這次執行後新出現、在 ``verify`` 資料夾裡的 png 複製到報告資料夾。"""
    found: list[str] = []
    for png in sorted(project.rglob("*.png")):
        if SHOT_DIR in png.parts and png.stat().st_mtime >= since - 1:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(png, dest / png.name)
            found.append(png.name)
    return found


def verify(config: Any, sdk: Path, project: Path, out: Path, *, timeout: int = 900, keep_test: bool = False,
           runner: Runner = run_command) -> dict[str, Any]:
    """整套流程；回傳摘要 dict（也寫進 ``out/report.md``）。"""
    project, out = Path(project), Path(out)
    game = project / "game"
    if not game.is_dir():
        raise VerifyError(f"{project} 底下沒有 game 資料夾")
    prefix, warning = find_runner(sdk)
    labels = scene_labels(game)
    test_path = game / TEST_FILE
    for target in (test_path, out / "report.md"):
        if config.is_write_protected(target):
            raise VerifyError(f"不能寫入唯讀資料夾：{target}")
    out.mkdir(parents=True, exist_ok=True)

    issues = check_project(project)
    pre_errors = [i for i in issues if i["level"] == "error"]
    lint_code, lint_text = runner(prefix + [str(project), "lint"], timeout)
    (out / "lint.txt").write_text(lint_text, encoding="utf-8")
    lint = lint_parse.parse_lint(lint_text)
    reach = rpy_reach.check_game(game)  # 在寫入測試檔之前檢查，只看匯出的劇本

    shutil.rmtree(project / "tests" / "screenshots" / SHOT_DIR, ignore_errors=True)  # Ren'Py 遇到同名舊圖會拿來比對，必須先清掉
    test_path.write_text(build_testsuite(labels), encoding="utf-8")
    started = time.time()
    try:
        test_code, test_text = runner(prefix + [str(project), "test", SUITE], timeout)
    finally:
        if not keep_test and test_path.exists():
            test_path.unlink()
    (out / "test.txt").write_text(test_text, encoding="utf-8")
    parsed = parse_test_output(test_text)
    shots = collect_shots(project, started, out / "screens")

    summary = {"runner_warning": warning, "preflight_errors": len(pre_errors), "preflight_warnings": len(issues) - len(pre_errors),
               "lint_code": lint_code, "test_code": test_code, "cases": parsed["cases"], "traceback": parsed["has_traceback"], "syntax_error": parsed["syntax_error"],
               "screenshots": len(shots), "scene_labels": len([x for x in labels if "_sc" in x]), "out": str(out),
               "lint_issues": len(lint["issues"]), "lint_crashed": lint["crashed"],
               "reach_errors": len([i for i in reach["issues"] if i["level"] == "error"]),
               "reach_warnings": len([i for i in reach["issues"] if i["level"] != "error"])}
    extra = [lint_parse.render_markdown(lint), rpy_reach.render_markdown(reach)]
    (out / "report.md").write_text(render_report(summary, pre_errors, extra), encoding="utf-8")
    return summary


def render_report(summary: dict[str, Any], pre_errors: list[Any], extra_sections: list[str] | None = None) -> str:
    mark = {True: "通過", False: "失敗", None: "未知（看 test.txt）"}
    lines = ["# Ren'Py 驗收報告", "", f"- 前置檢查（匯出後程式自查）：{summary['preflight_errors']} 錯誤、{summary['preflight_warnings']} 警告",
             f"- Lint：結束碼 {summary['lint_code']}（完整輸出與警告見 lint.txt、下方 Lint 節）",
             f"- 自動測試：結束碼 {summary['test_code']}；{summary['screenshots']} 張截圖（screens/）", ""]
    if summary["runner_warning"]:
        lines += [f"> 注意：{summary['runner_warning']}", ""]
    lines += ["## 驗收項目（五項）", "", "| 檢查 | 結果 |", "|---|---|"]
    for key, name, _case in CHECKS:
        lines.append(f"| {name} | {mark[summary['cases'][key]]} |")
    lines += ["", "（「結局」與「主循環」合在同一個測試：從頭走到 `vn_end` 再回到主選單。）", ""]
    if summary["syntax_error"]:
        lines += ["> 自動測試檔被 Ren'Py 拒絕（語法錯誤），前幾行：", "", "```", *summary["syntax_error"], "```", ""]
    if summary["traceback"]:
        lines += ["> test.txt 裡有 Traceback／Exception，請看內容。", ""]
    for issue in pre_errors:
        lines.append(f"- 前置檢查錯誤 {issue['file']}:{issue['line']} {issue['message']}")
    text = "\n".join(lines) + "\n"
    for section in extra_sections or []:
        text += "\n" + section
    return text


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    from .config import load_config

    parser = argparse.ArgumentParser(description="Verify an exported Ren'Py project (lint + automated play-through).")
    parser.add_argument("--sdk", required=True, help="Ren'Py SDK 資料夾")
    parser.add_argument("--project", required=True, help="Ren'Py 專案資料夾（裡面有 game/）")
    parser.add_argument("--out", default=None, help="報告資料夾（預設 <repo>/reports/verify_<時間>）")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--keep-test", action="store_true", help="跑完不要刪掉 game/vn_verify_test.rpy")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        out = Path(args.out) if args.out else config.project_root / "reports" / time.strftime("verify_%Y%m%d_%H%M%S")
        summary = verify(config, Path(args.sdk), Path(args.project), out, timeout=args.timeout, keep_test=args.keep_test)
    except (T2vnError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print((Path(summary["out"]) / "report.md").read_text(encoding="utf-8"))
    print(f"報告：{summary['out']}")
    bad = summary["preflight_errors"] or summary["test_code"] != 0 or any(v is False for v in summary["cases"].values())
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
