"""Regression for the v0.28.2 read_md BOM / missing-kind guards.

Pre-0.28.2 (stress-run finding G54): a BOM-prefixed md would have
python-frontmatter silently parse its metadata as {} (the BOM
confuses `---` detection). Any kb-write RMW op (tag add, ref add,
ai-zone append, thought update, etc.) would then write back a
file with fresh fm `{kb_tags: [...]}` and the ORIGINAL frontmatter
dumped into the body as literal text. The next kb-mcp index run
would see `kind=None` and silently skip the paper.

v0.28.2 refuses to return from read_md when:
  (a) the file starts with a UTF-8 BOM, or
  (b) the parsed frontmatter is empty OR missing `kind`.

Callers (tag/ref/ai_zone/thought.update/topic.update/pref.update
ops) all read via read_md, so the fix fires uniformly.
"""
from __future__ import annotations

import pathlib

import pytest

from conftest import skip_if_no_frontmatter


def test_bom_prefix_refused(tmp_path):
    skip_if_no_frontmatter()
    from kb_write.frontmatter import read_md, FrontmatterError

    p = tmp_path / "bom.md"
    p.write_bytes(
        b"\xef\xbb\xbf"  # UTF-8 BOM
        b"---\n"
        b"kind: paper\n"
        b"title: x\n"
        b"---\n"
        b"body\n"
    )
    with pytest.raises(FrontmatterError) as exc:
        read_md(p)
    assert "BOM" in str(exc.value)


def test_empty_frontmatter_refused(tmp_path):
    skip_if_no_frontmatter()
    from kb_write.frontmatter import read_md, FrontmatterError

    p = tmp_path / "nofm.md"
    p.write_text("just a body with no frontmatter\n")
    with pytest.raises(FrontmatterError) as exc:
        read_md(p)
    # Must mention kind / missing so the operator knows WHY.
    msg = str(exc.value).lower()
    assert "kind" in msg or "missing" in msg


def test_kind_none_refused(tmp_path):
    skip_if_no_frontmatter()
    from kb_write.frontmatter import read_md, FrontmatterError

    p = tmp_path / "nullkind.md"
    p.write_text(
        "---\n"
        "kind: ~\n"          # explicit YAML null
        "title: x\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(FrontmatterError):
        read_md(p)


def test_valid_md_still_works(tmp_path):
    skip_if_no_frontmatter()
    from kb_write.frontmatter import read_md

    p = tmp_path / "ok.md"
    p.write_text(
        "---\n"
        "kind: paper\n"
        "title: normal\n"
        "---\n"
        "body\n"
    )
    fm, body, mtime = read_md(p)
    assert fm["kind"] == "paper"
    assert fm["title"] == "normal"
    assert body.strip() == "body"
    assert mtime > 0


def test_tag_add_on_bom_md_no_data_loss(tmp_path):
    """End-to-end: the original G54 reproducer. Without the guard,
    `tag.add` on a BOM-md would overwrite with a fresh frontmatter
    containing only kb_tags, losing kind/title/zotero_key. With the
    guard, it refuses and the file is unchanged."""
    skip_if_no_frontmatter()
    from kb_write.config import WriteContext
    from kb_write.ops import tag as tag_ops
    from kb_write.frontmatter import FrontmatterError

    (tmp_path / "papers").mkdir()
    md = tmp_path / "papers" / "BOMPAPER.md"
    original = (
        b"\xef\xbb\xbf"
        b"---\r\n"
        b"kind: paper\r\n"
        b"title: bom\r\n"
        b"zotero_key: BOMPAPER\r\n"
        b"---\r\n"
        b"body with CRLF\r\n"
    )
    md.write_bytes(original)

    ctx = WriteContext(
        kb_root=tmp_path, git_commit=False, reindex=False,
        lock=False, dry_run=False,
    )
    with pytest.raises(FrontmatterError):
        tag_ops.add(ctx, "papers/BOMPAPER", "new-tag")

    # File on disk UNCHANGED.
    assert md.read_bytes() == original, (
        "tag.add on BOM-md must refuse before writing — file changed"
    )
