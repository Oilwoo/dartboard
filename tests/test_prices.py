import json
from datetime import date, datetime, timedelta
from itertools import pairwise
from urllib.parse import parse_qs, urlparse

import pytest

from dartboard.prices import KST, Kis, KisError

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=KST)


class FakeKis:
    """Answers like the KIS server: token endpoint plus daily chart rows inside the requested dates."""

    def __init__(self, rows=None, expires="2026-11-04 18:00:00", fail=False):
        self.rows, self.expires, self.fail = rows or [], expires, fail
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/oauth2/tokenP"):
            return {"access_token": f"tok{len(self.calls)}", "access_token_token_expired": self.expires}
        if self.fail:
            return {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        rows = [r for r in self.rows if q["FID_INPUT_DATE_1"] <= r["stck_bsop_date"] <= q["FID_INPUT_DATE_2"]]
        return {"rt_cd": "0", "output2": sorted(rows, key=lambda r: r["stck_bsop_date"], reverse=True)}

    def gets(self):
        return [(url, h) for m, url, h, _ in self.calls if m == "GET"]

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def kis(fake, tmp_path, **kw):
    return Kis("key", "secret", token_path=str(tmp_path / "kis_token.json"), http=fake,
               sleep=lambda s: None, now=lambda: NOW, **kw)


def stock_row(day: str, o: str, c: str) -> dict:
    return {"stck_bsop_date": day, "stck_oprc": o, "stck_clpr": c}


def test_token_is_cached_across_restarts(tmp_path):
    fake = FakeKis()
    kis(fake, tmp_path).daily("005930", date(2026, 11, 2), date(2026, 11, 3))
    kis(fake, tmp_path).daily("005930", date(2026, 11, 2), date(2026, 11, 3))  # a new process, same file
    assert len(fake.posts()) == 1
    assert fake.gets()[1][1]["authorization"] == "Bearer tok1"
    saved = json.loads((tmp_path / "kis_token.json").read_text())
    assert saved["expires"].endswith("+09:00")


def test_token_close_to_expiry_is_renewed(tmp_path):
    (tmp_path / "kis_token.json").write_text(json.dumps(
        {"token": "old", "expires": (NOW + timedelta(minutes=5)).isoformat()}))
    fake = FakeKis()
    kis(fake, tmp_path).daily("005930", date(2026, 11, 3), date(2026, 11, 3))
    assert len(fake.posts()) == 1 and fake.gets()[0][1]["authorization"] == "Bearer tok1"


def test_expiry_is_read_in_korean_time_on_a_utc_server(tmp_path):
    # KIS says "expires 18:05" meaning Korean time. On a UTC server, 09:00 UTC is 18:00 KST:
    # only 5 minutes are left, so the token must be renewed (a naive clock would see 9 hours left).
    fake = FakeKis(expires="2026-11-03 18:05:00")

    def at(utc):
        return Kis("key", "secret", token_path=str(tmp_path / "kis_token.json"), http=fake,
                   sleep=lambda s: None, now=lambda: datetime.fromisoformat(utc))

    at("2026-11-03T08:00:00+00:00").daily("005930", date(2026, 11, 3), date(2026, 11, 3))
    at("2026-11-03T09:00:00+00:00").daily("005930", date(2026, 11, 3), date(2026, 11, 3))
    assert len(fake.posts()) == 2


def test_long_range_is_split_into_contiguous_windows(tmp_path):
    fake = FakeKis()
    kis(fake, tmp_path).daily("005930", date(2026, 1, 1), date(2026, 5, 10))
    windows = sorted((parse_qs(urlparse(u).query)["FID_INPUT_DATE_1"][0],
                      parse_qs(urlparse(u).query)["FID_INPUT_DATE_2"][0]) for u, _ in fake.gets())
    assert len(windows) == 3
    assert windows[0][0] == "20260101" and windows[-1][1] == "20260510"
    for (_, hi), (lo, _) in pairwise(windows):  # no gap, no overlap
        assert datetime.strptime(lo, "%Y%m%d") - datetime.strptime(hi, "%Y%m%d") == timedelta(days=1)


def test_stock_request_uses_adjusted_prices_and_returns_oldest_first(tmp_path):
    fake = FakeKis(rows=[stock_row("20261102", "100", "110"), stock_row("20261103", "111", "121"),
                         stock_row("20261030", "", "")])  # empty row (no trade) is skipped
    bars = kis(fake, tmp_path).daily("005930", date(2026, 10, 30), date(2026, 11, 3))
    assert [(b.day, b.open, b.close) for b in bars] == [(date(2026, 11, 2), 100.0, 110.0),
                                                        (date(2026, 11, 3), 111.0, 121.0)]
    url, headers = fake.gets()[0]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["FID_ORG_ADJ_PRC"] == "0" and q["FID_COND_MRKT_DIV_CODE"] == "J" and q["FID_INPUT_ISCD"] == "005930"
    assert headers["tr_id"] == "FHKST03010100" and headers["custtype"] == "P"
    assert url.startswith("https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/")


def test_index_request(tmp_path):
    fake = FakeKis(rows=[{"stck_bsop_date": "20261103", "bstp_nmix_oprc": "2500.5", "bstp_nmix_prpr": "2510.25"}])
    bars = kis(fake, tmp_path).daily("0001", date(2026, 11, 3), date(2026, 11, 3), index=True)
    assert [(b.open, b.close) for b in bars] == [(2500.5, 2510.25)]
    url, headers = fake.gets()[0]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["FID_COND_MRKT_DIV_CODE"] == "U" and "FID_ORG_ADJ_PRC" not in q
    assert headers["tr_id"] == "FHKUP03500100"


def test_error_reply_raises(tmp_path):
    with pytest.raises(KisError, match="EGW00201"):
        kis(FakeKis(fail=True), tmp_path).daily("005930", date(2026, 11, 3), date(2026, 11, 3))


def test_demo_environment_uses_demo_domain_and_slower_pace(tmp_path):
    fake, slept = FakeKis(), []
    Kis("key", "secret", env="demo", token_path=str(tmp_path / "t.json"), http=fake, sleep=slept.append,
        now=lambda: NOW).daily("005930", date(2026, 11, 3), date(2026, 11, 3))
    assert fake.calls[0][1].startswith("https://openapivts.koreainvestment.com:29443/") and slept == [0.5]
