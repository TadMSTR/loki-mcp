"""The counts-only LogQL builder behind count_matches and compare_windows.

The guarantee is "never returns line text". It rests on these grammars, so the tables
below are the contract: every refused row is a way to put a stage into the query that
could copy line content into a label, and every accepted row is something an agent
looking for a leak needs to be able to say.
"""

from __future__ import annotations

import time

import pytest
from fastmcp.exceptions import ToolError

from loki_mcp import _logql

# ── line_filter ───────────────────────────────────────────────────────────────

ACCEPTED_FILTERS = [
    "",
    "   ",
    '|= "error"',
    '!= "debug"',
    '|~ "(?i)(passw|token|secret|bearer)"',  # a | inside a quoted string
    '!~ "^GET /health"',
    "|~ `(?i)authorization: bearer \\S+`",  # backtick string, backslash kept raw
    '|= "a" != "b" |~ "c" !~ "d"',  # a chain
    '|="tight"!="spacing"',
    '|= "two  spaces"',
    '|= "escaped \\" quote"',
    '|= "pipe | json inside a string"',
    '|= "| regexp \\"(?P<x>.*)\\""',  # a stage named inside a string is just text
    '|= ""',
]

REFUSED_FILTERS = [
    # Parser and formatting stages: each can copy line text into a label.
    "| json",
    "| logfmt",
    '| regexp "(?P<x>.*)"',
    '|= "login" | regexp "(?P<x>.*)"',
    '| pattern "<x> <_>"',
    "| label_format x=`{{__line__}}`",
    '| line_format "{{.msg}}"',
    "| unwrap bytes",
    "| unpack",
    "| decolorize",
    "| drop level | keep x",
    '| x="y"',  # label filter
    '| __error__=""',
    # Quoting
    '|= "unterminated',
    '|= "escaped close\\"',
    "|= `unterminated",
    '|= "new\nline"',
    # Not a line filter at all, or a filter shape this tool does not accept
    "error",
    '"error"',
    "|= error",
    '|> "<_> error"',
    '!> "<_> error"',
    '|= "a" or "b"',
    '|= ip("10.0.0.0/8")',
    '|= "a" [5m]',
    '|= "a")',
    '|= "a" } | json',
    '|= "a"\n| json',
]


@pytest.mark.parametrize("line_filter", ACCEPTED_FILTERS)
def test_line_filter_accepted(line_filter: str) -> None:
    assert _logql.check_line_filter(line_filter) == line_filter.strip()


@pytest.mark.parametrize("line_filter", REFUSED_FILTERS)
def test_line_filter_refused(line_filter: str) -> None:
    with pytest.raises(ToolError, match="only line filters"):
        _logql.check_line_filter(line_filter)


# ── selector ──────────────────────────────────────────────────────────────────

ACCEPTED_SELECTORS = [
    '{job="docker"}',
    ' { host = "argus" , job =~ "docker.*" } ',
    '{host!="forge",level!~"debug|info"}',
    '{job="docker",}',
    "{job=`raw`}",
    '{job="has } brace and | pipe"}',
]

REFUSED_SELECTORS = [
    "{}",
    "",
    'job="docker"',
    '{job="docker"} | json',
    '{job="docker"} |= "x"',  # filters belong in line_filter
    '{job="docker"} | regexp "(?P<x>.*)"',
    '{job="docker"}}',
    "{job=docker}",
    '{"job"="docker"}',
    '{job="docker" host="argus"}',
    '{job="docker"',
    '{job="unterminated}',
    'sum(count_over_time({job="docker"}[1h]))',
    '{job="a"} or {job="b"}',
    '{1job="a"}',
]


@pytest.mark.parametrize("selector", ACCEPTED_SELECTORS)
def test_selector_accepted(selector: str) -> None:
    assert _logql.check_selector(selector) == selector.strip()


@pytest.mark.parametrize("selector", REFUSED_SELECTORS)
def test_selector_refused(selector: str) -> None:
    with pytest.raises(ToolError, match="stream selector and nothing else"):
        _logql.check_selector(selector)


# ── by ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("by", "names"),
    [
        ("", []),
        ("  ", []),
        ("host", ["host"]),
        ("host, container", ["host", "container"]),
        ("host,host", ["host"]),
        ("__tenant_id__", ["__tenant_id__"]),
        ("host\n", ["host"]),  # stripped; the name used is "host"
    ],
)
def test_by_accepted(by: str, names: list[str]) -> None:
    assert _logql.parse_by(by) == names


@pytest.mark.parametrize("by", ["host,", "a b", "1x", "x)", "x) (count_over_time", "x\ny"])
def test_by_refused(by: str) -> None:
    with pytest.raises(ToolError, match="label names"):
        _logql.parse_by(by)


# ── range and query ───────────────────────────────────────────────────────────

S = 1_000_000_000


def test_range_seconds() -> None:
    assert _logql.range_seconds(0, 3600 * S) == 3600
    assert _logql.range_seconds(0, 720 * 3600 * S) == 720 * 3600


@pytest.mark.parametrize("end", [0, S - 1])
def test_range_shorter_than_a_second_refused(end: int) -> None:
    with pytest.raises(ToolError, match="at least one second"):
        _logql.range_seconds(0, end)


def test_range_past_lookback_refused() -> None:
    with pytest.raises(ToolError, match="720h"):
        _logql.range_seconds(0, (720 * 3600 + 1) * S)


def test_count_query_shapes() -> None:
    assert _logql.count_query('{job="a"}', "", [], 60) == 'sum (count_over_time({job="a"} [60s]))'
    assert (
        _logql.count_query('{job="a"}', '|= "x  y"', ["host", "job"], 3600)
        == 'sum by (host, job) (count_over_time({job="a"} |= "x  y" [3600s]))'
    )


# ── cost of a refusal ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("check", "value"),
    [
        # The shape that was quadratic before the runs were made possessive: a valid
        # element, a long whitespace run, then a character that can't follow.
        (_logql.check_selector, '{a="b"' + " " * 4000 + "!"),
        (_logql.check_selector, "{" + ('a="b"' + " " * 40 + ",") * 80 + "!"),
        (_logql.check_line_filter, '|= "a"' + " " * 4000 + "!"),
        (_logql.check_line_filter, ('|= "a"' + " " * 40) * 80 + "!"),
    ],
)
def test_refusal_is_linear_at_the_length_limit(check, value: str) -> None:
    assert len(value) <= _logql.MAX_PART_CHARS
    started = time.perf_counter()
    with pytest.raises(ToolError):
        check(value)
    # Measured: 0.39 s for 30,000 characters before the fix, so ~7 ms at this size even
    # when quadratic. Repeat to make the bound meaningful: 200 refusals in under a second.
    for _ in range(199):
        with pytest.raises(ToolError):
            check(value)
    assert time.perf_counter() - started < 1.0


def test_possessive_runs_have_no_quadratic_shape() -> None:
    # Bypass the length cap and time the regex itself at 10x the old measurement's size.
    value = '{a="b"' + " " * 300_000 + "!"
    started = time.perf_counter()
    assert not _logql.SELECTOR_RE.fullmatch(value)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "check", [_logql.check_selector, _logql.check_line_filter, _logql.parse_by]
)
def test_over_length_refused(check) -> None:
    with pytest.raises(ToolError, match="the limit is 4096"):
        check("x" * (_logql.MAX_PART_CHARS + 1))
