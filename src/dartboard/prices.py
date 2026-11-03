"""KIS (한국투자증권) Open API: adjusted daily prices for scoring the forecast ledger.

Endpoints, TR ids, parameters and field names follow the official samples in
koreainvestment/open-trading-api (examples_llm, checked 2026-11-03):
  * token        POST /oauth2/tokenP  {grant_type, appkey, appsecret} -> access_token, access_token_token_expired
  * stock daily  GET  /uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice   tr_id FHKST03010100
                 FID_ORG_ADJ_PRC "0" = adjusted price; at most 100 rows per call
  * index daily  GET  /uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice  tr_id FHKUP03500100
                 FID_COND_MRKT_DIV_CODE "U", FID_INPUT_ISCD "0001" = KOSPI composite
  * index codes (inquire_index_daily_price docstring): 0001 KOSPI, 1001 KOSDAQ, 2001 KOSPI200
  * success when rt_cd == "0"; the sample sleeps 0.05 s between calls (real) and 0.5 s (demo)
  * long ranges page with the tr_cont header; this client asks for 60-day windows instead,
    which stay under the per-call row cap, so it never needs to page
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")  # KIS reports token expiry in Korean time
DOMAINS = {"real": "https://openapi.koreainvestment.com:9443", "demo": "https://openapivts.koreainvestment.com:29443"}
SLEEP_S = {"real": 0.05, "demo": 0.5}
STOCK = ("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100")
INDEX = ("/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice", "FHKUP03500100")
BENCHMARK = {"Y": "0001", "K": "1001"}  # OpenDART corp_cls -> index: KOSPI / KOSDAQ composite
WINDOW_DAYS = 60  # calendar days per request: comfortably under the 100-row cap

Http = Callable[[str, str, dict, dict | None], dict]  # (method, url, headers, json_body) -> json


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    close: float


class KisError(RuntimeError):
    pass


def _urllib_http(method: str, url: str, headers: dict, body: dict | None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


class Kis:
    def __init__(self, app_key: str, app_secret: str, env: str = "real", token_path: str = "data/kis_token.json",
                 http: Http | None = None, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = lambda: datetime.now(KST)):
        if env not in DOMAINS:
            raise ValueError("env must be 'real' or 'demo'")
        self.key, self.secret, self.env = app_key, app_secret, env
        self.token_path, self.http, self.sleep, self.now = token_path, http or _urllib_http, sleep, now

    # tokens last about a day; reuse the cached one instead of asking for a new one every run
    def _token(self) -> str:
        try:
            with open(self.token_path, encoding="utf-8") as f:
                cached = json.load(f)
            if datetime.fromisoformat(cached["expires"]) - self.now() > timedelta(minutes=10):
                return cached["token"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        res = self.http("POST", f"{DOMAINS[self.env]}/oauth2/tokenP", {"content-type": "application/json"},
                        {"grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret})
        token = res.get("access_token")
        if not token:
            raise KisError(f"token request failed: {res}")
        expires = datetime.strptime(res["access_token_token_expired"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        os.makedirs(os.path.dirname(self.token_path) or ".", exist_ok=True)
        fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # the token is a secret
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"token": token, "expires": expires.isoformat()}, f)
        return token

    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {self._token()}",
                   "appkey": self.key, "appsecret": self.secret, "tr_id": tr_id, "custtype": "P"}
        res = self.http("GET", f"{DOMAINS[self.env]}{path}?{urllib.parse.urlencode(params)}", headers, None)
        self.sleep(SLEEP_S[self.env])
        if res.get("rt_cd") != "0":
            raise KisError(f"{res.get('msg_cd')}: {res.get('msg1')}")
        return res

    def daily(self, code: str, start: date, end: date, index: bool = False) -> list[Bar]:
        """Daily bars from start to end inclusive, oldest first. Stocks use adjusted prices."""
        path, tr_id = INDEX if index else STOCK
        o_key, c_key = ("bstp_nmix_oprc", "bstp_nmix_prpr") if index else ("stck_oprc", "stck_clpr")
        bars: dict[str, Bar] = {}
        hi = end
        while hi >= start:
            lo = max(start, hi - timedelta(days=WINDOW_DAYS - 1))
            params = {"FID_COND_MRKT_DIV_CODE": "U" if index else "J", "FID_INPUT_ISCD": code,
                      "FID_INPUT_DATE_1": lo.strftime("%Y%m%d"), "FID_INPUT_DATE_2": hi.strftime("%Y%m%d"),
                      "FID_PERIOD_DIV_CODE": "D"}
            if not index:
                params["FID_ORG_ADJ_PRC"] = "0"
            for row in self._get(path, tr_id, params).get("output2") or []:
                d, o, c = row.get("stck_bsop_date"), row.get(o_key), row.get(c_key)
                if d and o and c and float(c) > 0:
                    bars[d] = Bar(datetime.strptime(d, "%Y%m%d").date(), float(o), float(c))
            hi = lo - timedelta(days=1)
        return [bars[k] for k in sorted(bars)]
