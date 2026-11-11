"""Counts-only LogQL for count_matches and compare_windows.

These two tools exist to answer "is there a leak, and where" without copying the leak:
security's secret-pattern scans through query_logs return the matching lines, which then
sit in the transcript and in Langfuse. A count query can still return log text, though,
because a LogQL parser or formatting stage copies line content into a label, and
``sum by (<label>)`` hands that label back. Measured against Loki 3.7.8 (2026-11-11):

    sum by (x) (count_over_time({job="probe"} |= "login" | regexp "(?P<x>.*)" [1h]))

returned 100 series whose ``x`` was the whole log line. ``| label_format x=`{{__line__}}```,
``| logfmt`` and ``| pattern`` leak the same way.

So the query is never taken from the caller. It is built here from three parts, each
validated against a grammar that has no room for a pipeline stage:

  selector     a stream selector and nothing after it: ``{label op "value", ...}``
  line_filter  only line filters: ``|=``, ``!=``, ``|~``, ``!~``, each followed by one
               double-quoted or backtick string
  by           label names, comma-separated

Validation is a full match of each part, not a search for forbidden words. A deny-list of
stage names (``| json``, ``| regexp``, ...) would have to be complete and stay complete as
Loki adds stages; a grammar of what is allowed does not.
"""

from __future__ import annotations

import re

from fastmcp.exceptions import ToolError

# A LogQL string literal: double-quoted with Go escapes, or a raw backtick string.
# `\\.` consumes an escaped character whole, so `"a\"` (an escaped closing quote) is
# unterminated, exactly as Loki's lexer reads it. A newline is refused inside a
# double-quoted string, as Go's strconv.Unquote refuses it.
_STRING = r'(?:"(?:[^"\\\n]|\\.)*"|`[^`]*`)'
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"

# Whitespace runs are possessive (`\s*+`, Python 3.11+): adjacent optional runs over the
# same characters made `{a="b"` + 30,000 spaces + `!` take 0.4 s to refuse, quadratic in
# the argument, on the event loop. Possessive runs give nothing back, so a
# refusal is one linear scan. MAX_PART_CHARS bounds the input as well.
_MATCHER = rf"\s*+{_NAME}\s*+(?:=~|!~|!=|=)\s*+{_STRING}\s*+"
SELECTOR_RE = re.compile(rf"\s*+\{{{_MATCHER}(?:,{_MATCHER})*+,?\s*+\}}\s*+")

# Loki also has `|>`/`!>` (pattern filters), `or` between filter values and `ip(...)`.
# None of them extract text, but none is needed to find a leak either, and each widens
# the grammar this guarantee rests on. Add one only with a test that it cannot leak.
_FILTER = rf"\s*+(?:\|=|!=|\|~|!~)\s*+{_STRING}"
LINE_FILTER_RE = re.compile(rf"(?:{_FILTER})*+\s*+")

# Longest selector, line_filter or by accepted. A real one is a few hundred characters.
MAX_PART_CHARS = 4096

_NAME_RE = re.compile(_NAME)

# The longest range a single count may cover: 720 h, the max_query_lookback of the Loki this
# was written against. A range past the server's own lookback would be cut by Loki without
# saying so.
MAX_RANGE_SECONDS = 720 * 3600


def _check_length(name: str, value: str) -> None:
    if len(value) > MAX_PART_CHARS:
        raise ToolError(f"{name} is {len(value)} characters; the limit is {MAX_PART_CHARS}.")


def check_selector(selector: str) -> str:
    """Return ``selector`` stripped if it is a bare stream selector, else ToolError."""
    _check_length("selector", selector)
    if not SELECTOR_RE.fullmatch(selector):
        raise ToolError(
            f"selector must be a stream selector and nothing else, such as "
            f'{{host="web01", job=~"docker.*"}}; got {selector!r}. Put line filters in '
            "line_filter. Parser and formatting stages are not accepted by this tool."
        )
    return selector.strip()


def check_line_filter(line_filter: str) -> str:
    """Return ``line_filter`` stripped if it is only a chain of line filters, else ToolError."""
    _check_length("line_filter", line_filter)
    if not LINE_FILTER_RE.fullmatch(line_filter):
        raise ToolError(
            "line_filter accepts only line filters: |= != |~ !~, each followed by one "
            '"double-quoted" or `backtick` string, e.g. |~ "(?i)password" != "debug". '
            "Parser and formatting stages (| json, | logfmt, | regexp, | pattern, "
            "| label_format, | line_format, | unwrap, ...) are refused: they can copy line "
            f"text into a label, and this tool returns labels. Got {line_filter!r}."
        )
    return line_filter.strip()


def parse_by(by: str) -> list[str]:
    """Return the label names in a comma-separated ``by``, in order, de-duplicated."""
    _check_length("by", by)
    names = [n.strip() for n in by.split(",")] if by.strip() else []
    bad = [n for n in names if not _NAME_RE.fullmatch(n)]
    if bad:
        raise ToolError(f"by must be comma-separated label names; invalid: {bad!r}")
    return list(dict.fromkeys(names))


def range_seconds(start_ns: int, end_ns: int) -> int:
    """The LogQL range, in whole seconds, covering [start, end). Refuses past the lookback."""
    seconds = (end_ns - start_ns) // 1_000_000_000
    if seconds < 1:
        raise ToolError("The time window must be at least one second long.")
    if seconds > MAX_RANGE_SECONDS:
        raise ToolError(
            f"The time window is {seconds / 3600:.0f}h; the longest this tool counts is "
            f"{MAX_RANGE_SECONDS // 3600}h (Loki's max_query_lookback)."
        )
    return seconds


def count_query(selector: str, line_filter: str, by: list[str], seconds: int) -> str:
    """Build the count query. Every input must already have passed the checks above."""
    # Joined, never string-replaced: a filter value can itself hold two spaces.
    expr = f"{selector} {line_filter}" if line_filter else selector
    inner = f"count_over_time({expr} [{seconds}s])"
    grouping = f" by ({', '.join(by)})" if by else ""
    return f"sum{grouping} ({inner})"
