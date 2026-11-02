import hashlib
import re
from pathlib import Path

import pytest

VENDOR = Path(__file__).resolve().parents[2] / "vendor"


@pytest.mark.parametrize("name,count", [("novel_studio", 5), ("ruxi", 6)])
def test_hashes_match_vendored_md(name, count):
    root = VENDOR / name
    table = (root / "VENDORED.md").read_text(encoding="utf-8")
    pairs = re.findall(r"\| `([a-zA-Z_.-]+)` \|[^|]*\| `([0-9a-f]{64})` \|", table)
    assert len(pairs) == count
    for file_name, digest in pairs:
        assert hashlib.sha256((root / file_name).read_bytes()).hexdigest() == digest, file_name


def test_modules_import():
    import vendor.novel_studio.comfy  # noqa: F401
    import vendor.novel_studio.importer  # noqa: F401
    import vendor.novel_studio.lang  # noqa: F401
    import vendor.novel_studio.providers  # noqa: F401
    import vendor.ruxi.build_html  # noqa: F401
    import vendor.ruxi.walk_script  # noqa: F401
