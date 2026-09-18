"""Pure-function tests for worddiff_v1. No database or Flask client."""

from __future__ import annotations

import unicodedata

from annotation_quality.comparison import (
    ALIGNMENT_PRIORITY,
    COMPARISON_VERSION,
    MAX_COMPARISON_CELLS,
    compare_transcripts,
)


def seg(segment_id, start, end, text, exclude=False, **extra):
    payload = {
        "id": segment_id,
        "start": start,
        "end": end,
        "text": text,
        "exclude_from_training": exclude,
    }
    payload.update(extra)
    return payload


def compare(original_text, secondary_text, **kwargs):
    return compare_transcripts(
        [seg(1, 0.0, 1.0, original_text)],
        [seg(1, 0.0, 1.0, secondary_text)],
        **kwargs,
    )


def words(n, prefix="w"):
    return " ".join(f"{prefix}{i}" for i in range(n))


def test_identical_texts_have_zero_distance_and_do_not_need_review():
    result = compare("hello world", "hello world")
    assert result.comparison_version == COMPARISON_VERSION
    assert result.edit_distance == 0
    assert result.n_original == 2
    assert result.n_secondary == 2
    assert result.word_difference_rate == 0
    assert result.substitutions == result.insertions == result.deletions == 0
    assert result.needs_word_review is False
    assert result.needs_review is False
    assert result.reason_codes == ()
    assert [op.op for op in result.ops] == ["match", "match"]


def test_single_word_replace_insert_and_delete():
    replaced = compare("alpha bravo charlie", "alpha xray charlie")
    assert replaced.edit_distance == 1
    assert replaced.substitutions == 1
    assert [op.op for op in replaced.ops] == ["match", "replace", "match"]
    assert replaced.ops[1].original_word == "bravo"
    assert replaced.ops[1].secondary_word == "xray"

    inserted = compare("alpha charlie", "alpha bravo charlie")
    assert inserted.edit_distance == 1
    assert inserted.insertions == 1
    assert [op.op for op in inserted.ops] == ["match", "insert", "match"]
    assert inserted.ops[1].original_word is None
    assert inserted.ops[1].secondary_word == "bravo"
    assert inserted.ops[1].original is None

    deleted = compare("alpha bravo charlie", "alpha charlie")
    assert deleted.edit_distance == 1
    assert deleted.deletions == 1
    assert [op.op for op in deleted.ops] == ["match", "delete", "match"]
    assert deleted.ops[1].secondary_word is None
    assert deleted.ops[1].secondary is None


def test_repeated_words_and_stable_equal_cost_priority():
    repeated = compare("the the the", "the the")
    assert repeated.edit_distance == 1
    assert repeated.deletions == 1
    # match > delete walks from the end, so the leading extra word is deleted.
    assert [op.op for op in repeated.ops] == ["delete", "match", "match"]
    assert repeated.ops[0].original_word == "the"

    swapped = compare("x y", "y x")
    assert swapped.edit_distance == 2
    assert swapped.substitutions == 2
    assert [op.op for op in swapped.ops] == ["replace", "replace"]
    assert ALIGNMENT_PRIORITY == ("match", "replace", "delete", "insert")

    duplicate = compare("a a", "a")
    assert [op.op for op in duplicate.ops] == ["delete", "match"]

    first = compare("x y", "y x")
    second = compare("x y", "y x")
    assert first.ops == second.ops
    assert [(op.op, op.original_word, op.secondary_word) for op in first.ops] == [
        ("replace", "x", "y"),
        ("replace", "y", "x"),
    ]


def test_rate_zero_exactly_ten_percent_and_slightly_over():
    zero = compare(words(100), words(100))
    assert zero.word_difference_rate == 0
    assert zero.needs_word_review is False
    assert "word_difference_exceeded" not in zero.reason_codes

    ten = compare(words(100), " ".join(
        [f"x{i}" if i < 10 else f"w{i}" for i in range(100)]
    ))
    assert ten.edit_distance == 10
    assert ten.n_original == ten.n_secondary == 100
    assert ten.word_difference_rate == 10 / 100
    assert ten.edit_distance * 10000 == 100 * 1000
    assert ten.needs_word_review is False
    assert ten.needs_review is False

    eleven = compare(words(100), " ".join(
        [f"x{i}" if i < 11 else f"w{i}" for i in range(100)]
    ))
    assert eleven.edit_distance == 11
    assert eleven.word_difference_rate == 11 / 100
    assert eleven.edit_distance * 10000 > 100 * 1000
    assert eleven.needs_word_review is True
    assert eleven.reason_codes == ("word_difference_exceeded",)
    assert eleven.needs_review is True


