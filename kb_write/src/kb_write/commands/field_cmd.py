"""`kb-write tag` and `kb-write ref` — list-field frontmatter ops."""
from __future__ import annotations

from ..ops import tag as tag_ops
from ..ops import ref as ref_ops
from ._shared import _emit_result


# ---------- tag ----------
def register_tag(sub) -> None:
    p = sub.add_parser("tag", help="Add/remove kb_tags on any md.")
    ps = p.add_subparsers(dest="tag_cmd", required=True)

    a = ps.add_parser("add", help="Append a tag.")
    a.add_argument("target", help="E.g. papers/ABCD1234, topics/X, thoughts/Y")
    a.add_argument("--tag", required=True)
    a.add_argument("--expected-mtime", type=float,
                   help="Optional mtime guard.")
    a.set_defaults(func=_cmd_tag_add)

    r = ps.add_parser("remove", help="Remove a tag.")
    r.add_argument("target")
    r.add_argument("--tag", required=True)
    r.add_argument("--expected-mtime", type=float)
    r.set_defaults(func=_cmd_tag_remove)


def _cmd_tag_add(args, ctx):
    result = tag_ops.add(
        ctx, args.target, args.tag, expected_mtime=args.expected_mtime,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_tag_remove(args, ctx):
    result = tag_ops.remove(
        ctx, args.target, args.tag, expected_mtime=args.expected_mtime,
    )
    _emit_result(args, ctx, result)
    return 0


# ---------- ref ----------
def register_ref(sub) -> None:
    p = sub.add_parser("ref", help="Add/remove kb_refs on any md.")
    ps = p.add_subparsers(dest="ref_cmd", required=True)

    a = ps.add_parser("add", help="Append a reference.")
    a.add_argument("target")
    a.add_argument("--target-ref", "--ref", dest="target_ref", required=True,
                   help="Reference to add, e.g. papers/ABCD1234.")
    a.add_argument("--expected-mtime", type=float)
    a.set_defaults(func=_cmd_ref_add)

    r = ps.add_parser("remove", help="Remove a reference.")
    r.add_argument("target")
    r.add_argument("--target-ref", "--ref", dest="target_ref", required=True)
    r.add_argument("--expected-mtime", type=float)
    r.set_defaults(func=_cmd_ref_remove)


def _cmd_ref_add(args, ctx):
    result = ref_ops.add(
        ctx, args.target, args.target_ref,
        expected_mtime=args.expected_mtime,
    )
    _emit_result(args, ctx, result)
    return 0


def _cmd_ref_remove(args, ctx):
    result = ref_ops.remove(
        ctx, args.target, args.target_ref,
        expected_mtime=args.expected_mtime,
    )
    _emit_result(args, ctx, result)
    return 0
