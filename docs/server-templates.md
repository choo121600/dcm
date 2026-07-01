# Server Template Guide (`/setup-server`)

> 🌏 한국어 버전: [server-templates.ko.md](./server-templates.ko.md)

A feature that sets up **roles (+permissions), categories, channels (text/voice), and private access**
all at once from a single YAML or JSON file. This document explains how to write a template file and how it works.

- Ready-to-use examples: [`examples/server-template.yaml`](../examples/server-template.yaml) ·
  [`examples/server-template.json`](../examples/server-template.json) ·
  [`examples/server-template-minimal.yaml`](../examples/server-template-minimal.yaml)

---

## 1. Quick Start

1. Create a template file (`.yaml` / `.yml` / `.json`, **UTF-8**). You can just copy an example file and rename it.
2. In Discord, run the `/setup-server` slash command and **attach** the file to the `template` option.
3. When the bot shows a **preview**, press the **`확인 실행`** (run confirmation) button → setup is done in one shot.

> `/setup-server` can only be used by **admins (with an administrator role) or the server owner**.

**Or in natural language**: call the bot by name — `썩스가재야` (`썩스가재` is the bot's name) — while **attaching** the same template file and saying "set it up like this," and you'll get the same preview + confirmation button. Both the slash command and natural language **can only be applied by admins/the owner** (see §8 below).

---

## 2. How It Works

- **Two-step confirmation**: attaching a file does not create anything immediately; it goes through a *preview + confirmation*.
  It is applied when you press the button, or run it again (re-attaching the file) with the `confirm:true` option.
- **Idempotent**: anything that already exists is **skipped**. So if you edit the template and run it again,
  *only the newly added items* are created. Criteria for deciding something is "the same":
  - Role → reused if the **name** matches
  - Category → reused if the **name** matches
  - Channel → reused if the **name + type (text/voice)** match within the same category (name is case-insensitive)
- **Creation order**: roles → categories (+private access) → channels.
- **Private category** (`private: true`): blocks `@everyone` from viewing the category and allows only the `visible_to` roles.
  Channels created under that category **inherit** these permissions.
- **Partial failure**: if it fails partway through, it reports what was created up to that point and **does not roll back automatically**
  (Discord has no transactions). Read the message and clean up, or just run it again (safe, since it's idempotent).

---

## 3. Template Format

The top level is an object (mapping) with two keys: `roles` and `categories`. **At least one of the two** must be present.

```yaml
roles:        # 선택
  - ...
categories:   # 선택
  - ...
```

### 3.1 `roles[]` — Roles

| Field | Required | Description |
|---|---|---|
| `name` | ✅ | Role name (1–100 characters) |
| `permissions` | — | List of permission names. Omitted/empty list means no permissions |

```yaml
roles:
  - name: 운영진
    permissions: [manage_channels, manage_roles, kick_members]
  - name: 멤버           # permissions 생략 = 권한 없음
```

#### Available permission names (closed set — names not on the list are rejected)

| Name | Meaning | Notes |
|---|---|---|
| `administrator` | All permissions | ⚠️ Very dangerous — avoid using it if possible |
| `manage_channels` | Create/edit/delete channels | Management permission (high risk) |
| `manage_roles` | Create/edit/assign roles | Management permission (high risk) |
| `manage_guild` (alias `manage_server`) | Manage server settings | Management permission (high risk) |
| `manage_messages` | Delete/pin messages | Management permission (high risk) |
| `manage_webhooks` | Manage webhooks | Management permission (high risk) |
| `manage_nicknames` | Change other members' nicknames | |
| `kick_members` | Kick members | |
| `ban_members` | Ban members | |
| `moderate_members` (alias `timeout_members`) | Time members out | |

> Roles that include management permissions carry high risk, so they **always go through the confirmation step** (applying a template is inherently high-risk).

### 3.2 `categories[]` — Categories

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | ✅ | — | Category name (1–100 characters) |
| `channels` | — | `[]` | List of channels under this category |
| `private` | — | `false` | If `true`, blocks `@everyone` from viewing and allows only the `visible_to` roles |
| `visible_to` | — | `[]` | List of role **names** allowed to view when `private` |

> The role names in `visible_to` must exist in the same template's `roles` or already exist on the server.
> Names that cannot be found are silently skipped (no view permission is added for that role).

### 3.3 `channels[]` — Channels

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | ✅ | — | Channel name (1–100 characters) |
| `type` | — | `text` | `text` or `voice` |

```yaml
categories:
  - name: 2026-summer
    private: true
    visible_to: [운영진, 멤버]
    channels:
      - name: 공지
        type: text
      - name: 일반          # type 생략 → text
      - name: 회의
        type: voice
```

---

## 4. YAML vs JSON

Both work. YAML is a superset of JSON, so identical content produces identical results. The YAML above and the JSON below are the same template.

```json
{
  "categories": [
    {
      "name": "2026-summer",
      "private": true,
      "visible_to": ["운영진", "멤버"],
      "channels": [
        { "name": "공지", "type": "text" },
        { "name": "일반", "type": "text" },
        { "name": "회의", "type": "voice" }
      ]
    }
  ]
}
```

---

## 5. Limits and Rules

| Item | Limit |
|---|---|
| Name length (role/category/channel) | 1 – 100 characters |
| Number of roles | Max 100 |
| Number of categories | Max 50 |
| Channels per category | Max 50 |
| Top level | At least one of `roles` or `categories` |
| Duplicate names | Duplicates forbidden within the same kind (role vs role / category vs category) |

---

## 6. Permissions and Safety

- `/setup-server` can only be run by **admins (a designated administrator role) or the server owner**.
- For the bot to actually create things, it needs **Manage Channels / Manage Roles** permissions in Discord
  (add Kick/Ban/Moderate Members too if you want to grant moderation permissions). Place the bot's role *above the roles it will manage*.
- Every change is recorded in the Audit Log against *the person who requested it*.

---

## 7. Common Errors and Fixes

| Message (excerpt) | Cause | Fix |
|---|---|---|
| `빈 템플릿이야…` (empty template) | The file was empty | Fill in content and re-attach |
| `최상위는 매핑(객체)이어야 해` (top level must be a mapping/object) | The file starts with a list/sentence | Use an object starting with `roles:` / `categories:` |
| `알 수 없는 권한 'X'` (unknown permission 'X') | Typo in a permission name | Check the permission names table in 3.1 |
| `…type: 'text' 또는 'voice'여야 해` (type must be 'text' or 'voice') | Typo in the channel type | Use `text` or `voice` |
| `중복 역할/카테고리 이름` (duplicate role/category name) | The same name appears twice | Make the name unique |
| `이름이 너무 길어 (최대 100자)` (name is too long, max 100 characters) | Name length exceeded | Keep it under 100 characters |
| `YAML/JSON 파싱 실패` (YAML/JSON parse failure) | Indentation/syntax error | Check indentation (spaces), no tabs |
| `템플릿 확인 토큰이 유효하지 않거나 만료됐어` (template confirmation token is invalid or expired) | A token was given without a preview, or it expired | Re-attach the file to start from the preview |

---

## 8. Applying via natural language (`썩스가재야`)

Instead of the slash command, you can also summon the bot in natural language.

**Applying a full template** — call the bot by name (`썩스가재야`) while **attaching** a template file (`.yaml`/`.yml`/`.json`)
and request the setup. Just like `/setup-server`, you'll get a preview + `확인 실행` (run confirmation) button.

```
썩스가재야 이 파일대로 서버 세팅해줘   (+ server-template.yaml 첨부)
```

- **Only admins (with an administrator role) or the server owner** can apply it (non-admins are politely refused).
- Since it's a button in a public channel, **the presser's permissions are re-checked too** (not just anyone can press it).
- Attached files are treated **as data only** and sent solely to the parser (never stored in memory/conversation).
- To trigger it, there must be a **setup-intent word** such as "세팅 / 셋업 / 적용 / 템플릿 / 구성 / 만들어" (set up / setup / apply / template / configure / create)
  (just sharing a file is handled as ordinary conversation). Files can be up to 256KB.

**For just one or two items**, you don't even need a file.

```
썩스가재야 2026-summer 카테고리 만들어줘
썩스가재야 공지 텍스트 채널 만들어줘
썩스가재야 회의 음성 채널 만들어줘
```

Clear requests are executed automatically by the system, and if information is missing (name/type, etc.), the bot asks back.
