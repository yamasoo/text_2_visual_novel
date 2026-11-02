import copy
import json
from pathlib import Path

import pytest

from t2vn.annotate import (AnnotateError, ChapterContext, annotate_chapter, build_messages, expand, main,
                           has_quoted_speech, merge_new_characters, parse_chapter_range, parse_response, split_text, unfaithful_clauses)
from t2vn.llm import LlmError, ReplayLLM, make_llm

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).parent / "fixtures"
WORK = ROOT / "works" / "sample_book"
# 這組測試拿作者本機的真實書（works/sample_book）當黃金集。works/ 內含小說原文，
# 不進 repo；沒有資料時整組跳過而不是失敗（clone 後仍要能綠燈）。
if not all(p.is_file() for p in ((WORK / "chapters" / "ch001.json"), (WORK / "gold" / "ch001.json"),
                                 (FIX / "ch001_ops.json"))):
    pytest.skip("needs works/sample_book gold set (not committed, see .gitignore)", allow_module_level=True)
CHAPTER = json.loads((WORK / "chapters" / "ch001.json").read_text(encoding="utf-8"))
GOLD = json.loads((WORK / "gold" / "ch001.json").read_text(encoding="utf-8"))
CAST = json.loads((WORK / "cast.json").read_text(encoding="utf-8"))
OPS = json.loads((FIX / "ch001_ops.json").read_text(encoding="utf-8"))
# 只取第 1 章出場的角色，避免之後 cast.json 增加角色影響測試
CAST = {**CAST, "characters": {k: v for k, v in CAST["characters"].items()
                               if k in {"ellie_reed", "lao_zhang", "chen_boatman", "harbor_skipper", "a_ming", "player"}}}


def strip_assets(doc):
    doc = copy.deepcopy(doc)
    for scene in doc["scenes"]:
        scene.pop("bg"), scene.pop("ambience")
    return doc


def test_gold_ops_expand_to_gold():
    result = expand(OPS, CHAPTER, CAST, "sample_book", "zh-TW")
    assert result.issues == []
    assert strip_assets(result.scene_doc) == strip_assets(GOLD)
    assert result.scene_doc["scenes"][0]["bg"] == {"asset_id": "bg_scrapyard_dawn"}
    assert len(result.flagged) == 4


def ops_with(mutator):
    doc = copy.deepcopy(OPS)
    mutator(doc)
    return doc


def issues_of(doc):
    return expand(doc, CHAPTER, CAST, "sample_book", "zh-TW").issues


def test_missing_paragraphs_reported():
    def drop(doc):
        doc["scenes"][3]["ops"] = doc["scenes"][3]["ops"][:1]
    assert any("49–51" in i for i in issues_of(ops_with(drop)))


def test_unknown_speaker_and_bad_expr():
    def bad(doc):
        doc["scenes"][0]["ops"][4]["s"] = "ghost"
        doc["scenes"][0]["ops"][9]["e"] = "furious"
    found = issues_of(ops_with(bad))
    assert any("ghost" in i for i in found) and any("furious" in i for i in found)


def test_rewritten_narration_is_rejected():
    def rewrite(doc):
        doc["scenes"][0]["ops"][3]["t"] = "她用力拍了一下工具箱，大聲叫老張。"
    assert any("verbatim" in i for i in issues_of(ops_with(rewrite)))


def test_order_must_not_go_backwards():
    def swap(doc):
        ops = doc["scenes"][0]["ops"]
        ops[0], ops[1] = ops[1], ops[0]
    assert any("backwards" in i for i in issues_of(ops_with(swap)))


def test_bad_scene_fields():
    def bad(doc):
        doc["scenes"][0]["location_id"] = "舊工坊"
        doc["scenes"][0]["time_of_day"] = "noon"
        doc["scenes"][1]["weather"] = "sunny"
        doc["scenes"][1]["cast"].append({"id": "nobody"})
    found = " ".join(issues_of(ops_with(bad)))
    for word in ("location_id", "noon", "sunny", "nobody"):
        assert word in found


def test_unfaithful_clauses_rules():
    src = "「好了，」她拍拍工具箱，朝老張喊，「試一趟看看。」"
    assert unfaithful_clauses("好了，試一趟看看。", src) == []
    assert unfaithful_clauses("她拍拍工具箱。", src) == []
    assert unfaithful_clauses("他朝老張喊。", src) == []  # 句首補代名詞可接受
    assert unfaithful_clauses("她非常生氣。", src) == ["她非常生氣"]


def test_split_text_limits():
    long = "，".join(["這是一段很長的句子" * 3] * 6) + "。"
    assert all(len(x) <= 90 for x in split_text(long))
    assert all(len(x) <= 90 for x in split_text("字" * 300))
    assert "".join(split_text(long)) == long


