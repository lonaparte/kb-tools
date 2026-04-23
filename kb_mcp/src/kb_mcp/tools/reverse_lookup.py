"""find_paper_by_attachment_key: O(1) reverse lookup via the projection DB."""
from __future__ import annotations

from ..store import Store


def find_paper_by_attachment_key_impl(store: Store, attachment_key: str) -> str:
    """Look up a paper key given an attachment key.

    Returns a formatted string with the paper info, or a "[not found]"
    message. O(1) via the paper_attachments primary key index.
    """
    row = store.execute("""
        SELECT pa.paper_key, pa.is_main, pa.position,
               p.title, p.year, p.authors
        FROM paper_attachments pa
        LEFT JOIN papers p ON p.paper_key = pa.paper_key
        WHERE pa.attachment_key = ?
    """, (attachment_key,)).fetchone()

    if row is None:
        return (
            f"[not found] No imported paper has attachment_key={attachment_key!r}. "
            "Either the paper hasn't been imported yet, or this isn't a "
            "valid attachment key."
        )

    lines = [
        f"paper_key: {row['paper_key']}",
        f"title: {row['title'] or '(no title)'}",
        f"year: {row['year'] or '?'}",
        f"authors: {row['authors'] or '[]'}",
        f"is_main_pdf: {'yes' if row['is_main'] else 'no'}",
        f"position: {row['position']}",
        f"md_path: papers/{row['paper_key']}.md",
    ]
    return "\n".join(lines)
