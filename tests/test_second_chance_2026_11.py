"""Second chance (21 Nov 2026): a company given N opportunities is exempt from
the shared data-centre gate, never from its own host cooldown or the
throttled-hosts stop; only a request actually sent uses an opportunity, a
skip is recorded and keeps it. No network."""
import scraper
import workday_runtime as wr


def co(**extra):
    return {"id": "c1", "name": "Marvell", "url": "https://marvell.wd1.myworkdayjobs.com/MarvellCareers", **extra}


def test_opportunities_left_count_only_real_attempts():
    assert wr.second_chance_left(co()) == 0
    assert wr.second_chance_left(co(second_chance=2)) == 2
    one = co(second_chance=2, scan={"second_chance": {"attempts": [{"at": "x"}], "skips": [{"at": "y"}, {"at": "z"}]}})
    assert wr.second_chance_left(one) == 1
    assert wr.second_chance_left(co(second_chance=True)) == 0


def test_the_ledger_records_attempts_and_skips_separately(monkeypatch):
    monkeypatch.setattr(scraper, "_now_iso", lambda: "2026-11-21T18:05:00+00:00")
    c = co(second_chance=2)
    skipped = scraper._second_chance(c, {}, True, False, {"reason": "cooldown"})
    assert skipped == {"attempts": [], "skips": [{"at": "2026-11-21T18:05:00+00:00", "reason": "cooldown"}]}
    tried = scraper._second_chance(c, {"second_chance": skipped}, True, True, {"outcome": "rate_limited", "http_status": 429})
    assert len(tried["attempts"]) == 1 and len(tried["skips"]) == 1
    assert scraper._second_chance(c, {"second_chance": tried}, False, True, {}) == tried     # targeted runs never count
    assert scraper._second_chance(co(), {}, True, True, {}) is None                          # only granted companies


class _Http:
    def __init__(self, throttled=()):
        self.throttled = set(throttled)


def test_the_shared_gate_is_skipped_but_its_own_cooldown_and_the_stop_are_not():
    rec = wr.Recovery.__new__(wr.Recovery)
    rec.mode, rec.config, rec.http = "partial", {"mode": "auto"}, _Http({"analogdevices.wd1.myworkdayjobs.com"})
    rec.cluster_of = lambda company: "wd1"
    rec.cluster_status = lambda name: (True, 0, 0)
    assert wr.Recovery.gate_reason(rec, co(second_chance=2)) == ""                   # own attempt, not the DC probe
    assert wr.Recovery.gate_reason(rec, co()) == "cluster_gate"                      # everyone else still waits
    exhausted = co(second_chance=1, scan={"second_chance": {"attempts": [{"at": "x"}]}})
    assert wr.Recovery.gate_reason(rec, exhausted) == "cluster_gate"
    src = open(wr.__file__).read()
    i = src.index("def defer_reason")
    body = src[i:src.index("def ", i + 10)]
    assert body.index("cooldown") < body.index("gate_reason") < body.index("circuit")   # cooldown first, stop after
