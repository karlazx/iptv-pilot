import pytest

from scripts.diagnostics.evaluate_asr import edit_distance, normalize


@pytest.mark.parametrize("reference,hypothesis,errors", [
    ("abc", "abc", 0), ("abc", "adc", 1), ("abc", "ac", 1),
    ("abc", "abdc", 1), ("", "abc", 3), ("abc", "", 3),
])
def test_character_errors_include_substitution_deletion_and_insertion(
    reference, hypothesis, errors
):
    assert edit_distance(reference, hypothesis) == errors


def test_format_normalization_keeps_content_errors():
    assert normalize("ＡＢＣ，１２３。") == "abc123"
    assert normalize("上海 19:00") == "上海1900"
    assert edit_distance(normalize("收入一百元"), normalize("收入一千元")) == 1
