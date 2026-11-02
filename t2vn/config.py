"""讀取 config.yaml，並負責「書目錄唯讀」的寫入保護。

規則（見 docs/05_資料格式規格.md 第 2 節）：
- 專案目錄可讀寫；書目錄與 novel-studio 唯讀。
- 設定檔的 key 一律英文（語言標籤如 zh-TW 除外，它們本身是 ASCII）。
- 所有寫檔的程式都應先呼叫 ``Config.assert_writable(path)``。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import executors
from .errors import ConfigError, T2vnError, WriteProtectedError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.example.yaml"

BOOK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
READ_ONLY = "read_only"


@dataclass(frozen=True)
class BookConfig:
    """一本書的設定。``source_dir`` 是相對於書庫根目錄的資料夾名（值，可以是中文）。"""

    book_id: str
    source_dir: str
    lang: str
    display_name: dict[str, str]
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    """整份設定。路徑都已轉成 ``Path``，但不保證存在（用 ``check_paths`` 檢查）。"""

    project_root: Path
    book_library: Path
    reference_sources: dict[str, Path] = field(default_factory=dict)
    reference_commits: dict[str, str] = field(default_factory=dict)
    books: dict[str, BookConfig] = field(default_factory=dict)
    # 預設執行者（供應商或「供應商/模型」）與使用者在 config.yaml 寫的供應商覆寫（見 executors.py）
    llm_default: str = "deepseek"
    llm_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 下面四個是從預設執行者算出來的（唯讀，給舊程式與狀態列用）
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-flash"
    llm_thinking: str = "off"
    llm_max_output: int = 32768
    comfyui_base_url: str = "http://127.0.0.1:8188"
    works_dir_override: Path | None = None  # 作品（匯入、標註、原型）的存放位置；沒設就是 <project_root>/works
    server_port: int = 7866
    annotate_chunk_chars: int = 0  # 0＝自動（上限 15000 字，再依執行者上下文長度調整）

    # ------------------------------------------------------------ 查詢

    def enabled_books(self) -> list[BookConfig]:
        """回傳啟用中的書，依 book_id 排序。"""
        return sorted((b for b in self.books.values() if b.enabled), key=lambda b: b.book_id)

    def book(self, book_id: str) -> BookConfig:
        """取得書設定；未知的 book_id 丟 ConfigError。"""
        try:
            return self.books[book_id]
        except KeyError:
            known = ", ".join(sorted(self.books)) or "(none)"
            raise ConfigError(f"unknown book_id {book_id!r}; configured books: {known}") from None

    def book_dir(self, book_id: str) -> Path:
        """書在書庫中的資料夾（唯讀）。"""
        return self.book_library / self.book(book_id).source_dir

    def work_dir(self, book_id: str) -> Path:
        """這本書的產出資料夾：``<project_root>/works/<book_id>``。"""
        self.book(book_id)
        return self.works_dir / book_id

    @property
    def works_dir(self) -> Path:
        """所有作品的根資料夾：config.yaml 的 ``works_dir``，沒設就是 ``<project_root>/works``。"""
        return self.works_dir_override if self.works_dir_override is not None else self.project_root / "works"

    # ------------------------------------------------------------ 寫入保護

    def protected_roots(self) -> list[Path]:
        """所有唯讀根目錄（解析成絕對路徑）。"""
        roots = [self.book_library, *self.reference_sources.values()]
        return [_resolve(p) for p in roots]

    def is_write_protected(self, path: str | Path) -> bool:
        """路徑是否位於任何唯讀目錄之內（含目錄本身）。"""
        target = _resolve(path)
        return any(_is_within(target, root) for root in self.protected_roots())

    def assert_writable(self, path: str | Path) -> Path:
        """確認可以寫入此路徑，回傳解析後的絕對路徑；否則丟 WriteProtectedError。

        條件：必須在專案目錄之內，且不在任何唯讀目錄之內。
        """
        target = _resolve(path)
        if self.is_write_protected(target):
            raise WriteProtectedError(f"refusing to write inside a read-only directory: {target}")
        if not (_is_within(target, _resolve(self.project_root)) or _is_within(target, _resolve(self.works_dir))):
            raise WriteProtectedError(f"refusing to write outside the project root or works_dir: {target}")
        return target

    # ------------------------------------------------------------ 檢查

    def check_paths(self) -> list[str]:
        """檢查設定的路徑是否存在，回傳問題清單（空清單＝沒問題）。"""
        problems: list[str] = []
        if not self.project_root.is_dir():
            problems.append(f"project_root not found: {self.project_root}")
        if not self.book_library.is_dir():
            problems.append(f"book_library.path not found: {self.book_library}")
        for name, path in self.reference_sources.items():
            if not path.is_dir():
                problems.append(f"reference_sources.{name}.path not found: {path}")
        for book in self.enabled_books():
            if self.book_library.is_dir() and not (self.book_library / book.source_dir).is_dir():
                problems.append(f"book {book.book_id}: source_dir not found: {self.book_library / book.source_dir}")
        return problems


# ---------------------------------------------------------------- 載入


def load_config(path: str | Path | None = None) -> Config:
    """讀取並驗證 config.yaml。``path`` 預設為專案根目錄的 ``config.yaml``。"""
    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path} (copy config.example.yaml to config.yaml)")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{cfg_path.name}: invalid YAML: {exc}") from exc
    return parse_config(raw, base_dir=cfg_path.parent)


def parse_config(raw: Any, base_dir: str | Path | None = None) -> Config:
    """驗證已解析的設定內容並轉成 Config。``base_dir`` 用來決定 project_root 的預設值。"""
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping at the top level")
    _check_ascii_keys(raw, "")

    default_root = Path(base_dir) if base_dir is not None else REPO_ROOT
    project_root = Path(str(raw["project_root"])) if raw.get("project_root") else default_root

    library_raw = _require_mapping(raw, "book_library")
    book_library = _protected_path(library_raw, "book_library")

    sources: dict[str, Path] = {}
    commits: dict[str, str] = {}
    refs = raw.get("reference_sources") or {}
    if not isinstance(refs, dict):
        raise ConfigError("reference_sources must be a mapping")
    for name, entry in refs.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"reference_sources.{name} must be a mapping")
        sources[str(name)] = _protected_path(entry, f"reference_sources.{name}")
        commits[str(name)] = str(entry.get("commit") or "")

    books = _parse_books(raw.get("books"))

    comfy = raw.get("comfyui") or {}
    if not isinstance(comfy, dict):
        raise ConfigError("comfyui must be a mapping")
    llm_section = executors.normalize_llm_section(raw.get("llm"))
    default_executor = executors.resolve_executor(executors.build_providers(llm_section["providers"]), llm_section["default"])

    annotate = raw.get("annotate") or {}
    if not isinstance(annotate, dict):
        raise ConfigError("annotate must be a mapping")
    try:
        chunk_chars = int(annotate.get("chunk_chars") or 0)
    except (TypeError, ValueError):
        raise ConfigError("annotate.chunk_chars must be an integer") from None
    if not 0 <= chunk_chars <= 200000:
        raise ConfigError("annotate.chunk_chars must be between 0 and 200000 (0 = automatic)")

    works_raw = raw.get("works_dir")
    server = raw.get("server") or {}
    if not isinstance(server, dict):
        raise ConfigError("server must be a mapping")
    try:
        port = int(server.get("port") or 7866)
    except (TypeError, ValueError):
        raise ConfigError("server.port must be an integer") from None
    if not 1024 <= port <= 65535:
        raise ConfigError("server.port must be between 1024 and 65535")

    config = Config(
        project_root=project_root,
        book_library=book_library,
        reference_sources=sources,
        reference_commits=commits,
        books=books,
        llm_default=llm_section["default"],
        llm_overrides=llm_section["providers"],
        llm_provider=str(default_executor.get("base_id") or default_executor["id"]),
        llm_model=str(default_executor.get("model") or ""),
        llm_thinking=executors.structured_effort(default_executor),
        llm_max_output=int(default_executor.get("max_output") or 32768),
        comfyui_base_url=str(comfy.get("base_url") or "http://127.0.0.1:8188"),
        works_dir_override=Path(str(works_raw)) if works_raw else None,
        server_port=port,
        annotate_chunk_chars=chunk_chars,
    )
    _check_overlap(config)
    return config


# ---------------------------------------------------------------- 內部工具


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _check_ascii_keys(obj: Any, where: str) -> None:
    """設定檔的 key 一律 ASCII（值可以是任何語言）。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str) or not key.isascii():
                raise ConfigError(f"config key must be ASCII: {where}{key!r}")
            _check_ascii_keys(value, f"{where}{key}.")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _check_ascii_keys(value, f"{where}[{index}].")


