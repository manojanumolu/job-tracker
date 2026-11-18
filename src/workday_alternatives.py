"""Offline policy gate for NEW official sitemap integrations; never probes sites.

An operator supplies independently verified policy evidence. A successful fixture
does not establish current permission. Existing JioStar mapping is unchanged.
"""
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from workday_state import HOST, epoch

JIOSTAR_SITE = "https://jiostar.wd102.myworkdayjobs.com/JioStar"


def permitted(config, now=None):
    if config.get("site") == JIOSTAR_SITE:
        return True  # existing integration, not a new recovery claim
    now = now or datetime.now(timezone.utc)
    policy = config.get("access_policy")
    if not isinstance(policy, dict) or policy.get("terms_permit_automation") is not True:
        return False
    checked = epoch(policy.get("verified_at"))
    if not checked or not 0 <= now.timestamp() - checked <= 30 * 86400:
        return False
    site = config.get("site", "")
    parsed = urlsplit(site)
    if parsed.scheme != "https" or not HOST.fullmatch(parsed.hostname or "") or parsed.username or parsed.port:
        return False
    robots = policy.get("robots_text")
    if not isinstance(robots, str) or not robots.strip() or len(robots) > 100000:
        return False
    if not isinstance(policy.get("terms_url"), str) or not policy["terms_url"].startswith("https://"):
        return False
    parser = RobotFileParser()
    parser.parse(robots.splitlines())
    sitemap = site.rstrip("/") + "/siteMap.xml"
    return (sitemap in (parser.site_maps() or []) and parser.can_fetch("FresherJobTracker", sitemap)
            and parser.can_fetch("FresherJobTracker", site.rstrip("/") + "/job/example")
            and parser.can_fetch("FresherJobTracker", f"https://{parsed.hostname}/en-US{parsed.path}/job/example"))
