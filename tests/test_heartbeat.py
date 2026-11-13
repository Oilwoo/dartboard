import logging

from dartboard.heartbeat import ping

URL = "https://hc-ping.com/0f1e2d3c-secret-uuid"


class Opener:
    def __init__(self, fail=False):
        self.requests, self.fail = [], fail

    def __call__(self, request, timeout):
        self.requests.append((request.full_url, request.get_method(), timeout))
        if self.fail:
            raise OSError("network down")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_success_and_failure_pings():
    opener = Opener()
    assert ping(URL, opener=opener) and ping(URL + "/", ok=False, opener=opener)
    assert [(u, m) for u, m, _ in opener.requests] == [(URL, "HEAD"), (URL + "/fail", "HEAD")]


def test_no_url_means_no_ping():
    opener = Opener()
    assert not ping("", opener=opener) and opener.requests == []


def test_a_monitor_outage_never_stops_the_bot_and_the_url_is_never_logged(caplog):
    with caplog.at_level(logging.WARNING):
        assert not ping(URL, opener=Opener(fail=True))
    assert "OSError" in caplog.text and "secret" not in caplog.text
