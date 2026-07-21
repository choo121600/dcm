# DCM 배포 가이드 (24/7 + 원격 관리)

> 🌏 English version: [README.md](./README.md)

DCM은 **아웃바운드 연결만** 사용합니다(Discord 게이트웨이 + Anthropic API) — 인바운드 포트 없음, 웹 서버 없음(ARCHITECTURE.ko.md §14.5).

---

## 하드 cutover 체크리스트

운영 환경 첫 기동 전 아래 항목을 순서대로 확인하십시오.

### 1. .env 필수값 설정

```bash
# WorkingDirectory(/opt/dcm)에서 pydantic-settings가 .env를 자동 로드합니다.
cp .env.example .env
nano .env
chmod 600 /opt/dcm/.env   # 서비스 사용자만 읽을 수 있도록(ARCHITECTURE.ko.md §14.1)
```

**필수(未설정 시 기동 불가):**

| 변수 | 설명 |
|---|---|
| `DISCORD_TOKEN` | Developer Portal → 앱 → Bot → Reset Token |
| `ANTHROPIC_API_KEY` | Anthropic 콘솔 API 키(쉼표 구분 복수 허용) |
| `ADMIN_GUILD_ID` | 슬래시 명령 등록 대상 Discord 서버 ID |
| `ADMIN_ROLE_ID` | 관리 명령 권한을 갖는 역할 ID |

**선택(온보딩 기능 활성화 시 설정):**

| 변수 | 설명 |
|---|---|
| `BOT_NAME` | 봇 표시 이름(기본값: 지우) |
| `WELCOME_CHANNEL_ID` | 신규 멤버 환영 메시지를 보낼 채널 ID |
| `WELCOME_MESSAGE` | 환영 메시지 텍스트 |
| `DEFAULT_ROLE_ID` | 신규 멤버에게 자동 부여할 역할 ID |

### 2. Discord Developer Portal — Privileged Intent 활성화

Developer Portal → 앱 → Bot → **Privileged Gateway Intents** 에서 아래 두 항목을 반드시 켜십시오.

- ✅ **Message Content Intent** — 멘션 본문 파싱에 필요
- ✅ **Server Members Intent** — `on_member_join` 발화 및 온보딩에 필요

> **경고:** 두 인텐트는 코드가 항상 요청하므로, Developer Portal에서 비활성 상태면 봇은
> 기동 시 `PrivilegedIntentsRequired` 예외로 **즉시 실패**(systemd 재기동 루프)합니다 — 무음 실패가 아니라 시끄럽게 실패합니다.
> 정상 기동 시 `on_ready`가 활성 인텐트를 로그로 출력하므로 §5에서 확인하십시오.

### 3. 봇 역할·권한 최소권한 원칙

Discord 서버에 봇을 초대할 때 **Administrator 권한을 절대 부여하지 마십시오.**

**Administrator 금지** — 토큰 탈취 시 서버 전체가 위험에 노출됩니다(ARCHITECTURE.ko.md §14.6).

최소 필요 권한:

- Manage Channels
- Manage Roles
- Kick Members
- Ban Members
- Moderate Members (타임아웃)

**봇 역할을 관리 대상 역할 위로 드래그**하십시오. Discord 역할 계층상 봇 역할보다 높은 역할은 조작할 수 없습니다.

### 4. 토큰 회전

cutover 직전, Developer Portal → 앱 → Bot → **Reset Token** 으로 봇 토큰을 갱신하고 `.env`의 `DISCORD_TOKEN`을 교체한 뒤 서비스를 재시작하십시오. 유출 의심 시 즉시 회전.

```bash
sudo systemctl restart dcm
```

### 5. 라이브 길드 기동 스모크 (운영자 수동 단계)

> 이 단계는 **운영자 DISCORD_TOKEN**이 필요한 인간 수동 확인 단계입니다.
> CI/오프라인 환경에서는 자동화할 수 없습니다.

기동 후 확인:

```bash
journalctl -u dcm -f   # 실시간 로그 스트림
```

on_ready 로그에서 아래 두 줄을 확인하십시오:

```
privileged intent message_content=True
privileged intent members=True
```

정상이면 두 줄이 모두 출력되고 봇이 `on_ready`에 도달합니다(= 두 privileged 인텐트 활성 확인). 로그가 보이지 않고 `PrivilegedIntentsRequired` 예외와 재기동 루프가 나타나면, Developer Portal에서 해당 인텐트(특히 Server Members)를 활성화하고 재기동하십시오.

---

## 1. 호스트 초기 설정

```bash
sudo useradd --system --create-home --home-dir /opt/dcm dcm
sudo -u dcm git clone <repo> /opt/dcm
cd /opt/dcm
sudo -u dcm python3 -m venv .venv
sudo -u dcm .venv/bin/pip install -e .

sudo -u dcm cp .env.example .env
sudo -u dcm nano .env          # 위 체크리스트 §1 참고
sudo chmod 600 /opt/dcm/.env
sudo -u dcm mkdir -p /opt/dcm/data
```

