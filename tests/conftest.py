"""Shared test setup.

Many Workday-mechanics tests use the real company ids "sanofi" (and "cisco")
as their stand-in Workday tenant. In Nov 2026 both employers moved to their
official career sites (scraper.COMPANY_SOURCES), so those tests would no
longer exercise Workday at all. For every test except the ones about the new
sources, the two overrides are removed for the test's duration, so the
Workday tests keep testing Workday; production configuration is unchanged.
"""
import pytest

_SOURCE_TESTS = {"test_career_sitemap_source_2026_11.py", "test_phenom_source_2026_11.py",
                 "test_official_alternatives_2026_11.py"}
_WORKDAY_STAND_INS = ("sanofi", "cisco")
_SANOFI_WORKDAY = [{"type": "workday", "api": "https://sanofi.wd3.myworkdayjobs.com/wday/cxs/sanofi/SanofiCareers/jobs"}]


@pytest.fixture(autouse=True)
def _workday_stand_in_companies(request, monkeypatch):
    if request.node.path.name in _SOURCE_TESTS:
        yield
        return
    import scraper
    restored = {k: v for k, v in scraper.COMPANY_SOURCES.items() if k not in _WORKDAY_STAND_INS}
    restored["sanofi"] = _SANOFI_WORKDAY          # its configuration before Nov 2026; Cisco had none (URL-derived)
    monkeypatch.setattr(scraper, "COMPANY_SOURCES", restored)
    yield
