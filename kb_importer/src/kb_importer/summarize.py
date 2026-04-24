"""LLM summarization for kb-importer --fulltext.

Takes extracted fulltext + paper metadata; produces a 7-section
Chinese markdown summary that will be written into the paper's md
body under H2 headings.

Provider abstraction: minimal — just a single `complete(prompt) -> str`
method. Supports OpenAI, Gemini, and DeepSeek out of the box. All
three have OpenAI-compatible chat completion APIs at the HTTP layer,
so we talk HTTP directly rather than pulling in three separate SDKs.

The 7 sections (per docs/fulltext-design.md):
  1. 论文的主要内容
  2. 研究问题
  3. 方法
  4. 实验/案例
  5. 结论
  6. 作者评价
  7. 对我研究的意义
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Protocol


log = logging.getLogger(__name__)


# The exact section titles used in the final md output. Order matters.
SECTION_TITLES: tuple[str, ...] = (
    "论文的主要内容",
    "研究问题",
    "方法",
    "实验/案例",
    "结论",
    "作者评价",
    "对我研究的意义",
)


SYSTEM_PROMPT = """\
你是一位电力电子 / 电力系统领域的研究型助理。给定一篇论文的正文 \
(可能被截断),请按固定 7 节结构产出中文摘要,以 JSON 返回。

规则:
- 每节 2-6 句话,简洁而具体。给具体数字、方程标号、结论,不要空话。
- 不臆造。原文没有的内容不写;原文用英文术语就直接用英文术语。
- 第 6 节 "作者评价" 总结作者自己声明的贡献/局限,不要你自己的评价。
- 第 7 节 "对我研究的意义" 一句话指出这篇对后续 grid-forming / \
电力电子稳定性方向的读者可能的借鉴点。

严格按以下 JSON 返回,不要包裹 code fence,不要加任何其他文字:

{
  "section_1": "...",
  "section_2": "...",
  "section_3": "...",
  "section_4": "...",
  "section_5": "...",
  "section_6": "...",
  "section_7": "..."
}
"""


USER_PROMPT_TMPL = """\
论文元数据:
  标题: {title}
  作者: {authors}
  年份: {year}
  DOI:   {doi}
  摘要:  {abstract}

论文正文 (可能经过清洗 / 截断):
---
{fulltext}
---

