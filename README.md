# 🎯 dartboard

**DART 공시를 읽고, 모든 판단에 성적표를 남기는 투자 리서치 에이전트**

이름은 두 가지 뜻입니다. 금융감독원 전자공시 시스템 **DART**를 읽는 **보드**이고, 다트판처럼 판단이 과녁에 맞았는지 하나하나 기록하는 **점수판**입니다.

> 수익을 "내는" AI가 아니라 수익을 **"검증하는"** AI를 만듭니다.
> LLM이 예측한 수익률은 과거 데이터에서 부풀려지기 쉽습니다(미래 정보 누출). 그래서 이 프로젝트는 모든 판단을 만들어진 순간에 고정해 기록하고, 시간이 지난 뒤 시장 대비로 채점합니다.

## 무엇을 하나

1. **공시 요약**: 새 공시를 Qwen으로 요약합니다.
2. **숫자 검증**: 요약 속 숫자가 공시 원문에 실제로 있는지 확인합니다. 없으면 멈추고 Discord로 사람에게 묻습니다.
3. **예측 성적표**: 의견(매수·매도·관망)을 시점 고정으로 기록하고, 기간이 지나면 시장 대비 초과수익으로 채점합니다. DB가 기록의 수정·삭제 명령을 거부하고, 기록마다 앞 기록의 해시를 이어 붙여서 파일을 몰래 고치면 드러나게 했습니다(변조 감지). 다만 파일을 통째로 다시 쓰는 것까지 막지는 못합니다.
4. **통제된 실행**: 돈이 걸린 동작은 LLM이 아니라 코드로 된 정책 엔진이 결정합니다. 1회·하루·시간당 한도, 중복 차단, 긴급 정지를 지원합니다.

## 구성

```
[데이터]   OpenDART(공시 목록·원문) · KIS Open API(수정주가·지수)
    ↓
[OCI 서버, 24시간]  LangGraph 워크플로 · 정책 엔진 · 성적표 DB · Discord 봇
    ↓  Tailscale
[4090 PC]           vLLM + Qwen (OpenAI 호환 API)
    ↓  PC가 꺼져 있으면 1.5초 안에 전환
[클라우드 예비]      Groq → Gemini
```

| 층 | 선택 |
|---|---|
| 모델 | Qwen |
| 추론 엔진 | vLLM (Docker) |
| 에이전트 | LangGraph |
| 알림·승인 | Discord |
| 저장 | SQLite (append-only) |

## 진행 단계

- [x] **0. 기반**: 정책 엔진, LLM 자동 전환, 성적표, 숫자 검증, LangGraph 1단계 흐름, Discord 알림·승인 (테스트 34개)
- [x] **1. 봇 품질 보증**: 관심 종목 공시 감시, 공시 원문 수집(`document.xml`), KIS 수정주가로 자동 채점(다음 거래일 시가 진입, 시장 지수 대비), Discord 봇(`/report`), 재시작해도 승인 대기와 기록이 이어지는 체크포인트, 서버용 Docker (테스트 71개)
- [ ] **2. 보유 종목 악재 경보**: 감사보고서 지연, CB 리픽싱, 전환 주식 추가상장, 최대주주 지분 변동. PR-AUC·리드타임으로 평가
- [ ] **3. 공시 유형별 기저율**: 자사주·CB·유상증자·감사 관련·내부자 매매 5개 유형의 과거 반응 분포
- [ ] 연구 트랙: Lazy Prices 한국판 (사업보고서 문장 변화와 이후 수익률)

## 1단계 흐름

```
10분마다 (평일 07~21시)   OpenDART에서 오늘 공시 목록 → 관심 종목만 골라냄
        ↓                 (그날 첫 실행은 최근 3일을 다시 훑어 주말·심야 공시도 놓치지 않음)
        ↓                 (임원·주요주주 소유상황보고서와 사업·반기·분기보고서는 읽지 않고 건너뜀)
공시 원문 내려받기 → Qwen 요약 → 숫자 검증
        ↓ 숫자가 전부 원문에 있음          ↓ 원문에 없는 숫자가 있음
성적표에 기록 → Discord 알림     Discord [승인]/[거절] → 승인 시에만 기록 (30분 무응답 = 거절)
        ↓
매일 18:30   기간이 끝난 의견을 KIS 수정주가로 채점 → 결과를 Discord에 게시 (/report로 언제든 조회)
```

공시 하나가 LangGraph 스레드 하나이고, 단계마다 체크포인트에 저장됩니다. 그래서 봇이 중간에 죽거나 Discord 전송이 실패해도 끝난 단계(성적표 기록 등)는 다시 하지 않고 멈춘 곳부터 이어갑니다. 다만 성적표에 쓴 직후, 그 단계가 끝났다는 저장 전에 죽으면 같은 기록이 두 번 남을 수 있습니다. 이 틈을 막는 중복 방지는 다음 설계에서 다룹니다.

## 시작하기

```bash
pip install -e ".[dev]"
pytest
```

### 서버(OCI)에서 봇 실행

1. 키 준비. 키는 서버의 `.env`에만 넣습니다.
   - OpenDART 인증키: [opendart.fss.or.kr](https://opendart.fss.or.kr)
   - KIS 앱키·시크릿: [KIS Developers](https://apiportal.koreainvestment.com) (한국투자증권 계좌 필요, 없으면 채점만 꺼짐). 돈과 주식이 없는 계좌의 실전 키를 권장합니다. 모의투자 키로 받은 시세는 빠지거나 부정확할 수 있다고 한국투자가 안내합니다.
   - Discord 봇: [개발자 포털](https://discord.com/developers/applications) → 앱 생성 → **Bot**에서 토큰 발급 → **Installation**에서 Guild Install에 `bot`·`applications.commands` 범위와 `View Channels`·`Send Messages`·`Embed Links` 권한 지정 → 설치 링크로 내 서버에 추가
   - 채널 ID·내 사용자 ID: Discord 설정 → 고급 → 개발자 모드 켜기 → 채널/내 이름 우클릭 → ID 복사
2. 실행

```bash
git clone https://github.com/Oilwoo/dartboard && cd dartboard
cp .env.example .env
nano .env
cp watchlist.example.yaml watchlist.yaml   # 실제 종목은 이 파일에만 적습니다(git이 무시함)
nano watchlist.yaml
docker compose up -d --build
docker compose logs -f
```

3. 백업: 성적표·체크포인트는 Docker 볼륨에 있습니다.

```bash
docker compose cp bot:/app/data ./backup
```

vLLM (4090 PC):

```bash
cd deploy/vllm
cp .env.example .env
docker compose up -d
```

## 원칙

- **본인 계좌 전용.** 타인에게 유료로 종목 신호를 주면 유사투자자문업 신고, 타인 계좌를 자동매매하면 투자일임업 등록 대상입니다.
- **비밀값과 보유 종목은 커밋하지 않습니다.** `.env`, 실제 관심 종목(`watchlist.yaml`), 백업 폴더(`backup/`)는 `.gitignore`에 있습니다.
- **검증 근거**는 [docs/verification.md](docs/verification.md)에 있습니다.
