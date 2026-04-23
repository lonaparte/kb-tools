"""AI-facing prompt templates shipped with kb-importer.

These are data files (markdown), not Python code. They exist here so
they can be discovered via `importlib.resources.files("kb_importer.templates")`
regardless of how kb-importer is installed (editable, wheel, sdist).

Currently shipping:
- ai_summary_prompt.md: the prompt given to an LLM when generating
  per-paper summaries for `kb-importer set-summary`.

Users who want to adjust the template should edit the installed file
directly (its path can be found via `kb-importer show-template --path`).
kb-importer never rewrites this file, and never parses its contents —
it only hands the text to LLM agents as-is.
"""
