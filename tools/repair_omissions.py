"""把標註時被漏掉的敘述原文補回 scenes：python tools/repair_omissions.py --book sample_book [--chapters 1,2] [--dry-run]

只補原文有、場景檔沒有的子句（純對白標籤如「她說」不算），不改現有行。改寫前先備份成 chNNN.json.bak。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from t2vn.annotate import paragraph_omissions, repair_omissions  # noqa: E402
from t2vn.config import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="補回標註時漏掉的敘述（原文逐字）。")
    parser.add_argument("--book", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--chapters", help="章號，逗號分隔；預設全部有 scenes 的章")
    parser.add_argument("--dry-run", action="store_true", help="只報告，不寫檔")
    args = parser.parse_args(argv)
    config = load_config(Path(args.config))
    work = config.work_dir(args.book)
    scene_dir = work / "scenes"
    wanted = {int(x) for x in args.chapters.split(",")} if args.chapters else None
    total = 0
    for path in sorted(scene_dir.glob("ch*.json")):
        number = int(path.stem[2:])
        if wanted and number not in wanted:
            continue
        chapter_path = work / "chapters" / path.name
        if not chapter_path.exists():
            print(f"{path.name}: 找不到章節原文，略過")
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
        paragraphs = {p["para_no"]: p["text"] for p in chapter["paragraphs"]}
        before = len(paragraph_omissions([ln for s in doc["scenes"] for ln in s["lines"]], paragraphs))
        if not before:
            print(f"{path.name}: 完整")
            continue
        added = repair_omissions(doc, paragraphs)
        after = len(paragraph_omissions([ln for s in doc["scenes"] for ln in s["lines"]], paragraphs))
        print(f"{path.name}: {before} 個段落有漏句 → 補 {added} 行，剩 {after} 個段落")
        total += added
        if not args.dry_run:
            config.assert_writable(path)
            shutil.copy2(path, path.with_suffix(".json.bak"))
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"共補 {total} 行" + ("（dry-run，未寫檔）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