请按 system prompt 要求返回 7 节 JSON。
"""


@dataclass
class SummaryResult:
    sections: dict[int, str]    # {1: "...", 2: "...", ..., 7: "..."}
    provider: str               # "openai" | "gemini" | "deepseek"
    model: str                  # actual model name used
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_markdown(self) -> str:
        """Render as H2 sections in the exact order defined above."""
        lines = []
        for i, title in enumerate(SECTION_TITLES, start=1):
            body = self.sections.get(i, "").strip()
            lines.append(f"## {i}. {title}")
            lines.append("")
            lines.append(body if body else "*(未生成)*")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


class SummarizerError(Exception):
    """Raised when summarization fails in a way the caller should see.
    Transient HTTP errors should be retried by the caller; we raise
    only on unrecoverable problems (API key missing, malformed
    response after retries).

    v27: carries a machine-readable `code` so downstream classifiers
    (re_read, re_summarize, events) can route by tag instead of
    substring-matching the message string. Subclasses override `code`.

    Codes:
      "bad_request"  — provider returned 400 / malformed response
      "llm_other"    — any other unclassified LLM-side failure
      "not_found"    — provider returned 404 for the paper
      "pdf_missing"  — the PDF input couldn't be located / opened
      "quota"        — provider-level quota exhausted (see QuotaExhaustedError)
    """
    code: str = "llm_other"


class BadRequestError(SummarizerError):
    """Provider rejected the request (HTTP 400, malformed JSON reply,
    schema mismatch). Distinct from generic failures because a
    retry with the same input will always hit the same 400 — don't
    retry this, surface it instead."""
    code: str = "bad_request"


class PdfMissingError(SummarizerError):
    """The PDF this summary depends on couldn't be located on disk
    or via the Zotero API. Recovery path is different from an LLM
    failure (fix the attachment, not the model)."""
    code: str = "pdf_missing"


class QuotaExhaustedError(SummarizerError):
    """Raised when the provider signals the configured model has hit
    its quota. Distinguished from generic SummarizerError so the
    caller can choose to fall back to a different model instead of
    treating the paper as a permanent failure.

    Attributes:
        quota_type: "daily" if the provider indicates a per-day quota
            (RPD exhaustion — today's requests are over), "rate" if
            per-minute / burst (retry after short sleep works), or
            "unknown" if we couldn't classify. Callers typically:
              - daily → switch to fallback model, keep running
              - rate  → sleep `retry_after` seconds, retry same model
              - unknown → treat as daily (safer; avoids hot-looping)
        retry_after: seconds to wait before retry, parsed from the
            error payload when available, else None.
        model: the model that ran out of quota, for logging.
    """
    code: str = "quota"

    def __init__(
        self,
        message: str,
        *,
        quota_type: str = "unknown",
        retry_after: float | None = None,
        model: str = "",
    ):
        super().__init__(message)
        self.quota_type = quota_type
        self.retry_after = retry_after
        self.model = model


class LLMProvider(Protocol):
    """Minimal contract. Each provider wraps its own HTTP client.
    Returns raw completion text; parsing is the caller's job.
    """

    name: str
    model: str

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int = 8000,
        temperature: float = 0.2,
    ) -> tuple[str, int, int]:
        """Return (text, prompt_tokens, completion_tokens).

        If the provider doesn't expose token counts, return 0s — the
        caller reports them as "unknown" in the aggregate stats rather
        than crashing.
        """
        ...


# ----------------------------------------------------------------------
# Concrete providers. Kept small — HTTP-level, no SDK dependency.
# ----------------------------------------------------------------------


class OpenAIChatProvider:
    """OpenAI-compatible chat completion. Works out of the box for:
    - OpenAI (api.openai.com)
    - DeepSeek (api.deepseek.com, same wire format)
    - OpenRouter (openrouter.ai/api/v1, same wire format +
      optional HTTP-Referer / X-Title headers for ranking)
    - Any OpenAI-compatible gateway (Together, Groq, vLLM, ...)

    DeepSeek reuse: set base_url="https://api.deepseek.com/v1" and
    model="deepseek-chat". Its `gpt-4o-mini`-equivalent pricing is
    lower than OpenAI's; for summarization volume it saves real money.

    OpenRouter reuse: set base_url="https://openrouter.ai/api/v1"
    and model="openai/gpt-4o-mini" (or any other OpenRouter-catalog
    ID). One key unlocks many upstream models; useful when you want
    OpenAI-flavoured chat behavior but pay through OpenRouter.
    `extra_headers` carries OpenRouter's optional `HTTP-Referer` /
    `X-Title` for opt-in attribution.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        name: str = "openai",
        extra_headers: dict[str, str] | None = None,
    ):
        self.name = name
        self.model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._extra_headers = dict(extra_headers or {})

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int = 8000,
        temperature: float = 0.2,
    ) -> tuple[str, int, int]:
        import urllib.request
        import urllib.error
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_output_tokens,
            "temperature": temperature,
        }).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # Caller-supplied headers override the defaults above only if
        # they deliberately collide; normally these are attribution-
        # only (HTTP-Referer / X-Title for OpenRouter ranking).
        headers.update(self._extra_headers)
        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            # 400 Bad Request: the request itself is broken (prompt
            # too long, malformed JSON body, unsupported model, etc.).
            # Retrying with the same input can't help; surface as
            # BadRequestError so the classifier routes to
            # `llm_bad_request` without message-matching.
            if e.code == 400:
                raise BadRequestError(
                    f"{self.name} HTTP 400: {detail or e.reason}"
                ) from e
            raise SummarizerError(
                f"{self.name} HTTP {e.code}: {detail or e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise SummarizerError(
                f"{self.name} network error: {e.reason}"
            ) from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # Non-JSON when we expected JSON is a request / response
            # schema mismatch — route through BadRequestError so it
            # bubbles up as `llm_bad_request` not generic LLM failure.
            raise BadRequestError(
                f"{self.name} returned non-JSON: {raw[:200]}"
            ) from e

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise SummarizerError(
                f"{self.name} response missing choices[0].message.content: "
                f"{json.dumps(data)[:300]}"
            ) from e
        usage = data.get("usage") or {}
        return (
            text,
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
        )


