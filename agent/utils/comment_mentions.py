"Classify line-leading agent mentions outside Markdown code."

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MentionDisposition = Literal[
    "accepted",
    "no_mention",
    "mid_line",
    "inline_code",
    "fenced_code",
]


@dataclass(frozen=True)
class MentionClassification:
    "Result of classifying agent mentions in comment text."

    disposition: MentionDisposition
    alias: str | None = None
    start: int | None = None
    end: int | None = None


def _mention_pattern(aliases: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    return re.compile(rf"(?:{alternatives})(?![A-Za-z0-9_-])", re.IGNORECASE)


def _is_escaped(text: str, position: int) -> bool:
    backslashes = 0
    position -= 1
    while position >= 0 and text[position] == "\\":
        backslashes += 1
        position -= 1
    return backslashes % 2 == 1


def _fenced_code_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    offset = 0
    fence_start: int | None = None
    fence_marker: str | None = None
    fence_length = 0
    for line_with_ending in text.splitlines(keepends=True):
        line = line_with_ending.rstrip("\r\n")
        fence_match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence_start is None:
            invalid_backtick_info = bool(
                fence_match and fence_match.group(1)[0] == "`" and "`" in line[fence_match.end() :]
            )
            if fence_match and not invalid_backtick_info:
                fence_start = offset
                fence_marker = fence_match.group(1)[0]
                fence_length = len(fence_match.group(1))
        elif (
            fence_match
            and fence_match.group(1)[0] == fence_marker
            and len(fence_match.group(1)) >= fence_length
            and not line[fence_match.end() :].strip()
        ):
            ranges.append((fence_start, offset + len(line_with_ending)))
            fence_start = None
            fence_marker = None
            fence_length = 0
        offset += len(line_with_ending)
    if fence_start is not None:
        ranges.append((fence_start, len(text)))
    return ranges


def _paired_backtick_ranges(text: str, start: int, end: int) -> list[tuple[int, int]]:
    runs = [
        run
        for run in re.finditer(r"`+", text[start:end])
        if not _is_escaped(text, start + run.start())
    ]
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(runs):
        opening = runs[index]
        closing_index = index + 1
        while closing_index < len(runs) and len(runs[closing_index].group()) != len(
            opening.group()
        ):
            closing_index += 1
        if closing_index == len(runs):
            index += 1
            continue
        ranges.append((start + opening.start(), start + runs[closing_index].end()))
        index = closing_index + 1
    return ranges


def _inline_code_ranges(text: str, fenced_ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    segment_start = 0
    for fence_start, fence_end in fenced_ranges:
        ranges.extend(_paired_backtick_ranges(text, segment_start, fence_start))
        segment_start = fence_end
    ranges.extend(_paired_backtick_ranges(text, segment_start, len(text)))
    return ranges


def _in_ranges(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)


def classify_comment_mention(text: str, aliases: tuple[str, ...]) -> MentionClassification:
    "Classify whether text contains an actionable line-leading mention."
    pattern = _mention_pattern(aliases)
    fenced_ranges = _fenced_code_ranges(text)
    inline_ranges = _inline_code_ranges(text, fenced_ranges)
    found_mid_line = False
    found_inline_code = False
    found_fenced_code = False

    for mention in pattern.finditer(text):
        if _in_ranges(mention.start(), fenced_ranges):
            found_fenced_code = True
        elif _in_ranges(mention.start(), inline_ranges):
            found_inline_code = True
        elif mention.start() == 0 or text[mention.start() - 1] in "\r\n":
            return MentionClassification(
                disposition="accepted",
                alias=mention.group(),
                start=mention.start(),
                end=mention.end(),
            )
        else:
            found_mid_line = True

    if found_mid_line:
        return MentionClassification(disposition="mid_line")
    if found_inline_code:
        return MentionClassification(disposition="inline_code")
    if found_fenced_code:
        return MentionClassification(disposition="fenced_code")
    return MentionClassification(disposition="no_mention")


def extract_adjacent_repo_directive(
    text: str, mention: MentionClassification
) -> dict[str, str] | None:
    "Extract a full repo directive immediately following an accepted mention."
    if mention.disposition != "accepted" or mention.end is None:
        return None
    line_tail = text[mention.end :].splitlines()[0] if text[mention.end :] else ""
    match = re.match(
        r"^[ \t]+repo(?::|[ \t]+)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"
        r"(?=$|[\s.,;:!?—–])",
        line_tail,
        re.IGNORECASE,
    )
    if not match:
        return None
    return {"owner": match.group(1), "name": match.group(2)}
