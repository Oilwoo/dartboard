# 검증 기록

이 프로젝트의 설계 결정은 아래 측정과 확인에 근거합니다. (2026-11-02 ~ 11-03, Linux 클라우드 환경)

## 직접 측정

| 항목 | 방법 | 결과 |
|---|---|---|
| LLM 자동 전환 (라이브러리 기본 설정) | 가짜 OpenAI 호환 서버로 "PC 꺼짐 + Groq 429" 재현 | **61.34초** (SDK가 Retry-After 30초를 두 번 기다림) |
| LLM 자동 전환 (재시도 0회, 연결 대기 1.5초) | 같은 조건 | PC 켜짐 0.05초 · PC 꺼짐 0.03초 · PC 절전 1.52초 · 전부 실패 시 즉시 None |
| 파일 MCP 서버 | MCP 클라이언트로 도구 호출 | 허용 폴더 밖 `/etc/passwd` 읽기 차단 확인 |
| Playwright MCP | 헤드리스 크롬으로 테스트 페이지 열기 | 페이지 속 가격 읽기 성공 |
| changedetection.io | 1초 간격 감시, 가격 3회 변경 | 알림 도착 1.91초 · 1.82초 · 0.74초 |
| 해외 AI 직군 공고 7,466건 | Built In 원문 직접 집계 | LangChain 23.1% · LangGraph 14.8% · vLLM 2.1% · Ollama 0.4% · llama.cpp 0.2% |
| 1단계 테스트 | 가짜 DART·LLM·Discord·KIS로 전체 흐름 | 70개 통과 |
| 테스트가 버그를 잡는지 | 고친 버그 두 개를 일부러 되살림 | 둘 다 테스트 실패로 잡힘 (토큰 시간대, 재시작 시 처음부터 다시 실행) |
| 봇 설치·기동 | 새 가상환경에 `pip install .` 후 Discord 로그인만 막고 기동 | DB 3개 생성, `/report` 등록, 10분 감시·18:30 채점 예약 확인. 설정이 없으면 빠진 키 이름을 알려주고 종료 |

## 공식 문서·소스에서 확인

- OpenDART `list.json`: 회사 코드 없이 조회하면 검색 기간 3개월 제한. 상태 코드 `000` 정상, `013` 데이터 없음, `020` 요청 한도 초과(하루 약 2만 건)
- vLLM 0.33.0 (2026-11-02): Windows 직접 지원 없음(WSL 또는 Docker), GPU 연산 능력 7.5 이상, OpenAI 호환 API, `--api-key`/`VLLM_API_KEY`, 도구 호출은 `--enable-auto-tool-choice`와 `--tool-call-parser` 필요(Qwen2.5는 `hermes`)
- LangGraph 1.2.19: `interrupt()`와 `Command(resume=...)`로 사람 승인 후 재개, 체크포인터로 상태 저장
- Discord 상호작용: 3초 안에 1차 응답, 토큰은 15분 유효
- OpenDART `document.xml`: 성공하면 ZIP(본문 `<접수번호>.xml` + 첨부), 실패하면 `<status>`·`<message>`가 든 XML (OpenDartReader 구현으로 확인)
- KIS Open API (koreainvestment/open-trading-api 공식 예제, 2026-11-03):
  - 토큰 `POST /oauth2/tokenP`, 만료 시각 `access_token_token_expired`는 한국 시간
  - 종목 일봉 `inquire-daily-itemchartprice`(`FHKST03010100`), `FID_ORG_ADJ_PRC=0`이 수정주가, 한 번에 최대 100건
  - 지수 일봉 `inquire-daily-indexchartprice`(`FHKUP03500100`), 지수 코드 코스피 `0001` · 코스닥 `1001` · 코스피200 `2001`
  - 성공 판정 `rt_cd == "0"`, 예제의 호출 간격 실전 0.05초 · 모의 0.5초
- langgraph-checkpoint-sqlite 3.1.3: `AsyncSqliteSaver.from_conn_string()`, 의존성 sqlite-vec 0.1.9에 ARM(aarch64) 휠이 있어 OCI ARM에서 설치 가능
- Discord 봇 설치(공식 문서): 개발자 포털 **Installation**에서 Guild Install에 `bot`·`applications.commands` 범위 지정 후 설치 링크 사용
- 3차 상법 개정: 2026-03-06 공포·시행, 새로 취득한 자사주는 1년 내 소각, 기존 보유분은 1년 6개월 유예

## 구현 중 발견해 고친 문제

- **KIS 토큰 시간대**: KIS는 만료 시각을 한국 시간으로 줍니다. 서버 시계(OCI 기본 UTC)와 그대로 비교하면 이미 만료된 토큰을 최대 9시간 계속 씁니다. 시간대를 붙여 비교하도록 고치고 테스트로 고정했습니다.
- **YAML 종목코드**: 일반 YAML 읽기는 `000660`(SK하이닉스)을 8진수 숫자 432로 바꿉니다. `005930`은 9가 있어 8진수가 아니라서 멀쩡하기 때문에 눈치채기 어렵습니다. 모든 값을 글자 그대로 읽도록 바꿨습니다.

## 교차 검증

Fable 5.1은 크레딧 부족(HTTP 429)으로 실행되지 않아 Sonnet 5.5로 같은 검토를 받았습니다.
그 결과로 철회한 주장: 국내 CB 연구의 구체 수치와 "리픽싱은 일관되게 부정적"이라는 표현, 밸류업 효과 수치.
