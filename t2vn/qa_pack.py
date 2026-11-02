"""S8：產生「獨立驗收包」——給另開的 Claude 對話（Opus）用，**不含開發過程**。

只放驗收需要的東西：規則文件、資料格式、驗收清單、原文章節、劇本（scene graph）、角色表、匯出的 ``.rpy``、
素材清單、最新一份自動驗收報告。刻意不放 ``HANDOFF.md``、``decisions.md``、任務卡、研究文件，
也不放圖片／音檔本身（只列清單），避免被開發過程影響，也避免資料夾過大。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .errors import T2vnError, WriteProtectedError

REPO_FILES = ("CLAUDE.md", "docs/05_資料格式規格.md", "docs/20_標註規則.md", "docs/30_RenPy匯出規格.md",
              "docs/qa/驗收清單.md", "docs/qa/交接說明.md")
EXCLUDED_NOTE = ("docs/HANDOFF.md", "docs/decisions.md", "docs/tasks/", "docs/research/", "歷次驗收報告", "圖片與音檔本體")
CHAPTER_RE = re.compile(r"^vn_ch([0-9]{3})\.rpy$")
MEDIA_DIRS = ("images", "audio")
SKIP_RPY = ("vn_verify_test.rpy",)
SHRINK_OVER = 1 << 20   # 超過 1 MB 的驗收截圖縮成 JPEG（原圖動輒 5–7 MB，對話讀不動）
SHRINK_WIDTH = 1280
SHRINK_QUALITY = 80


class QaPackError(T2vnError):
    """產生驗收包失敗（訊息直接給使用者看）。"""


def exported_chapters(project: Path) -> list[int]:
    """專案 ``game/`` 裡已匯出的章號（由 ``vn_chNNN.rpy`` 判斷），由小到大。"""
    game = Path(project) / "game"
    return sorted(int(m.group(1)) for p in game.glob("vn_ch*.rpy") if (m := CHAPTER_RE.match(p.name)))


def find_scenes(work: Path, number: int) -> Path:
    """章節劇本檔：先找 ``scenes/``，再找 ``gold/``（與匯出器同一個順序）。"""
    for folder in ("scenes", "gold"):
        path = Path(work) / folder / f"ch{number:03d}.json"
        if path.is_file():
            return path
    raise QaPackError(f"找不到第 {number} 章的劇本（scenes/ch{number:03d}.json 或 gold/ch{number:03d}.json）")


def latest_report(work: Path) -> Path | None:
    """``build/reports`` 底下最新的 ``verify_*`` 資料夾；沒有則 ``None``。"""
    root = Path(work) / "build" / "reports"
    rows = sorted((p for p in root.glob("verify_*") if p.is_dir()), key=lambda p: p.name) if root.is_dir() else []
    return rows[-1] if rows else None


def media_inventory(game: Path) -> list[str]:
    """``game/images``、``game/audio`` 的檔案清單，每行 ``<相對路徑>\\t<位元組>``，排序後。"""
    lines: list[str] = []
    for sub in MEDIA_DIRS:
        root = Path(game) / sub
        if root.is_dir():
            lines += [f"{p.relative_to(game).as_posix()}\t{p.stat().st_size}" for p in sorted(root.rglob("*")) if p.is_file()]
    return sorted(lines)


def shrink_image(src: Path, dest: Path) -> bool:
    """把大張 PNG 縮成寬 :data:`SHRINK_WIDTH` 以內的 JPEG 存到 ``dest``；沒有 Pillow 或失敗回 ``False``（呼叫端改複製原檔）。"""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(src) as image:
            image = image.convert("RGB")
            if image.width > SHRINK_WIDTH:
                image = image.resize((SHRINK_WIDTH, round(image.height * SHRINK_WIDTH / image.width)))
            image.save(dest, "JPEG", quality=SHRINK_QUALITY)
    except OSError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build_pack(config: Any, repo_root: Path, work: Path, project: Path, out: Path, *, book_id: str = "",
               chapters: list[int] | None = None, report: Path | None = None, dist_note: str = "",
               shrink_screens: bool = True) -> dict[str, Any]:
    """產生驗收包到 ``out``（必須不存在或是空資料夾）；回傳 ``{"out", "files", "bytes", "chapters", "missing"}``。

    ``chapters`` 預設為專案已匯出的全部章。缺的必要檔案（章節劇本、cast.json、``game/``）→ :class:`QaPackError`；
    缺的選用檔案（repo 文件、驗收報告）記在 ``missing`` 與 ``PACK.md``。
    """
    repo_root, work, project, out = Path(repo_root), Path(work), Path(project), Path(out)
    game = project / "game"
    if not game.is_dir():
        raise QaPackError(f"{project} 底下沒有 game 資料夾；請先匯出")
    try:
        config.assert_writable(out)
    except WriteProtectedError as exc:
        raise QaPackError(f"不能寫入 {out}（要在專案資料夾或 works 底下，且不能是唯讀的書庫）") from exc
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise QaPackError(f"{out} 已經存在且不是空的；請換一個資料夾名稱")
    numbers = sorted(set(chapters)) if chapters else exported_chapters(project)
    if not numbers:
        raise QaPackError("專案裡沒有已匯出的章節（沒有 vn_chNNN.rpy）")
    cast = work / "cast.json"
    if not cast.is_file():
        raise QaPackError(f"找不到角色表：{cast}")
    scene_files = {n: find_scenes(work, n) for n in numbers}

    copied: list[tuple[str, Path]] = []  # (包內路徑, 來源)
    missing: list[str] = []
    for rel in REPO_FILES:
        src = repo_root / rel
        (copied.append((rel, src)) if src.is_file() else missing.append(rel))
    schema = repo_root / "schema"
    if schema.is_dir():
        copied += [(f"schema/{p.name}", p) for p in sorted(schema.glob("*.json"))]
    for n, src in scene_files.items():
        copied.append((f"scenes/ch{n:03d}.json", src))
        source = work / "chapters" / f"ch{n:03d}.json"
        (copied.append((f"source/ch{n:03d}.json", source)) if source.is_file() else missing.append(f"source/ch{n:03d}.json"))
    for name in ("index.json",):
        if (work / "chapters" / name).is_file():
            copied.append((f"source/{name}", work / "chapters" / name))
    copied.append(("cast.json", cast))
    if (work / "book.json").is_file():
        copied.append(("book.json", work / "book.json"))
    for p in sorted(game.glob("*.rpy")) + [game / "vn_export.json"]:
        if p.is_file() and p.name not in SKIP_RPY:
            copied.append((f"renpy/game/{p.name}", p))
    picked = Path(report) if report else latest_report(work)
    if picked and picked.is_dir():
        copied += [(f"verify_report/{p.relative_to(picked).as_posix()}", p) for p in sorted(picked.rglob("*")) if p.is_file()]
    else:
        missing.append("verify_report/（還沒有自動驗收報告）")

    out.mkdir(parents=True, exist_ok=True)
    (out / "renpy").mkdir(exist_ok=True)
    inventory = media_inventory(game)
    media_path = out / "renpy" / "media_inventory.tsv"
    media_path.write_text("\n".join(inventory) + ("\n" if inventory else ""), encoding="utf-8")
    total = 0
    shrunk = 0
    rows: list[str] = []
    for rel, src in copied:
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if shrink_screens and rel.startswith("verify_report/") and rel.endswith(".png") and src.stat().st_size > SHRINK_OVER:
            jpg = dest.with_suffix(".jpg")
            if shrink_image(src, jpg):
                shrunk += 1
                rel, dest = rel[:-4] + ".jpg", jpg
            else:
                shutil.copy2(src, dest)
        else:
            shutil.copy2(src, dest)
        total += dest.stat().st_size
        rows.append(f"| `{rel}` | {dest.stat().st_size} | `{_sha256(dest)[:12]}` |")
    rows.append(f"| `renpy/media_inventory.tsv` | {media_path.stat().st_size} | `{_sha256(media_path)[:12]}` |")

    lines = ["# 驗收包（PACK）", "", f"- 書：{book_id or work.name}", f"- 產生時間：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 章節：{', '.join(f'第 {n} 章' for n in numbers)}", f"- 媒體清單：{len(inventory)} 個檔案（見 `renpy/media_inventory.tsv`，不含檔案本體）"]
    if dist_note:
        lines.append(f"- 成品位置：{dist_note}")
    if shrunk:
        lines.append(f"- 驗收截圖：{shrunk} 張超過 1 MB 的 PNG 已縮成 JPEG（寬 {SHRINK_WIDTH} 像素內，副檔名 .jpg）；小於 1 MB 的 PNG 維持原樣（多半是全黑的標題卡），原圖在開發端的報告資料夾")
    lines += ["", "請先讀 `docs/qa/交接說明.md`，再照 `docs/qa/驗收清單.md` 逐項驗收。", "",
              "## 刻意不放的東西", "", *[f"- {item}" for item in EXCLUDED_NOTE], ""]
    if missing:
        lines += ["## 缺少的選用檔案", "", *[f"- {item}" for item in missing], ""]
    lines += ["## 檔案清單", "", "| 路徑 | 位元組 | sha256（前 12 碼） |", "|---|---|---|", *rows, ""]
    (out / "PACK.md").write_text("\n".join(lines), encoding="utf-8")
    return {"out": str(out), "files": len(copied) + 2, "bytes": total, "chapters": numbers, "missing": missing, "shrunk": shrunk}
