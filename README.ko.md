# Discord Community Manager (dcm)

[![CI](https://github.com/choo121600/dcm/actions/workflows/ci.yml/badge.svg)](https://github.com/choo121600/dcm/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Code of Conduct](https://img.shields.io/badge/Contributor%20Covenant-2.1-4baaaa.svg)](./CODE_OF_CONDUCT.md)

[English](./README.md) · **한국어**

24시간 상시 운영되는 Discord 커뮤니티 관리 봇입니다 — 서버를 관리하고(온보딩, 역할,
채널, 모더레이션) 설정 가능한 페르소나로 대화합니다(기본값 **썩스가재**;
`@썩스가재`로 멘션하면 캐릭터에 맞게 답변합니다).
사람처럼 **사소한 것은 잊으면서** **기억하고 성장하도록** 설계되었습니다.

- 아키텍처 및 로드맵: [`ARCHITECTURE.md`](./ARCHITECTURE.md) (한국어: [`ARCHITECTURE.ko.md`](./ARCHITECTURE.ko.md))
- 페르소나: [`persona.md`](./persona.md)
- 기여: [`CONTRIBUTING.md`](./CONTRIBUTING.md) · 변경 이력: [`CHANGELOG.md`](./CHANGELOG.md)

> **상태:** M1–M4 구현 완료 — 봇은 대화하고, 기억하며(중요도 가중 회상), 잊고
> (시간 감쇠 + 가지치기, 그리고 `잊어줘` 명령어), 성장합니다(회고 → 의미/자기 기억).
> M5 다듬기는 일부만 완료되었습니다. 로드맵은 ARCHITECTURE.md §11을 참고하세요.

## Setup

### 1. Discord 봇 생성
1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**으로 이동합니다.
2. **Bot** 탭 → **Reset Token** → 토큰을 복사합니다(`.env`에 들어갑니다).
3. **Message Content Intent를 활성화합니다**(Bot 탭 → Privileged Gateway Intents).
   이것이 없으면 봇은 메시지 텍스트를 읽지 못해 응답할 수 없습니다. 가장 흔한 설정 실수입니다.
4. **OAuth2 → URL Generator**:
   - **채팅 전용:** 스코프 `bot`; 권한 **View Channels**, **Send Messages**, **Read Message History**.
   - **서버 관리 포함:** 스코프 `applications.commands`와 권한 **Manage Channels** + **Manage Roles**를 추가합니다. **Administrator는 절대 부여하지 마세요**(최소 권한 원칙 — ARCHITECTURE.md §14.6–§14.7). 봇의 역할을 봇이 관리해야 할 역할들보다 *위로* 드래그하세요.
   생성된 URL을 열어 봇을 초대합니다. 서버 관리 슬래시 명령어는 **관리자 전용**이며(호출자가 **Manage Guild** 권한을 가지고 있어야 합니다) `ADMIN_GUILD_ID`에 등록됩니다.

### 2. 설치
Python 3.11+ 가 필요합니다.

```bash
git clone <repo> dcm && cd dcm
uv sync            # or: pip install -e .
cp .env.example .env
chmod 600 .env     # keep secrets readable only by you (ARCHITECTURE.md §14.1)
```

### 3. `.env` 설정
```dotenv
DISCORD_TOKEN=...            # from step 1
ANTHROPIC_API_KEY=sk-ant-... # one key, or comma-separated for a key pool
BOT_NAME=썩스가재             # to rename: change this AND the bot's username in the portal
ADMIN_GUILD_ID=...           # server (guild) id for admin slash-command registration (right-click server → Copy Server ID)
```

### 4. 실행
```bash
uv run dcm       # or: python -m dcm
```
`썩스가재 online …` 메시지와 Discord의 녹색 상태 표시가 보여야 합니다. 그런 다음 서버에서:
```
@썩스가재 안녕
```

## 24시간 상시 운영
`uv run dcm`은 터미널을 닫으면 중단됩니다. 항상 켜 두려면 다음 중 하나를 선택하세요:
- **홈 서버 / Raspberry Pi**: `systemd` 서비스(크래시 시 자동 재시작).
- **클라우드**: fly.io / Railway / `Dockerfile`을 통한 소규모 VPS.
어느 쪽이든: 크래시 시 재시작하고, (M2부터는) SQLite 파일을 영구 볼륨에 두세요.

## 서버 템플릿 (`/setup-server`)
단일 **YAML 또는 JSON** 파일로 서버 전체를 설정합니다 — 역할(권한 포함), 카테고리,
텍스트/음성 채널. 관리자 전용 `/setup-server` 슬래시 명령어를 실행하고 템플릿을 첨부하면,
봇이 미리보기를 보여주고 확인 후 적용합니다. 다시 실행해도 안전합니다
(**멱등성**: 이미 존재하는 역할/카테고리/채널은 건너뜁니다). **전체 가이드:**
[docs/server-templates.md](docs/server-templates.md) — 스키마, 권한 이름, 제한,
바로 사용할 수 있는 예제(YAML & JSON).

```yaml
roles:
  - name: 운영진
    permissions: [manage_channels, manage_roles, kick_members]
categories:
  - name: 2026-summer
    private: true            # visible only to the visible_to roles
    visible_to: [운영진]
    channels:
      - { name: 공지, type: text }
      - { name: 회의, type: voice }
```

일회성 변경은 자연어로 요청할 수도 있습니다(예: `썩스가재야 2026-summer 카테고리 만들어줘`).

## 테스트
기억 코어와 망각에 대한 오프라인 테스트(키/네트워크 불필요):
```bash
PYTHONPATH=src python tests/test_memory.py
PYTHONPATH=src python tests/test_forgetting.py
```

## 보안 참고 사항
- `.env`는 git에서 무시됩니다 — 실제 비밀 값을 절대 커밋하지 마세요. 키는 로그에 기록되지 않습니다.
- 봇은 아웃바운드 연결만 수행합니다(인바운드 포트 / 웹 서버 없음).
- 필요한 채널에만 초대하세요. 전체 보안 모델은 ARCHITECTURE.md §14를 참고하세요.

## 현지화
봇의 언어는 설정할 수 있습니다. 사용자에게 노출되는 문자열은 `src/dcm/i18n/locales/`에 있으며
(`en.yaml`, `ko.yaml` 및 `en/`, `ko/` 아래의 네임스페이스별 조각 파일), `BOT_LOCALE`로 선택합니다
(기본값 `ko`). 언어를 추가하려면 해당 로케일의 파일을 복사해 값을 번역하고 `BOT_LOCALE`을 그 코드로
설정하세요. 설계는 [`ARCHITECTURE.md`](./ARCHITECTURE.md) §10을 참고하세요.

## 커스터마이징
일부 항목은 의도적으로 배포 환경에 특화되어 있습니다 — 여러분의 커뮤니티에 맞게 교체하세요.
- **`persona.md`** — 봇의 캐릭터(예시 대사는 페르소나의 언어로 작성).
- **`knowledge.md`** — 프롬프트에 주입되는 서버/커뮤니티 정적 지식(`KNOWLEDGE_FILE`).
- `src/dcm/service/study_lookup.py`의 스터디 이름 매칭은 특정 커뮤니티 데이터에 맞춰져 있습니다.

## 기여
기여를 환영합니다! 로컬 설정, 우리가 실행하는 검사(`pytest` + `ruff`), 프로젝트 규칙은
[`CONTRIBUTING.md`](./CONTRIBUTING.md)를 참고하세요. 또한
[Code of Conduct](./CODE_OF_CONDUCT.md)도 읽어 주세요.

## 라이선스
[MIT](./LICENSE) © Yeonguk
