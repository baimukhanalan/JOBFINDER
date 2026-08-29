"""Unit tests for the deterministic role-category classifier (no network)."""
from backend.applier.role_category import classify_role


def c(title, dept=""):
    return classify_role(title, dept)[0]


def test_software_engineer():
    assert c("Senior Software Engineer") == "Engineering"


def test_security_engineer_is_engineering():
    assert c("Senior Application Security Engineer") == "Engineering"


def test_account_executive_is_sales():
    assert c("Enterprise Account Executive - West") == "Sales / GTM"


def test_sales_engineer_is_sales_not_engineering():
    assert c("Sales Engineer, Enterprise") == "Sales / GTM"


def test_customer_success_is_support():
    assert c("Customer Success Manager - UAE") == "Customer Support & Success"


def test_customer_service_rep_is_support():
    assert c("Customer Service Representative (Mandarin Required)") == "Customer Support & Success"


def test_ml_engineer_is_engineering():
    # the fleet treats any *engineer* title as Engineering; Data & ML is the
    # scientist/analyst function
    assert c("Staff Machine Learning Engineer, Personalization") == "Engineering"


def test_data_scientist_is_data():
    assert c("Senior Data Scientist, Personalization") == "Data & ML"


def test_data_analyst_is_data():
    assert c("Senior Data Analyst") == "Data & ML"


def test_product_manager_is_product():
    assert c("Product Manager II, Search Experience") == "Product"


def test_product_marketing_is_marketing_not_product():
    assert c("Product Marketing Manager") == "Marketing & Comms"


def test_product_designer_is_design():
    assert c("Senior Product Designer") == "Design"


def test_communications_is_marketing():
    assert c("Head of Executive Communications") == "Marketing & Comms"


def test_recruiter_beats_sales():
    assert c("Recruiter, Sales (Contract)") == "People & Recruiting"


def test_talent_acquisition_is_people():
    assert c("Lead Talent Acquisition Partner, GTM") == "People & Recruiting"


def test_fpna_is_finance():
    assert c("Senior FP&A Analyst, Corporate Finance") == "Finance & Accounting"


def test_program_manager_is_operations():
    assert c("Program Manager, Trust & Safety") == "Operations"


def test_sales_operations_is_operations_not_sales():
    assert c("Sales Operations Manager") == "Operations"


def test_counsel_is_legal():
    assert c("Corporate Counsel, Commercial") == "Legal & Compliance"


def test_general_manager_is_executive():
    assert c("General Manager - Spain") == "Executive / Leadership"


def test_cfo_is_executive():
    assert c("Chief Financial Officer") == "Executive / Leadership"


def test_community_manager_is_marketing():
    assert c("Community Manager") == "Marketing & Comms"


def test_department_fallback():
    # title is generic -> fall back to the department signal
    assert c("Coordinator", "Engineering") == "Engineering"


def test_unrecognized_is_other():
    cat, src = classify_role("Warehouse Associate", "")
    assert cat == "Other" and src == "unknown"


def test_source_is_rule_on_hit():
    assert classify_role("Senior Software Engineer", "")[1] == "rule"
