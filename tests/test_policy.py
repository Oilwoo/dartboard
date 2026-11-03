import os

from dartboard.policy import Action, Decision, PolicyEngine

HERE = os.path.join(os.path.dirname(__file__), "..", "src", "dartboard")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def engine(clock=None):
    e = PolicyEngine.from_file(os.path.join(HERE, "policy.yaml"), ":memory:")
    if clock:
        e.clock = clock
    return e


def test_within_limits_runs_automatically_but_dry_run_by_default():
    v = engine().decide(Action("e1", "upbit:order", 30_000))
    assert v.decision is Decision.AUTO and v.dry_run is True


def test_over_per_order_limit_needs_approval():
    assert engine().decide(Action("e1", "upbit:order", 60_000)).decision is Decision.NEEDS_APPROVAL


def test_unknown_target_needs_approval():
    assert engine().decide(Action("e1", "bank:transfer", 1_000)).decision is Decision.NEEDS_APPROVAL


def test_daily_limit_and_rolling_window():
    c = Clock()
    e = engine(c)
    for i in range(4):  # 4 x 50,000 = 200,000 (the daily limit), spread over hours
        a = Action(f"e{i}", "upbit:order", 50_000)
        assert e.decide(a).decision is Decision.AUTO
        e.record_executed(a)
        c.t += 3_601
    assert e.decide(Action("e9", "upbit:order", 10_000)).reason == "would exceed daily spend limit"
    c.t += 86_400  # a day later the window has rolled
    assert e.decide(Action("e10", "upbit:order", 10_000)).decision is Decision.AUTO


def test_hourly_order_count():
    e = engine()
    for i in range(3):
        a = Action(f"e{i}", "upbit:order", 1_000)
        e.record_executed(a)
    assert e.decide(Action("e4", "upbit:order", 1_000)).reason == "hourly order limit reached"


def test_duplicate_event_is_refused():
    e = engine()
    a = Action("same-event", "upbit:order", 1_000)
    e.record_executed(a)
    assert e.decide(a).decision is Decision.DENY


def test_rejected_event_is_not_asked_again():
    e = engine()
    a = Action("asked-once", "upbit:order", 90_000)
    assert e.decide(a).decision is Decision.NEEDS_APPROVAL
    e.record_rejected(a)
    assert e.decide(a).decision is Decision.DENY


def test_kill_switch_blocks_everything():
    e = engine()
    e.halt()
    assert e.decide(Action("e1", "upbit:order", 1_000)).decision is Decision.DENY
    e.halt(False)
    assert e.decide(Action("e1", "upbit:order", 1_000)).decision is Decision.AUTO


def test_non_positive_amount_is_refused():
    assert engine().decide(Action("e1", "upbit:order", 0)).decision is Decision.DENY
