"""檢查 vendor/t2c 載得起來，而且出圖設定組得出來。

驗收條件見整合計畫 I4-A：

- from vendor import t2c 可用，而且 vendor/t2c 的每個模組都匯入成功
- layout.load_pages()、comfy.load_workflow() 讀得到套件自帶的資料
- resolve_profile(cfg) 的 base_url 等於 config.yaml 的 comfyui.base_url
- 同一個 profile 的 workflow 指到存在的檔案

另外兩項是踩過的坑：default_config.json 帶 BOM 會讓 json.loads 炸在第一個字元；
t2c 一旦出現絕對自匯入（from t2c import ...），from vendor import t2c 就載不起來。

用法::

    python tools/comic_check.py

綠燈回 0，有失敗回 1。tools/sync_t2c.py 同步完會自動跑它。
"""
from __future__ import annotations

import codecs
import importlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEST = REPO / "vendor" / "t2c"
CONFIG_JSON = DEST / "default_config.json"
SELF_IMPORT = re.compile(r"^\s*(?:from|import)\s+t2c\b", re.MULTILINE)


def _modules() -> list[str]:
    return sorted(path.stem for path in DEST.glob("*.py") if path.stem != "__init__")


def checker():
    """回傳 (通過數, 失敗清單)。每個檢查自己印一行。"""
    results: list[tuple[bool, str, str]] = []

    def run(label: str, fn) -> None:
        try:
            detail = fn() or ""
            results.append((True, label, str(detail)))
        except Exception as error:
            results.append((False, label, "%s: %s" % (type(error).__name__, error)))

    def imports() -> str:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        importlib.import_module("vendor.t2c")
        names = _modules()
        for name in names:
            importlib.import_module("vendor.t2c.%s" % name)
        return "%d 個模組" % (len(names) + 1)

    def no_bom() -> str:
        head = CONFIG_JSON.read_bytes()[:3]
        if head == codecs.BOM_UTF8:
            raise ValueError("default_config.json 帶 UTF-8 BOM")
        return "沒有 BOM"

    def no_self_import() -> str:
        hits = []
        for path in sorted(DEST.glob("*.py")):
            if SELF_IMPORT.search(path.read_text(encoding="utf-8")):
                hits.append(path.name)
        if hits:
            raise ValueError("絕對自匯入：%s（from vendor import t2c 會載不起來）" % "、".join(hits))
        return "0 筆"

    def pages() -> str:
        from vendor.t2c import layout
        data = layout.load_pages()
        templates = data.get("templates") or {}
        if not templates:
            raise ValueError("內建版型檔沒有 templates")
        return "%d 個版型" % len(templates)

    def workflow() -> str:
        from vendor.t2c import comfy
        data = comfy.load_workflow()
        if not isinstance(data, dict) or not data:
            raise ValueError("workflow 不是非空物件")
        return "%d 個頂層鍵" % len(data)

    def profile() -> str:
        from t2vn.config import load_config
        from vendor.t2c import comfy
        config = load_config()
        cfg = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        cfg.setdefault("comfy", {})["url"] = config.comfyui_base_url
        picked = comfy.resolve_profile(cfg)
        if picked["base_url"] != config.comfyui_base_url:
            raise ValueError("base_url %r != config.yaml 的 %r" % (picked["base_url"], config.comfyui_base_url))
        path = Path(picked["workflow"])
        if not path.is_file():
            raise ValueError("workflow 不存在：%s" % path)
        return "%s（%s）→ %s" % (picked["name"], picked["kind"], config.comfyui_base_url)

    run("from vendor import t2c 與全部模組", imports)
    run("default_config.json 沒有 BOM", no_bom)
    run("沒有絕對自匯入", no_self_import)
    run("layout.load_pages()", pages)
    run("comfy.load_workflow()", workflow)
    run("resolve_profile 的 base_url 與 workflow", profile)

    for ok, label, detail in results:
        print("  %s %s：%s" % ("ok  " if ok else "FAIL", label, detail))
    failed = [label for ok, label, _ in results if not ok]
    print("comic_check：%d/%d 通過" % (len(results) - len(failed), len(results)))
    if failed:
        print("失敗：%s" % "、".join(failed))
    return len(results) - len(failed), len(results)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if not DEST.is_dir():
        print("error: 找不到 %s（先跑 python tools/sync_t2c.py）" % DEST, file=sys.stderr)
        return 1
    passed, total = checker()
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
