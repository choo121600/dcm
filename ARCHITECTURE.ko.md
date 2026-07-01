# 아키텍처 — Discord Community Manager (dcm)

> 🌏 English version: [ARCHITECTURE.md](./ARCHITECTURE.md)
>
> 이 문서는 dcm의 설계 참조 문서입니다. 섹션 앵커(예: `§14.1`)는 `src/` 전반의 코드
> 주석에서 인용되므로, 문서를 편집할 때 번호 체계를 안정적으로 유지해 주십시오.

## 1. Overview

dcm는 24/7 상시 가동되는 Discord 커뮤니티 관리 봇입니다. 두 가지 역할을 동시에 수행합니다.

1. **서버를 관리합니다** — 온보딩, 역할, 카테고리/채널, 모더레이션, 정리(cleanup),
   공지, 그리고 템플릿 파일로부터의 원샷 서버 셋업.
2. **페르소나를 통해 대화합니다** — 봇을 멘션하면 캐릭터를 유지한 채로 답하며,
   사람처럼 **사소한 것은 잊으면서** 시간이 지남에 따라 **기억하고 성장합니다**.

설계 우선순위는 순서대로 다음과 같습니다. *가동을 유지한다*(우아한 성능 저하, 재시작에
안전한 상태), *안전을 유지한다*(최소 권한, 로그에 비밀 정보 노출 금지, 프롬프트 인젝션 저항),
그리고 *이식성을 유지한다*(얇은 플랫폼 어댑터 뒤에 자리한 작고 라이브러리에 종속되지 않는 코어).

## 2. Platform- and library-agnostic core

대화 코어(`orchestrator.py`)는 Discord 및 특정 채팅 라이브러리에 종속되지 않습니다. 평범한
데이터(작성자, 텍스트, 최근 버퍼)를 받아 응답 문자열을 반환하며, 절대 `discord`를 임포트하지
않습니다. 이 덕분에 핵심 로직을 오프라인에서 단위 테스트할 수 있고, 어댑터 하나만 작성하면(§3)
봇을 다른 플랫폼으로 재호스팅할 수 있습니다. 메모리는 선택 사항입니다. 스토어나 임베더가
연결되어 있지 않아도 오케스트레이터는 여전히 대화합니다.

## 3. Chat-platform isolation boundary

`platform/base.py`는 `ChatPlatform` 프로토콜과 `AuthContext` 프리미티브를 정의하며, 이것이
**격리 이음새(isolation seam)**입니다. `platform/pycord_adapter.py`는 `discord`를 임포트하는
*유일한* 모듈로, 게이트웨이 이벤트를 코어 호출로 변환하고 코어 결과를 다시 Discord 메시지,
임베드, 버튼, 슬래시 명령으로 렌더링합니다.

### 3.1 Server-management surface

권한이 필요한 작업(카테고리, 채널, 역할의 생성/수정/삭제, 모더레이션, 템플릿 적용, 정리)은
하나의 인가 + 확인 경로를 공유하는 두 가지 방식으로 노출됩니다.

- **슬래시 명령** — `ADMIN_GUILD_ID`에 등록됩니다.
- **자연어** — NL 라우터(`agent/router.py`)가 멘션을 닫힌 동사 집합으로 파싱하여 동일한
  서비스 계층으로 디스패치합니다.

두 방식 모두 `guild_admin.py`를 거치며, 이곳에서 고위험 작업을 명시적인 미리보기 → 확인
단계 뒤에 게이트로 둡니다. 인가는 역할 기반(`ADMIN_ROLE_ID`)이며, 길드 소유자는 항상
허용됩니다.

## 4. Persona (the fixed-identity layer)

`persona.md`는 시스템 프롬프트의 고정 정체성 계층 **그 자체**입니다. 사람이 직접 편집하며
오케스트레이터가 그대로 주입하고, 그 뒤에 성장한 자기 기억, 검색된 기억, 최근 대화 버퍼를
덧붙입니다. 페르소나 파일은 관례상 영어로 작성되며, 예시 문장은 봇의 런타임 언어로 되어
있는데, 이는 봇이 서버에서 실제로 사용하는 언어이기 때문입니다. 봇의 테마를 바꾸는 것은
단일 파일 편집으로 끝납니다.

