# 운영 안내

서버(OCI)에서 봇을 처음 켜고, 매일 지켜보고, 문제가 생겼을 때 되살리는 방법입니다.

## 처음 시작

1. 설정 파일을 만들고 키를 넣습니다. 키는 서버의 `.env`에만 둡니다.

   ```bash
   cp .env.example .env && nano .env
   cp watchlist.example.yaml watchlist.yaml && nano watchlist.yaml
   ```

2. 켜기 전에 점검합니다. 외부 서비스마다 가장 적은 호출로 확인하고, 비밀값은 출력하지 않습니다.

   ```bash
   docker compose build
   docker compose run --rm bot python -m dartboard.check
   ```

   | 항목 | 확인하는 것 |
   |---|---|
   | 설정 | 필수 값, 관심 종목, 비어 있는 선택 값 |
   | 저장소 | 데이터 폴더에 쓸 수 있는지, 1단계 성적표 파일이 아닌지, 남은 디스크(2GB 이상) |
   | OpenDART | 최근 3일 주요사항보고서·수시공시·공정공시 건수와 시장 구분 |
   | Discord | 봇 이름, 알림 채널, 백업 채널, 승인자 |
   | AI | 설정된 AI마다 예시 공시 하나를 판단시켜 봄(봇과 같은 요청 형식) |
   | 한국투자 | 일봉 종가가 정규장 종가인지(다음 날 "전일 대비"로 맞춰 봄), 지수 일봉, 휴장일 API |
   | 하트비트 | 점검 신호 한 번 |

   한국투자 토큰을 새로 받을 때마다 알림톡이 옵니다. 받은 토큰은 데이터 폴더에 저장해 하루 동안 다시 씁니다.

3. 모두 통과하면 켭니다.

   ```bash
   docker compose up -d && docker compose logs -f
   ```

   `restart: unless-stopped`라서 설정이 빠진 채 켜면 같은 오류로 계속 다시 시작합니다. 켠 직후에 로그를 봅니다.

## 매일 18:30에 하는 일

1. 기간이 끝난 예측을 채점합니다(한국투자 키가 있을 때).
2. 최근 10일의 지수 일봉과 한국투자 휴장일 API를 맞춰 봅니다. 지수 일봉이 하루 빠지면 그 뒤 청산일이 모두 밀리기 때문입니다.
3. 성적표의 해시 체인을 처음부터 다시 계산하고, 마지막 해시를 Discord에 남깁니다. 이 메시지가 서버 밖에 남은 기준점입니다.
4. 성적 요약을 올립니다(`/report`로 언제든 볼 수 있습니다).
5. 백업을 만들고, 바로 임시 폴더에 복원해 검사한 뒤, 백업 채널에 파일로 올립니다.

## 하트비트

봇은 10분마다(밤과 주말 포함) 바깥 감시 서비스에 신호를 보냅니다. 신호가 끊기면 그 서비스가 알려 줍니다.

1. [healthchecks.io](https://healthchecks.io)에서 체크를 만듭니다. Period 10분, Grace 20분.
2. 체크의 ping 주소를 `.env`의 `HEALTHCHECK_URL`에 넣습니다. 이 주소는 비밀번호처럼 다룹니다.
3. 알림을 받을 방법(이메일 등)은 healthchecks.io에서 정합니다.

감시 중 한 번의 실행이 실패하면 `/fail` 신호를 보냅니다. 하트비트는 봇 프로세스가 살아 있는지를 봅니다. OpenDART나 AI의 일시 오류는 로그와 Discord 메시지로 봅니다.

## 백업과 복구

- 백업 파일에는 `ledger.db`, `state.db`, `checkpoints.db`의 일관된 스냅숏과 매니페스트(파일 해시, 성적표 기록 수, 마지막 해시)가 들어 있습니다.
- 서버의 Docker 볼륨(`/app/data/backups`)에 최근 14개를 두고, 날마다 Discord 백업 채널에 한 개씩 올립니다. 파일이 20 MiB를 넘으면 서버에만 남기고 그렇다고 알립니다.
- 성적표에는 종목별 판단이 들어 있습니다. 백업 채널은 본인만 보는 채널로 둡니다.

손으로 만들고 검사하기:

```bash
docker compose exec bot python -m dartboard.backup create /app/data /app/data/backups
docker compose exec bot python -m dartboard.backup verify /app/data/backups/<파일>
```

### 새 서버로 되살리기

1. 새 서버에 이 저장소를 받고, `.env`와 `watchlist.yaml`을 다시 만듭니다.
2. Discord 백업 채널에서 가장 최근 백업 파일을 받아 저장소 폴더에 둡니다.
3. 빈 폴더로 복원합니다. 이미 파일이 있는 폴더에는 복원하지 않습니다.

   ```bash
   docker compose build
   docker run --rm -v "$PWD:/w" --entrypoint python dartboard:local -m dartboard.backup restore /w/<파일> /w/restored
   ```

4. 출력된 기록 수와 마지막 해시를 Discord에 남은 그날의 해시와 비교합니다.
5. 컨테이너를 켜지 않고 만들기만 해서 볼륨을 준비하고(봇 사용자 권한으로 만들어짐), 복원한 파일을 넣은 뒤 켭니다. 볼륨 이름은 보통 `<저장소 폴더 이름>_dartboard-data`입니다(`docker volume ls`로 확인).

   ```bash
   docker compose create --no-build
   docker run --rm -v dartboard_dartboard-data:/data -v "$PWD/restored:/r:ro" --entrypoint sh dartboard:local \
     -c 'cp /r/*.db /data/'
   docker compose up -d
   ```

   2026-11-13에 같은 서버에서 다른 프로젝트 이름으로 이 순서를 연습했습니다. 기록 2,332개의 해시 체인과 마지막 해시가 원본과 같았습니다.

## 디스크

- 끝난 공시의 LangGraph 체크포인트는 성적표에 기록이 남은 뒤 지웁니다. 사람의 답을 기다리는 공시만 체크포인트를 가집니다.
- 체크포인트에는 공시 원문 중 AI가 읽는 앞부분(기본 12,000자)만 둡니다.
- 성적표는 지우지 않으므로 계속 늘어납니다. 점검 명령이 남은 디스크를 보여 줍니다.