def test_integer_threshold_matches_unrounded_rate_examples():
    ten_words = compare(words(10), "x0 " + " ".join(f"w{i}" for i in range(1, 10)))
    assert ten_words.edit_distance == 1
    assert ten_words.n_original == 10
    assert ten_words.needs_word_review is False
    assert "word_difference_exceeded" not in ten_words.reason_codes

    nine_words = compare(words(9), "x0 " + " ".join(f"w{i}" for i in range(1, 9)))
    assert nine_words.edit_distance == 1
    assert nine_words.n_original == 9
    # 1/9 ≈ 0.111... must not be rounded down to 10% before the compare.
    assert nine_words.word_difference_rate == 1 / 9
    assert nine_words.edit_distance * 10000 > 9 * 1000
    assert nine_words.needs_word_review is True
    assert nine_words.reason_codes == ("word_difference_exceeded",)


def test_case_punctuation_apostrophe_hyphen_whitespace_and_combining():
    assert compare("Hello!", "hello").edit_distance == 0
    assert compare("HELLO WORLD", "hello world").edit_distance == 0
    assert compare("hello, world.", "hello world").edit_distance == 0
    assert compare("hello?", "hello").edit_distance == 0
    assert compare("مرحبا!", "مرحبا").edit_distance == 0
    assert compare("مرحبا، العالم", "مرحبا العالم").edit_distance == 0
    assert compare("hello؟", "hello").edit_distance == 0

    apostrophe = compare("can't", "cant")
    assert apostrophe.edit_distance == 0
    assert apostrophe.n_original == apostrophe.n_secondary == 1
    curly = compare("it\u2019s fine", "its fine")
    assert curly.edit_distance == 0
    assert compare("well-known", "wellknown").edit_distance == 0
    assert compare("well\u2010known", "wellknown").edit_distance == 0

    # Apostrophe deletion must not split the word.
    split_wrong = compare("can't", "can t")
    assert split_wrong.edit_distance != 0

    assert compare("hello\n\nworld", "hello world").edit_distance == 0
    assert compare("hello    world", "hello world").edit_distance == 0
    assert compare("  hello world  ", "hello world").edit_distance == 0
    assert compare("hello\tworld", "hello world").edit_distance == 0

    composed = "café"
    decomposed = "cafe\u0301"
    assert unicodedata.normalize("NFC", decomposed) == composed
    assert compare(composed, decomposed).edit_distance == 0
    assert compare("café", "cafe").edit_distance == 1


def test_digits_senses_and_arabic_letters_are_not_over_normalized():
    assert compare("2", "two").edit_distance == 1
    assert compare("123", "١٢٣").edit_distance == 1
    assert compare("big", "large").edit_distance == 1
    assert compare("running", "run").edit_distance == 1
    assert compare("كتب", "كتاب").edit_distance == 1
    assert compare("ا", "أ").edit_distance == 1
    assert compare("ا", "إ").edit_distance == 1
    assert compare("ة", "ه").edit_distance == 1
    assert compare("ى", "ي").edit_distance == 1
    # Arabic diacritics are kept; combining fatha is not stripped.
    assert compare("ب", "بَ").edit_distance == 1
    assert compare("بَ", "بَ").edit_distance == 0


def test_different_segmentation_same_text_keeps_per_side_mappings():
    original = [seg(1, 0.0, 2.0, "hello world")]
    secondary = [seg(10, 0.0, 1.0, "hello"), seg(20, 1.0, 2.0, "world")]
    result = compare_transcripts(original, secondary)
    assert result.edit_distance == 0
    assert result.word_difference_rate == 0
    assert result.n_original == result.n_secondary == 2
    assert result.ops[0].original.segment_id == 1
    assert result.ops[0].secondary.segment_id == 10
    assert result.ops[1].original.segment_id == 1
    assert result.ops[1].secondary.segment_id == 20
    assert result.ops[0].original.start_s == 0.0
    assert result.ops[0].original.end_s == 2.0
    assert result.ops[0].secondary.start_s == 0.0
    assert result.ops[0].secondary.end_s == 1.0
    assert result.ops[1].secondary.start_s == 1.0
    assert result.ops[1].secondary.end_s == 2.0
    nfc = unicodedata.normalize("NFC", "hello world")
    assert nfc[result.ops[0].original.text_start:result.ops[0].original.text_end] == "hello"
    assert nfc[result.ops[1].original.text_start:result.ops[1].original.text_end] == "world"

    unordered = [seg(2, 1.0, 2.0, "world"), seg(1, 0.0, 1.0, "hello")]
    resorted = compare_transcripts(unordered, [seg(9, 0.0, 2.0, "hello world")])
    assert resorted.edit_distance == 0
    assert resorted.original_normalized == "hello world"


