import io
import zipfile

import pytest

from dartboard.dart import DartError, RateLimited, fetch_document, xml_to_text

RCEPT = "20261103000247"
MAIN = """<?xml version="1.0" encoding="utf-8"?>
<DOCUMENT><BODY><SECTION-1><TITLE>자기주식 취득 결정</TITLE>
<TABLE><TR><TD>1. 취득예정주식(주)</TD><TD>보통주식</TD><TD>1,250,000</TD></TR>
<TR><TD>2. 취득예정금액(원)</TD><TD>보통주식</TD><TD>87,500,000,000</TD></TR></TABLE>
<P>이사회 결의일 &amp; 비고</P><P>   </P></SECTION-1></BODY></DOCUMENT>"""


def zipped(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_table_rows_keep_their_labels():
    lines = xml_to_text(MAIN).splitlines()
    assert "1. 취득예정주식(주) | 보통주식 | 1,250,000" in lines
    assert "이사회 결의일 & 비고" in lines
    assert "" not in lines


def test_main_document_is_read_and_attachments_ignored():
    urls = []
    data = zipped({f"{RCEPT}_00760.xml": "<P>첨부 감사보고서</P>".encode(), f"{RCEPT}.xml": MAIN.encode()})
    text = fetch_document("key", RCEPT, fetch=lambda url: urls.append(url) or data)
    assert "87,500,000,000" in text and "첨부" not in text
    assert "document.xml" in urls[0] and f"rcept_no={RCEPT}" in urls[0]


def test_declared_legacy_encoding_is_decoded():
    xml = '<?xml version="1.0" encoding="euc-kr"?><P>매출액 1,000억원</P>'.encode("cp949")
    assert fetch_document("key", "1", fetch=lambda url: zipped({"1.xml": xml})) == "매출액 1,000억원"


def test_undeclared_cp949_falls_back():
    xml = "<P>영업이익 250억원</P>".encode("cp949")
    assert fetch_document("key", "1", fetch=lambda url: zipped({"1.xml": xml})) == "영업이익 250억원"


def test_error_reply_raises_with_status_and_message():
    err = ('<?xml version="1.0" encoding="UTF-8"?><result><status>014</status>'
           '<message>파일이 존재하지 않습니다.</message></result>').encode()
    with pytest.raises(DartError, match="014: 파일이 존재하지 않습니다"):
        fetch_document("key", "1", fetch=lambda url: err)


def test_rate_limit_reply_raises_rate_limited():
    err = b"<result><status>020</status><message>limit</message></result>"
    with pytest.raises(RateLimited):
        fetch_document("key", "1", fetch=lambda url: err)
