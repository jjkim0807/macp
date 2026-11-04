"""Pins ``scripts/KAKAO_ALERTS.md`` anchors to real code.

The catalog cites each kakao alert source as a `` `module/path.py:function` ``
token (no line number — line numbers drift silently every time the module is
edited). This test extracts every such token from the doc and asserts the
named function is actually defined in that module, so a rename or deletion
breaks here instead of leaving the catalog quietly pointing at nothing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "scripts" / "KAKAO_ALERTS.md"

ANCHOR_RE = re.compile(r"`([\w./-]+\.py):([A-Za-z_][A-Za-z0-9_]*)`")
STALE_LINE_ANCHOR_RE = re.compile(r"`[\w./-]+\.py:\d+`")

# Only the "발신처 전수 표 — macp 코드" table is this repo's own catalog (module:function
# anchors this test pins). The following "외부 리포" table and replay-log section cite
# other repos' files/lines incidentally and are out of scope.
_SECTION_START = "## 발신처 전수 표 — macp 코드"
_SECTION_END = "## 발신처 전수 표 — 외부 리포"


def _macp_code_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index(_SECTION_START)
    end = text.index(_SECTION_END)
    return text[start:end]


def _doc_anchors() -> list[tuple[str, str]]:
    return ANCHOR_RE.findall(_macp_code_section())


def _defines_function(module_path: Path, func_name: str) -> bool:
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return True
    return False


def test_doc_has_anchors() -> None:
    assert len(_doc_anchors()) >= 20, "expected the macp-code catalog table to yield anchors"


def test_every_doc_anchor_function_exists() -> None:
    missing: list[str] = []
    for rel_path, func_name in _doc_anchors():
        module_path = REPO_ROOT / rel_path
        if not module_path.is_file():
            missing.append(f"{rel_path}: file does not exist")
            continue
        if not _defines_function(module_path, func_name):
            missing.append(f"{rel_path}:{func_name} — function not found")
    assert not missing, "KAKAO_ALERTS.md anchors drifted:\n" + "\n".join(missing)


def test_doc_anchors_carry_no_line_numbers() -> None:
    stale = STALE_LINE_ANCHOR_RE.findall(_macp_code_section())
    assert not stale, f"found file:line anchors that should be module:function: {stale}"