def _classify_quota_kind(detail: str) -> str:
    """Given the error body text from Gemini, guess whether the quota
    is per-day (RPD, resets at midnight UTC → switch model) or
    per-minute (RPM, resets in seconds → sleep+retry same model).

    Gemini's 429 body text is descriptive English like:
      "Quota exceeded for generate_requests_per_model_per_day,
       limit: 250"
    or
      "Resource has been exhausted (e.g. check quota)."
    We do simple substring matching. Unknown → "unknown" (caller
    will treat conservatively as daily to avoid hot-looping).
    """
    s = detail.lower()
    if "per_day" in s or "per day" in s or "daily" in s or "rpd" in s:
        return "daily"
    if "per_minute" in s or "per minute" in s or "rpm" in s:
        return "rate"
    return "unknown"


def _extract_retry_delay(detail: str) -> float | None:
    """Try to parse the retry-after hint Gemini puts in the 429 body,
    e.g. "Please retry in 34.2s" or "retryDelay: 12s". Returns seconds
    as float, or None if we can't find one. Caller uses this to sleep
    before retrying — not authoritative, just a hint.
    """
    # "Please retry in 34.2s"
    m = re.search(r"retry in ([0-9.]+)\s*s", detail, flags=re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # "retryDelay": "34s"
    m = re.search(r'retryDelay["\s:]+["\']([0-9.]+)s', detail,
                  flags=re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


class GeminiProvider:
    """Google Gemini via the generativelanguage.googleapis.com REST
    endpoint. Separate from the OpenAI-compatible interface because
    Gemini uses a different request shape (contents/parts).

    Model examples:
      - "gemini-2.0-flash"         (fast, cheap, 1M context)
      - "gemini-2.5-flash"         (newer, slightly better reasoning)
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        name: str = "gemini",
    ):
        self.name = name
        self.model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int = 8000,
        temperature: float = 0.2,
    ) -> tuple[str, int, int]:
        import urllib.request
        import urllib.error
        # Gemini uses query-string API key, not bearer auth.
        url = (
            f"{self._base_url}/models/{self.model}:generateContent"
            f"?key={self._api_key}"
        )

        # Thinking budget / level handling:
        # - Gemini 3.x series (gemini-3-*, gemini-3.1-*): use
        #   thinkingConfig.thinkingLevel = "low" to keep thinking
        #   overhead small. Our 7-section summary doesn't need deep
        #   reasoning and "low" cuts cost + latency drastically.
        # - Gemini 2.5 series split by variant:
        #   * gemini-2.5-flash / gemini-2.5-flash-lite accept
        #     thinkingBudget=0 to disable thinking entirely.
        #   * gemini-2.5-pro DOES NOT accept 0 ("Budget 0 is invalid.
        #     This model only works in thinking mode."). Its valid
        #     range is 128-32768 or -1 (dynamic). We use 128 — the
        #     minimum — to keep thinking-token cost minimal while
        #     remaining within the model's contract. This matters
        #     because 2.5-pro is the default
        #     `--fulltext-fallback-model` that kicks in once
        #     3.1-pro-preview hits its 250 RPD quota; without this
        #     carve-out, every fallback request 400s and the
        #     remaining papers all become llm-fail.
        # - Older models (2.0 etc) don't have thinking; omit the key.
        # Without this, Gemini 2.5/3.x eat most of maxOutputTokens as
        # thinking tokens and truncate the actual JSON → "LLM returned
        # non-JSON twice" errors that users hit in real runs.
        generation_config: dict = {
            "maxOutputTokens": max_output_tokens,
            "temperature": temperature,
            "responseMimeType": "application/json",
        }
        m = self.model.lower()
        if m.startswith("gemini-3") or "gemini-3." in m:
            generation_config["thinkingConfig"] = {"thinkingLevel": "low"}
        elif m.startswith("gemini-2.5-flash"):
            # -flash and -flash-lite allow thinkingBudget=0.
            generation_config["thinkingConfig"] = {"thinkingBudget": 0}
        elif m.startswith("gemini-2.5"):
            # -pro and any other 2.5-* variant we haven't seen:
            # stay on the safe side with the minimum positive budget.
            # Using 128 (the documented minimum for 2.5-pro) keeps
            # the same "minimise thinking overhead" intent without
            # tripping the API's 0-forbidden rule.
            generation_config["thinkingConfig"] = {"thinkingBudget": 128}

        body = json.dumps({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation_config,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            # Classify quota exhaustion separately from other HTTP
            # failures. Gemini returns 429 for both per-day (RPD) and
            # per-minute (RPM) quotas; caller wants to distinguish
            # because daily → switch model, per-minute → sleep+retry.
            if e.code == 429 or "RESOURCE_EXHAUSTED" in detail:
                quota_type = _classify_quota_kind(detail)
                retry_after = _extract_retry_delay(detail)
                raise QuotaExhaustedError(
                    f"gemini quota exhausted ({quota_type}, "
                    f"model={self.model}): {detail or 'HTTP 429'}",
                    quota_type=quota_type,
                    retry_after=retry_after,
                    model=self.model,
                ) from e
            # v0.28.2: HTTP 400 (bad request) and 404 (model not
            # found) are PERMANENT failures for this request —
            # retrying the same input to the same model will always
            # hit the same error. Distinguish them via BadRequestError
            # so the importer's retry loop can short-circuit (don't
            # re-call) AND, if the model itself is the problem (404 /
            # "model not found" in the detail), also stop trying
            # THIS MODEL for the rest of the batch. Pre-0.28.2, both
            # were buried in a generic SummarizerError and the
            # caller string-matched '400' at log time but didn't
            # change scheduling. Reviewer flagged this as "分类但不
            # 调度" — now the scheduler branch sees the right type.
            if e.code in (400, 404):
                raise BadRequestError(
                    f"gemini HTTP {e.code}: {detail or e.reason}"
                ) from e
            raise SummarizerError(
                f"gemini HTTP {e.code}: {detail or e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise SummarizerError(
                f"gemini network error: {e.reason}"
            ) from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SummarizerError(
                f"gemini returned non-JSON: {raw[:200]}"
            ) from e

        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as e:
            raise SummarizerError(
                f"gemini response missing candidates[0].content.parts: "
                f"{json.dumps(data)[:300]}"
            ) from e
        usage = data.get("usageMetadata") or {}
        return (
            text,
            int(usage.get("promptTokenCount", 0) or 0),
            int(usage.get("candidatesTokenCount", 0) or 0),
        )


def build_provider_from_env(
    provider: str = "gemini",
    model: str | None = None,
) -> LLMProvider:
    """Factory: read API key from env, pick the right provider class.

    Defaults to Gemini 2.0 Flash — cheapest and fast. If the user
    prefers OpenAI/DeepSeek, pass explicitly.
    """
    p = provider.lower()
    if p == "gemini":
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise SummarizerError(
                "GEMINI_API_KEY not set in environment. Get a free "
                "key at https://aistudio.google.com/apikey."
            )
        # Default: Gemini 3.1 Pro preview (current flagship as of
        # 2026-04). Reasons:
        # - gemini-2.0-flash was discontinued by Google; any new
        #   request 404s with "no longer available".
        # - gemini-3-pro-preview is also deprecated (2026-03-26).
        # - gemini-3.1-pro-preview is the current supported flagship,
        #   $2/M input / $12/M output; for 1200 papers ≈ $30 total.
        # - thinkingLevel="low" (set in GeminiProvider) cuts thinking
        #   overhead so 8000 output tokens is plenty for 7-section JSON.
        #
        # If the user wants cheaper, they can override with --fulltext-
        # model gemini-3-flash-preview or gemini-3.1-flash-lite.
        return GeminiProvider(
            api_key=key,
            model=model or "gemini-3.1-pro-preview",
        )
    if p == "openai":
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise SummarizerError("OPENAI_API_KEY not set")
        return OpenAIChatProvider(
            api_key=key, model=model or "gpt-4o-mini", name="openai",
        )
    if p == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise SummarizerError("DEEPSEEK_API_KEY not set")
        return OpenAIChatProvider(
            api_key=key,
            model=model or "deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            name="deepseek",
        )
    if p == "openrouter":
        # OpenRouter (https://openrouter.ai) routes chat requests to
        # many upstream providers. Wire format = OpenAI, so we reuse
        # OpenAIChatProvider with an overriden base_url.
        #
        # Default model: openai/gpt-4o-mini — cheap, fast, reliably
        # JSON-capable (the 7-section summarizer prompt expects JSON
        # back). Override with --fulltext-model for other catalog
        # entries, e.g. google/gemini-2.5-flash,
        # anthropic/claude-sonnet-4.5, deepseek/deepseek-chat.
        #
        # Shares `OPENROUTER_API_KEY` with the kb-mcp embedding
        # provider on purpose — one key covers both domains. The
        # two config surfaces stay independent, so you can use
        # OpenRouter for fulltext while keeping embeddings on
        # direct OpenAI (or vice versa).
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise SummarizerError(
                "OPENROUTER_API_KEY not set. Get a key at "
                "https://openrouter.ai/keys and put "
                "`export OPENROUTER_API_KEY=sk-or-...` in your "
                "shell rc."
            )
        # Optional attribution headers per OpenRouter docs. Surfaces
        # the project on their public leaderboard; safe to omit and
        # they don't affect routing.
        extra_headers = {
            "HTTP-Referer": "https://github.com/lichengsheng/ee-kb-tools",
            "X-Title": "ee-kb-tools",
        }
        return OpenAIChatProvider(
            api_key=key,
            model=model or "openai/gpt-4o-mini",
            base_url="https://openrouter.ai/api/v1",
            name="openrouter",
            extra_headers=extra_headers,
        )
    raise SummarizerError(
        f"unknown summarizer provider: {provider!r}. "
        f"supported: gemini | openai | deepseek | openrouter"
    )


# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------


def summarize_paper(
    *,
    provider: LLMProvider,
    fulltext: str,
    title: str,
    authors: str,
    year: int | str,
    doi: str,
    abstract: str,
    max_output_tokens: int = 8000,
) -> SummaryResult:
    """Run the LLM and parse the 7-section JSON response.

    Robustness:
    - Strip common wrappers (```json ... ```) the model might add
      despite our instructions.
    - If the JSON is malformed, retry once with a "your previous
      response wasn't valid JSON, return only the JSON object" nudge.
    - After that, raise SummarizerError — caller decides whether to
      skip this paper or abort the batch.
    """
    user = USER_PROMPT_TMPL.format(
        title=title or "(unknown)",
        authors=authors or "(unknown)",
        year=year or "(unknown)",
        doi=doi or "(none)",
        abstract=(abstract or "(none)")[:1000],
        fulltext=fulltext,
    )

    text, pt, ct = provider.complete(
        SYSTEM_PROMPT, user, max_output_tokens=max_output_tokens,
    )
    sections = _parse_sections(text)

    if sections is None:
        # One retry with an explicit "JSON only" nudge.
        retry_user = (
            user
            + "\n\n(Note: return only the JSON object; no markdown "
            "fences, no prefix, no commentary.)"
        )
        text2, pt2, ct2 = provider.complete(
            SYSTEM_PROMPT, retry_user,
            max_output_tokens=max_output_tokens,
        )
        pt += pt2
        ct += ct2
        sections = _parse_sections(text2)
        if sections is None:
            raise SummarizerError(
                f"LLM returned non-JSON twice; first 200 chars: "
                f"{text2[:200]!r}"
            )

    return SummaryResult(
        sections=sections,
        provider=provider.name,
        model=provider.model,
        prompt_tokens=pt,
        completion_tokens=ct,
    )


def _parse_sections(text: str) -> dict[int, str] | None:
    """Parse the model's text into {1..7: content}. Returns None on
    failure (caller retries or raises).

    Strips ```json ... ``` fences and any leading/trailing whitespace
    before attempting JSON parse.
    """
    t = text.strip()
    # Strip code fences if present.
    if t.startswith("```"):
        # Find first newline after opening fence
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()

    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    out: dict[int, str] = {}
    for i in range(1, 8):
        k = f"section_{i}"
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            out[i] = v.strip()
    # Quality gate: accept a response that has at least 5 of the 7
    # sections populated (≤2 missing is OK and will render as "未生成"
    # in those slots). Reject anything with ≥3 missing as essentially
    # broken output — otherwise the caller would write fulltext_processed
    # = true for a half-finished summary and then --fulltext would skip
    # the paper on subsequent runs, leaving the user stuck until they
    # know to pass --force-fulltext. Previous threshold ("just 1+2")
    # was too permissive and let section_1+2-only responses through.
    if len(out) >= 5:
        return out
    return None
