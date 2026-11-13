# 집 PC(RTX 4090) 준비 순서

집 PC에서 vLLM을 띄우고, 서버만 그 PC의 8000번 포트에 닿게 하는 순서입니다. PC 쪽 작업은 PC 앞에서 하고, 서버 쪽 작업(5단계)은 서버 관리자 권한이 필요합니다.

## 미리 확인

| 항목 | 기준 | 확인 방법 |
|---|---|---|
| NVIDIA 드라이버 | 580 이상(vLLM v0.33.0 이미지가 CUDA 13) | `nvidia-smi` |
| Windows | 10 22H2(빌드 19045) 또는 11 23H2(빌드 22631) 이상 | `winver` |
| WSL | 2.1.5 이상 | `wsl --version`, 낮으면 `wsl --update` |
| Docker Desktop | WSL 2 백엔드, GPU 시험 통과 | `docker run --rm -it --gpus=all nvcr.io/nvidia/k8s/cuda-sample:nbody nbody -gpu -benchmark` |

## 순서

1. **Tailscale 설치(PC)**: Windows용 앱을 설치하고 로그인한 뒤 PC 주소를 적어 둡니다: `tailscale ip -4`. 늘 켜 둘 PC라면 관리 화면에서 이 기기의 키 만료(180일)를 끕니다.
2. **접근 정책**: Tailscale 관리 화면의 Access controls에 [`deploy/tailscale/policy.hujson`](../deploy/tailscale/policy.hujson)을 붙여 넣고, `home-pc` 주소를 1단계의 주소로 바꿔 저장합니다. 정책 속 시험(서버 → PC 8000 허용, 22·3389 거부)이 실패하면 저장되지 않습니다.
3. **vLLM 설정(PC)**: `deploy/vllm/.env.example`을 `.env`로 복사하고 두 값을 바꿉니다.
   - `VLLM_API_KEY`: 길고 무작위인 값
   - `VLLM_BIND_ADDR`: 1단계의 Tailscale 주소. 기본값 `127.0.0.1`이면 PC 자신만 닿습니다.
4. **vLLM 시작(PC)**: `cd deploy/vllm && docker compose up -d`. 로그의 `GPU KV cache size` 줄을 확인합니다. 그다음 PC에서 시험합니다.

   ```powershell
   curl.exe -H "Authorization: Bearer <VLLM_API_KEY>" http://<PC Tailscale 주소>:8000/v1/models
   ```

5. **Tailscale 설치(서버, 관리자 권한)**: 공식 설치 스크립트로 설치하고 `tag:server` 꼬리표를 붙여 로그인합니다. 꼬리표는 2단계 정책에 `tagOwners`가 있어야 붙습니다.

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up --advertise-tags=tag:server
   ```

6. **Windows 방화벽(PC, 관리자 PowerShell)**: 8000번을 서버의 Tailscale 주소에서만 받습니다.

   ```powershell
   New-NetFirewallRule -DisplayName "vLLM from dartboard server" -Direction Inbound -Protocol TCP -LocalPort 8000 -RemoteAddress <서버 Tailscale 주소> -Action Allow
   ```

   Docker Desktop이 설치하면서 자체 허용 규칙을 만들 수 있습니다. 그래서 7단계의 "다른 기기에서 막히는지"가 진짜 확인입니다.
7. **막혔는지 시험**:
   - 서버에서 `curl -H "Authorization: Bearer <VLLM_API_KEY>" http://<PC Tailscale 주소>:8000/v1/models` → 모델 목록이 나와야 합니다.
   - Tailscale에 연결한 다른 기기(휴대폰 등)에서 같은 주소 → 연결되지 않아야 합니다.
   - 같은 공유기의 다른 기기에서 PC의 공유기 쪽 주소 `:8000` → 연결되지 않아야 합니다.
8. **서버 설정**: 서버의 `.env`에 아래 값을 넣고 점검한 뒤 다시 시작합니다.

   ```
   LOCAL_LLM_URL=http://<PC Tailscale 주소>:8000/v1
   LOCAL_LLM_KEY=<VLLM_API_KEY와 같은 값>
   LOCAL_LLM_MODEL=local
   ```

   ```bash
   docker compose run --rm bot python -m dartboard.check
   docker compose up -d
   ```

## 운영 팁

- 관심 종목이 아닌 회사의 판단(종목군)은 집 PC 모델로만 합니다. PC가 꺼져 있으면 쌓였다가 켜지면 한 번에 30건씩 처리합니다.
- 진입은 "판단이 기록된 뒤 첫 시가"입니다. 장 시작(09:00) 전에 PC가 하루 한 번만 켜져 있어도 밤사이 공시가 공식 채점에 들어갑니다. 공시 접수일 다음 첫 거래일을 넘겨 판단하면 기록은 하되 공식 채점에서 빠집니다.
