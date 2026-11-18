"""Single-flight, run-wide Workday pacing and durable host circuits (M7)."""
from __future__ import annotations

import math
import time
from contextvars import ContextVar
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from workday_state import HOST, epoch, iso

CURRENT = ContextVar("workday_run", default=None)


class WorkdayStop(Exception):
    def __init__(self, category, host, status=None, *, attempted=False, retry_after_s=0, retry_after_valid=False):
        super().__init__(f"Workday {category} ({host})")
        self.diagnostic = {"failure_category": category, "host": host, "http_status": status,
                           "attempted": attempted, "retry_after_s": retry_after_s,
                           "retry_after_valid": retry_after_valid}


def retry_after(value: str | None, now: float) -> tuple[float, bool]:
    try:
        if value is None or len(value) > 128:
            return 0, False
        if value.strip().isdigit():
            seconds = float(value.strip())
        else:
            dt = parsedate_to_datetime(value)
            seconds = dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp() - now
        if not math.isfinite(seconds) or seconds < 0 or now + seconds >= 253402214400:
            return 0, False
        return seconds, True
    except (ValueError, TypeError, OverflowError):
        return 0, False


class Controller:
    def __init__(self, state, config, *, clock=time.monotonic, wall=time.time, sleep=time.sleep):
        self.state, self.config = state, config
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.next_global, self.next_host = 0.0, {}
        self.started = clock()
        self.spent = 0.0
        self.requests = 0
        self.throttled = set()
        self.closed = set()
        self.events = []

    def cooling(self, host):
        return epoch(self.state["hosts"].get(host, {}).get("cooldown_until")) > self.wall()

    def _budget(self, wait=0):
        c = self.config
        return (self.requests >= c["max_requests"] or self.spent + wait >= c["workday_budget_s"]
                or self.clock() - self.started + wait >= c["run_budget_s"])

    def _observation(self, host, status, category, delay=0, valid=False):
        previous = self.state["hosts"].get(host, {})
        failures = min(10000, previous.get("failures", 0) + 1) if category else 0
        fallback = min(self.config["max_cooldown_hours"], self.config["cooldown_hours"] * 2 ** min(failures - 1, 10)) * 3600 if category else 0
        self.state["hosts"][host] = {
            "observed_at": iso(self.wall()), "cooldown_until": iso(self.wall() + max(delay, fallback)) if category else "",
            "failures": failures, "http_status": status, "failure_category": category,
            "retry_after_s": delay, "retry_after_valid": valid,
        }

    def request(self, client, method, url, **kwargs):
        host = urlsplit(url).hostname or ""
        if not HOST.fullmatch(host) or urlsplit(url).scheme != "https" or urlsplit(url).username or urlsplit(url).port:
            raise WorkdayStop("configuration", "", attempted=False)
        api = "/wday/cxs/" in urlsplit(url).path
        if self.state["hosts"].get(host, {}).get("failure_category") in ("forbidden", "authentication", "redirect"):
            raise WorkdayStop("configuration", host)
        if host in self.closed or self.cooling(host):
            raise WorkdayStop("cooldown", host)
        if api and len(self.throttled) >= self.config["api_breaker_hosts"]:
            raise WorkdayStop("circuit", host)
        retry_wait = 0.0
        recorded = False
        started = self.clock()
        try:
            for attempt in range(self.config["max_retries"] + 1):
                wait = max(0, self.next_global - self.clock(), self.next_host.get(host, 0) - self.clock())
                if self._budget(self.clock() - started + wait):
                    if recorded:
                        obs = self.state["hosts"][host]
                        raise WorkdayStop(obs["failure_category"], host, obs["http_status"], attempted=True,
                                          retry_after_s=obs["retry_after_s"], retry_after_valid=obs["retry_after_valid"])
                    raise WorkdayStop("budget", host)
                if wait:
                    self.sleep(wait)
                self.next_global = self.clock() + self.config["global_spacing_s"]
                self.next_host[host] = self.clock() + self.config["host_spacing_s"]
                self.requests += 1
                self.events.append({"host": host, "phase": "list" if api and method == "POST" else "detail",
                                    "http_status": None, "failure_category": ""})
                event = self.events[-1]
                try:
                    # Never let httpx follow an unpaced/unapproved redirect behind the controller.
                    # The client's existing, honest User-Agent is kept: a throttled
                    # caller never presents a new identity to a rate limiter.
                    response = client.request(method, url, follow_redirects=False, **kwargs)
                except httpx.HTTPError as exc:
                    category = "timeout" if isinstance(exc, httpx.TimeoutException) else "network"
                    event["failure_category"] = category
                    if attempt < self.config["max_retries"]:
                        delay = self.config["backoff_s"] * 2 ** attempt
                        if retry_wait + delay <= self.config["retry_wait_budget_s"] and not self._budget(self.clock() - started + delay):
                            self.sleep(delay)
                            retry_wait += delay
                            continue
                    self._observation(host, None, category)
                    self.closed.add(host)
                    raise WorkdayStop(category, host, attempted=True) from None
                status = response.status_code
                event["http_status"] = status
                if 200 <= status < 300:
                    self._observation(host, status, "")
                    self.closed.discard(host)
                    self.throttled.discard(host)
                    return response
                category = ("rate_limit" if status == 429 else "forbidden" if status == 403 else
                            "authentication" if status == 401 else "not_found" if status == 404 else
                            "server_error" if status >= 500 else "redirect" if 300 <= status < 400 else "http_error")
                event["failure_category"] = category
                delay, valid = retry_after(response.headers.get("Retry-After"), self.wall())
                if status == 429:
                    self.throttled.add(host)
                if status == 429 or status >= 500:
                    # Circuit opens immediately; only this synchronous designated trial may retry.
                    self.closed.add(host)
                    if not recorded:
                        self._observation(host, status, category, delay, valid)
                        recorded = True
                    else:
                        obs = self.state["hosts"][host]
                        fallback = min(self.config["max_cooldown_hours"], self.config["cooldown_hours"] * 2 ** min(obs["failures"] - 1, 10)) * 3600
                        obs.update(observed_at=iso(self.wall()), http_status=status, failure_category=category,
                                   cooldown_until=iso(max(epoch(obs["cooldown_until"]), self.wall() + max(delay, fallback))),
                                   retry_after_s=delay, retry_after_valid=valid)
                    # A 429 is never retried blind: only a valid Retry-After short enough
                    # for this run's retry budget earns one paced retry. Otherwise the host
                    # cools down durably and nothing else is sent to it this run.
                    sleep_for = delay if status == 429 else max(delay, self.config["backoff_s"] * 2 ** attempt)
                    may_retry = valid if status == 429 else True
                    if may_retry and attempt < self.config["max_retries"] and retry_wait + sleep_for <= self.config["retry_wait_budget_s"] and not self._budget(self.clock() - started + sleep_for):
                        response.close()
                        self.sleep(sleep_for)
                        retry_wait += sleep_for
                        continue
                # A vanished posting must not cool down all other postings on the host.
                if status != 404:
                    self.closed.add(host)
                    if not recorded:
                        self._observation(host, status, category, delay, valid)
                response.close()
                raise WorkdayStop(category, host, status, attempted=True, retry_after_s=delay, retry_after_valid=valid)
            raise AssertionError("unreachable")
        finally:
            self.spent += self.clock() - started


def request(client, method, url, **kwargs):
    run = CURRENT.get()
    if run is None:
        return client.request(method, url, **kwargs)
    return run.http.request(client, method, url, **kwargs)
