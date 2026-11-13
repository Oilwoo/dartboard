"""KIS (한국투자증권) Open API client: adjusted daily prices for scoring.

Endpoints, TR ids and fields follow the official samples in koreainvestment/open-trading-api.
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

KST = ZoneInfo("Asia/Seoul")
DOMAINS = {"real": "https://openapi.koreainvestment.com:9443", "demo": "https://openapivts.koreainvestment.com:29443"}
SLEEP_S = {"real": 0.4, "demo": 1.1}  # under KIS limits (2026-04-20 notice): 모의 1/s, a new real key 3/s for 3 days
STOCK = ("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100")
INDEX = ("/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice", "FHKUP03500100")
HOLIDAY = ("/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R")  # real server only; KIS asks for 1 call a day
BENCHMARK = {"Y": "0001", "K": "1001"}  # OpenDART corp_cls -> KOSPI / KOSDAQ composite index
WINDOW_DAYS = 60  # per request; stays under the 100-row cap, so no paging is needed

Http = Callable[[str, str, dict, dict | None], dict]  # (method, url, headers, json body) -> json reply


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
                 http: Http = _urllib_http, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] = lambda: datetime.now(KST)):
        if env not in DOMAINS:
            raise ValueError("env must be 'real' or 'demo'")
        self.key, self.secret, self.env = app_key, app_secret, env
        self.token_path, self.http, self.sleep, self.now = token_path, http, sleep, now

    def _token(self) -> str:
        """Reuses the saved token (valid about a day) until 10 minutes before it expires."""
        try:
            with open(self.token_path, encoding="utf-8") as f:
                saved = json.load(f)
            if datetime.fromisoformat(saved["expires"]) - self.now() > timedelta(minutes=10):
                return saved["token"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        res = self.http("POST", f"{DOMAINS[self.env]}/oauth2/tokenP", {"content-type": "application/json"},
                        {"grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret})
        token = res.get("access_token")
        if not token:
            raise KisError(f"token request failed: {res}")
        # the expiry comes in Korean time; comparing it with a UTC server clock as-is is off by 9 hours
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
        """Daily bars from start to end inclusive, oldest first. Stock prices are adjusted."""
        path, tr_id = INDEX if index else STOCK
        open_key, close_key = ("bstp_nmix_oprc", "bstp_nmix_prpr") if index else ("stck_oprc", "stck_clpr")
        bars: dict[str, Bar] = {}
        hi = end
        while hi >= start:
            lo = max(start, hi - timedelta(days=WINDOW_DAYS - 1))
            params = {"FID_COND_MRKT_DIV_CODE": "U" if index else "J", "FID_INPUT_ISCD": code,
                      "FID_INPUT_DATE_1": lo.strftime("%Y%m%d"), "FID_INPUT_DATE_2": hi.strftime("%Y%m%d"),
                      "FID_PERIOD_DIV_CODE": "D"}
            if not index:
                params["FID_ORG_ADJ_PRC"] = "0"  # 0 = adjusted price
            for row in self._get(path, tr_id, params).get("output2") or []:
                day, o, c = row.get("stck_bsop_date"), row.get(open_key), row.get(close_key)
                if day and o and c and float(c) > 0:
                    bars[day] = Bar(datetime.strptime(day, "%Y%m%d").date(), float(o), float(c))
            hi = lo - timedelta(days=1)
        return [bars[k] for k in sorted(bars)]

    def daily_rows(self, code: str, start: date, end: date, adjusted: bool = True) -> list[dict]:
        """Raw daily rows (at most 100) for checks that need more fields than open and close."""
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                  "FID_INPUT_DATE_2": end.strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D",
                  "FID_ORG_ADJ_PRC": "0" if adjusted else "1"}
        return self._get(*STOCK, params).get("output2") or []

    def open_days(self, base: date) -> dict[date, bool]:
        """The KRX calendar from `base` as KIS has it: day -> market open (opnd_yn)."""
        if self.env != "real":
            raise KisError("the holiday API has no 모의투자 version")
        res = self._get(*HOLIDAY, {"BASS_DT": base.strftime("%Y%m%d"), "CTX_AREA_NK": "", "CTX_AREA_FK": ""})
        rows = res.get("output") or []
        return {datetime.strptime(r["bass_dt"], "%Y%m%d").date(): r.get("opnd_yn") == "Y"
                for r in (rows if isinstance(rows, list) else [rows]) if r.get("bass_dt")}