진화하는 특성은 여기에 존재하지 **않습니다** — 그것은 봇의 자기 기억(§5.6)에 존재합니다.
이 계층은 안정적으로 유지되어 캐릭터의 일관성을 지킵니다.

## 5. Memory — remember, and forget, like a person

### 5.1 Memory types
- **Episodic** — 개별적으로 기억되는 대화(누가, 언제, 무엇을 말했는지).
- **Semantic** — 에피소드 기억에서 추출·통합된 사실.
- **Self** — 봇의 진화하는 자기 모델(자신의 특성이며, 페르소나에 덧붙여짐).

### 5.2 Flow
처리되는 각 멘션마다 오케스트레이터는 가장 관련성 높은 기억을 검색하고(§5.4), 프롬프트를
구성한 뒤 응답합니다. 새로운 기억의 저장은 **응답 경로 밖에서** 이루어지므로 지연 시간과
비용이 응답을 결코 막지 않습니다.

### 5.3 Ingestion
`memory/ingest.py`는 완료된 대화를 저렴한 모델(§12)을 사용해 저장된 기억으로 변환하며,
응답이 전송된 후 비동기로 수행됩니다.

### 5.4 Retrieval & scoring
검색은 후보를 **관련성(relevance) + 최신성(recency) + 중요도(importance)**의 가중합으로
순위를 매깁니다(`memory/scoring.py`; `w_rel` 같은 가중치는 설정 가능, §9). 의미적 유사도는
설정된 임베딩 제공자(오프라인/비의미 테스트용 `local`, 또는 Voyage/OpenAI 같은 실제 제공자)를
사용합니다.

### 5.5 Forgetting
기억은 실제로 희미해집니다. 각 기억은 **중요도에 따라 늘어나는 반감기(half-life)**를 가지므로,
사소한 것은 빠르게 감쇠하고 중요한 것은 오래 남습니다(`memory/forgetting.py`,
`memory/scoring.py`). 주기적인 프루닝(§7)이 잔존율이 낮은 기억을 아카이브한 뒤 삭제합니다.
망각은 **라이브 스토어에서는 되돌릴 수 없지만 감사(audit) 가능합니다**. 삭제는
`forgotten_memories` 아카이브에 기록됩니다(§6, §12).

### 5.6 Reflection & growth
`memory/reflection.py`는 주기적으로 에피소드 기억을 의미 기억과 자기 기억으로 통합한 뒤,
통합된 원본 기억의 중요도를 낮춰 희미해지게 합니다 — 봇이 "배우고" 원재료를 놓아 보내는
것입니다.

## 6. Storage

상태는 **SQLite**에 저장되므로, 단일 호스트 배포는 외부 데이터베이스가 필요 없습니다. 스키마는
`memory/schema.sql`과 `leveling/schema.sql`에 있습니다.

- **락 도메인 격리** — 메모리와 레벨링은 **별도의 데이터베이스 파일**(`MEMORY_DB`,
  `LEVELING_DB`)을 사용하므로, 뜨거운 쓰기 경로가 다른 쪽의 락과 경합하지 않습니다.
- **멀티 길드 스코핑** — 스토어는 길드 단위로 스코프가 지정되어 한 서버의 데이터가 다른
  서버로 결코 새어 나가지 않습니다(`_GuildScopedStore`).
- **재시작 안전** — SQLite 파일을 영속 볼륨에 두면 봇이 깔끔하게 재개됩니다.

## 7. Background jobs

`scheduler.py`는 주기적인 메모리 유지보수를 실행합니다. **프루닝**(망각, §5.5)과
**리플렉션**(성장, §5.6)을 독립적인 간격(`PRUNE_INTERVAL_HOURS`, `REFLECT_INTERVAL_HOURS`)으로
수행합니다. 잡은 오류 시 조용히 성능을 낮출 뿐, 채팅 경로를 결코 다운시키지 않습니다.

## 8. Component map

