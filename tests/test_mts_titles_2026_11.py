"""Audit follow-up (Nov 2026): "Member of Technical Staff" is judged on its
posting instead of being read as a senior title, while every senior MTS
level stays senior. The experience rules are unchanged. No network."""
import pytest

from job_classifier import Category, classify_job

D = "Design and build distributed services used by millions of customers. "


@pytest.mark.parametrize("title, text, expected", [
    ("Associate Member of Technical Staff", D + "Open to 2026 graduates; 0-1 years of experience.", Category.FRESHER),
    ("Software Engineering AMTS", D + "Freshers welcome.", Category.FRESHER),
    ("Member of Technical Staff - New Grad", D, Category.ENTRY_LEVEL),
    # the experience rules are unchanged: a requirement still rejects, no evidence still isn't alerted
    ("Member of Technical Staff", D + "3+ years of experience in Java.", Category.EXPERIENCED),
    ("Associate Member of Technical Staff", D + "Minimum 2 years of experience required.", Category.EXPERIENCED),
    ("Member of Technical Staff", D, Category.UNKNOWN),
    ("Member of Technical Staff II", D, Category.EXPERIENCED),
])
def test_member_of_technical_staff_is_judged_on_its_posting(title, text, expected):
    assert classify_job(title, text).category == expected


@pytest.mark.parametrize("title", [
    "Senior Member of Technical Staff", "Consulting Member of Technical Staff", "Principal Member of Technical Staff",
    "Lead Member of Technical Staff", "Software Engineering SMTS", "Software Engineering LMTS",
    "Software Engineering PMTS", "CMTS, Database", "Staff Software Engineer", "Staff Accountant", "Chief of Staff",
    "Technical Staff Lead",
])
def test_senior_levels_stay_senior(title):
    assert classify_job(title, D + "Freshers welcome.").category == Category.SENIOR, title
