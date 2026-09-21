"""Deterministic worddiff_v1 comparison kernel.

Pure functions only: no I/O, database, or web framework imports.

Normalization (in order): Unicode NFC, casefold, then punctuation/whitespace.
Apostrophes and hyphens that join letters inside a word are deleted
(``can't`` → ``cant``, letter-hyphen-letter). Every other Unicode punctuation
character becomes a space; whitespace collapses.

Equal-cost Levenshtein paths are resolved with a fixed backward walk order:
match > replace > delete > insert. That prefers a diagonal match/replace
when it is as cheap as a vertical delete or horizontal insert.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence
import unicodedata

COMPARISON_VERSION = "worddiff_v1"
MAX_COMPARISON_CELLS = 10_000_000
DEFAULT_THRESHOLD_BPS = 1000

REASON_WORD_DIFFERENCE_EXCEEDED = "word_difference_exceeded"
REASON_SUBMISSION_STATUS_CONFLICT = "submission_status_conflict"
REASON_EMPTY_ORIGINAL_TEXT = "empty_original_text"
REASON_EMPTY_SECONDARY_TEXT = "empty_secondary_text"
REASON_BAD_QUALITY_CONFLICT = "bad_quality_conflict"
REASON_COMPARISON_UNAVAILABLE = "comparison_unavailable"

UNAVAILABLE_INPUT_TOO_LARGE = "input_too_large"
UNAVAILABLE_COMPUTE_FAILURE = "compute_failure"

_REASON_ORDER = (
    REASON_WORD_DIFFERENCE_EXCEEDED,
    REASON_SUBMISSION_STATUS_CONFLICT,
    REASON_EMPTY_ORIGINAL_TEXT,
    REASON_EMPTY_SECONDARY_TEXT,
    REASON_BAD_QUALITY_CONFLICT,
    REASON_COMPARISON_UNAVAILABLE,
)

# Joiners deleted only between letters; otherwise they become word boundaries.
_WORD_JOINERS = frozenset({
    "'",       # U+0027 APOSTROPHE
    "\u2018",  # LEFT SINGLE QUOTATION MARK
    "\u2019",  # RIGHT SINGLE QUOTATION MARK
    "\u02BC",  # MODIFIER LETTER APOSTROPHE (Lm, so it is listed explicitly)
    "\uFF07",  # FULLWIDTH APOSTROPHE
    "-",       # U+002D HYPHEN-MINUS
    "\u00AD",  # SOFT HYPHEN
    "\u2010",  # HYPHEN
    "\u2011",  # NON-BREAKING HYPHEN
    "\uFF0D",  # FULLWIDTH HYPHEN-MINUS
})

_BQ_ENDPOINT_TOLERANCE_MS = 1


@dataclass(frozen=True)
class TimeInterval:
    """Merged Bad Quality coverage after quantizing to integer milliseconds."""

    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class WordMapping:
    """Owning segment, raw-text code point span, and that segment's times."""

    segment_id: Any
    text_start: int
    text_end: int
    start_s: float
    end_s: float


@dataclass(frozen=True)
class EditOp:
    """One unit-cost word operation, including match, with per-side mappings."""

    op: str
    original_word: str | None
    secondary_word: str | None
    original: WordMapping | None
    secondary: WordMapping | None


@dataclass(frozen=True)
class ComparisonResult:
    comparison_version: str
    threshold_bps: int
    n_original: int | None
    n_secondary: int | None
    edit_distance: int | None
    substitutions: int | None
    insertions: int | None
    deletions: int | None
    word_difference_rate: float | None
    needs_word_review: bool
    needs_review: bool
    reason_codes: tuple[str, ...]
    ops: tuple[EditOp, ...]
    original_bad_quality: tuple[TimeInterval, ...]
    secondary_bad_quality: tuple[TimeInterval, ...]
    original_normalized: str | None
    secondary_normalized: str | None
    comparison_unavailable: str | None = None


@dataclass(frozen=True)
class _Segment:
    segment_id: Any
    start_s: float
    end_s: float
    text: str
    exclude_from_training: bool


@dataclass(frozen=True)
class _Token:
    word: str
    mapping: WordMapping


