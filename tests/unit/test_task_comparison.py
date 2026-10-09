from officeflow.application.task_comparison import compare_text


def test_literal_words_lines_and_emoji_not_numeric_interpretation():
    before = "업무 🙂\n예산 100만원\n이전\n"
    after = "업무 🙂\n예산 100(천원)\n다음\n"
    diff = compare_text(before, after)
    assert not diff.fallback
    assert "만원" in "".join(before[start:end] for start, end in diff.left)
    assert "(천원)" in "".join(after[start:end] for start, end in diff.right)
    assert all("🙂" not in before[start:end] for start, end in diff.left)


def test_repeated_line_rewrite_budget_and_oversized_fallback():
    assert compare_text("a\n" * 5_000, "b\n" * 5_000, budget=0).fallback
    assert compare_text("a" * 100_001, "").fallback
    assert compare_text("", "").left == ()
    assert compare_text("ab", "ab").right == ()
    assert compare_text("abc\n", "abc\nnew\n").right