def _require_mapping(raw: dict, key: str) -> dict:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} is required and must be a mapping")
    return value


def _protected_path(entry: dict, where: str) -> Path:
    if not entry.get("path"):
        raise ConfigError(f"{where}.path is required")
    access = entry.get("access", READ_ONLY)
    if access != READ_ONLY:
        raise ConfigError(f"{where}.access must be {READ_ONLY!r} (got {access!r}); this directory is never written")
    return Path(str(entry["path"]))


def _parse_books(raw: Any) -> dict[str, BookConfig]:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ConfigError("books must be a list")
    books: dict[str, BookConfig] = {}
    for index, entry in enumerate(raw):
        where = f"books[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a mapping")
        book_id = str(entry.get("book_id") or "")
        if not BOOK_ID_RE.match(book_id):
            raise ConfigError(f"{where}.book_id {book_id!r} must match {BOOK_ID_RE.pattern}")
        if book_id in books:
            raise ConfigError(f"duplicate book_id {book_id!r}")
        source_dir = str(entry.get("source_dir") or "")
        if not source_dir or Path(source_dir).is_absolute() or ".." in Path(source_dir).parts:
            raise ConfigError(f"{where}.source_dir must be a relative folder name inside book_library")
        lang = str(entry.get("lang") or "")
        if not LANG_RE.match(lang):
            raise ConfigError(f"{where}.lang {lang!r} must be a BCP-47 tag such as zh-TW")
        names = entry.get("display_name")
        if not isinstance(names, dict) or not names:
            raise ConfigError(f"{where}.display_name must be a non-empty mapping of language tag to name")
        for tag, name in names.items():
            if not LANG_RE.match(str(tag)) or not isinstance(name, str) or not name:
                raise ConfigError(f"{where}.display_name has an invalid entry: {tag!r}")
        books[book_id] = BookConfig(
            book_id=book_id,
            source_dir=source_dir,
            lang=lang,
            display_name={str(k): str(v) for k, v in names.items()},
            enabled=bool(entry.get("enabled", True)),
        )
    return books