```
Discord gateway
      │
platform/pycord_adapter.py ── the only module importing `discord`  (§3)
      │            │
      │            └── agent/router.py ──▶ service/*  (guild_admin, template,
      │  (NL verbs)                        announcements, cleanup, onboarding,
      │                                    leveling, study_lookup, copywriter)
      ▼
orchestrator.py  ── library-agnostic core  (§2)
      │
      ├── llm.py            Anthropic calls + key pool  (§9.1)
      ├── memory/*          store, ingest, retrieve, forget, reflect  (§5, §6)
      ├── i18n/*            locale catalogs for user-facing strings  (§10)
      └── persona.md        fixed-identity layer  (§4)
```

## 9. Runtime configuration

모든 설정은 `config.py`(pydantic-settings)를 통해 환경 변수 / `.env`에서 로드됩니다. 전체
목록은 `.env.example`을 참고하십시오. 주요 옵션: `MODEL`, `INGEST_MODEL`, `MAX_TOKENS`,
`MAX_INPUT_CHARS`, `RECENT_BUFFER_SIZE`, 메모리 가중치와 반감기, 백그라운드 잡 간격,
그리고 `BOT_LOCALE`(§10).

### 9.1 LLM credentials & key pool
`llm.py`는 Anthropic을 **자격 증명 목록 + 선택 전략** 뒤에 감쌉니다. `ANTHROPIC_API_KEY`는
키 하나 또는 쉼표로 구분된 여러 키를 받아 **풀(pool)**을 구성합니다(속도 제한을 분산).
각 자격 증명은 비밀이 아닌 `label`을 가지며, **키 값은 절대 로그에 남지 않습니다** — 오직
레이블만 남습니다(§14.1).

## 10. Internationalization (i18n)

봇의 사용자 대상 문자열은 코드 변경 없이 선택한 언어로 말할 수 있도록 **소스에서 분리되어**
로케일 카탈로그로 외부화됩니다.

- 카탈로그: `src/dcm/i18n/locales/en.yaml`과 `ko.yaml`(점 표기 키, `{param}` 플레이스홀더).
- 조회: `t("key", **params)`가 활성 로케일에 대해 해석하며, 기본 로케일로 폴백합니다.
- 선택: `BOT_LOCALE` 설정(기본값 `ko`, 원래 동작을 보존).

모든 것이 번역 가능한 문자열은 아닙니다. 세 가지 범주가 구별됩니다.
1. **표시 문자열(Display strings)** → 로케일 카탈로그(번역함).
2. **LLM 프롬프트 스캐폴딩** → "reply in `<locale>`" 지시가 포함된 영어 명령어. 페르소나의
   목소리 자체는 `persona.md`(§4)에서 나옵니다.
3. **입력 매칭 데이터**(NL 트리거 단어, 정규식, 라이브 Discord 객체 이름) → *기능적*이며
   로케일 단위로 스코프가 지정됩니다. 사용자 입력을 파싱하므로, 순진하게 번역하기보다
   사용자의 언어와 일치해야 합니다.

## 11. Roadmap

마일스톤:
- **M1** — 페르소나를 통한 대화. ✅
- **M2** — 메모리: 중요도 가중 회상. ✅
- **M3** — 망각: 시간 감쇠 + 프루닝, 그리고 셀프서비스 "forget me" 명령. ✅
- **M4** — 성장: 의미/자기 기억으로의 리플렉션. ✅
- **M5** — 다듬기(일부).

서버 관리 트랙(**ralplan S1–S7**): 관리자 등록, 역할 기반 인가, 채널/역할/카테고리 작업,
모더레이션, 템플릿 적용, 온보딩, 그리고 라이브 길드 스모크 단계. 활동 **레벨링**(G001–G004):
XP 점수화, 감쇠, 쿼터, 어뷰징 방지 게이팅.

## 12. Reliability & graceful degradation

- **채팅 경로를 절대 하드 실패시키지 않습니다.** LLM을 사용할 수 없으면 오케스트레이터는
  오류 대신 페르소나 목소리의 폴백을 반환합니다.
- **가능한 곳에서는 저렴하게.** 인제스션/분류는 대화보다 저렴한 모델(`INGEST_MODEL`)을
  사용합니다.
