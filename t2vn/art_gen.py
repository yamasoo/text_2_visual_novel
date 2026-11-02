"""素材出圖：依 ``manifest.json`` 逐項用 ComfyUI 產生候選圖，挑選後處理成遊戲用的圖。

規格見 ``docs/31_美術管線規格.md``。可從失敗處續跑（已有的候選圖不重畫）、可隨時停止。
ComfyUI 透過 ``ComfyBackend`` 介面接入（預設 ``VendorBackend`` 包裝 ``vendor/novel_studio/comfy.py``），測試用假的後端。
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from PIL import Image

from . import art_prompts, asset_manifest, character_assets, sprite_consistency
from .asset_manifest import build_manifest, collect_needs
from .errors import JobStopped, T2vnError
from .sprite_cutout import cutout

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW = REPO_ROOT / "config" / "comfy" / "anima.api.json"
CANDIDATES_DIR = "_candidates"
DEFAULT_CANDIDATES = 4
GEN_KINDS = ("bg", "sprite", "headshot")
BG_SIZE = (1920, 1080)
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")


class ArtGenError(T2vnError):
    """出圖設定或素材資料有問題（不是單張圖失敗）。"""


class ComfyBackend(Protocol):
    """出圖後端。``prepare`` 把提示詞放進 workflow；``run`` 送出並等待，回傳圖片 bytes 清單。"""

    def models(self) -> dict[str, list[str]]: ...
    def prepare(self, profile: dict[str, Any], positive: str, negative: str, seed: int, width: int, height: int) -> Any: ...
    def run(self, job: Any, should_stop: Callable[[], bool]) -> list[bytes]: ...


class VendorBackend:
    """包裝 ``vendor.novel_studio.comfy``（連 ComfyUI、改 workflow、送出、取圖）。"""

    def __init__(self, base_url: str, workflow_path: Path | str | None = None) -> None:
        from vendor.novel_studio import comfy

        self.comfy = comfy
        self.base_url = base_url
        self.workflow_path = Path(workflow_path) if workflow_path else DEFAULT_WORKFLOW
        self._workflow: dict[str, Any] | None = None

    def _wf(self) -> dict[str, Any]:
        if self._workflow is None:
            try:
                self._workflow = self.comfy.load_workflow(str(self.workflow_path))
            except (self.comfy.ComfyError, OSError) as exc:
                raise ArtGenError(f"cannot load workflow {self.workflow_path}: {exc}") from exc
        return self._workflow

    def models(self) -> dict[str, list[str]]:
        try:
            return self.comfy.model_lists(self.comfy.object_info(self.base_url))
        except self.comfy.ComfyError as exc:
            raise ArtGenError(str(exc)) from exc

    def prepare(self, profile: dict[str, Any], positive: str, negative: str, seed: int, width: int, height: int) -> Any:
        try:
            return self.comfy.prepare(self._wf(), profile, positive, negative, seed, width, height, count=1, keep_output=False)
        except self.comfy.ComfyError as exc:
            raise ArtGenError(str(exc)) from exc

    def run(self, job: Any, should_stop: Callable[[], bool]) -> list[bytes]:
        wf, out_id = job
        original_submit = self.comfy.submit
        self.comfy.submit = self._submit  # 補上 widget_idx_map，避免某些自訂節點（focus_nodes）在 ComfyUI 終端印錯誤
        try:
            images, _ = self.comfy.run(self.base_url, wf, out_id, should_stop)
        except self.comfy.ComfyError as exc:
            if str(exc) == "已停止":
                raise JobStopped("stopped") from exc
            raise ImageFailure(str(exc)) from exc
        finally:
            self.comfy.submit = original_submit
        return images

    def _submit(self, base: str, wf: dict[str, Any]) -> str:
        """同 ``comfy.submit``，但 ``extra_pnginfo.workflow`` 多帶一個空的 ``widget_idx_map``。"""
        import uuid

        comfy = self.comfy
        reply = comfy._req(base, "/prompt", {"prompt": wf, "client_id": uuid.uuid4().hex,
                                             "extra_data": {"extra_pnginfo": {"workflow": {"nodes": [], "widget_idx_map": {}}}}}, timeout=30)
        if reply.get("node_errors"):
            raise comfy.ComfyError(comfy._explain_error(400, json.dumps(reply)))
        pid = reply.get("prompt_id")
        if not pid:
            raise comfy.ComfyError("ComfyUI 沒有回傳任務編號：" + json.dumps(reply, ensure_ascii=False)[:300])
        return pid


class ImageFailure(T2vnError):
    """單張圖失敗（ComfyUI 回報的錯誤）；該素材標記失敗，其餘繼續。"""


@dataclass
class GenReport:
    generated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stopped: bool = False


# ------------------------------------------------------------------ 檔案

def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtGenError(f"cannot read {path}: {exc}") from exc


def _write_bytes(config: Any, path: Path, data: bytes) -> None:
    config.assert_writable(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(config: Any, path: Path, data: Any) -> None:
    _write_bytes(config, path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def load_art(work: Path) -> dict[str, Any]:
    """``works/<book>/art/style.json``（沒有就空的；程式內有保底預設）。"""
    return _read_json(work / "art" / "style.json", {})


def save_art(config: Any, work: Path, art: dict[str, Any]) -> None:
    _write_json(config, work / "art" / "style.json", art)


def load_scene_docs(work: Path) -> list[dict[str, Any]]:
    """所有章節的 scene graph；同一章 ``scenes/`` 優先於 ``gold/``。"""
    docs: dict[str, dict[str, Any]] = {}
    for folder in ("gold", "scenes"):
        for path in sorted((work / folder).glob("ch[0-9][0-9][0-9].json")):
            docs[path.name] = _read_json(path, {})
    return [docs[name] for name in sorted(docs)]


def load_manifest(work: Path) -> dict[str, Any] | None:
    return _read_json(work / "manifest.json", None)


def _card_expressions(work: Path) -> dict[str, list[str]]:
    """各角色外型卡勾選的頭像表情（``art/appearance/<char_id>.json`` 的 ``expressions``）。"""
    found: dict[str, list[str]] = {}
    for path in sorted((work / "art" / "appearance").glob("*.json")):
        card = _read_json(path, {})
        exprs = card.get("expressions") if isinstance(card, dict) else None
        if isinstance(exprs, list):
            found[path.stem] = [e for e in exprs if isinstance(e, str)]
    return found


def refresh_manifest(config: Any, work: Path, book_id: str, lang: str = "zh-TW") -> dict[str, Any]:
    """重新掃描場景並與既有 manifest 合併（保留進度），寫回 ``manifest.json``。"""
    docs = load_scene_docs(work)
    if not docs:
        raise ArtGenError("no scenes/ or gold/ chapter files found")
    cast = _read_json(work / "cast.json", {})
    style = _read_json(work / "prototype" / "style.json", None)
    needs = collect_needs(docs, cast, style, lang)
    needs += character_assets.character_needs(cast, lang, {n["asset_id"] for n in needs}, _card_expressions(work))
    needs += asset_manifest.title_needs(lang, {n["asset_id"] for n in needs})
    manifest = build_manifest(book_id, needs, load_manifest(work))
    wanted = {n["asset_id"] for n in needs}
    # 取消勾選的頭像表情、以及不再需要的非 neutral 立繪：還沒出過圖（planned、沒有候選圖）就從清單拿掉，不留「待出圖」殘留。
    manifest["assets"] = [a for a in manifest["assets"]
                          if not (a["kind"] in ("headshot", "sprite") and a["asset_id"] not in wanted and a["status"] == "planned"
                                  and not list_candidates(work, a["asset_id"]))]
    _write_json(config, work / "manifest.json", manifest)
    return manifest


# ------------------------------------------------------------------ 候選圖

def candidate_dir(work: Path, asset_id: str) -> Path:
    if not ID_RE.match(asset_id):
        raise ArtGenError(f"invalid asset_id {asset_id!r}")
    return work / "assets" / CANDIDATES_DIR / asset_id


def list_candidates(work: Path, asset_id: str) -> list[dict[str, Any]]:
    """已有的候選圖（依種子排序），每項 ``{name, seed, file, meta}``；``file`` 相對作品資料夾。"""
    folder = candidate_dir(work, asset_id)
    out: list[dict[str, Any]] = []
    for png in sorted(folder.glob("*.png")) if folder.is_dir() else []:
        meta = _read_json(png.with_suffix(".json"), {})
        out.append({"name": png.name, "seed": meta.get("seed"), "file": png.relative_to(work).as_posix(), "meta": meta})
    return out


def resolve_profile(backend: ComfyBackend, art: dict[str, Any]) -> dict[str, Any]:
    """出圖設定檔：``art["comfy"]`` 的 unet／clip／vae／loras；空白的模型向 ComfyUI 查檔名後依檔名猜（Anima）。"""
    settings = dict(art.get("comfy") or {})
    profile: dict[str, Any] = {"kind": "anima", "unet": settings.get("unet", ""), "clip": settings.get("clip", ""),
                               "vae": settings.get("vae", ""), "loras": settings.get("loras") or []}
    if not all(profile[k] for k in ("unet", "clip", "vae")):
        lists = backend.models()
        from vendor.novel_studio import comfy

        guess = comfy.guess_models("anima", lists)
        for key in ("unet", "clip", "vae"):
            profile[key] = profile[key] or guess.get(key, "")
        missing = [k for k in ("unet", "clip", "vae") if not profile[k]]
        if missing:
            raise ArtGenError("找不到 Anima 的模型檔（" + ", ".join(missing) + "）。請確認檔案已放進 ComfyUI 的 models 資料夾，"
                              "或在畫風設定的 comfy 區填入檔名。ComfyUI 目前有：" + json.dumps(lists, ensure_ascii=False)[:400])
    return profile


def pending_assets(manifest: dict[str, Any], only: list[str] | None = None, regenerate: bool = False) -> list[dict[str, Any]]:
    """要出圖的項目：bg／sprite、還有場景在用（``used_in`` 非空）、狀態是 planned（``regenerate`` 時 planned 與 generated 都算；``only`` 指定時不看狀態）。"""
    chosen = []
    for asset in manifest.get("assets", []):
        if asset["kind"] not in GEN_KINDS:
            continue
        if only is not None:
            if asset["asset_id"] in only:
                chosen.append(asset)
            continue
        if not asset.get("used_in"):
            continue
        if asset["status"] == "planned" or (regenerate and asset["status"] == "generated"):
            chosen.append(asset)
    return chosen


def _seed_for(asset: dict[str, Any], attempt: int) -> int:
    """立繪：同一角色同一次嘗試用同一個種子（各表情臉才會一致）；其他素材照舊用素材 id。"""
    if asset.get("kind") == "sprite":
        try:
            char_id, _expr = sprite_consistency.split_sprite(asset)
        except ValueError:
            return art_prompts.seed_for(asset["asset_id"], attempt)
        return sprite_consistency.character_seed(char_id, attempt)
    if asset.get("kind") == "headshot":
        try:
            char_id, _expr = art_prompts.split_headshot_file(asset.get("file"))
        except ValueError:
            return art_prompts.seed_for(asset["asset_id"], attempt)
        return sprite_consistency.character_seed(char_id, attempt)
    return art_prompts.seed_for(asset["asset_id"], attempt)


def _order_todo(todo: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """背景在前，立繪依角色與表情順序（同角色連續出圖）。"""
    others = [a for a in todo if a.get("kind") != "sprite"]
    sprites = [a for a in todo if a.get("kind") == "sprite"]
    ordered = sprite_consistency.order_sprite_assets(sprites)
    leftovers = [a for a in sprites if a not in ordered]
    return others + ordered + leftovers


def generate(config: Any, work: Path, backend: ComfyBackend, *, candidates: int | None = None, only: list[str] | None = None,
             regenerate: bool = False, should_stop: Callable[[], bool] = lambda: False,
             progress: Callable[[dict[str, Any]], None] | None = None) -> GenReport:
    """逐項出圖。每個素材目標 ``candidates`` 張（預設取 ``art["comfy"]["candidates"]``，再沒有就 4）。

    - 續跑：已有的候選圖算數，只補不足的張數；全部到位的素材直接標記 generated。
    - ``regenerate``：每個素材再加 ``candidates`` 張新的（用新的種子，舊圖保留）。
    - 單張圖失敗（ImageFailure）：該素材記進 ``failed``，其餘繼續；失敗前已存的候選圖保留。
    - 停止（``should_stop()`` 為真或後端丟 JobStopped）：存好目前進度後回傳 ``stopped=True``。
    - 每個素材完成就立刻寫 manifest（status、prompt_en、negative_prompt、seed、model）。
    """
    emit = progress or (lambda event: None)
    manifest = load_manifest(work)
    if manifest is None:
        raise ArtGenError("manifest.json not found; refresh the manifest first")
    art = load_art(work)
    target = int(candidates or (art.get("comfy") or {}).get("candidates") or DEFAULT_CANDIDATES)
    if target < 1 or target > 16:
        raise ArtGenError("candidates must be between 1 and 16")
    todo = _order_todo(pending_assets(manifest, only, regenerate))
    report = GenReport()
    if not todo:
        return report
    profile = resolve_profile(backend, art)
    model_name = profile["unet"]
    backgrounds = art_prompts.bg_index(load_scene_docs(work))
    backgrounds.setdefault(asset_manifest.TITLE_BG_ID, (asset_manifest.TITLE_LOCATION_ID, asset_manifest.TITLE_TIME_OF_DAY))
    for index, asset in enumerate(todo, 1):
        asset_id = asset["asset_id"]
        if should_stop():
            report.stopped = True
            break
        try:
            prompt = art_prompts.prompt_for_asset(asset, backgrounds, art)
        except ValueError as exc:
            report.failed[asset_id] = str(exc)
            continue
        report.warnings += [f"{asset_id}: {w}" for w in prompt["warnings"]]
        folder = candidate_dir(work, asset_id)
        existing = list_candidates(work, asset_id)
        have = len(existing)
        first_attempt = have if regenerate else 0
        wanted = target if regenerate else max(0, target - have)
        done_seeds = {c["seed"] for c in existing}
        made = 0
        stopped = False
        try:
            attempt = first_attempt
            while made < wanted:
                seed = _seed_for(asset, attempt)
                attempt += 1
                if seed in done_seeds:
                    continue
                if should_stop():
                    stopped = True
                    break
                emit({"event": "image", "asset_id": asset_id, "index": index, "total": len(todo), "n": have + made + 1, "of": have + wanted})
                job = backend.prepare(profile, prompt["positive"], prompt["negative"], seed, prompt["width"], prompt["height"])
                images = backend.run(job, should_stop)
                if not images:
                    raise ImageFailure("ComfyUI returned no image")
                _write_bytes(config, folder / f"{seed}.png", images[0])
                _write_json(config, folder / f"{seed}.json", {"seed": seed, "asset_id": asset_id, "positive": prompt["positive"],
                                                              "negative": prompt["negative"], "width": prompt["width"],
                                                              "height": prompt["height"], "model": model_name})
                made += 1
        except JobStopped:
            stopped = True
        except ImageFailure as exc:
            report.failed[asset_id] = str(exc)
        total_now = len(list_candidates(work, asset_id))
        if total_now and asset_id not in report.failed and not stopped:
            asset.update(status="generated" if asset["status"] == "planned" else asset["status"], prompt_en=prompt["positive"],
                         negative_prompt=prompt["negative"], model=model_name)
            if asset.get("seed") is None:
                asset["seed"] = (list_candidates(work, asset_id)[0].get("seed"))
            (report.generated if made else report.skipped).append(asset_id)
        _write_json(config, work / "manifest.json", manifest)
        emit({"event": "asset_done", "asset_id": asset_id, "index": index, "total": len(todo)})
        if stopped:
            report.stopped = True
            break
    return report


# ------------------------------------------------------------------ 挑選與處理

def _crop_16_9(image: Image.Image) -> Image.Image:
    width, height = image.size
    target = BG_SIZE[0] / BG_SIZE[1]
    if width / height > target:
        new_w = round(height * target)
        left = (width - new_w) // 2
        return image.crop((left, 0, left + new_w, height))
    new_h = round(width / target)
    top = (height - new_h) // 2
    return image.crop((0, top, width, top + new_h))


def _align_character(config: Any, work: Path, manifest: dict[str, Any], picked: dict[str, Any]) -> None:
    """同一角色已處理好的立繪（含剛挑的這張）對齊到同一個畫布與腳底線；對不起來就維持原樣。"""
    try:
        char_id, picked_expr = sprite_consistency.split_sprite(picked)
    except ValueError:
        return
    images: dict[str, Image.Image] = {}
    paths: dict[str, Path] = {}
    for asset in manifest["assets"]:
        if asset is not picked and asset.get("status") not in ("processed", "final"):
            continue
        try:
            other_char, expr = sprite_consistency.split_sprite(asset)
        except ValueError:
            continue
        path = work / asset["file"]
        if other_char != char_id or not path.is_file():
            continue
        with Image.open(path) as opened:
            images[expr] = opened.convert("RGBA")
        paths[expr] = path
    if len(images) < 2:
        return
    canvas = images[picked_expr].size
    try:
        aligned = sprite_consistency.align_sprites(images, canvas=canvas)
    except ValueError:
        return
    for expr, image in aligned.items():
        out = io.BytesIO()
        image.save(out, "PNG")
        _write_bytes(config, paths[expr], out.getvalue())


def select_candidate(config: Any, work: Path, asset_id: str, name: str, tolerance: int = 40) -> dict[str, Any]:
    """把候選圖 ``name``（檔名，如 ``123.png``）處理成遊戲用的圖：背景裁成 16:9 並縮到 1920×1080；立繪去背放進 800×1600。
    寫到 manifest 該項的 ``file``，狀態改成 ``processed``，``seed`` 記下這張的種子。回傳更新後的 manifest 項目。"""
    manifest = load_manifest(work)
    if manifest is None:
        raise ArtGenError("manifest.json not found")
    asset = next((a for a in manifest["assets"] if a["asset_id"] == asset_id), None)
    if asset is None or asset["kind"] not in GEN_KINDS:
        raise ArtGenError(f"{asset_id}: not an image asset in the manifest")
    if "/" in name or "\\" in name or not name.endswith(".png"):
        raise ArtGenError(f"invalid candidate name {name!r}")
    source = candidate_dir(work, asset_id) / name
    if not source.is_file():
        raise ArtGenError(f"candidate {name} of {asset_id} does not exist")
    meta = _read_json(source.with_suffix(".json"), {})
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    if asset["kind"] == "bg":
        result = _crop_16_9(image).resize(BG_SIZE, Image.LANCZOS)
    elif asset["kind"] == "headshot":
        result = image  # 頭像不去背、不縮放，原圖直接當成品
    else:
        try:
            result, _ = cutout(image, tolerance=tolerance)
        except ValueError as exc:
            raise ArtGenError(f"{asset_id}: cutout failed: {exc}") from exc
    buffer = io.BytesIO()
    result.save(buffer, "PNG")
    _write_bytes(config, work / asset["file"], buffer.getvalue())
    asset["status"] = "processed"
    if asset["kind"] == "sprite":
        _align_character(config, work, manifest, asset)
    if meta.get("seed") is not None:
        asset["seed"] = meta["seed"]
    _write_json(config, work / "manifest.json", manifest)
    return asset


def set_status(config: Any, work: Path, asset_id: str, status: str, license_text: str | None = None) -> dict[str, Any]:
    """手動改狀態（例如標 final）。標 ``final`` 時授權不可為空。"""
    if status not in ("planned", "generated", "selected", "processed", "final"):
        raise ArtGenError(f"unknown status {status!r}")
    manifest = load_manifest(work)
    if manifest is None:
        raise ArtGenError("manifest.json not found")
    asset = next((a for a in manifest["assets"] if a["asset_id"] == asset_id), None)
    if asset is None:
        raise ArtGenError(f"{asset_id}: not in the manifest")
    if license_text is not None:
        asset["license"] = license_text
    if status == "final" and not asset.get("license"):
        raise ArtGenError(f"{asset_id}: license must not be empty before marking final")
    asset["status"] = status
    _write_json(config, work / "manifest.json", manifest)
    return asset


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.art_gen --book sample_book refresh | generate [--only ID ...] [--candidates N] [--regenerate] | select ASSET_ID NAME.png | status``"""
    import argparse
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    from .config import load_config

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--book", required=True)
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("refresh")
    sub.add_parser("status")
    gen = sub.add_parser("generate")
    gen.add_argument("--only", nargs="+", default=None)
    gen.add_argument("--candidates", type=int, default=None)
    gen.add_argument("--regenerate", action="store_true")
    sel = sub.add_parser("select")
    sel.add_argument("asset_id")
    sel.add_argument("name")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        book = config.book(args.book)
        work = config.work_dir(book.book_id)
        if args.command == "refresh":
            manifest = refresh_manifest(config, work, book.book_id, book.lang)
            print(f"ok: {len(manifest['assets'])} asset(s)")
        elif args.command == "status":
            manifest = load_manifest(work)
            if manifest is None:
                raise ArtGenError("manifest.json not found; run refresh first")
            for kind in ("bg", "sprite", "bgm", "ambience"):
                counts: dict[str, int] = {}
                for a in manifest["assets"]:
                    if a["kind"] == kind:
                        counts[a["status"]] = counts.get(a["status"], 0) + 1
                print(f"{kind}: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "-"))
        elif args.command == "generate":
            backend = VendorBackend(config.comfyui_base_url)
            report = generate(config, work, backend, candidates=args.candidates, only=args.only, regenerate=args.regenerate,
                              progress=lambda e: print(f"[{e['index']}/{e['total']}] {e['asset_id']}" + (f" {e['n']}/{e['of']}" if e["event"] == "image" else " done")))
            for warning in report.warnings:
                print(f"warning: {warning}")
            for asset_id, reason in report.failed.items():
                print(f"failed: {asset_id}: {reason}", file=sys.stderr)
            print(f"ok: generated {len(report.generated)}, unchanged {len(report.skipped)}, failed {len(report.failed)}" + (", stopped" if report.stopped else ""))
            return 1 if report.failed else 0
        else:
            asset = select_candidate(config, work, args.asset_id, args.name)
            print(f"ok: {asset['asset_id']} -> {asset['file']} ({asset['status']})")
    except (T2vnError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
