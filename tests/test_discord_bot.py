from dartboard.discord_bot import alert_embed
from dartboard.faithfulness import FaithfulnessReport


def test_embed_shows_verification_and_links_to_the_filing():
    e = alert_embed("테스트전자", "자기주식취득결정", "요약 본문", FaithfulnessReport(3, 2, ["3.2%"]), "buy",
                    "20261103000247")
    fields = {f.name: f.value for f in e.fields}
    assert fields["숫자 검증"] == "2/3 원문과 일치"
    assert fields["원문에서 못 찾은 숫자"] == "3.2%"
    assert e.url.endswith("rcpNo=20261103000247")


def test_fully_verified_summary_has_no_warning_field():
    e = alert_embed("테스트전자", "공시", "요약", FaithfulnessReport(2, 2, []), None, "1")
    assert "원문에서 못 찾은 숫자" not in {f.name for f in e.fields}
