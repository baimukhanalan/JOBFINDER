import inspect

from backend.tools.employer_segmentation import classify_employer, refresh_segments


def test_segments_staffing_without_rejecting_the_lane():
    segment, risks = classify_employer({
        "brand_name": "Acme Staffing", "industry": "workforce solutions",
    })
    assert segment == "staffing"
    assert risks == []


def test_flags_shell_fund_and_aggregate_entities():
    segment, risks = classify_employer({
        "brand_name": "Example Payroll Shared Services Investment Fund Trust",
        "industry": "financial services",
    })
    assert segment == "general"
    assert "shell_or_shared_services" in risks
    assert "fund_or_trust" in risks


def test_assigns_specialized_verticals():
    assert classify_employer({"brand_name": "City of Austin"})[0] == "government"
    assert classify_employer({"brand_name": "Duke University"})[0] == "education"
    assert classify_employer({"brand_name": "Mercy Hospital"})[0] == "healthcare"


def test_refresh_rechecks_active_population_at_update_time():
    assert "WHERE company_id=%s AND in_target_population" in inspect.getsource(refresh_segments)
