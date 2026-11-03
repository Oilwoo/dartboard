from dartboard.faithfulness import check

SOURCE = """
1. 취득예정주식수 : 1,250,000주
2. 취득예정금액 : 87,500,000,000원
3. 자기주식 취득 목적 : 주주가치 제고 및 소각
4. 발행주식총수 대비 비율 : 2.3%
5. 취득 예상기간 : 2026년 11월 4일 ~ 2027년 2월 3일
6. 전년 대비 영업이익 증감률 12.5%
"""


def test_all_numbers_supported():
    s = "자기주식 1,250,000주를 87,500,000,000원에 취득하며, 발행주식총수의 2.3%에 해당합니다."
    rep = check(s, SOURCE)
    assert rep.checked == 3 and rep.score == 1.0


def test_wrong_number_is_flagged():
    s = "발행주식총수의 3.2%에 해당하는 1,250,000주를 취득합니다."
    rep = check(s, SOURCE)
    assert rep.unsupported == ["3.2%"]
    assert rep.score == 0.5


def test_spelled_out_percent_and_trailing_zero():
    assert check("영업이익이 12.50퍼센트 늘었습니다.", SOURCE).score == 1.0


def test_years_and_bare_numbers_are_not_judged():
    rep = check("2026년 11월부터 3단계로 진행합니다.", SOURCE)
    assert rep.checked == 0 and rep.score == 1.0


def test_converted_units_are_flagged_for_review_not_guessed():
    # 875억원 equals 87,500,000,000원, but conversions are left to a human or a later model check
    rep = check("취득금액은 875억원입니다.", SOURCE)
    assert rep.unsupported == ["875억"]
