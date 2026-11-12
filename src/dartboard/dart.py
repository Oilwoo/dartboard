"""OpenDART client: the disclosure list (list.json) and the original filing text (document.xml)."""
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

Fetch = Callable[[str], bytes]


class DartError(RuntimeError):
    pass


class RateLimited(DartError):
    pass


@dataclass(frozen=True)
class Disclosure:
    rcept_no: str       # receipt number, unique per filing
    rcept_dt: str       # date only, no time of day
    corp_code: str
    corp_name: str
    stock_code: str
    report_nm: str
    corp_cls: str = ""  # Y KOSPI, K KOSDAQ, N KONEX, E other
    filing_type: str = ""  # OpenDART pblntf_detail_ty asked for, e.g. B001; the list itself does not say


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def list_disclosures(api_key: str, bgn: date, end: date, corp_code: str | None = None,
                     filing_type: str | None = None, fetch: Fetch = _download) -> list[Disclosure]:
    """Filings received from bgn to end. filing_type is an OpenDART detail type such as B001."""
    if corp_code is None and (end - bgn).days > 92:
        raise ValueError("OpenDART limits searches without corp_code to 3 months; split the range")
    out, page = [], 1
    while True:
        params = {"crtfc_key": api_key, "bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
                  "page_no": page, "page_count": 100}
        if corp_code:
            params["corp_code"] = corp_code
        if filing_type:
            params["pblntf_detail_ty"] = filing_type
        data = json.loads(fetch(f"{BASE}/list.json?{urllib.parse.urlencode(params)}"))
        status = data.get("status")
        if status == "013":  # nothing filed in the range
            return out
        if status == "020":
            raise RateLimited(data.get("message", "request limit exceeded"))
        if status != "000":
            raise DartError(f"{status}: {data.get('message')}")
        out += [Disclosure(r["rcept_no"], r["rcept_dt"], r["corp_code"], r["corp_name"],
                           (r.get("stock_code") or "").strip(), r["report_nm"], r.get("corp_cls", ""),
                           filing_type or "")
                for r in data.get("list", [])]
        if page >= int(data.get("total_page", 1)):
            return out
        page += 1


# document.xml answers with a ZIP whose main file is <rcept_no>.xml, or with a small XML error.
_DECL = re.compile(rb'<\?xml[^>]*encoding=["\']([A-Za-z0-9_-]+)["\']', re.I)
_CELL_END = re.compile(r"</(?:TD|TE|TU|TH)\s*>", re.I)
_ROW_END = re.compile(r"</TR\s*>", re.I)
_BLOCK = re.compile(r"<(?:/?P|/?TITLE|BR|/?TABLE|PGBRK|/?SECTION-\d+|/?COVER|/?LIBRARY|/?BODY)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def _decode(data: bytes) -> str:
    """The declared encoding first; older filings are often cp949 without saying so."""
    m = _DECL.search(data[:300])
    for enc in ([m.group(1).decode()] if m else []) + ["utf-8", "cp949"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def xml_to_text(xml: str) -> str:
    """Plain text. Table rows become 'cell | cell' lines so every number keeps its label."""
    t = _CELL_END.sub(" | ", xml)
    t = _ROW_END.sub("\n", t)
    t = _BLOCK.sub("\n", t)
    t = html.unescape(_TAG.sub("", t))
    lines = (re.sub(r"[ \t 　]+", " ", line).strip(" |") for line in t.splitlines())
    return "\n".join(line for line in lines if line)


def fetch_document(api_key: str, rcept_no: str, fetch: Fetch = _download) -> str:
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