- **감사 가능한 망각.** 삭제는 제거 전에 아카이브됩니다(§5.5).
- **크래시 시 재시작은 호스트의 몫**(systemd/PaaS)이며, 상태는 재시작에 안전합니다(§6).

## 13. Command & interaction surface

사용자는 봇을 **멘션**하거나(대화 + NL 관리) **슬래시 명령**(관리, 레벨링, 정리, 공지,
`/setup-server`)을 통해 상호작용합니다. 관리자 작업은 관리자 역할 또는 길드 소유권이
필요하며, 고위험일 경우 명시적 확인이 필요합니다(§3.1).

### 13.5 Self-service memory commands
가벼운 의도 감지(`commands.py`)를 통해 어떤 사용자든 자연어로 봇에게 자신에 관해 기억하는
것을 **보여 달라(show)**거나 자신을 **잊어 달라(forget)**고 요청할 수 있습니다 — 프라이버시
어포던스입니다(§14.2). 정규식/키워드 기반이며 로케일 단위로 스코프가 지정되고, 잘못된
트리거를 피하기 위해 의도적으로 좁게 설계되었습니다(§10).

## 14. Security model

### 14.1 Secrets & logging
`.env`는 git에서 무시되며, 실제 비밀 정보는 절대 커밋되지 않습니다. API 키는 **절대 로그에
기록되지 않으며** — 비밀이 아닌 레이블만 기록됩니다. 심층 방어(defense in depth)로,
`logging_setup.py`는 모든 로그 레코드에서 Anthropic 키나 Discord 토큰 형태의 무언가를
지워 내는 `SecretRedactor`를 설치합니다.

### 14.2 Privileged intents
봇은 **Message Content**와 **Server Members** 특권 게이트웨이 인텐트를 요구합니다. 코드는
항상 이를 요청하므로, Developer Portal에서 비활성화되어 있으면 봇은 조용히 오작동하는 대신
시작 시 **큰 소리로 실패합니다**(재시작 루프). `on_ready`에서는 어떤 특권 인텐트가
활성화되어 있는지 로그로 남겨 운영자가 검증할 수 있게 합니다.

### 14.3 Prompt-injection resistance
시스템 프롬프트는 페르소나를 덮어쓰거나 지시를 드러내려는 메시지를 무시하도록 모델에
지시합니다. `/setup-server`에 전달된 첨부 파일은 **데이터로만** 취급됩니다(파서로 전송될 뿐,
메모리나 대화에 절대 저장되지 않습니다).

### 14.4 Rate & size caps
비용과 어뷰징은 입력 상한(`MAX_INPUT_CHARS`), 응답 상한(`MAX_TOKENS`), 그리고 **사용자별
멘션 쿨다운**(`COOLDOWN_SECONDS`)으로 제한됩니다. 독점 방지 넛지가 공개 채널에서 한 사람이
봇을 독차지하지 못하게 합니다.

### 14.5 No inbound ports
봇은 **아웃바운드 연결만** 수행합니다(Discord 게이트웨이 + Anthropic API). 인바운드 포트도,
웹 서버도 없습니다 — 노출하거나 방화벽으로 막을 것이 없습니다. 혹시 메트릭/헬스 엔드포인트를
추가한다면, 절대 `0.0.0.0`이 아니라 내부 주소에 바인딩하십시오.

### 14.6 Least privilege — never Administrator
봇에게 **Administrator**를 절대 부여하지 마십시오. Administrator 권한을 가진 토큰이 탈취되면
서버 전체가 위험에 빠집니다. 필요한 특정 권한만 부여하십시오(Manage Channels, Manage Roles,
그리고 사용한다면 모더레이션 권한).

### 14.7 Role hierarchy
Discord는 봇 자신의 역할보다 **아래에 있는** 역할만 관리할 수 있습니다. 봇이 관리해야 하는
역할들보다 봇의 역할을 *위로* 드래그하십시오. 봇이 수행한 모든 변경은 서버 감사 로그에
요청한 사용자의 것으로 귀속됩니다.
