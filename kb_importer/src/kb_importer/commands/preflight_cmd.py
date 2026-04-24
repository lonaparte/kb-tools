"""`kb-importer preflight` — verify the fulltext LLM provider is
reachable before running a real import.

Sends a single 5-token "echo ping" request to the configured (or
flag-overridden) provider / model, then reports:
  - provider + model being used
  - success / failure (structured error if fail)
  - prompt + completion tokens actually charged
  - round-trip time

Motivation: catches wrong API key / wrong model name / network /
regional outage BEFORE burning real tokens on a 1000-paper batch.
Designed to be cheap — one tiny call per run.

No Zotero side required. Doesn't touch the KB. Pure provider ping.
"""
from __future__ import annotations

import argparse
import sys
import time

from ..config import Config


def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "preflight",
        help=(
            "Ping the configured fulltext LLM provider with a tiny "
            "request to verify key / model / network before running "
            "a real import. Safe to run unlimited times — one call "
            "per invocation, ~5 tokens each way."
        ),
    )
    p.add_argument(
        "--fulltext-provider", default="gemini",
        choices=["gemini", "openai", "deepseek", "openrouter"],
        help=(
            "Provider to ping. Default gemini. Same choices as "
            "`import papers --fulltext-provider`."
        ),
    )
    p.add_argument(
        "--fulltext-model", default=None,
        help=(
            "Model to ping. Default: provider's import-default "
            "(gemini-3.1-pro-preview / gpt-4o-mini / deepseek-chat "
            "/ openai/gpt-oss-120b:free)."
        ),
    )
    p.add_argument(
        "--prompt", default="reply with just the word 'ok'",
        help=(
            "Exact user message sent to the provider. Default is a "
            "5-token cue that elicits a short response. Override if "
            "you want to validate a specific prompt shape (e.g. a "
            "model that rejects short inputs)."
        ),
    )
    p.add_argument(
        "--max-tokens", type=int, default=200,
        help=(
            "Max output tokens the provider is allowed to emit in "
            "its reply. Default 200 — deliberately generous because "
            "Gemini 2.5/3.x 'thinking' models eat most of the "
            "budget as hidden thinking tokens before emitting the "
            "actual answer. A tight default (e.g. 10) would cause "
            "spurious MAX_TOKENS-with-empty-reply failures on those "
            "models. 200 tokens is still ~$0.0001 per preflight "
            "at current pricing."
        ),
    )
    p.set_defaults(func=_cmd_preflight)


def _cmd_preflight(args: argparse.Namespace, cfg: Config) -> int:
    # Late import so `preflight --help` doesn't pull the full
    # summarize stack (which pulls the openai SDK if installed).
    from ..summarize import (
        build_provider_from_env,
        SummarizerError,
        QuotaExhaustedError,
        BadRequestError,
    )

    try:
        provider = build_provider_from_env(
            args.fulltext_provider, args.fulltext_model,
        )
    except SummarizerError as e:
        print(
            f"✗ preflight: could not build provider "
            f"{args.fulltext_provider!r}: {e}",
            file=sys.stderr,
        )
        print(
            "\nHints:",
            file=sys.stderr,
        )
        print(
            "  - Check the provider's API key env var is exported "
            "in your shell rc:",
            file=sys.stderr,
        )
        print(
            "      gemini     → GEMINI_API_KEY",
            file=sys.stderr,
        )
        print(
            "      openai     → OPENAI_API_KEY",
            file=sys.stderr,
        )
        print(
            "      deepseek   → DEEPSEEK_API_KEY",
            file=sys.stderr,
        )
        print(
            "      openrouter → OPENROUTER_API_KEY",
            file=sys.stderr,
        )
        return 2

    label = f"{provider.name}/{provider.model}"
    print(f"→ preflight: {label}  (sending {len(args.prompt)}-char prompt)")

    t0 = time.monotonic()
    try:
        text, pin, pout = provider.complete(
            system="You are a ping responder. Answer briefly.",
            user=args.prompt,
            max_output_tokens=args.max_tokens,
            temperature=0.0,
        )
    except QuotaExhaustedError as e:
        dt = time.monotonic() - t0
        print(
            f"✗ preflight: quota exhausted ({e.quota_type}) on "
            f"{label} after {dt:.2f}s. {e}",
            file=sys.stderr,
        )
        return 3
    except BadRequestError as e:
        dt = time.monotonic() - t0
        print(
            f"✗ preflight: provider rejected request (HTTP 400/404) "
            f"on {label} after {dt:.2f}s. {e}",
            file=sys.stderr,
        )
        print(
            "\nThis usually means:",
            file=sys.stderr,
        )
        print(
            "  - Model name is mis-spelled or deprecated.",
            file=sys.stderr,
        )
        print(
            "  - API endpoint doesn't support chat completions.",
            file=sys.stderr,
        )
        print(
            "  - Prompt shape is rejected (rare for a 5-token ping).",
            file=sys.stderr,
        )
        return 4
    except SummarizerError as e:
        dt = time.monotonic() - t0
        print(
            f"✗ preflight: LLM call failed on {label} after "
            f"{dt:.2f}s. {e}",
            file=sys.stderr,
        )
        return 5
    dt = time.monotonic() - t0

    preview = text.strip().replace("\n", "\\n")[:120]
    print(
        f"✓ preflight: {label} responded in {dt:.2f}s "
        f"(prompt_tokens={pin}, completion_tokens={pout})"
    )
    print(f"  reply: {preview!r}")
    return 0
