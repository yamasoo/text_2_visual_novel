"""t2x-suite 整合測試：增量章節同步、視覺卡連動漫畫卡、source_dir 與 T2X_* 環境變數 fallback。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from t2vn.config import Config, load_config
from t2vn.studio import Studio


def _make_v3_book(library: Path, folder: str = "demo_book") -> Path:
    book_dir = library / folder
    (book_dir / "manuscript").mkdir(parents=True)
    (book_dir / "bible" / "visual-cards").mkdir(parents=True)
    (book_dir / "book.json").write_text(
        json.dumps({"format_version": 3, "title": "測試作品", "lang": "zh-TW"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (book_dir / "manuscript" / "ch-0001.md").write_text(
        "# 第一章 起點\n\n第一段文字。\n\n第二段文字。\n",
        encoding="utf-8",
    )
    (book_dir / "bible" / "visual-cards" / "hero.md").write_text(
        "---\n"
        "id: hero\n"
        "name: 主角\n"
        "aliases:\n"
        "  - 阿主\n"
        "status: confirmed\n"
        "sections:\n"
        "  head:\n"
        "    - 20-year-old woman\n"
        "    - black hair\n"
        "  body:\n"
        "    - slim build\n"
        "  upper: []\n"
        "  lower: []\n"
        "  accessory:\n"
        "    - silver hairpin\n"
        "outfits:\n"
        "  default:\n"
        "    top:\n"
        "      - white shirt\n"
        "    bottom:\n"
        "      - blue skirt\n"
        "    set: []\n"
        "vn:\n"
        "  sprite_pose: standing straight\n"
        "  expressions:\n"
        "    neutral: calm look\n"
        "---\n",
        encoding="utf-8",
    )
    return book_dir


def _make_studio(tmp_path: Path) -> tuple[Studio, Path]:
    library = tmp_path / "library"
    library.mkdir()
    project_root = tmp_path / "proj"
    project_root.mkdir()
    cfg = Config(project_root=project_root, book_library=library)
    return Studio(cfg), library


def test_list_books_includes_source_dir_and_incremental_chapter_sync(tmp_path: Path) -> None:
    studio, library = _make_studio(tmp_path)
    book_dir = _make_v3_book(library, "demo_book")

    imp = studio.import_library_book("demo_book", book_id="demo", init_comic=False)
    assert imp["book_id"] == "demo"
    assert imp["chapters"] == 1

    # list_books 需回傳 source_dir 供 Portal 與深連結比對
    listed = studio.list_books()
    assert len(listed) == 1
    assert listed[0]["source_dir"] == "demo_book"

    # 模擬第一章已有校對劇本
    work = studio.work("demo")
    scenes_dir = work / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    scene_file = scenes_dir / "ch001.json"
    scene_file.write_text('{"reviewed": true, "keep": "untouched"}', encoding="utf-8")

    # 尚未新增章節時：0 新增、0 修訂
    diff0 = studio.check_library_chapters("demo")
    assert diff0["has_library"] is True
    assert diff0["added"] == []
    assert diff0["modified"] == []
    assert diff0["unchanged"] == 1

    # 在書庫新增第 2 章並修訂第 1 章
    (book_dir / "manuscript" / "ch-0001.md").write_text(
        "# 第一章 起點（修訂）\n\n修改後的第一段。\n",
        encoding="utf-8",
    )
    (book_dir / "manuscript" / "ch-0002.md").write_text(
        "# 第二章 啟程\n\n新章第一段。\n",
        encoding="utf-8",
    )

    diff1 = studio.check_library_chapters("demo")
    assert [c["chapter_no"] for c in diff1["added"]] == [2]
    assert [c["chapter_no"] for c in diff1["modified"]] == [1]
    assert diff1["modified"][0]["has_scenes"] is True

    # 預設只同步新增章節，略過已修訂舊章
    res1 = studio.sync_library_chapters("demo", include_modified=False)
    assert res1["added"] == [2]
    assert res1["updated"] == []
    assert res1["skipped_modified"] == [1]
    assert res1["total"] == 2
    assert len(studio.chapters("demo")) == 2
    assert json.loads(scene_file.read_text(encoding="utf-8"))["keep"] == "untouched"

    # 勾選 include_modified 才更新第 1 章正文，但依然不動 scenes/
    res2 = studio.sync_library_chapters("demo", include_modified=True)
    assert res2["added"] == []
    assert res2["updated"] == [1]
    assert json.loads(scene_file.read_text(encoding="utf-8"))["keep"] == "untouched"
    ch1 = json.loads((work / "chapters" / "ch001.json").read_text(encoding="utf-8"))
    assert ch1["paragraphs"][0]["text"] == "修改後的第一段。"


def test_sync_visual_cards_auto_updates_comic_bibles(tmp_path: Path) -> None:
    studio, library = _make_studio(tmp_path)
    book_dir = _make_v3_book(library, "demo_book")

    imp = studio.import_library_book("demo_book", book_id="demo", init_comic=True)
    assert imp["comic_project"] == "main"

    # 更新書庫視覺卡，再執行 sync_library_visual_cards(overwrite=True)
    (book_dir / "bible" / "visual-cards" / "hero.md").write_text(
        "---\n"
        "id: hero\n"
        "name: 主角\n"
        "status: confirmed\n"
        "sections:\n"
        "  head:\n"
        "    - 20-year-old woman\n"
        "    - silver hair\n"
        "  body:\n"
        "    - slim build\n"
        "  upper: []\n"
        "  lower: []\n"
        "  accessory: []\n"
        "outfits:\n"
        "  default:\n"
        "    top:\n"
        "      - red jacket\n"
        "    bottom:\n"
        "      - black pants\n"
        "    set: []\n"
        "vn:\n"
        "  sprite_pose: standing straight\n"
        "---\n",
        encoding="utf-8",
    )

    report = studio.sync_library_visual_cards("demo", overwrite=True)
    assert "main" in report["comic_projects_synced"]
    comic_card = studio.work("demo") / "comic" / "main" / "bible" / "characters" / "hero.md"
    assert comic_card.is_file()
    content = comic_card.read_text(encoding="utf-8")
    assert "silver hair" in content
    assert "red jacket" in content


def test_t2x_env_fallbacks_do_not_override_explicit_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib_env = tmp_path / "env_lib"
    lib_env.mkdir()
    lib_explicit = tmp_path / "explicit_lib"
    lib_explicit.mkdir()

    monkeypatch.setenv("T2X_LIBRARY_DIR", str(lib_env))
    monkeypatch.setenv("T2X_COMFYUI_URL", "http://127.0.0.1:9199")
    monkeypatch.setenv("T2X_TAGCOMPLETE_CSV", str(tmp_path / "env_tags.csv"))

    # 空白設定 → 吃環境變數 fallback
    cfg_blank = tmp_path / "blank.yaml"
    cfg_blank.write_text(
        f"project_root: {tmp_path / 'proj1'}\n"
        "book_library:\n"
        "  path: \"\"\n"
        "  read_only: true\n"
        "comfyui: {}\n"
        "tagcomplete: {}\n",
        encoding="utf-8",
    )
    loaded1 = load_config(cfg_blank)
    assert loaded1.book_library == lib_env
    assert loaded1.comfyui_base_url == "http://127.0.0.1:9199"
    assert loaded1.tag_csv == str(tmp_path / "env_tags.csv")

    # 已有明確設定 → 不被環境變數覆蓋
    cfg_set = tmp_path / "set.yaml"
    cfg_set.write_text(
        f"project_root: {tmp_path / 'proj2'}\n"
        "book_library:\n"
        f"  path: {lib_explicit}\n"
        "  read_only: true\n"
        "comfyui:\n"
        "  base_url: http://127.0.0.1:8188\n"
        "tagcomplete:\n"
        f"  tag_csv: {tmp_path / 'my_tags.csv'}\n",
        encoding="utf-8",
    )
    loaded2 = load_config(cfg_set)
    assert loaded2.book_library == lib_explicit
    assert loaded2.comfyui_base_url == "http://127.0.0.1:8188"
    assert loaded2.tag_csv == str(tmp_path / "my_tags.csv")
