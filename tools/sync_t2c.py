"""把 text2comics 的 t2c 套件單向同步到 vendor/t2c。

用法::

    python tools/sync_t2c.py              # 同步，然後跑 tools/comic_check.py
    python tools/sync_t2c.py --check      # 只比對有沒有漂移，不改檔（有漂移回 1）
    python tools/sync_t2c.py --src PATH   # 指定 text2comics 的資料夾
    python tools/sync_t2c.py --no-check   # 同步但不跑 comic_check

規則見 docs/decisions.md 的 D1：

- 方向只有一邊：text2comics → 這裡。t2c 要改就回 text2comics 改、提交、再同步，
  不要在 vendor/t2c/ 手改。
- 只搬 t2c/（套件本體，含 t2c/data/）＋ config.example.json ＋ LICENSE；
  不搬 t2c/web/、app.py、server/（宿主有自己的前端與任務層，見 D7）。
- 複製的是 **commit 的內容**，不是工作區：來源如果有未提交的修改，照樣只搬最後一次提交，
  這樣 VENDORED.md 的雜湊在任何機器上都重現得出來（換行不會因為 autocrlf 而變）。
- VENDORED.md 由這支程式產生。內容沒變就不重寫，所以第二次跑必須零 diff。
- 預設來源是專案資料夾旁邊的 dev/text2comics；別的機器用 --src 或環境變數 T2C_SOURCE。
"""
from __future__ import annotations

import argparse
import ast
import codecs
import hashlib
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEST = REPO / "vendor" / "t2c"
VENDORED_MD = DEST / "VENDORED.md"
DEFAULT_SRC = REPO.parent / "dev" / "text2comics"
NL = chr(10)

# (來源資料夾, 目的資料夾)；目的以 vendor/t2c 為根
COPY_DIRS = [("t2c", ".")]
# (來源檔案, 目的檔案)
COPY_FILES = [("config.example.json", "default_config.json"), ("LICENSE", "LICENSE")]
SKIP_PARTS = {"__pycache__", ".pytest_cache"}


def source_plan(src: Path) -> dict[str, tuple[Path, str]]:
    """目的相對路徑（posix）→ (來源檔案, 來源 repo 內的相對路徑)。"""
    plan: dict[str, tuple[Path, str]] = {}
    for src_rel, dest_rel in COPY_DIRS:
        base = src / src_rel
        if not base.is_dir():
            raise SystemExit("error: 找不到來源資料夾 %s（用 --src 指定 text2comics）" % base)
        for path in sorted(base.rglob("*")):
            if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
                continue
            rel = path.relative_to(base).as_posix()
            repo_rel = (Path(src_rel) / rel).as_posix()
            plan[(Path(dest_rel) / rel).as_posix()] = (path, repo_rel)
    for src_rel, dest_rel in COPY_FILES:
        path = src / src_rel
        if not path.is_file():
            raise SystemExit("error: 找不到來源檔案 %s" % path)
        plan[dest_rel] = (path, src_rel)
    return plan


