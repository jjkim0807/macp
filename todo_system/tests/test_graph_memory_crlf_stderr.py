"""#144 CRLF frontmatter + comment lines; #145 scan progress on stderr."""
from __future__ import annotations

from pathlib import Path

from todo_system import graph_memory as gm

LF = "---\ntags:\n  - todo\n# decision_status: open | done\nparent: \"[[umbrella]]\"\ndepends_on:\n  - other-task\n---\n# Title\n\nSee [[note-a]].\n"


def test_crlf_frontmatter_parses_like_lf():
    fm_lf, body_lf = gm.parse_frontmatter(LF)
    fm_crlf, body_crlf = gm.parse_frontmatter(LF.replace("\n", "\r\n"))
    assert fm_crlf == fm_lf
    assert fm_crlf["depends_on"] == ["other-task"]
    assert body_crlf.replace("\r\n", "\n") == body_lf


def test_comment_lines_are_not_keys():
    fm, _ = gm.parse_frontmatter(LF)
    assert not any(k.startswith("#") for k in fm)
    assert fm["parent"] == "[[umbrella]]"


def _vault(tmp_path: Path, text: str) -> Path:
    items = tmp_path / "Todo" / "items"
    items.mkdir(parents=True)
    (items / "a-task.md").write_bytes(text.encode("utf-8"))
    return tmp_path


def test_crlf_file_still_yields_typed_edges(tmp_path):
    root = _vault(tmp_path, LF.replace("\n", "\r\n"))
    con = gm.init_relations_db(tmp_path / "rel.sqlite")
    try:
        gm.scan_vault_and_index(vault_root=root, con=con)
        types = {r["edge_type"] for r in con.execute("SELECT edge_type FROM edges")}
    finally:
        con.close()
    assert {"depends_on", "parent"} <= types


def test_scan_progress_goes_to_stderr_not_stdout(tmp_path, capsys):
    root = _vault(tmp_path, LF)
    con = gm.init_relations_db(tmp_path / "rel.sqlite")
    try:
        gm.scan_vault_and_index(vault_root=root, con=con)
    finally:
        con.close()
    out, err = capsys.readouterr()
    assert out == ""
    assert "indexed" in err