def _check_overlap(config: Config) -> None:
    """唯讀目錄不可以放在專案目錄裡面，專案目錄也不可以放在唯讀目錄裡面。"""
    project = _resolve(config.project_root)
    for root in config.protected_roots():
        if _is_within(root, project):
            raise ConfigError(f"read-only directory must be outside project_root: {root}")
        if _is_within(project, root):
            raise ConfigError(f"project_root must not be inside a read-only directory: {root}")
        works = _resolve(config.works_dir)
        if _is_within(root, works) or _is_within(works, root):
            raise ConfigError(f"works_dir and the read-only directory {root} must not contain each other")


# ---------------------------------------------------------------- CLI


def init_config(path: str | Path | None = None, example: str | Path | None = None,
                project_root: str | Path | None = None) -> Path:
    """第一次使用：把 ``config.example.yaml`` 複製成 ``config.yaml``，並把 project_root 指到這個 checkout。

    只改 ``project_root`` 那一行，其餘註解原樣保留。``config.yaml`` 已存在就丟 ConfigError，不覆寫。
    """
    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if cfg_path.exists():
        raise ConfigError(f"config file already exists: {cfg_path} (edit it instead of re-creating)")
    src = Path(example) if example is not None else EXAMPLE_CONFIG_PATH
    if not src.is_file():
        raise ConfigError(f"example config not found: {src}")
    if not cfg_path.parent.is_dir():
        raise ConfigError(f"folder does not exist: {cfg_path.parent}")
    root = Path(project_root) if project_root is not None else cfg_path.parent.resolve()
    lines = src.read_text(encoding="utf-8").splitlines(keepends=True)
    replacement = f"project_root: {root}\n"
    for index, text in enumerate(lines):
        if text.startswith("project_root:"):
            lines[index] = replacement
            break
    else:
        lines.insert(0, replacement)
    cfg_path.write_text("".join(lines), encoding="utf-8")
    return cfg_path


def main(argv: list[str] | None = None) -> int:
    """``python -m t2vn.config [--config PATH] [--init]``：檢查設定與路徑；``--init`` 產生 config.yaml。"""
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Validate config.yaml and check that its paths exist.")
    parser.add_argument("--config", default=None, help="path to config.yaml (default: <repo>/config.yaml)")
    parser.add_argument("--init", action="store_true",
                        help="create config.yaml from config.example.yaml (refuses to overwrite)")
    args = parser.parse_args(argv)
    if args.init:
        try:
            created = init_config(args.config)
        except T2vnError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"created {created}")
        print("next: edit it and set book_library.path to your novel folder, then start the studio again")
        return 0
    try:
        config = load_config(args.config)
    except T2vnError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"project_root : {config.project_root}")
    print(f"works_dir    : {config.works_dir}")
    print(f"book_library : {config.book_library} (read only)")
    for name, path in config.reference_sources.items():
        commit = config.reference_commits.get(name) or "(commit not recorded)"
        print(f"reference    : {name} -> {path} (read only, {commit})")
    for book in config.enabled_books():
        print(f"book         : {book.book_id} -> {book.source_dir} [{book.lang}]")
    problems = config.check_paths()
    for problem in problems:
        print(f"problem      : {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