def compare_transcripts(
    original_segments: Sequence[Mapping[str, Any]],
    secondary_segments: Sequence[Mapping[str, Any]],
    *,
    original_status: str = "annotated",
    secondary_status: str = "annotated",
    threshold_bps: int = DEFAULT_THRESHOLD_BPS,
) -> ComparisonResult:
    """Compare two transcripts with worddiff_v1.

    Segment mappings are read-only. Distance is extra evidence and does not
    rewrite caller strings.
    """
    threshold = int(threshold_bps)
    original_parsed = _parse_segments(original_segments)
    secondary_parsed = _parse_segments(secondary_segments)

    original_bq = _bad_quality_union(original_parsed)
    secondary_bq = _bad_quality_union(secondary_parsed)
    original_tokens = _tokenize_transcript(original_parsed)
    secondary_tokens = _tokenize_transcript(secondary_parsed)
    n_original = len(original_tokens)
    n_secondary = len(secondary_tokens)
    original_normalized = " ".join(token.word for token in original_tokens)
    secondary_normalized = " ".join(token.word for token in secondary_tokens)

    reasons: set[str] = set()
    if (
        str(original_status).casefold() == "annotated"
        and str(secondary_status).casefold() == "skipped"
    ):
        reasons.add(REASON_SUBMISSION_STATUS_CONFLICT)
    if n_original == 0:
        reasons.add(REASON_EMPTY_ORIGINAL_TEXT)
    if n_secondary == 0:
        reasons.add(REASON_EMPTY_SECONDARY_TEXT)
    if _bad_quality_conflict(original_bq, secondary_bq):
        reasons.add(REASON_BAD_QUALITY_CONFLICT)

    edit_distance: int | None = None
    substitutions: int | None = None
    insertions: int | None = None
    deletions: int | None = None
    ops: tuple[EditOp, ...] = ()
    unavailable: str | None = None
    cells = n_original * n_secondary

    if cells > MAX_COMPARISON_CELLS:
        reasons.add(REASON_COMPARISON_UNAVAILABLE)
        unavailable = UNAVAILABLE_INPUT_TOO_LARGE
    else:
        try:
            edit_distance, ops, substitutions, insertions, deletions = (
                _word_alignment(original_tokens, secondary_tokens)
            )
        except MemoryError:
            reasons.add(REASON_COMPARISON_UNAVAILABLE)
            unavailable = UNAVAILABLE_COMPUTE_FAILURE
            edit_distance = None
            substitutions = None
            insertions = None
            deletions = None
            ops = ()

    needs_word_review = False
    word_difference_rate: float | None = None
    if (
        edit_distance is not None
        and n_original > 0
        and n_secondary > 0
    ):
        denom = max(n_original, n_secondary)
        word_difference_rate = edit_distance / denom
        # Integer compare: D/N > T/10000 without rounding the ratio first.
        needs_word_review = edit_distance * 10000 > denom * threshold
        if needs_word_review:
            reasons.add(REASON_WORD_DIFFERENCE_EXCEEDED)

    reason_codes = tuple(code for code in _REASON_ORDER if code in reasons)
    return ComparisonResult(
        comparison_version=COMPARISON_VERSION,
        threshold_bps=threshold,
        n_original=n_original,
        n_secondary=n_secondary,
        edit_distance=edit_distance,
        substitutions=substitutions,
        insertions=insertions,
        deletions=deletions,
        word_difference_rate=word_difference_rate,
        needs_word_review=needs_word_review,
        needs_review=bool(reason_codes),
        reason_codes=reason_codes,
        ops=ops,
        original_bad_quality=original_bq,
        secondary_bad_quality=secondary_bq,
        original_normalized=original_normalized,
        secondary_normalized=secondary_normalized,
        comparison_unavailable=unavailable,
    )


