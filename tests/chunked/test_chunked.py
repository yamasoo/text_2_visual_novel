"""分塊標註：切塊、合併、檢查點續跑、提示詞、寫檔整合。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from t2vn import chunked
from t2vn.annotate import AnnotateError, ChapterContext, annotate_chapter_to_disk, build_messages, main
from t2vn.chunked import ChunkFailure, ChunkStore, annotate_chunked, auto_chunk_chars, merge_chunks, plan_chunks, plan_hash
from t2vn.errors import JobStopped
from t2vn.llm import LlmError, ReplayLLM, RunHooks
_BOOK = Path(__file__).resolve().parents[2] / "works" / "sample_book"
if not (_BOOK / "chapters" / "ch001.json").is_file() or not (_BOOK / "gold" / "ch001.json").is_file():
    pytest.skip("needs works/sample_book gold set (not committed, see .gitignore)", allow_module_level=True)

from tests.annotate.test_annotate import CAST, CHAPTER, GOLD, OPS, strip_assets, write_project

PARAS = CHAPTER["paragraphs"]


def chunk_replies(chunks):
    """把黃金 op 依段落範圍切成每塊一份回應（跨塊的場被切成兩半，屬性相同；x 指示跟著前一個 op）。"""
    replies = []
    for chunk in chunks:
        lo, hi = chunk[0]["para_no"], chunk[-1]["para_no"]
        scenes = []
        last_p = 1
        for scene in OPS["scenes"]:
            ops = []
            for op in scene["ops"]:
                p = op.get("p", last_p)
                last_p = p
                if lo <= p <= hi:
                    ops.append(op)
            if ops:
                scenes.append({**copy.deepcopy(scene), "ops": ops})
        reply = {"chapter_id": "ch001", "scenes": scenes}
        if replies == []:
            reply["new_characters"] = OPS.get("new_characters", [])
        replies.append(json.dumps(reply, ensure_ascii=False))
    return replies


def ctx():
    return ChapterContext(CHAPTER, CAST)


# ---------------------------------------------------------------- 切塊


def test_plan_chunks_covers_every_paragraph_once_in_order():
    chunks = plan_chunks(PARAS, 1500)
    assert len(chunks) >= 3
    flat = [p["para_no"] for c in chunks for p in c]
    assert flat == [p["para_no"] for p in PARAS]
    assert all(len(c) >= chunked.MIN_CHUNK_PARAGRAPHS for c in chunks)
    # 沒有一塊明顯超過上限（容許最後湊進來的和一段的誤差）
    assert all(sum(len(p["text"]) for p in c) < 1500 * 1.6 for c in chunks)


def test_plan_chunks_small_or_disabled_is_single_chunk():
    assert len(plan_chunks(PARAS, 0)) == 1
    assert len(plan_chunks(PARAS, 10**6)) == 1
    assert len(plan_chunks(PARAS[:4], 10)) == 1  # 太短不切


def test_plan_chunks_avoids_cutting_before_a_quote_when_possible():
    paras = [{"para_no": i + 1, "text": ("「話」" if i % 5 == 4 else "敘述" * 20)} for i in range(30)]
    for chunk in plan_chunks(paras, 150)[1:]:
        # 下一塊的第一段盡量不是對話開頭
        assert not chunk[0]["text"].startswith("「") or len(chunk) < 4


def test_plan_hash_changes_with_text_or_cut():
    a = plan_chunks(PARAS, 1500)
    assert plan_hash(a) == plan_hash(plan_chunks(PARAS, 1500))
    assert plan_hash(a) != plan_hash(plan_chunks(PARAS, 2000))
    edited = copy.deepcopy(PARAS)
    edited[3]["text"] += "。"
    assert plan_hash(a) != plan_hash(plan_chunks(edited, 1500))


def test_auto_chunk_chars_follows_context_length():
    small = {"id": "x", "context_tokens": 8192, "max_output": 2000}
    big = {"id": "x", "context_tokens": 200000, "max_output": 32768}
    assert auto_chunk_chars(small) < auto_chunk_chars(big) == 15000
    assert auto_chunk_chars(None) == 15000
    assert auto_chunk_chars(small, cap=800) == 1500 or auto_chunk_chars(small, cap=800) >= 800  # 有下限，不會小到不能用


# ---------------------------------------------------------------- 標註與合併


def run(llm, tmp_path, chunks, **kw):
    store = ChunkStore(tmp_path / "_chunks" / "ch001")
    return annotate_chunked(ctx(), llm, "sample_book", "zh-TW", chunks, store, **kw), store


def test_chunked_result_matches_gold(tmp_path):
    chunks = plan_chunks(PARAS, 1500)
    llm = ReplayLLM(chunk_replies(chunks))
    done, _ = run(llm, tmp_path, chunks)
    assert done.chunks == len(chunks) and done.resumed == 0
    assert strip_assets(done.result.scene_doc) == strip_assets(GOLD)
    assert done.result.scene_doc["source"]["paragraphs"] == [1, len(PARAS)]
    assert len(done.result.flagged) == 4  # 推測說話者的標記跟著併回正確的場與行


def test_each_chunk_prompt_only_has_its_paragraphs(tmp_path):
    chunks = plan_chunks(PARAS, 1500)
    llm = ReplayLLM(chunk_replies(chunks))
    run(llm, tmp_path, chunks)
    second = llm.calls[1][1]["content"]
    first_no, last_no = chunks[1][0]["para_no"], chunks[1][-1]["para_no"]
    assert f"第 2 塊：只標註第 {first_no}–{last_no} 段" in second
    assert f"[{first_no}] " in second and f"[{chunks[0][0]['para_no']}] " not in second.split("# 正文")[1]
    assert "上一塊最後一場" in second and "前一章最後一場" not in second


def test_failure_keeps_earlier_chunks_and_resume_skips_them(tmp_path):
    chunks = plan_chunks(PARAS, 1500)
    replies = chunk_replies(chunks)
    broken = "not json"
    first = ReplayLLM([replies[0], broken, broken, broken])
    with pytest.raises(ChunkFailure) as caught:
        run(first, tmp_path, chunks)
    assert caught.value.chunk == 2 and caught.value.total == len(chunks) and len(caught.value.raw) == 3
    store = ChunkStore(tmp_path / "_chunks" / "ch001")
    assert store.load(1) is not None and store.load(2) is None
    # 重跑：只需要剩下的塊的回應；第一塊不會再呼叫模型
    again = ReplayLLM(replies[1:])
    done, _ = run(again, tmp_path, chunks)
    assert done.resumed == 1 and len(again.calls) == len(chunks) - 1
    assert strip_assets(done.result.scene_doc) == strip_assets(GOLD)


def test_fresh_ignores_checkpoints_and_changed_text_invalidates_them(tmp_path):
    chunks = plan_chunks(PARAS, 1500)
    replies = chunk_replies(chunks)
    with pytest.raises(ChunkFailure):
        run(ReplayLLM([replies[0], "x", "x", "x"]), tmp_path, chunks)
    llm = ReplayLLM(replies)
    done, _ = run(llm, tmp_path, chunks, fresh=True)
    assert done.resumed == 0 and len(llm.calls) == len(chunks)
    # 不同切法：舊檢查點作廢
    later = tmp_path / "later"
    with pytest.raises(ChunkFailure):
        run(ReplayLLM([replies[0], "x", "x", "x"]), later, chunks)
    other = plan_chunks(PARAS, 2200)
    llm2 = ReplayLLM(chunk_replies(other))
    done2, _ = run(llm2, later, other)
    assert done2.resumed == 0 and len(llm2.calls) == len(other)


def test_stop_midway_keeps_finished_chunks(tmp_path):
    chunks = plan_chunks(PARAS, 1500)
    replies = chunk_replies(chunks)
    stop = {"now": False}
    llm = ReplayLLM(replies)
    base = llm.__call__

    def calling(messages, on_delta=None, should_stop=None):
        if len(llm.calls) == 1:
            stop["now"] = True
        return base(messages, on_delta=on_delta, should_stop=should_stop)

    hooks = RunHooks(should_stop=lambda: stop["now"])
    with pytest.raises(JobStopped):
        run(calling, tmp_path, chunks, hooks=hooks)
    store = ChunkStore(tmp_path / "_chunks" / "ch001")
    assert store.load(1) is not None


def test_new_characters_survive_resume(tmp_path):
    chunks = plan_chunks(PARAS, 1500)
    replies = chunk_replies(chunks)
    doc = json.loads(replies[0])
    doc["new_characters"] = [{"id": "shop_boss", "name": "工頭", "aliases": [], "role": "minor"}]
    doc["scenes"][0]["ops"][3]["s"] = "shop_boss" if doc["scenes"][0]["ops"][3].get("k") == "d" else doc["scenes"][0]["ops"][3].get("s")
    replies[0] = json.dumps(doc, ensure_ascii=False)
    with pytest.raises(ChunkFailure):
        run(ReplayLLM([replies[0], "x", "x", "x"]), tmp_path, chunks)
    saved = ChunkStore(tmp_path / "_chunks" / "ch001").load(1)
    assert "shop_boss" in saved["added"] and "shop_boss" in saved["added_defs"]
    done, _ = run(ReplayLLM(replies[1:]), tmp_path, chunks)
    assert "shop_boss" in done.result.added_characters and "shop_boss" in done.result.cast_doc["characters"]


def test_merge_joins_same_place_scenes_at_boundary_only():
    def scene(sid, loc, lines, para_start):
        return {"scene_id": sid, "location_id": loc, "time_of_day": "day", "bgm": None, "bg": {"asset_id": f"bg_{loc}_day"},
                "ambience": f"amb_{loc}", "cast": [], "next": None,
                "lines": [{"type": "narration", "text": f"t{i}", "source_para": para_start + i} for i in range(lines)]}
    parts = [([scene("ch001_sc01", "a", 2, 1), scene("ch001_sc02", "b", 2, 3)], {("ch001_sc02", 2)}),
             ([scene("ch001_sc01", "b", 2, 5), scene("ch001_sc02", "b", 2, 7)], {("ch001_sc01", 1)})]
    doc, flagged, notes = merge_chunks("ch001", "bk", "zh-TW", 8, parts)
    assert [s["location_id"] for s in doc["scenes"]] == ["a", "b", "b"]  # 交界的 b+b 合併；第二塊內部的不動
    assert [s["scene_id"] for s in doc["scenes"]] == ["ch001_sc01", "ch001_sc02", "ch001_sc03"]
    assert len(doc["scenes"][1]["lines"]) == 4 and doc["scenes"][-1]["next"] is None and doc["scenes"][0]["next"] == "ch001_sc02"
    assert flagged == {("ch001_sc02", 2), ("ch001_sc02", 3)} and len(notes) == 1


# ---------------------------------------------------------------- 寫檔整合


def test_disk_flow_failure_then_resume(tmp_path):
    cfg, work = write_project(tmp_path)
    from t2vn.config import load_config
    config = load_config(cfg)
    chunks = plan_chunks(PARAS, 1500)
    replies = chunk_replies(chunks)
    notes: dict = {}
    outlines: dict = {}
    result = annotate_chapter_to_disk(config, work, 1, ReplayLLM([replies[0], "x", "x", "x"]), "sample_book", "zh-TW", outlines, notes, {}, chunk_chars=1500)
    assert result.status == "error" and "第 2/" in result.note and "檢查點" in result.note
    assert (work / "scenes" / "_chunks" / "ch001" / "c01.json").is_file() and not (work / "scenes" / "ch001.json").exists()
    assert (work / "scenes" / "_raw" / "ch001_c02_r3.json").is_file()
    log = (work / "scenes" / "_log" / "ch001.log").read_text(encoding="utf-8")
    assert "failed" in log
    llm = ReplayLLM(replies[1:])
    result = annotate_chapter_to_disk(config, work, 1, llm, "sample_book", "zh-TW", outlines, notes, {}, chunk_chars=1500)
    assert result.status == "ok" and len(llm.calls) == len(chunks) - 1
    assert (work / "scenes" / "ch001.json").is_file()
    assert not (work / "scenes" / "_chunks" / "ch001").exists()  # 成功後檢查點清掉
    saved = json.loads((work / "scenes" / "ch001.json").read_text(encoding="utf-8"))
    assert strip_assets(saved) == strip_assets(GOLD)


def test_cli_chunk_chars_option(tmp_path):
    cfg, work = write_project(tmp_path)
    replies = chunk_replies(plan_chunks(PARAS, 1500))
    files = []
    for i, text in enumerate(replies):
        path = tmp_path / f"r{i}.json"
        path.write_text(text, encoding="utf-8")
        files.append(str(path))
    assert main(["--book", "sample_book", "--chapters", "1", "--config", str(cfg), "--chunk-chars", "1500", "--replay", *files]) == 0
    assert (work / "scenes" / "ch001.json").is_file()