def _git(src: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(src), *args], capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _content(src: Path, head: str, path: Path, repo_rel: str) -> bytes:
    """檔案內容以 commit 為準；不是 git 工作區才退回讀工作區的檔案。"""
    if head:
        done = subprocess.run(["git", "-C", str(src), "show", "%s:%s" % (head, repo_rel)], capture_output=True)
        if done.returncode == 0:
            return done.stdout
    return path.read_bytes()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dir_digest(rows: list[tuple[str, str]]) -> str:
    """(相對路徑, SHA-256) 排序後串起來再雜湊；一次比對整個目錄。"""
    body = NL.join("%s %s" % (rel, sha) for rel, sha in sorted(rows))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def source_meta(src: Path, head: str) -> dict[str, str]:
    version = ""
    init_py = src / "t2c" / "__init__.py"
    if init_py.is_file():
        try:
            for node in ast.parse(init_py.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__version__" for t in node.targets):
                    version = str(getattr(node.value, "value", "") or "")
        except SyntaxError:
            version = ""
    dirty = _git(src, "status", "--porcelain", "--", "t2c", *[rel for rel, _ in COPY_FILES])
    return {
        "path": str(src),
        "commit": head or "(不是 git 工作區，改讀工作區的檔案)",
        "date": _git(src, "log", "-1", "--format=%cs") or "(未知)",
        "version": version or "(未標版本)",
        "dirty": ("有未提交的修改（這次只搬 %s 的內容）" % head[:12]) if dirty else "無",
    }


def _summary(path: Path) -> str:
    """表格的說明欄：模組 docstring 的第一行，讀不到就寫常數。"""
    if path.suffix != ".py":
        return "出圖設定範本" if path.name == "default_config.json" else "授權全文"
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    except (SyntaxError, UnicodeDecodeError):
        return "(原始碼讀不到)"
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    sentence = first.split("。")[0].strip() or first
    return sentence.replace("|", "/")[:120] or "(沒有 docstring)"


def _origin(rel: str) -> str:
    for src_rel, dest_rel in COPY_FILES:
        if dest_rel == rel:
            return src_rel
    return "t2c/%s" % rel


def render_vendored(plan: dict[str, tuple[Path, str]], meta: dict[str, str], rows: list[tuple[str, str]]) -> str:
    root_rows = [(rel, sha) for rel, sha in sorted(rows) if "/" not in rel]
    data_rows = [(rel, sha) for rel, sha in sorted(rows) if "/" in rel]
    lines = [
        "# vendor/t2c",
        "",
        "從 [text2comics](https://github.com/yamasoo/text2comics)（MIT）複製的 `t2c` 套件。"
        "**不要直接修改這裡的檔案**；要改就回 text2comics 改、提交、再跑 `python tools/sync_t2c.py`。",
        "",
        "## 來源",
        "",
        "- 本機來源：`%s`" % meta["path"],
        "- commit：`%s`" % meta["commit"],
        "- commit 日期：%s" % meta["date"],
        "- 套件版本：`%s`" % meta["version"],
        "- 同步時工作區：%s" % meta["dirty"],
        "- 目錄 digest：`%s`" % _dir_digest(rows),
        "- 授權：MIT，`LICENSE` 一併保留。",
        "- 這份檔案由 `tools/sync_t2c.py` 產生，不要手改。",
        "",
        "## 檔案",
        "",
        "內容取自 commit，所以換行是 LF；說明欄是模組 docstring 的第一行（自動抓，不手寫）。",
        "",
        "| 檔案 | 原路徑 | SHA-256 | 說明 | 修改 |",
        "|---|---|---|---|---|",
    ]
    for rel, sha in root_rows:
        lines.append("| `%s` | `%s` | `%s` | %s | 無 |" % (rel, _origin(rel), sha, _summary(plan[rel][0])))
    lines += [
        "",
        "## 資料檔",
        "",
        "套件自帶的出圖資料，`t2c/data/` 原樣搬過來。`layout.load_pages()` 與 "
        "`comfy.load_workflow()` 讀的就是這裡；檔案不在就整條出圖線停擺。",
        "",
        "| 檔案 | 原路徑 | SHA-256 |",
        "|---|---|---|",
    ]
    for rel, sha in data_rows:
        lines.append("| `%s` | `t2c/%s` | `%s` |" % (rel, rel, sha))
    lines += [
        "",
        "## 沒有搬過來的東西",
        "",
        "- `t2c/web/`、`app.py`、`server/`、`tools/`：宿主介面自成一套，見 `docs/decisions.md` D7。",
        "- `t2c/vendor/`：不搬第二份 fekit（整合計畫的風險 R5）。",
        "",
        "## 更新流程",
        "",
        "1. 在 text2comics 改 `t2c/`，跑它的測試，提交。",
        "2. 在這裡跑 `python tools/sync_t2c.py`。",
        "3. 它會重寫這份檔案，然後自動跑 `tools/comic_check.py`；綠燈才提交。",
        "",
        "同步讀的是 commit，不是工作區。text2comics 有未提交的修改時，這裡搬的仍是最後一次提交；",
        "上面「同步時工作區」那一行會寫出來。",
        "",
    ]
    return NL.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 text2comics 的 t2c 同步到 vendor/t2c")
    parser.add_argument("--src", default=os.environ.get("T2C_SOURCE") or str(DEFAULT_SRC),
                        help="text2comics 的資料夾（預設 %s，也可用環境變數 T2C_SOURCE）" % DEFAULT_SRC)
    parser.add_argument("--check", action="store_true", help="只比對，不改任何檔案；有漂移回 1")
    parser.add_argument("--no-check", action="store_true", help="同步後不跑 tools/comic_check.py")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    src = Path(args.src)
    plan = source_plan(src)
    head = _git(src, "rev-parse", "HEAD")

    contents: dict[str, bytes] = {}
    for rel, (path, repo_rel) in sorted(plan.items()):
        data = _content(src, head, path, repo_rel)
        if rel == "default_config.json" and data[:3] == codecs.BOM_UTF8:
            print("error: %s 帶 UTF-8 BOM，json.loads 會炸在第一個字元" % path, file=sys.stderr)
            return 2
        contents[rel] = data

    changing, unchanged = [], 0
    for rel, data in sorted(contents.items()):
        target = DEST / rel
        if target.is_file() and target.read_bytes() == data:
            unchanged += 1
        else:
            changing.append(rel)

    stale: list[str] = []
    if DEST.is_dir():
        for path in sorted(DEST.rglob("*")):
            if not path.is_file() or path.name == VENDORED_MD.name:
                continue
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            rel = path.relative_to(DEST).as_posix()
            if rel not in contents:
                stale.append(rel)

    rows = [(rel, _sha256(data)) for rel, data in contents.items()]
    text = render_vendored(plan, source_meta(src, head), rows)
    md_changed = not VENDORED_MD.is_file() or VENDORED_MD.read_text(encoding="utf-8") != text

    if args.check:
        if changing or stale or md_changed:
            print("漂移：%d 個檔案要更新、%d 個要刪、VENDORED.md %s"
                  % (len(changing), len(stale), "要重寫" if md_changed else "沒變"))
            for rel in changing:
                print("  更新 %s" % rel)
            for rel in stale:
                print("  刪除 %s" % rel)
            return 1
        print("沒有漂移：%d 個檔案與 %s 的 commit 相同" % (unchanged, src))
        return 0

    for rel in changing:
        target = DEST / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[rel])
    for rel in stale:
        (DEST / rel).unlink()
    if md_changed:
        VENDORED_MD.parent.mkdir(parents=True, exist_ok=True)
        VENDORED_MD.write_text(text, encoding="utf-8", newline=NL)

    print("同步完成：更新 %d、未變 %d、刪除 %d、VENDORED.md %s"
          % (len(changing), unchanged, len(stale), "已重寫" if md_changed else "未變"))

    if args.no_check:
        return 0
    done = subprocess.run([sys.executable, str(REPO / "tools" / "comic_check.py")], cwd=str(REPO))
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