def _parse_segments(segments: Sequence[Mapping[str, Any]]) -> list[_Segment]:
    parsed: list[_Segment] = []
    for raw in segments:
        segment_id = _first_present(raw, ("segment_id", "id"))
        if segment_id is None:
            raise ValueError("segment missing id/segment_id")
        start = _first_present(raw, ("start_s", "start"))
        end = _first_present(raw, ("end_s", "end"))
        if start is None or end is None:
            raise ValueError(f"segment {segment_id!r}: missing start/end")
        text = raw.get("text", "")
        if text is None:
            text = ""
        elif not isinstance(text, str):
            raise TypeError(f"segment {segment_id!r}: text must be a string")
        parsed.append(
            _Segment(
                segment_id=segment_id,
                start_s=float(start),
                end_s=float(end),
                text=text,
                exclude_from_training=bool(raw.get("exclude_from_training", False)),
            )
        )
    parsed.sort(key=lambda s: (s.start_s, s.end_s, s.segment_id))
    return parsed


def _first_present(raw: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def _tokenize_transcript(segments: Sequence[_Segment]) -> list[_Token]:
    tokens: list[_Token] = []
    for segment in segments:
        if segment.exclude_from_training:
            continue
        tokens.extend(_tokenize_segment(segment))
    return tokens


def _nfd_with_sources(text: str) -> Iterator[tuple[str, int]]:
    """Canonical decomposition/order while retaining each raw character index."""
    marks: list[tuple[str, int]] = []
    for index, char in enumerate(text):
        for decomposed in unicodedata.normalize("NFD", char):
            if unicodedata.combining(decomposed):
                marks.append((decomposed, index))
            else:
                yield from sorted(marks, key=lambda item: unicodedata.combining(item[0]))
                marks.clear()
                yield decomposed, index
    yield from sorted(marks, key=lambda item: unicodedata.combining(item[0]))


def _nfc_with_raw_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    nfc = unicodedata.normalize("NFC", text)
    if nfc == text:
        return nfc, [(index, index + 1) for index in range(len(text))]

    # Both strings have the same ordered NFD stream. Pair its characters to
    # carry raw spans through composition, expansion, and combining-mark
    # reordering, including Hangul (whose Jamo have combining class zero).
    starts = [len(text)] * len(nfc)
    ends = [0] * len(nfc)
    for (_, raw_index), (_, nfc_index) in zip(
        _nfd_with_sources(text), _nfd_with_sources(nfc), strict=True,
    ):
        starts[nfc_index] = min(starts[nfc_index], raw_index)
        ends[nfc_index] = max(ends[nfc_index], raw_index + 1)
    return nfc, list(zip(starts, ends, strict=True))


def _tokenize_segment(segment: _Segment) -> list[_Token]:
    nfc, raw_spans = _nfc_with_raw_spans(segment.text)
    folded: list[tuple[str, int, int]] = []
    for char, (raw_start, raw_end) in zip(nfc, raw_spans, strict=True):
        for folded_char in char.casefold():
            folded.append((folded_char, raw_start, raw_end))

    tokens: list[_Token] = []
    buf: list[str] = []
    span_start: int | None = None
    span_end: int | None = None

    def flush() -> None:
        nonlocal buf, span_start, span_end
        if not buf:
            return
        assert span_start is not None and span_end is not None
        tokens.append(
            _Token(
                word="".join(buf),
                mapping=WordMapping(
                    segment_id=segment.segment_id,
                    text_start=span_start,
                    text_end=span_end,
                    start_s=segment.start_s,
                    end_s=segment.end_s,
                ),
            )
        )
        buf = []
        span_start = None
        span_end = None

    length = len(folded)
    index = 0
    while index < length:
        char, raw_start, raw_end = folded[index]
        if (
            char in _WORD_JOINERS
            and buf
            and buf[-1].isalpha()
            and _next_is_letter(folded, index + 1)
        ):
            span_end = max(span_end or 0, raw_end)
            index += 1
            continue
        if char in _WORD_JOINERS or _is_unicode_punctuation(char) or char.isspace():
            flush()
            index += 1
            continue
        span_start = raw_start if span_start is None else min(span_start, raw_start)
        span_end = max(span_end or 0, raw_end)
        buf.append(char)
        index += 1
    flush()
    return tokens


def _next_is_letter(folded: Sequence[tuple[str, int, int]], index: int) -> bool:
    return index < len(folded) and folded[index][0].isalpha()


def _is_unicode_punctuation(char: str) -> bool:
    return unicodedata.category(char).startswith("P")


def _to_ms(seconds: float) -> int:
    return int(round(float(seconds) * 1000.0))


def _bad_quality_union(segments: Sequence[_Segment]) -> tuple[TimeInterval, ...]:
    raw: list[tuple[int, int]] = []
    for segment in segments:
        if not segment.exclude_from_training:
            continue
        start_ms = _to_ms(segment.start_s)
        end_ms = _to_ms(segment.end_s)
        if end_ms > start_ms:
            raw.append((start_ms, end_ms))
    if not raw:
        return ()
    raw.sort()
    merged = [raw[0]]
    for start_ms, end_ms in raw[1:]:
        prev_start, prev_end = merged[-1]
        if start_ms <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end_ms))
        else:
            merged.append((start_ms, end_ms))
    return tuple(TimeInterval(start_ms=start, end_ms=end) for start, end in merged)


