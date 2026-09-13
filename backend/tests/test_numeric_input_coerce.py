"""A prose draft typed into a NUMERIC input is rejected by the browser and the box stays empty
(Ashby: 'Missing entry for required field: How much is your expected salary?'). coerce_for_input
keeps just the number for type=number / inputmode=numeric and leaves other inputs untouched."""
from backend.applier.filler import coerce_for_input


def test_prose_salary_into_number_input_becomes_digits():
    assert coerce_for_input("PHP 80,000 per month", "number", "") == "80000"
    assert coerce_for_input("$95,000 - $110,000 per year", "number", "") == "95000"
    assert coerce_for_input("around 1,200 USD", "", "numeric") == "1200"


def test_decimal_kept_when_allowed():
    assert coerce_for_input("3.5 years", "number", "") == "3.5"
    assert coerce_for_input("3.5", "", "decimal") == "3.5"
    assert coerce_for_input("3.5", "", "numeric") == "3"


def test_text_inputs_unchanged():
    assert coerce_for_input("PHP 80,000 per month", "text", "") == "PHP 80,000 per month"
    assert coerce_for_input("PHP 80,000 per month", "", "") == "PHP 80,000 per month"
    assert coerce_for_input("Immediately", "number", "") == "Immediately"   # no number -> untouched
