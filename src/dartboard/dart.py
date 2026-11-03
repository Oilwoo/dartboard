"""Minimal OpenDART client for the disclosure list API (list.json).

Rules taken from the official developer guide (opendart.fss.or.kr, DS001):
  * without corp_code the search window is limited to 3 months
  * status "000" = OK, "013" = no data, "020" = request limit exceeded (about 20,000 calls per day)
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Callable

BASE = "https://opendart.fss.or.kr/api"


class DartError(RuntimeError):
    pass


class RateLimited(DartError):
    pass


@dataclass(frozen=True)
class Disclosure:
    rcept_no: str      # receipt number; the original filing is at https://dart.fss.or.kr/dsaf001/main.do?rcpNo=<rcept_no>
    rcept_dt: str      # receipt DATE only (no time of day): backtests enter at the next trading day's open
    corp_code: str
    corp_name: str
    stock_code: str
    report_nm: str


def _months_between(a: date, b: date) -> float:
    return (b - a).days / 30.4


def list_disclosures(api_key: str, bgn: date, end: date, corp_code: str | None = None,
                     pblntf_ty: str | None = None, page_count: int = 100,
                     fetch: Callable[[str], bytes] | None = None) -> list[Disclosure]:
    if corp_code is None and _months_between(bgn, end) > 3:
        raise ValueError("OpenDART limits searches without corp_code to 3 months; split the range")
    fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=20).read())
    out, page = [], 1
    while True:
        params = {"crtfc_key": api_key, "bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
                  "page_no": page, "page_count": page_count}
        if corp_code:
            params["corp_code"] = corp_code
        if pblntf_ty:
            params["pblntf_ty"] = pblntf_ty
        data = json.loads(fetch(f"{BASE}/list.json?{urllib.parse.urlencode(params)}"))
        status = data.get("status")
        if status == "013":
            return out
        if status == "020":
            raise RateLimited(data.get("message", "request limit exceeded"))
        if status != "000":
            raise DartError(f"{status}: {data.get('message')}")
        for r in data.get("list", []):
            out.append(Disclosure(r["rcept_no"], r["rcept_dt"], r["corp_code"], r["corp_name"],
                                  r.get("stock_code", ""), r["report_nm"]))
        if page >= int(data.get("total_page", 1)):
            return out
        page += 1