def _bad_quality_conflict(
    original: tuple[TimeInterval, ...],
    secondary: tuple[TimeInterval, ...],
) -> bool:
    if len(original) != len(secondary):
        return True
    for left, right in zip(original, secondary):
        if (
            abs(left.start_ms - right.start_ms) > _BQ_ENDPOINT_TOLERANCE_MS
            or abs(left.end_ms - right.end_ms) > _BQ_ENDPOINT_TOLERANCE_MS
        ):
            return True
    return False


def _word_alignment(
    original: Sequence[_Token],
    secondary: Sequence[_Token],
) -> tuple[int, tuple[EditOp, ...], int, int, int]:
    n = len(original)
    m = len(secondary)
    cols = m + 1
    dp = array("I", [0]) * ((n + 1) * cols)
    for i in range(1, n + 1):
        dp[i * cols] = i
    for j in range(1, m + 1):
        dp[j] = j
    for i in range(1, n + 1):
        row = i * cols
        prev = (i - 1) * cols
        original_word = original[i - 1].word
        for j in range(1, m + 1):
            diagonal = dp[prev + j - 1]
            cost = diagonal if original_word == secondary[j - 1].word else diagonal + 1
            delete_cost = dp[prev + j] + 1
            insert_cost = dp[row + j - 1] + 1
            if delete_cost < cost:
                cost = delete_cost
            if insert_cost < cost:
                cost = insert_cost
            dp[row + j] = cost

    ops_rev: list[EditOp] = []
    i = n
    j = m
    while i > 0 or j > 0:
        current = dp[i * cols + j]
        original_token = original[i - 1] if i > 0 else None
        secondary_token = secondary[j - 1] if j > 0 else None
        if (
            original_token is not None
            and secondary_token is not None
            and original_token.word == secondary_token.word
            and current == dp[(i - 1) * cols + (j - 1)]
        ):
            ops_rev.append(_edit_op("match", original_token, secondary_token))
            i -= 1
            j -= 1
            continue
        if (
            original_token is not None
            and secondary_token is not None
            and original_token.word != secondary_token.word
            and current == dp[(i - 1) * cols + (j - 1)] + 1
        ):
            ops_rev.append(_edit_op("replace", original_token, secondary_token))
            i -= 1
            j -= 1
            continue
        if original_token is not None and current == dp[(i - 1) * cols + j] + 1:
            ops_rev.append(_edit_op("delete", original_token, None))
            i -= 1
            continue
        if secondary_token is not None and current == dp[i * cols + (j - 1)] + 1:
            ops_rev.append(_edit_op("insert", None, secondary_token))
            j -= 1
            continue
        raise RuntimeError("worddiff_v1 alignment reconstruction failed")

    ops = tuple(reversed(ops_rev))
    substitutions = sum(1 for op in ops if op.op == "replace")
    insertions = sum(1 for op in ops if op.op == "insert")
    deletions = sum(1 for op in ops if op.op == "delete")
    return int(dp[n * cols + m]), ops, substitutions, insertions, deletions


def _edit_op(
    op: str,
    original: _Token | None,
    secondary: _Token | None,
) -> EditOp:
    return EditOp(
        op=op,
        original_word=None if original is None else original.word,
        secondary_word=None if secondary is None else secondary.word,
        original=None if original is None else original.mapping,
        secondary=None if secondary is None else secondary.mapping,
    )
