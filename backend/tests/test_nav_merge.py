"""Merged «Вакансии» nav (Каталог · Mass Hiring · Незавершённые) — pure, no DB. Run:
    PYTHONPATH=. python3 -m pytest backend/tests/test_nav_merge.py -q
"""
from backend.tools import mailcrm_ui as m


def test_nav_collapsed_to_vacancies():
    keys = [k for _h, k, _l, _s in m._NAV]
    assert len(m._NAV) == 5
    assert "vacancies" in keys
    assert "masshiring" not in keys and "unfinished" not in keys and "catalog" not in keys
    vac = next(r for r in m._NAV if r[1] == "vacancies")
    assert vac[0] == "/catalog" and vac[2] == "Вакансии"


def test_all_sub_surfaces_light_the_one_entry():
    for active in ("catalog", "masshiring", "unfinished", "vacancies"):
        html = m._nav_links(active)
        assert html.count('class="active"') == 1, active
        assert '<a class="active" href="/catalog">' in html, active
    html = m._nav_links("stats")
    assert '<a class="active" href="/stats">' in html
    assert '<a class="active" href="/catalog">' not in html


def test_vacancies_seg_marks_active_and_counts():
    seg = m.vacancies_seg("masshiring", {"masshiring": 42})
    for h in ("/catalog", "/mass-hiring", "/unfinished"):
        assert f'href="{h}"' in seg
    assert 'class="active" href="/mass-hiring"' in seg
    assert seg.count('class="active"') == 1
    assert "<b>42</b>" in seg


def test_topbar_catalog_keeps_search_others_show_vacancies():
    assert 'class="gm-search"' in m._topbar("catalog")
    assert ">Вакансии<" in m._topbar("masshiring")
    assert ">Вакансии<" in m._topbar("unfinished")


def test_page_head_seg_html_used_verbatim_and_keeps_fab():
    ph = m._page_head("Ttl", seg_html='<div class="seg-nav vac-seg">ZZ</div>')
    assert 'vac-seg">ZZ' in ph and ">Ttl<" not in ph
    ph2 = m._page_head("Ttl", primary={"label": "Go", "onclick": "f()"},
                       seg_html='<div class="seg-nav vac-seg">Z</div>')
    assert "fab-compose" in ph2