## 2. 서비스 설치

```bash
sudo cp deploy/dcm.service /etc/systemd/system/dcm.service
sudo systemctl daemon-reload
sudo systemctl enable --now dcm   # 즉시 기동 + 부팅 시 자동 기동
```

## 3. 원격 관리

```bash
systemctl status dcm
journalctl -u dcm -f              # 실시간 로그(키는 §14.1에 의해 로그에 노출 안 됨)
sudo systemctl restart dcm        # .env 변경 또는 코드 업데이트 후

# 업데이트:
cd /opt/dcm && sudo -u dcm git pull && sudo -u dcm .venv/bin/pip install -e . \
  && sudo systemctl restart dcm
```

## 4. 로컬 우선 LLM 라우팅 (선택)

노트북이 **켜져 있을 때는 노트북의 프록시로** LLM을 태우고, **꺼져 있을 때만** 서버의
`ANTHROPIC_API_KEY`로 폴백합니다(ARCHITECTURE.ko.md §9.1). 봇 자체는 **이 서버에 단일 인스턴스로**
유지하십시오 — 노트북에 두 번째 라이브 봇을 띄우지 **마십시오**(토큰당 Discord 게이트웨이 세션 1개;
writer가 둘이면 이중 응답 + SQLite 상태 분리, §6).

**동작 방식.** `LLMClient`는 크리덴셜을 순서대로 시도하고 *연결 실패를 포함한* 모든 오류에서 다음으로
폴백합니다. `PREFER_PROXY=true`면 프록시 크리덴셜이 **맨 앞**에 놓입니다:

- 노트북 **켜짐** → 프록시가 응답(저렴/로컬).
- 노트북 **꺼짐** → 프록시 연결이 빠르게 실패(짧은 `PROXY_CONNECT_TIMEOUT`)하고 호출은
  `ANTHROPIC_API_KEY`로 폴백하며, 서킷 브레이커가 `PROXY_BREAKER_COOLDOWN`초 동안 프록시를 건너뛰어
  이후 메시지는 메시지당 지연 없이 곧장 API 키로 갑니다.

### 4.1 노트북 — Tailscale로 프록시 실행

프록시는 [Tailscale](https://tailscale.com)로 이 서버에서 접근되므로 노트북은 **LAN 인바운드 포트를
열지 않습니다.** 바로 쓸 수 있는 compose 스택(프록시 + Tailscale 사이드카)이
[`deploy/local-proxy/`](./local-proxy/)에 있습니다:

```bash
cd deploy/local-proxy
cp .env.example .env          # TS_AUTHKEY + PROXY_IMAGE(+ 프록시 자체 설정) 채우기
docker compose up -d
docker compose exec tailscale tailscale ip -4   # tailnet IP 확인(또는 MagicDNS 이름 사용)
```

> 프록시는 포트 `8787`에서 **Anthropic Messages API**(`/v1/messages`)를 말해야 하며 `MODEL`과
> `INGEST_MODEL`을 모두 처리해야 합니다. **정책 주의(§9.1):** 두 번째 실제 키나 Bedrock/Vertex
> 게이트웨이를 두는 것은 안전하지만, 상시 가동 봇에 Claude 구독 토큰을 씌우는 것은 Anthropic 사용
> 정책 위반이 될 수 있습니다 — `PROXY_IMAGE`에 무엇을 두는지는 본인 판단입니다.

### 4.2 서버 — 프록시 우선 설정

`/opt/dcm/.env`에 추가하고 재시작하십시오:

```bash
PREFER_PROXY=true
FALLBACK_BASE_URL=http://dcm-local-proxy:8787   # MagicDNS 이름, 또는 http://<노트북-tailnet-ip>:8787
FALLBACK_API_KEY=                               # 프록시가 요구하는 토큰; 비우면 ANTHROPIC_API_KEY 재사용
# PROXY_CONNECT_TIMEOUT=2.0                      # 빠른 폴백용 connect 상한(기본 2초)
# PROXY_BREAKER_COOLDOWN=30                      # 연결 실패 후 프록시를 N초 건너뜀
```

```bash
sudo systemctl restart dcm
journalctl -u dcm -f   # 기동 로그에 "LLM routing: local proxy preferred, API key fallback" 출력
```

`PREFER_PROXY`를 설정하지 않으면(또는 `false`) `FALLBACK_BASE_URL`이 실제 키 오류 이후에만 시도되는
**최후 수단** 엔드포인트로 동작하는 기존 방식이 유지됩니다.

## 비고

- **24/7은 호스트의 책임**: 서버가 절전 모드로 전환되지 않도록 하십시오. systemd가 충돌/재부팅 시 봇을 재시작합니다.
- **인바운드 포트 없음**: 봇은 아웃바운드 연결만 사용합니다. 방화벽 규칙 추가 불필요.
- **메트릭/헬스 엔드포인트 추가 시**: `0.0.0.0`이 아닌 내부 주소에만 바인딩하여 "인바운드 포트 없음" 원칙을 유지하십시오(ARCHITECTURE.ko.md §14.5).
