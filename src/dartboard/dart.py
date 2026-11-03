"""Minimal OpenDART client for the disclosure list API (list.json).

Rules taken from the official developer guide (opendart.fss.or.kr, DS001):
  * without corp_code the search window is limited to 3 months
  * status "000" = OK, "013" = no data, "020" = request limit exceeded (about 20,000 calls per day)
"""
from __future__ import annotations

import html
import io
import json
import re
import urllib.parse
import urllib.request
import zipfile
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
    corp_cls: str = ""  # 'Y' KOSPI, 'K' KOSDAQ, 'N' KONEX, 'E' other


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
                                  (r.get("stock_code") or "").strip(), r["report_nm"], r.get("corp_cls", "")))
        if page >= int(data.get("total_page", 1)):
            return out
        page += 1


# --- original filing text (document.xml) -----------------------------------------------
# Behaviour checked against OpenDartReader (FinanceData/OpenDartReader, dart_list.document):
# an error comes back as a small XML with <status>/<message>; success is a ZIP whose
# alphabetically first XML ("<rcept_no>.xml") is the main document; attachments follow.

_DECL = re.compile(rb'<\?xml[^>]*encoding=["\']([A-Za-z0-9_-]+)["\']', re.I)
_CELL_END = re.compile(r"</(?:TD|TE|TU|TH)\s*>", re.I)
_ROW_END = re.compile(r"</TR\s*>", re.I)
_BLOCK = re.compile(r"<(?:/?P|/?TITLE|BR|/?TABLE|PGBRK|/?SECTION-\d+|/?COVER|/?LIBRARY|/?BODY)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def _decode(data: bytes) -> str:
    m = _DECL.search(data[:300])
    for enc in ([m.group(1).decode()] if m else []) + ["utf-8", "cp949"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def xml_to_text(xml: str) -> str:
    """DART XML -> plain text. Table rows become 'cell | cell' lines so numbers keep their labels."""
    t = _CELL_END.sub(" | ", xml)
    t = _ROW_END.sub("\n", t)
    t = _BLOCK.sub("\n", t)
    t = html.unescape(_TAG.sub("", t))
    lines = (re.sub(r"[ \t 　]+", " ", line).strip(" |") for line in t.splitlines())
    return "\n".join(line for line in lines if line)


def fetch_document(api_key: str, rcept_no: str, fetch: Callable[[str], bytes] | None = None) -> str:
    fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=60).read())
    data = fetch(f"{BASE}/document.xml?{urllib.parse.urlencode({'crtfc_key': api_key, 'rcept_no': rcept_no})}")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        status = re.search(rb"<status>\s*(\d+)\s*</status>", data)
        message = re.search(rb"<message>(.*?)</message>", data, re.S)
        code = status.group(1).decode() if status else "?"
        msg = _decode(message.group(1)).strip() if message else _decode(data[:200])
        if code == "020":
            raise RateLimited(msg)
        raise DartError(f"{code}: {msg}")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted(n for n in zf.namelist() if n.lower().endswith(".xml"))
        if not names:
            raise DartError("document ZIP contains no XML")
        main = f"{rcept_no}.xml" if f"{rcept_no}.xml" in names else names[0]
        return xml_to_text(_decode(zf.read(main)))