def test_field_aliases_and_inputs_are_not_mutated():
    original_text = "Hello!"
    original = {
        "segment_id": 7,
        "start_s": 0.25,
        "end_s": 1.5,
        "text": original_text,
        "exclude_from_training": False,
        "asr_text": "WRONG ASR",
    }
    secondary = [seg(1, 0.0, 1.0, "hello")]
    result = compare_transcripts([original], secondary)
    assert result.edit_distance == 0
    assert result.ops[0].original.segment_id == 7
    assert result.ops[0].original.start_s == 0.25
    assert result.ops[0].original.end_s == 1.5
    assert original["text"] is original_text
    assert original["text"] == "Hello!"
    assert original["asr_text"] == "WRONG ASR"


def test_one_side_empty_both_empty_and_all_bad_quality():
    original_empty = compare("", "hello world")
    assert original_empty.n_original == 0
    assert original_empty.n_secondary == 2
    assert original_empty.edit_distance == 2
    assert original_empty.word_difference_rate is None
    assert original_empty.needs_word_review is False
    assert original_empty.needs_review is True
    assert original_empty.reason_codes == ("empty_original_text",)
    assert original_empty.deletions == 0
    assert original_empty.insertions == 2

    secondary_empty = compare("hello world", "   ")
    assert secondary_empty.n_secondary == 0
    assert secondary_empty.edit_distance == 2
    assert secondary_empty.word_difference_rate is None
    assert secondary_empty.needs_review is True
    assert secondary_empty.reason_codes == ("empty_secondary_text",)
    assert secondary_empty.deletions == 2

    both_empty = compare("", "")
    assert both_empty.n_original == both_empty.n_secondary == 0
    assert both_empty.edit_distance == 0
    assert both_empty.word_difference_rate is None
    assert both_empty.needs_review is True
    assert both_empty.reason_codes == ("empty_original_text", "empty_secondary_text")

    all_bq = compare_transcripts(
        [seg(1, 0.0, 2.0, "hello world", exclude=True)],
        [seg(1, 0.0, 1.0, "noise", exclude=True), seg(2, 1.0, 2.0, "more", exclude=True)],
    )
    assert all_bq.n_original == all_bq.n_secondary == 0
    assert all_bq.word_difference_rate is None
    assert "empty_original_text" in all_bq.reason_codes
    assert "empty_secondary_text" in all_bq.reason_codes
    assert "bad_quality_conflict" not in all_bq.reason_codes
    assert all_bq.needs_review is True


def test_secondary_skipped_is_submission_status_conflict():
    same_text = compare(
        "hello world",
        "hello world",
        original_status="annotated",
        secondary_status="skipped",
    )
    assert same_text.edit_distance == 0
    assert same_text.needs_word_review is False
    assert same_text.needs_review is True
    assert same_text.reason_codes == ("submission_status_conflict",)

    skipped_empty = compare(
        "hello",
        "",
        original_status="annotated",
        secondary_status="skipped",
    )
    assert skipped_empty.reason_codes == (
        "submission_status_conflict",
        "empty_secondary_text",
    )
    assert skipped_empty.needs_review is True
    assert skipped_empty.word_difference_rate is None


def test_bad_quality_same_coverage_different_cuts_is_not_conflict():
    original = [seg(1, 0.0, 2.0, "noise", exclude=True), seg(2, 2.0, 4.0, "hello")]
    secondary = [
        seg(1, 0.0, 1.0, "a", exclude=True),
        seg(2, 1.0, 2.0, "b", exclude=True),
        seg(3, 2.0, 4.0, "hello"),
    ]
    result = compare_transcripts(original, secondary)
    assert result.edit_distance == 0
    assert result.reason_codes == ()
    assert result.needs_review is False
    assert result.original_bad_quality == result.secondary_bad_quality
    assert result.original_bad_quality[0].start_ms == 0
    assert result.original_bad_quality[0].end_ms == 2000