def test_parse_response_fences_and_noise():
    assert parse_response('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_response('好的：{"a": 1} 完成') == {"a": 1}
    with pytest.raises(AnnotateError):
        parse_response("沒有 JSON")


def test_merge_new_characters():
    new = [{"id": "shop_boss", "name": "工頭", "aliases": ["工頭"], "role": "minor"},
           {"id": "老人", "name": "老漁民", "role": "minor"},
           {"id": "bad_alias", "name": "X", "aliases": ["老張"], "role": "minor"}]
    roles = dict([("工頭", "supporting")])
    doc, added, issues = merge_new_characters(CAST, new, "ch002", "zh-TW", roles)
    assert added == ["shop_boss"]
    char = doc["characters"]["shop_boss"]
    assert char["role"] == "supporting" and char["first_chapter"] == "ch002" and 0 <= char["hue"] <= 360
    assert len(issues) == 2
    assert "shop_boss" not in CAST["characters"]  # 原本的不被改動


def context():
    return ChapterContext(CHAPTER, CAST, {"plot_points": "1. 補囊", "forbidden": "SECRET"}, dict([("老張", "船主")]),
                          ["scrapyard"], ["bgm_tension"],
                          {"location_id": "scrapyard", "time_of_day": "dawn"})


def test_build_messages():
    messages = build_messages(context())
    user = messages[1]["content"]
    assert messages[0]["role"] == "system" and "N 整段旁白" in messages[0]["content"]
    assert "[1] 天還沒亮透" in user and "[51] " in user
    assert '"id": "lao_zhang"' in user and '"note": "船主"' in user
    assert "情節點" in user and "SECRET" not in user and "bgm_tension" in user and "前一章最後一場" in user


def test_annotate_with_repair_round():
    bad = ops_with(lambda d: d["scenes"][0]["ops"].pop(0))
    llm = ReplayLLM([bad, OPS])
    result = annotate_chapter(context(), llm, "sample_book", "zh-TW")
    assert result.rounds == 2 and len(result.raw) == 2
    feedback = llm.calls[1][-1]["content"]
    assert "paragraphs not used: 1" in feedback
    assert strip_assets(result.scene_doc) == strip_assets(GOLD)


def test_annotate_gives_up():
    llm = ReplayLLM(["not json", "still not", "nope"])
    with pytest.raises(AnnotateError) as info:
        annotate_chapter(context(), llm, "sample_book", "zh-TW", max_repairs=2)
    assert len(info.value.raw) == 3 and info.value.issues


def test_annotate_adds_new_character():
    def add(doc):
        doc["new_characters"] = [{"id": "shop_boss", "name": "工頭", "aliases": [], "role": "minor"}]
        doc["scenes"][1]["ops"][0]["s"] = "shop_boss"
    result = annotate_chapter(context(), ReplayLLM([ops_with(add)]), "sample_book", "zh-TW")
    assert result.added_characters == ["shop_boss"]
    assert result.scene_doc["scenes"][1]["lines"][0]["speaker"] == "shop_boss"


def test_parse_chapter_range():
    assert parse_chapter_range("2-4") == [2, 3, 4] and parse_chapter_range("7") == [7]
    with pytest.raises(Exception):
        parse_chapter_range("5-2")


def test_make_llm_validation():
    with pytest.raises(LlmError):
        make_llm("nope", "m")
    with pytest.raises(LlmError):
        make_llm("deepseek", "m", thinking="extreme")


def write_project(tmp_path):
    project = tmp_path / "proj"
    work = project / "works" / "sample_book"
    (work / "chapters").mkdir(parents=True)
    (work / "chapters" / "ch001.json").write_text(json.dumps(CHAPTER, ensure_ascii=False), encoding="utf-8")
    (work / "cast.json").write_text(json.dumps(CAST, ensure_ascii=False), encoding="utf-8")
    library = tmp_path / "lib"
    (library / "book").mkdir(parents=True)
    cfg = project / "config.yaml"
    cfg.write_text(
        f"project_root: {json.dumps(str(project))}\n"
        f"book_library:\n  path: {json.dumps(str(library))}\n  access: read_only\n"
        "books:\n  - book_id: sample_book\n    source_dir: book\n    lang: zh-TW\n    display_name:\n      zh-TW: x\n",
        encoding="utf-8")
    return cfg, work


def test_cli_dry_run_and_replay(tmp_path):
    cfg, work = write_project(tmp_path)
    assert main(["--book", "sample_book", "--chapters", "1", "--config", str(cfg), "--dry-run"]) == 0
    assert (work / "scenes" / "_prompt" / "ch001.md").is_file()

    def add(doc):
        doc["new_characters"] = [{"id": "shop_boss", "name": "工頭", "aliases": [], "role": "minor"}]
        doc["scenes"][1]["ops"][0]["s"] = "shop_boss"
    replay = tmp_path / "r1.json"
    replay.write_text(json.dumps(ops_with(add), ensure_ascii=False), encoding="utf-8")
    args = ["--book", "sample_book", "--chapters", "1", "--config", str(cfg), "--replay", str(replay)]
    assert main(args) == 0
    assert (work / "scenes" / "ch001.json").is_file() and (work / "scenes" / "ch001_review.md").is_file()
    assert (work / "scenes" / "_raw" / "ch001_r1.json").is_file()
    assert "shop_boss" in json.loads((work / "cast.json").read_text(encoding="utf-8"))["characters"]
    assert main(args) == 0  # 已存在 -> 跳過，不呼叫 LLM


def test_cli_failure_keeps_raw(tmp_path):
    cfg, work = write_project(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    args = ["--book", "sample_book", "--chapters", "1", "--config", str(cfg), "--replay", str(bad), str(bad), str(bad)]
    assert main(args) == 1
    assert (work / "scenes" / "_raw" / "ch001_r3.json").is_file()
    assert not (work / "scenes" / "ch001.json").exists()


def test_n_on_paragraph_with_speech_is_rejected():
    # 第 4 段整段都是話與說話標記：用 N 會把台詞當旁白再念一次
    def bad(doc):
        doc["scenes"][0]["ops"][3:5] = [{"k": "N", "p": 4}]
    found = issues_of(ops_with(bad))
    assert any("quoted speech" in i and "paragraph 4" in i for i in found)


def test_quoted_name_is_not_speech():
    from t2vn.annotate import has_quoted_speech
    assert has_quoted_speech("擴音器說：「前方小船，報船號。」")
    assert not has_quoted_speech("她寫了「灰船」兩個字")
    assert not has_quoted_speech("封面寫著「漁季徵收帳」。")


def test_scene_too_long_is_split_with_warning():
    def merge(doc):
        first = doc["scenes"][0]
        for other in doc["scenes"][1:]:
            first["ops"] += other["ops"]
        doc["scenes"] = [first]
    result = expand(ops_with(merge), CHAPTER, CAST, "sample_book", "zh-TW")
    assert not any("limit 35" in i for i in result.issues)  # 不再退回重做
    assert any("35" in w for w in result.warnings)  # 但留下提醒請人確認切點
    scenes = result.scene_doc["scenes"]
    assert len(scenes) > 1
    assert all(len({l["source_para"] for l in sc["lines"] if "source_para" in l}) <= 35 for sc in scenes)


def test_wrong_paragraph_number_gets_a_hint():
    def shift(doc):
        doc["scenes"][0]["ops"][4]["p"] = 5   # 這句話其實在第 4 段
    found = issues_of(ops_with(shift))
    assert any("set p to 4" in i for i in found)


def test_speaker_missing_from_scene_cast_is_added():
    def drop(doc):
        doc["scenes"][1]["cast"] = [c for c in doc["scenes"][1]["cast"] if c["id"] != "harbor_skipper"]
    result = expand(ops_with(drop), CHAPTER, CAST, "sample_book", "zh-TW")
    assert result.issues == []
    cast = {c["char_id"]: c for c in result.scene_doc["scenes"][1]["cast"]}
    assert "harbor_skipper" in cast and cast["harbor_skipper"]["expr"] == "worried"
    assert len({c["pos"] for c in cast.values()}) == len(cast)


# ---- 匯入的書常混用半形標點（G11 第 3 章：原文「說明著:」，模型改寫成「說明著.」被判不逐字）

ASCII_SRC = '楊走到身邊,操作著電腦,過指點邊說明著:"只要把順序改變一下就可以了.""……這樣．你是要我們見死不救？"'


def test_unfaithful_ignores_ascii_punctuation_differences():
    assert unfaithful_clauses("楊走到身邊,操作著電腦,過指點邊說明著.", ASCII_SRC) == []
    assert unfaithful_clauses("楊走到身邊，操作著電腦，過指點邊說明著。", ASCII_SRC) == []
    assert unfaithful_clauses("只要把順序改變一下就可以了.", ASCII_SRC) == []
    assert unfaithful_clauses("……這樣．你是要我們見死不救？", ASCII_SRC) == []


def test_unfaithful_still_catches_added_words_with_ascii_punctuation():
    assert unfaithful_clauses("楊走到身邊,操作著電腦,過指點邊說明著.但是和之前不同", ASCII_SRC) == ["但是和之前不同"]


def test_ascii_and_curly_quotes_count_as_speech():
    assert has_quoted_speech(ASCII_SRC)
    assert has_quoted_speech("他說:\"走吧!\"")
    assert has_quoted_speech("他說:“走吧。”")
    assert not has_quoted_speech('那艘叫"灰船"的船靠岸了')
