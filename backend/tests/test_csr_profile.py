"""The CSR-ideal profile picker answers unkeyed personality/SJT items toward the hireable
customer-service profile (offer-optimal), and never touches cognitive/ability items."""
from backend.tools.assessment_harvester.core import csr_pick


def test_positive_statement_agreement_scale_picks_strongly_agree():
    opts = ["Strongly Disagree", "Disagree", "Neither", "Agree", "Strongly Agree"]
    # a positive work statement -> the most favourable end
    assert csr_pick("personality", "I always follow through and stay calm with customers", opts) == 4


def test_negative_statement_agreement_scale_picks_strongly_disagree():
    opts = ["Strongly Disagree", "Disagree", "Neither", "Agree", "Strongly Agree"]
    # a negative statement -> the least favourable end (disagree with the bad trait)
    assert csr_pick("personality", "I lose my temper and argue with rude customers", opts) == 0


def test_frequency_scale_positive_statement():
    opts = ["Never", "Rarely", "Sometimes", "Often", "Always"]
    assert csr_pick("personality", "I help customers and listen carefully", opts) == 4


def test_forced_choice_statements_picks_csr_positive():
    opts = ["I get bored with repetitive work", "I am reliable and enjoy helping people",
            "I prefer to work alone and avoid teams"]
    assert csr_pick("personality", "Which statement describes you best?", opts) == 1


def test_sjt_picks_helpful_option():
    opts = ["Ignore the customer and move on", "Argue that they are wrong",
            "Listen, apologize, and help the customer resolve the issue"]
    assert csr_pick("sjt", "What would you do?", opts) == 2


def test_cognitive_item_not_touched():
    # a numerical/ability item type is never CSR-picked (must stay factually correct)
    assert csr_pick("numerical", "What is 2+2?", ["3", "4", "5"]) is None
    assert csr_pick("verbal", "True or false?", ["True", "False"]) is None


def test_neutral_options_no_confident_pick():
    # no scale + no net-positive statement -> None (fall back to the model), never a random click
    assert csr_pick("personality", "Pick one", ["Option A", "Option B"]) is None