def test_ordinary_cut_points_without_bad_quality_are_not_conflicts():
    original = [seg(1, 0.0, 2.0, "hello world")]
    secondary = [seg(1, 0.0, 1.0, "hello"), seg(2, 1.0, 2.0, "world")]
    result = compare_transcripts(original, secondary)
    assert result.original_bad_quality == ()
    assert result.secondary_bad_quality == ()
    assert "bad_quality_conflict" not in result.reason_codes
    assert result.needs_review is False


def test_different_excluded_ranges_queue_bad_quality_conflict():
    original = [seg(1, 0.0, 2.0, "noise", exclude=True), seg(2, 2.0, 4.0, "hello")]
    secondary = [seg(1, 0.0, 1.0, "noise", exclude=True), seg(2, 1.0, 4.0, "hello")]
    result = compare_transcripts(original, secondary)
    assert result.needs_review is True
    assert "bad_quality_conflict" in result.reason_codes
    assert result.edit_distance == 0


def test_bad_quality_one_millisecond_endpoint_tolerance():
    original = [seg(1, 0.0, 1.0, "x", exclude=True), seg(2, 1.0, 2.0, "hello")]
    within = [seg(1, 0.0, 1.001, "x", exclude=True), seg(2, 1.001, 2.0, "hello")]
    ok = compare_transcripts(original, within)
    assert "bad_quality_conflict" not in ok.reason_codes
    assert ok.edit_distance == 0

    outside = [seg(1, 0.0, 1.003, "x", exclude=True), seg(2, 1.003, 2.0, "hello")]
    conflict = compare_transcripts(original, outside)
    assert "bad_quality_conflict" in conflict.reason_codes


def test_over_budget_does_not_invent_zero_distance(monkeypatch):
    assert MAX_COMPARISON_CELLS == 10_000_000
    monkeypatch.setattr(
        "annotation_quality.comparison.MAX_COMPARISON_CELLS",
        4,
    )
    result = compare(words(3), words(2, prefix="z"))
    assert result.n_original == 3
    assert result.n_secondary == 2
    assert result.n_original * result.n_secondary > 4
    assert result.edit_distance is None
    assert result.word_difference_rate is None
    assert result.substitutions is None
    assert result.insertions is None
    assert result.deletions is None
    assert result.ops == ()
    assert result.needs_review is True
    assert result.needs_word_review is False
    assert result.reason_codes == ("comparison_unavailable",)
    assert result.comparison_unavailable == "input_too_large"
    assert result.comparison_unavailable_detail["subreason"] == "input_too_large"
    assert result.comparison_unavailable_detail["cells"] == 6
    assert result.comparison_unavailable_detail["max_cells"] == 4


def test_cell_budget_boundary_still_returns_exact_distance(monkeypatch):
    monkeypatch.setattr(
        "annotation_quality.comparison.MAX_COMPARISON_CELLS",
        4,
    )
    result = compare("a b", "a b")
    assert result.n_original * result.n_secondary == 4
    assert result.edit_distance == 0
    assert result.comparison_unavailable is None
    assert result.needs_review is False


def test_punctuation_span_excludes_terminal_mark_and_keeps_joiner():
    hello = compare("Hello!", "hello")
    assert hello.ops[0].original.text_start == 0
    assert hello.ops[0].original.text_end == 5
    cant = compare("can't", "cant")
    assert cant.n_original == 1
    assert cant.ops[0].original.text_start == 0
    assert cant.ops[0].original.text_end == 5


def test_usable_punctuation_only_segment_counts_as_empty_text():
    result = compare("!!!", "hello")
    assert result.n_original == 0
    assert "empty_original_text" in result.reason_codes
    assert result.needs_review is True
    assert result.word_difference_rate is None


def test_comparison_kernel_source_stays_pure():
    from pathlib import Path

    source = Path("annotation_quality/comparison.py").read_text(encoding="utf-8")
    assert "difflib" not in source
    assert "flask" not in source.lower()
    assert "psycopg" not in source
    assert "annotation_repository" not in source
