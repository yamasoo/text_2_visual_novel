"""產生獨立驗收包：python tools/make_qa_pack.py --book sample_book --out <新資料夾>（邏輯在 t2vn/qa_pack.py）。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from t2vn import export_studio  # noqa: E402
from t2vn.config import load_config  # noqa: E402
from t2vn.qa_pack import QaPackError, build_pack  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="產生給獨立 QA 對話用的驗收包（不含開發過程文件）。")
    parser.add_argument("--book", required=True, help="書的 book_id")
    parser.add_argument("--out", required=True, help="輸出資料夾（必須不存在或是空的，且在專案資料夾或 works 底下，例如 reports/qa_pack_1）")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--chapters", help="章號，逗號分隔；預設為專案已匯出的全部章")
    parser.add_argument("--report", help="指定驗收報告資料夾；預設用最新一份")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    config = load_config(Path(args.config))
    work = config.work_dir(args.book)
    settings = export_studio.load_settings(work, args.book)
    chapters = [int(x) for x in args.chapters.split(",")] if args.chapters else None
    try:
        result = build_pack(config, repo, work, Path(settings["project"]), Path(args.out), book_id=args.book, chapters=chapters,
                            report=Path(args.report) if args.report else None, dist_note=str(export_studio.dist_dir(work)))
    except QaPackError as exc:
        print(f"失敗：{exc}")
        return 1
    print(f"驗收包已產生：{result['out']}（{result['files']} 個檔案、{result['bytes']} 位元組）")
    for item in result["missing"]:
        print(f"  缺少：{item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
