import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from t2vn import art_gen
from t2vn.art_gen import ArtGenError, GenReport, generate, list_candidates, pending_assets, refresh_manifest, resolve_profile, select_candidate, set_status
from t2vn.config import parse_config
from t2vn.errors import JobStopped

CAST = {"book_id": "bk", "characters": {"ellie": {"names": {"zh-TW": "艾莉"}}}}
SCENES = {"scenes": [{"scene_id": "ch001_sc01", "location_id": "yard", "time_of_day": "dawn", "bg": {"asset_id": "bg_yard_dawn"},
                      "cast": [{"char_id": "ellie", "expr": "neutral", "pos": "center"}], "bgm": "bgm_a",
                      "lines": [{"type": "narration", "text": "x", "source_para": 1}]}]}


def png(kind="bg", size=(1344, 768)):
    image = Image.new("RGB", size, (255, 255, 255) if kind == "sprite" else (40, 90, 160))
    if kind == "sprite":
        ImageDraw.Draw(image).rectangle([size[0] // 4, size[1] // 5, size[0] * 3 // 4, size[1] - 2], fill=(190, 40, 40))
    import io
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


class Fake:
    def __init__(self, fail_on=None, stop_after=None, lists=None):
        self.calls = []
        self.fail_on = fail_on or set()
        self.stop_after = stop_after
        self.lists = lists if lists is not None else {"unet": ["anima-preview.safetensors"], "clip": ["qwen_3_06b_base.safetensors"], "vae": ["qwen_image_vae.safetensors"], "lora": []}

    def models(self):
        return self.lists

    def prepare(self, profile, positive, negative, seed, width, height):
        self.calls.append({"profile": profile, "positive": positive, "negative": negative, "seed": seed, "size": (width, height)})
        return {"seed": seed, "size": (width, height), "positive": positive}

    def run(self, job, should_stop):
        if self.stop_after is not None and len(self.calls) > self.stop_after:
            raise JobStopped("stopped")
        if "sprite" in job["positive"] or "solo" in job["positive"].split(".")[0]:
            kind = "sprite"
        else:
            kind = "bg"
        if any(w in job["positive"] for w in self.fail_on):
            from t2vn.art_gen import ImageFailure
            raise ImageFailure("node 5 failed")
        return [png(kind, job["size"])]


@pytest.fixture
def env(tmp_path):
    project = tmp_path / "project"
    work = project / "works" / "bk"
    (work / "scenes").mkdir(parents=True)
    (work / "scenes" / "ch001.json").write_text(json.dumps(SCENES, ensure_ascii=False), encoding="utf-8")
    (work / "cast.json").write_text(json.dumps(CAST, ensure_ascii=False), encoding="utf-8")
    library = tmp_path / "library"
    (library / "b").mkdir(parents=True)
    config = parse_config({"project_root": str(project), "book_library": {"path": str(library), "access": "read_only"},
                           "books": [{"book_id": "bk", "source_dir": "b", "lang": "zh-TW", "display_name": {"zh-TW": "書"}}]})
    refresh_manifest(config, work, "bk")
    return config, work


def asset(work, asset_id):
    return next(a for a in json.loads((work / "manifest.json").read_text(encoding="utf-8"))["assets"] if a["asset_id"] == asset_id)


def test_pending_assets_selection(env):
    config, work = env
    manifest = art_gen.load_manifest(work)
    assert [a["asset_id"] for a in pending_assets(manifest)] == ["bg_yard_dawn", "sprite_ellie_neutral"]  # 音訊不出圖
    assert [a["asset_id"] for a in pending_assets(manifest, only=["sprite_ellie_neutral"])] == ["sprite_ellie_neutral"]
    manifest["assets"][0]["status"] = "generated"
    assert [a["asset_id"] for a in pending_assets(manifest)] == ["sprite_ellie_neutral"]
    assert [a["asset_id"] for a in pending_assets(manifest, regenerate=True)] == ["bg_yard_dawn", "sprite_ellie_neutral"]
    manifest["assets"][1]["used_in"] = []
    assert [a["asset_id"] for a in pending_assets(manifest)] == []


def test_generate_makes_candidates_and_updates_manifest(env):
    config, work = env
    backend = Fake()
    events = []
    report = generate(config, work, backend, candidates=2, progress=events.append)
    assert report.generated == ["bg_yard_dawn", "sprite_ellie_neutral"] and not report.failed and not report.stopped
    assert len(backend.calls) == 4 and len({c["seed"] for c in backend.calls}) == 4
    bg = asset(work, "bg_yard_dawn")
    assert bg["status"] == "generated" and bg["prompt_en"].startswith("masterpiece") and bg["model"] == "anima-preview.safetensors" and isinstance(bg["seed"], int)
    cands = list_candidates(work, "bg_yard_dawn")
    assert len(cands) == 2 and all(c["meta"]["asset_id"] == "bg_yard_dawn" for c in cands) and cands[0]["file"].startswith("assets/_candidates/bg_yard_dawn/")
    assert (backend.calls[0]["size"], backend.calls[2]["size"]) == ((1280, 720), (672, 1568))
    assert backend.calls[0]["profile"]["unet"] == "anima-preview.safetensors"
    assert [e["event"] for e in events].count("image") == 4 and [e for e in events if e["event"] == "asset_done"][-1]["index"] == 2
    assert any("yard" in w for w in report.warnings)  # 沒有地點設定的提醒


def test_resume_only_fills_missing_candidates(env):
    config, work = env
    generate(config, work, Fake(), candidates=2, only=["bg_yard_dawn"])
    seeds = {c["seed"] for c in list_candidates(work, "bg_yard_dawn")}
    manifest = art_gen.load_manifest(work)
    manifest["assets"][0]["status"] = "planned"
    (work / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    backend = Fake()
    report = generate(config, work, backend, candidates=3, only=["bg_yard_dawn"])
    assert len(backend.calls) == 1 and report.generated == ["bg_yard_dawn"]
    assert seeds < {c["seed"] for c in list_candidates(work, "bg_yard_dawn")} and len(list_candidates(work, "bg_yard_dawn")) == 3
    again = Fake()
    assert generate(config, work, again, candidates=3, only=["bg_yard_dawn"]).skipped == ["bg_yard_dawn"] and again.calls == []


def test_regenerate_adds_new_candidates_without_touching_old(env):
    config, work = env
    generate(config, work, Fake(), candidates=2, only=["bg_yard_dawn"])
    old = {c["name"]: (work / c["file"]).read_bytes() for c in list_candidates(work, "bg_yard_dawn")}
    generate(config, work, Fake(), candidates=2, only=["bg_yard_dawn"], regenerate=True)
    new = list_candidates(work, "bg_yard_dawn")
    assert len(new) == 4 and all((work / c["file"]).read_bytes() == old[c["name"]] for c in new if c["name"] in old)


def test_failure_is_isolated_and_keeps_saved_candidates(env):
    config, work = env
    report = generate(config, work, Fake(fail_on={"solo"}), candidates=2)
    assert report.generated == ["bg_yard_dawn"] and list(report.failed) == ["sprite_ellie_neutral"] and "node 5" in report.failed["sprite_ellie_neutral"]
    assert asset(work, "sprite_ellie_neutral")["status"] == "planned" and asset(work, "bg_yard_dawn")["status"] == "generated"


def test_stop_saves_progress(env):
    config, work = env
    report = generate(config, work, Fake(stop_after=1), candidates=3)
    assert report.stopped and len(list_candidates(work, "bg_yard_dawn")) == 1
    assert asset(work, "bg_yard_dawn")["status"] == "planned"  # 沒做完不標 generated
    calls = []
    report = generate(config, work, Fake(), candidates=1, should_stop=lambda: calls.append(1) or len(calls) > 1)
    assert report.stopped


def test_resolve_profile_guesses_models_and_reports_missing():
    profile = resolve_profile(Fake(), {})
    assert profile["unet"] == "anima-preview.safetensors" and profile["clip"].startswith("qwen_3_06b") and profile["vae"] == "qwen_image_vae.safetensors"
    chosen = resolve_profile(Fake(), {"comfy": {"unet": "mine.safetensors", "clip": "c", "vae": "v"}})
    assert (chosen["unet"], chosen["clip"], chosen["vae"]) == ("mine.safetensors", "c", "v")
    with pytest.raises(ArtGenError, match="unet"):
        resolve_profile(Fake(lists={"unet": [], "clip": ["qwen_3_06b"], "vae": ["qwen_image_vae"], "lora": []}), {})


def test_select_background_and_sprite(env):
    config, work = env
    generate(config, work, Fake(), candidates=1)
    bg_name = list_candidates(work, "bg_yard_dawn")[0]["name"]
    item = select_candidate(config, work, "bg_yard_dawn", bg_name)
    out = Image.open(work / "assets/bg/bg_yard_dawn.png")
    assert out.size == (1920, 1080) and item["status"] == "processed" and asset(work, "bg_yard_dawn")["status"] == "processed"
    sp_name = list_candidates(work, "sprite_ellie_neutral")[0]["name"]
    select_candidate(config, work, "sprite_ellie_neutral", sp_name)
    sprite = Image.open(work / "assets/sprite/ellie_neutral.png")
    assert sprite.size == (800, 1600) and sprite.mode == "RGBA" and sprite.getpixel((2, 2))[3] == 0 and sprite.getpixel((400, 1590))[3] == 255


def test_select_validates(env):
    config, work = env
    generate(config, work, Fake(), candidates=1)
    for name in ("../x.png", "a/b.png", "x.jpg", "999.png"):
        with pytest.raises(ArtGenError):
            select_candidate(config, work, "bg_yard_dawn", name)
    with pytest.raises(ArtGenError):
        select_candidate(config, work, "bgm_a", "1.png")
    with pytest.raises(ArtGenError):
        select_candidate(config, work, "Bad Id", "1.png")


def test_16_9_crop_of_other_aspect(env):
    config, work = env
    folder = work / "assets/_candidates/bg_yard_dawn"
    folder.mkdir(parents=True)
    Image.new("RGB", (1000, 1000), (1, 2, 3)).save(folder / "5.png")
    select_candidate(config, work, "bg_yard_dawn", "5.png")
    assert Image.open(work / "assets/bg/bg_yard_dawn.png").size == (1920, 1080)


def test_set_status_requires_license_for_final(env):
    config, work = env
    with pytest.raises(ArtGenError, match="license"):
        set_status(config, work, "bg_yard_dawn", "final")
    assert set_status(config, work, "bg_yard_dawn", "final", "CC0")["status"] == "final"
    with pytest.raises(ArtGenError):
        set_status(config, work, "bg_yard_dawn", "weird")


def test_refresh_manifest_keeps_progress(env):
    config, work = env
    generate(config, work, Fake(), candidates=1)
    manifest = refresh_manifest(config, work, "bk")
    assert [a for a in manifest["assets"] if a["asset_id"] == "bg_yard_dawn"][0]["status"] == "generated"


def test_writes_stay_inside_allowed_roots(env, tmp_path):
    config, work = env
    from t2vn.errors import WriteProtectedError
    with pytest.raises(WriteProtectedError):
        art_gen._write_bytes(config, tmp_path / "library" / "b" / "x.png", b"x")


def test_cli_refresh_status_select(env, tmp_path, capsys):
    config, work = env
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"project_root: {json.dumps(str(config.project_root))}\nbook_library:\n  path: {json.dumps(str(config.book_library))}\n  access: read_only\n"
                   "books:\n  - book_id: bk\n    source_dir: b\n    lang: zh-TW\n    display_name:\n      zh-TW: 書\n", encoding="utf-8")
    assert art_gen.main(["--book", "bk", "--config", str(cfg), "refresh"]) == 0
    assert art_gen.main(["--book", "bk", "--config", str(cfg), "status"]) == 0
    assert "bg: planned 2" in capsys.readouterr().out
    assert art_gen.main(["--book", "bk", "--config", str(cfg), "select", "bg_yard_dawn", "1.png"]) == 1


def test_vendor_backend_prepares_the_bundled_template():
    backend = art_gen.VendorBackend("http://127.0.0.1:1")
    profile = {"kind": "anima", "unet": "u.safetensors", "clip": "c.safetensors", "vae": "v.safetensors", "loras": []}
    wf, out_id = backend.prepare(profile, "1girl, solo", "bad", 123, 768, 1344)
    by_class = {n["class_type"]: n for n in wf.values()}
    assert by_class["UNETLoader"]["inputs"]["unet_name"] == "u.safetensors" and by_class["CLIPLoader"]["inputs"]["clip_name"] == "c.safetensors"
    assert by_class["VAELoader"]["inputs"]["vae_name"] == "v.safetensors"
    assert by_class["KSampler"]["inputs"]["seed"] == 123 and by_class["EmptySD3LatentImage"]["inputs"]["width"] == 768
    assert any(n["inputs"].get("text") == "1girl, solo" for n in wf.values() if n["class_type"] == "CLIPTextEncode")
    assert wf[out_id]["class_type"] == "PreviewImage"
    with pytest.raises(ArtGenError):
        backend.prepare({**profile, "unet": ""}, "x", "y", 1, 64, 64)  # 沒選模型
    with pytest.raises(ArtGenError):
        art_gen.VendorBackend("http://127.0.0.1:1", "/nonexistent/wf.json").prepare(profile, "x", "y", 1, 64, 64)
    with pytest.raises(ArtGenError):
        backend.models()  # 連不到 ComfyUI → 好讀的錯誤


def test_vendor_backend_submit_includes_widget_idx_map(monkeypatch):
    from vendor.novel_studio import comfy

    sent = []

    def fake_req(base, path, data=None, timeout=10, raw=False):
        if path == "/prompt":
            sent.append(data)
            return {"prompt_id": "p1"}
        if path.startswith("/history/"):
            return {"p1": {"status": {"completed": True}, "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "temp"}]}}}}
        if path.startswith("/view"):
            return b"PNGDATA"
        return {}

    monkeypatch.setattr(comfy, "_req", fake_req)
    original = comfy.submit
    backend = art_gen.VendorBackend("http://x")
    images = backend.run(({"1": {"class_type": "X", "inputs": {}}}, "9"), lambda: False)
    assert images == [b"PNGDATA"] and sent[0]["extra_data"]["extra_pnginfo"]["workflow"] == {"nodes": [], "widget_idx_map": {}}
    assert comfy.submit is original  # 用完還原
