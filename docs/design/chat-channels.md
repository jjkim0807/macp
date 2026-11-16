---
title: 대화형 입구 — 프로젝트 채널 설계 노트 (채널에 쓴 메시지가 라우팅되어 런을 만들고 답이 같은 채널로 돌아온다)
status: draft
created: 2026-11-17
related:
  - Todo/items/macp-chat-channel-entry-design.md                         # 부모 태스크
  - Todo/items/macp-chat-channel-entry-design-a-census-model-routing.md  # leaf — §1~§3
  - Todo/items/macp-chat-channel-entry-design-b-reply-safety-split.md    # leaf — §4~§6·부록
  - Todo/items/macp-run-event-stream-design.md            # 자매 — 런 이벤트 스트림(설계 머지됨)
  - Todo/items/macp-inflight-approval-cards-design.md     # 자매 — 승인 카드(설계 문서 미존재)
  - Todo/items/macp-event-triggers-design.md              # 자매 — 대시보드 확장·저장/드레인 분리 선례
  - Todo/items/macp-skill-promotion-ladder-design.md      # 자매 (같은 날 설계 묶음)
  - docs/design/run-event-stream.md      # 봉투 어휘·git 밖 보존·마스킹 패턴(§2.1·§3·§4)
  - docs/design/event-triggers.md        # 대시보드 확장·수신 즉시 지속·5 게이트(§3.1·§3.3·§4.3)
  - docs/design/graph-memory.md          # 정본 형식 · 공개 규율
  - docs/design/state-store.md           # git 추적 vs 호스트 로컬 분류(①)
  - docs/design/agent-backends.md        # AgentBackend capability — 즉답·라우팅 호출의 실행 면
  - Decisions/2026-09-11-no-github-actions-local-release-lane.md  # 완료 조건은 로컬 명령
  - Decisions/2026-10-13-macp-task-store.md                       # md 정본 — 제2의 태스크 저장소 금지
---

# 대화형 입구 — 프로젝트 채널 설계 노트

> **이 노트의 성격**: 분석·설계 전용. 코드를 한 줄도 바꾸지 않고 `docs/design/chat-channels.md` 한 파일만
> 추가한다. 실제 구현은 §6 분할안의 후속 leaf 가, 설계 머지 뒤 **사람이 게이트를 풀 때** 맡는다. 본문의
> `파일:줄` 앵커는 이 리포(`jjkim0807/macp`) 트리를 **읽어** 확인한 것이고, 각 절의 범례가 읽은 HEAD 를
> 밝힌다. 볼트 쪽 파일은 `(볼트)` 로 표시하고, 이 체크아웃 밖(대시보드 Android 앱, mailroom, 원격 조종
> 운영 스택)은 '미확인'으로 남겼다. 자매 설계 `docs/design/run-event-stream.md` 는 머지되어 있어 § 번호로
> 인용한다. 승인 카드 설계는 아직 문서가 없어 '미존재(설계 단계)'로 표기하고, 그 태스크의 AC 만
> 인터페이스 가정으로 쓴다. 외부 URL 은 인용하지 않는다 — 배경은 태스크 Spec 요약이다. 공개 리포라
> 예시·스키마는 합성 placeholder(`<channel-id>`·`<ULID>`·`<token>`)이고 실명·토큰·엔드포인트 host·절대
> 홈경로·SMS/메일 본문을 넣지 않는다. **Spec 이 뺀 것**: 이름과 페르소나를 가진 봇들의 명단(봇 명단)은 두지
> 않는다 — macp 의 단위는 프로젝트다.

## 왜 이 문서인가

사용자 의도(2026-11-13, 태스크 Spec): 배경으로 둔 두 선행 봇 중 **Grok Bot** 은 *동료에게 메시지를 보내듯*
봇에게 일을 시킨다. **OpenMausBot** 은 채널마다 대화록·지침·작업 폴더·봇 명단을 따로 두고, 받는 이를 정하지
않은 메시지는 라우팅 모델이 어느 봇이 받을지 고른다. 지금 macp 에서 일의 단위는 **태스크 md 하나뿐**이라,
작은 부탁 하나에도 규격에 맞는 파일을 써야 한다. 두 선행 봇의 서술은 태스크 Spec 요약을 옮긴 것이며 외부
URL 로 재인용하지 않는다.

이 노트는 그 입구를 설계한다. 대시보드(웹·앱)에 **프로젝트마다 채널** 하나를 두고, 채널에 쓴 메시지를
라우터가 **즉답 · quick · delegate · 정식 태스크** 중 하나로 보내며, 결과는 **같은 채널로** 돌아온다. 여섯
축: (§1) 지금 일을 시키는 경로와 마찰 전수표, (§2) 채널 모델, (§3) 라우팅, (§4) 결과 회신, (§5) 안전,
(§6) 단계별 구현 분할안과 라우트·화면 수준 완료 조건.

**핵심 전제 셋.**

- ① **제2의 태스크 저장소 없음** — 채널이 만드는 일은 전부 진짜 `Todo/items/<slug>.md` 다((볼트)
  `Decisions/2026-10-13-macp-task-store`; md 가 정본이고 `todo.db` 는 거기서 다시 만든다 `tasks.md:3-6`).
- ② **새 수신 프로세스 없음** — 채널은 본부 대시보드를 확장한다(event-triggers §3.1). 대시보드는 이미
  인증된 POST 둘을 받는다(`macp_dashboard.py:3157-3213`).
- ③ **봇 명단·페르소나 없음** — 단위는 프로젝트이고, "누가 받나"는 기존 `worker_mode` 레인이다(§3).

---

## §1 현황 전수표 — 지금 일을 시키는 경로 · 마찰 · 결과가 도착하는 곳

### 1.1 경로 전수 (사람이 macp 에 일을 시키는 일곱 경로)

범례 — **마찰**은 AC 의 두 종류로 적는다. **규격**은 md frontmatter 와 파서 규칙을 알아야 한다는 것,
**도착**은 결과가 시킨 곳이 아닌 다른 곳에 도착한다는 것이다. 여기에 **터미널**(Claude Code 세션이나 CLI 가
있어야 한다)을 더한다. `todo.md`·`decide.md`·`focus.md`·`macp.md`·`worker-quick.md`·`worker-research.md`·
`worker-plan.md` 는 `.claude/commands/` 아래 파일이다. §1~§3 의 `파일:줄` 앵커는 이 리포 HEAD(`39dfbee`)를
읽어 확인.

| # | 경로 | 메커니즘 (`파일:줄`) | 누가·어디서 | 마찰 | 결과가 도착하는 곳 |
|---|---|---|---|---|---|
| ① | **손으로 쓰는 태스크 md** | `cli new`·`validate` `tasks.md:16-26`; 필드 `:89-139`; 파서 규칙 `:153-164` | 사람 · 볼트 체크아웃 + 편집기/터미널 | **규격**: 필수 5필드 `:91-99`, `allowed_tools` 는 따옴표 문자열·`touches` 는 리스트·값 뒤 인라인 주석 금지 `:155-161`. **터미널**: 볼트 체크아웃과 편집기가 있어야 한다(커밋은 todo-sync 가 대신 `worker.py:2587-2588,2618`) | **도착**: md Progress Log·`status: done`(엔진이 되써넣음 `tasks.md:3-6`) · PR · `Memory/episodes/` · 메신저 경보 · 대시보드 · 브리프 — 편집기로는 돌아오지 않는다 |
| ② | **대화형 스킬** `/todo`·`/decide`·`/focus`·`/macp` | `/todo` 2단계 규칙형 라우터 `todo.md:14-29`(auto·delegate·manual — **quick 없음**); `/decide` `decide.md:11-35`; `/focus` `focus.md:14-41`; `/macp` `macp.md:20-47` | 사람 · 볼트(또는 프로젝트)에서 연 Claude Code 세션 | **터미널**: 세션이 있어야 한다 — 웹·앱에는 없다. `/todo` 가 규격을 대신 쓰지만 끝은 한 줄 확인이다(`todo.md:83-84`) | **도착**: ① 과 같은 곳. `/decide` 는 Progress Log + `human reply`(assignment 가 `kind: human` 일 때만 `decide.md:25-35`) |
| ③ | **대시보드 입력** | 읽기 `do_GET` `macp_dashboard.py:3023-3063` — 7 라우트(`/api/status` `:3027`·`/api/task` `:3031`·`/api/run` `:3039`·`/api/graph` `:3047`·`/graph` `:3051`·`/sms-setup` `:3054`·`/api/agent-mode` `:3057`). 쓰기 `do_POST` `:3157-3213` 은 **둘뿐**: `/api/agent-mode`(대시보드 토큰 `_dashboard_gate` `:2992-3021`, JSON 전용 `:3114-3119`, 4 KiB `:3093`) — 에이전트 work·game 전환; `/api/sms`(자기 비밀값 `MACP_SMS_TOKEN` `:3164-3168`) — 폰 SMS 릴레이. Android 앱은 같은 GET 계약을 그린다(이 리포 밖 — 미확인) | 사람 · 브라우저/앱 | **입구가 없다** — 태스크 캡처·결정 응답 라우트가 없다(요청 핸들러는 `do_GET` `:3023`·`do_POST` `:3157` 둘뿐) | 보기만 — 다른 경로의 결과를 모아 보여 줄 뿐이다 |
| ④ | **메신저 알림 응답** | 나가는 방향만: `deliver_pending` `human.py:117-160`(nudge 후 ack, TASK_ASSIGN 즉시 알림·`event_key=assign:<task>` `:135,150`); 답은 CLI `human reply` `:172-191`(`_STATUS_MAP` `:163-169`, CLI `:225-234`, `concepts.md:182-193`). 들어오는 메신저 경로는 없다(`human.py:1-14`) | 사람 · 알림은 폰, 답은 터미널(또는 `/decide`) | **터미널**: 폰에서 받고 터미널에서 답한다. `--corr` 규약(`_ledger_corr` `:194-214` 가 기본값을 채운다) | **도착**: ledger 전이 → 다음 틱 → 브리프·대시보드. 메신저로는 돌아오지 않는다 |
| ⑤ | **원격 호스트 inbox** `Worker/remote/<host>/inbox/<slug>.md` | M5 git 추적 전송 `worker.py:76-88`, `REMOTE_INBOX_DIR` `:107` | 사람·에이전트 · 볼트 클론 | **규격**(① 과 같은 md) + git push 가 곧 전송이다(`worker.py:2604-2606`) | **도착**: 원격 결과가 git 으로 돌아와 주 호스트의 리뷰 할 일로 뜬다(`worker.py:85-88`) |
| ⑥ | **advote 브리지** (앱 사용자 건의 → 태스크 → 같은 티켓 역보고) | `advote_bridge.py:3-9`; 저위험 화이트리스트(copy·cosmetic·config)만 auto, 나머지는 blocked, 주입 의심은 held(태스크 없음) `:11-24`; 원문은 신뢰하지 않는 데이터 펜스 안에만 `:69-75`; 폴링은 ≥ 10분 간격 `:110` | 앱 사용자 · 앱 안 | 앱 피드백 전용 — 프로젝트 일반 입구가 아니다 | **같은 티켓** — 결과가 입구로 돌아오는 유일한 경로 |
| ⑦ | **플래너·discovery·follow-ups** | `concepts.md:157-168` | 시스템(사람 아님) | 사람이 쓰는 입구가 아니다 | 새 태스크 md |

② 의 변형 하나: 휴대폰에서 여는 **원격 조종 세션**(대화형 Claude Code 세션)도 ② 와 같은 스킬을 쓴다. 이
세션은 볼트 쪽 운영 스택에서 돌고 이 체크아웃 밖이다 — 미확인. 폰에서 열 수는 있지만 여전히 세션이 필요하고,
결과는 ① 과 같은 곳에 흩어진다.

**읽는 법.**

- **규격** 마찰은 ①·⑤ 에 그대로 있고, ② 는 그것을 터미널 뒤로 숨길 뿐이다.
- **도착** 마찰은 ⑥ 을 뺀 모든 경로에 있다 — 결과가 Progress Log·PR·에피소드·메신저·대시보드·브리프로
  흩어진다.
- 메시지의 결과가 보낸 곳으로 돌아오는 경로는 ⑥ 하나뿐이고, 그마저 앱 피드백 전용이다.

### 1.2 공백 — 이 설계가 닫는 것

- (a) **웹·앱에는 일을 시킬 방법이 없다** — 대시보드는 보기 전용이고 쓰기 둘은 SMS 와 에이전트 모드다(①③).
- (b) **작은 부탁도 규격에 맞는 md 가 필요하다** — "이 문구 하나 바꿔 줘"와 "새 기능" 이 같은 입구를 지난다(①②).
- (c) **결과가 부탁한 곳으로 돌아오지 않는다** — 시킨 사람이 결과를 찾으러 다른 면을 열어야 한다(①~⑤).

채널은 ⑥ 의 고리(메시지 → 태스크 → 같은 곳으로 회신)를 **모든 프로젝트로 일반화**하고, 입구를 대시보드
(웹·앱)로 옮기며, md 는 사람이 아니라 라우터가 쓴다.

---

## §2 채널 모델 — 채널이 갖는 것 · 컨텍스트 격리와의 대응 · 대화록 저장 위치와 보존

### 2.1 채널 = 프로젝트 하나 (채널 id 와 `.macp` 대응)

채널은 새 격리 범위가 아니라 **기존 격리 단위(프로젝트)를 보는 view** 다. 채널을 지워도 프로젝트의
프로필·메모리·태스크는 그대로이고, git 밖 원문 대화록(2.4)만 사라진다.

- **채널 id = 대시보드 `project` 라벨.** 서버가 태스크 트리에서 계산한다. 자식이 있는 최상위 루트 아래의
  태스크는 그 루트 slug 에서 일반 접미사(`_PARENT_SUFFIXES` — `-project`·`-agent` 등 `macp_dashboard.py:1266`)를
  뗀 라벨(`_root_label` `:1295-1301`)을 받고, 트리 밖 태스크는 slug 접두·태그 휴리스틱(`_project_of`
  `:1269-1292`)을 받는다(`_project_labels` `:1304-1323`; `todo.db` 에서 읽음 `_project_map` `:1326-1335`). 이
  라벨이 이미 웹·앱의 묶음 키다(`_group_of` `:1338-1344`; (볼트) `docs/dashboard-parity.md` 의 `group =
  row["project"]` 규칙). 그래서 채널 목록은 곧 프로젝트 카드 목록이고, 새 분류 체계가 없으며, 리포 없는
  볼트 전용 프로젝트도 라벨을 가진다. 라벨이 빈 단독 태스크(`:1292`)에는 채널이 없다.
- **선언이 라벨을 얼린다.** 라벨은 계산값이라 루트 slug 가 바뀌면 함께 바뀐다. 채널 선언(2.2)의 `id` 가 그
  순간의 라벨을 고정하므로, 루트 slug 를 바꿔도 채널 id 와 대화록은 남는다.
- **`.macp` 대응.** 리포 쪽 정본 프로젝트 id 는 `ProjectConfig.name`(기본값은 디렉터리 basename
  `config.py:118`)이다. 라벨과 다르면 선언이 `macp_name:` 으로 적는다. 라우터는 채널을 `id` 로 찾고, 프로젝트
  설정·메모리는 선언 `repo:` 의 체크아웃에서 `find_project_root` `:177-190` → `load_project` `:158-168` 로 읽은
  뒤 그 `name` 이 `macp_name`(없으면 `id`)과 같은지 대조한다. 다르면 `note` 를 남기고 리포가 필요한
  타깃(§3 의 R3·R5)을 열지 않는다. 볼트 전용 채널에는 `.macp` 가 없으니 `macp_name` 도 없다.
- **선언이 없는 라벨은 읽기 전용 보기다** — 메시지를 받지 않는다. 선언은 프로젝트당 한 번 쓴다. 손으로 쓰거나,
  `macp init`(`macp.md:20-37`) 또는 대시보드(§6)가 기본값으로 만든다.
- OpenMausBot 의 채널별 봇 명단에 대응하는 것은 **없다**. "누가 받나"는 §3 의 레인이 정한다.

### 2.2 채널이 갖는 것 — 대화록 · 프로젝트 지침 · 작업 리포 · 연결 태스크 목록

**선언 파일** `Worker/channels/<channel-id>.md` — git 추적이다. 볼트 `.gitignore` 는 `Worker/` 아래 시끄러운
것(`runs/`·`*_state/` 등)만 무시하고(`worker.py:2611-2613`), todo-sync 가 `Worker` 를 통째로 커밋한다
(`VAULT_SYNC_PATHS` `:2618`). `Worker/` 아래 미커밋 파일은 큐를 세우지 않는다(`:2580-2581`). event-triggers
§2.1 의 `Worker/triggers/<id>.md` 와 같은 모양이다.

```yaml
# Worker/channels/<channel-id>.md frontmatter (합성 예시)
id: <channel-id>          # 대시보드 project 라벨 — 채널의 영구 키
title: <표시 이름>
repo: <git URL>           # 없으면 볼트 전용 채널(§3 의 R3·R5 없음)
macp_name: <name>         # 리포 .macp/config.toml 의 name 이 id 와 다를 때만
root: <root-slug>         # 선택 — 라벨을 계산한 루트 태스크
allow_quick: false        # R3(quick) 허용 — 기본 꺼짐
tags: [todo, <tag>]       # 생성 태스크의 기본 태그
```

본문에는 볼트 전용 채널의 지침을 쓰거나, 리포 채널이면 리포의 `.macp/memory/index.md` 를 가리키는 한 줄만
둔다. 생성 태스크의 권한 기본값(`allowed_tools`·`auto_merge`)은 §5 가 정한다.

| 소유물 | 무엇 | 사는 곳 | SoT | 채널이 하는 일 |
|---|---|---|---|---|
| **대화록** | 사람 메시지 · 라우터 판정 · 즉답 | git 밖 XDG `MACP_CHANNEL_DIR` 의 `<channel-id>/messages.jsonl`(2.4) | 원문의 정본 — 호스트 로컬 | 수신 핸들러와 드레인이 append 하고, 웹·앱이 그린다 |
| **프로젝트 지침** | 즉답과 생성 태스크가 따를 규칙 | 리포 채널: `<repo>/.macp/memory/index.md`(git 추적 `memory.py:1-11`, import 줄 `MEMORY_IMPORT` `:19`); 볼트 채널: 선언 본문 | 그 파일 | 즉답과 생성 태스크가 읽는다. 복사본을 두지 않는다 |
| **작업 리포** | 채널이 일하는 git 리포와 그 산출물 | 선언 `repo:` → 런마다 worktree 하나(`concepts.md:142-145`); 산출물은 `Worker/runs/<run-id>/`·PR·`Memory/episodes/` | 리포 · 볼트 | 링크만 건다 — 복제하지 않는다. "작업 리포"의 두 뜻(리포 자체·산출물 폴더)을 이 행이 함께 다룬다 |
| **연결 태스크 목록** | 이 채널이 만들었거나 이 프로젝트에 속한 태스크 | `Todo/items/*.md` frontmatter 위의 파생 질의: `channel == <id>` ∪ (`channel:` 없음 ∧ 대시보드 라벨 == `<id>`) — `channel:` 필드는 §3.1 | md(`tasks.md:3-6`); `todo.db` 는 캐시 | 질의 결과를 보여 줄 뿐이다 — 제2의 목록 없음 |

### 2.3 프로젝트 컨텍스트 격리와의 대응

| 격리 축 | 지금 (`파일:줄`) | 채널에서 |
|---|---|---|
| 프로필 | 볼트·예산·모델·계정 묶음 `config.py:4-9`; `Profile` `:82-105`, `daily_run_budget = 40` `:86` | 채널은 그 프로필 안에 산다. 채널이 만든 런은 그 프로필의 일일 런 예산을 쓴다 |
| 계정 | 프로젝트 핀 `config.py:113-115` → `account_for` `:218-242`·`current_account` `:244` | 라우팅·즉답 호출도 스테이지와 같은 규칙으로 계정을 고른다 |
| 프로젝트 설정 | `<project>/.macp/config.toml` `config.py:10-11`; `ProjectConfig` `:108-120`(`worker_mode` `:111`), `load_project` `:158-168` | `name` ↔ 채널 id 대응(2.1). **(제안)** 프로젝트 `worker_mode` 가 라우팅의 상한이 된다 — 예: `manual` 프로젝트의 채널은 즉답과 `manual` 태스크만 만든다 |
| 프로젝트 메모리 | 프로젝트와 함께 git 추적 `memory.py:1-11`; `MEMORY_IMPORT` `:19`; `link_agent_context` `:61-88` | 채널의 "프로젝트 지침"이 바로 이것이다 |
| 런 격리 | 런마다 worktree, `touches` 로 동시성 `concepts.md:142-145` | 채널 태스크도 같은 worktree·`touches` 규칙을 따른다 |

### 2.4 대화록 저장 위치와 보존 — 후보 비교 뒤 git 밖

| 후보 | git 추적? | 원문(마스킹 전) 노출면 | 볼트 churn | 큐 정지 | 스테이지 가시성 | 멀티호스트 | 판정 |
|---|---|---|---|---|---|---|---|
| (가) git 안 `Memory/channels/<channel-id>/…` | ✓ — `Memory` 는 동기화 경로(`worker.py:2618`) | 영구 히스토리 + 원격 리포 | 대화 한 줄마다 todo-sync 커밋·push | **위험** — `Memory/channels/` 는 allowed-dirty 접두(`:2580-2589`) 밖이라 커밋 전에 큐가 설 수 있다 | 볼트를 cwd 로 도는 research·plan 이 읽는다(`worker-research.md:9`·`worker-plan.md:9`) | ✓ git 으로 | **기각** |
| (나) 볼트 안 비추적 `Worker/_state/channels/` | ✗ — 볼트 `.gitignore` 가 `Worker/_state/` 를 무시(`worker.py:2611-2613`) | 볼트 트리 안 — 무시 규칙 하나가 빠지면 git 으로 간다 | 없음 | 없음 | 볼트 cwd 스테이지가 원문을 읽을 수 있다(주입·개인정보 면) | ✗ 호스트 로컬 | 원문은 **기각**. 원문 없는 카운터·드레인 커서만 여기 |
| **★ (다)** git 밖 XDG `MACP_CHANNEL_DIR`(기본 `~/.local/share/macp/channels/<channel-id>/messages.jsonl`) | ✗ | 볼트 밖이고 스테이지 작업 디렉터리(cwd 와 `--add-dir`) 밖이다 — `--add-dir` 는 외부 리포 클론 루트다(`stages.py:702-704`, `paths.py:380-388`). SMS 스토어와 같은 노출면(`sms.py:15-17,85-95,116-124`) | 없음 | 없음 | 기본으로 보이지 않는다. 단 allowlist 는 샌드박스가 아니라 가드레일이다(`concepts.md:138-140`) | ✗ 호스트 로컬(본부만 수신) | **채택** |
| (라) 외부 상태 `state.sqlite` 테이블 | ✗ | 볼트 밖 | 없음 | 없음 | ✗ | ✗ | **기각** — external 모드는 opt-in(`concepts.md:85-95`)이라 기본 모드를 받치지 못한다. append 로그는 파일이 맞다(run-event-stream §3.1) |
| (마) mailbox 봉투 | ✓(git mailbox 면) | 봉투 = 커밋 | 봉투 하나가 커밋 하나 → 대화가 커밋을 쏟아낸다 | — | — | ✓ | **기각** — 봉투는 ledger 의 입력이고 소비되면 지워진다(run-event-stream §3.1) |

**(다)를 고른 이유.**

1. 대화록은 **마스킹 전 원문**이다. 붙여 넣은 인증번호나 SMS·메일 본문이 들어갈 수 있다((볼트) CLAUDE.md
   §11·§11b 는 그런 본문을 어디에도 옮겨 적지 말라고 한다). 런 이벤트 스트림은 쓰는 순간 writer 한 곳에서
   마스킹하므로(run-event-stream §4) 런 폴더에 살 수 있지만, 대화록은 그럴 수 없다 — 사람은 자기가 쓴 말을
   그대로 봐야 하고, 라우터는 원문으로 판정한다. 그래서 대화록은 SMS 스토어의 자리를 잡는다: 볼트 밖,
   0700/0600, 보존 기간 한정.
2. (가)는 커밋 churn, 지울 수 없는 히스토리, 큐 정지를 부른다.
3. (나)는 볼트를 cwd 로 도는 스테이지가 읽는 곳에 원문을 둔다. event-triggers §3.3 의 이벤트 저장소
   (`Worker/_state/events/`)가 이 모양이지만, 대화록은 사람의 말 원문을 몇 주 동안 담고 즉답·라우팅 입력으로
   다시 읽히므로 노출면을 한 단계 더 좁힌다.
4. 메시지를 받는 곳은 본부 대시보드 하나라 호스트 로컬이면 충분하다. 다른 호스트가 본부를 넘겨받으면 원문만
   잃는다 — 결정과 결과는 아래 승격으로 이미 git 에 있다.

**줄 봉투.** run-event-stream §2.1 의 어휘를 빌린다.

- 필드: `v · channel · seq · ts · kind · src · msg_id · reply_to · payload`, 선택 `run_id`·`task_id`.
- `seq` 는 flock 아래에서 매긴다. writer 가 둘(수신 핸들러와 드레인)이라 SMS 스토어의 잠금 모양
  (`sms.py:85-95`)을 따른다.
- §1~§3 이 쓰는 kind: `message`(src `human`), `route`(src `router`), `reply`(src `answer`), `reroute`(src
  `human`), `note`(src `router`).
- §4 는 `v` 를 올리지 않고 kind 를 더할 수 있다(추가 호환 규칙 `run-event-stream.md:147`).

```json
{"v":1,"channel":"<channel-id>","seq":1,"ts":"2026-01-01T09:00:00Z","kind":"message","src":"human","msg_id":"<ULID-1>","reply_to":null,"payload":{"text":"<질문 원문>"}}
{"v":1,"channel":"<channel-id>","seq":2,"ts":"2026-01-01T09:00:01Z","kind":"route","src":"router","msg_id":"<ULID-2>","reply_to":"<ULID-1>","payload":{"rule":"R1","target":"answer","model":null}}
{"v":1,"channel":"<channel-id>","seq":3,"ts":"2026-01-01T09:00:20Z","kind":"reply","src":"answer","msg_id":"<ULID-3>","reply_to":"<ULID-1>","payload":{"text":"<답>","backend":"claude","rule":"R1","usage":{"input_tokens":0,"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"num_turns":0,"duration_ms":0,"model":"<model-id>","session_id":"00000000-0000-4000-8000-000000000000","total_cost_usd":0}}}
```

`reply` 의 `usage` 는 스테이지 사용량 기록과 같은 9 키(`USAGE_KEYS` `stages.py:530-534`)만 담는다 — 프롬프트·
도구 출력은 복사하지 않는다는 같은 규칙이다.

**보존·크기 (제안 — 사람 게이트: 숫자는 머지 뒤 사람이 확정한다).**

| 항목 | 값 | 근거 |
|---|---|---|
| 보존 | **90일** — `MACP_CHANNEL_RETENTION_DAYS` | SMS 7일(`sms.py:52`)·런 스트림 30일(run-event-stream §3.2)보다 길다. 사람은 몇 주에 걸쳐 대화로 돌아온다. 원문이라 무기한은 아니다 |
| 메시지 본문 | ≤ 4000자 | `MAX_BODY` `sms.py:53`, `_RAW_BODY_CAP` `advote_bridge.py:112`. 넘을 때의 거절 응답은 §5 |
| 권한 | 디렉터리 0700 · 파일 0600 | `sms.py:88,119-123` |
| 멱등 | `msg_id`(ULID) — 같은 id 는 한 번만 적재 | `sms.py:20-21` 모양 |
| 집행 | append 할 때 잠금 아래에서 오래된 줄을 지운다 | `sms.py:18-19` |
| 위치 override | `MACP_CHANNEL_DIR` → 기본 XDG 경로 | `MACP_SMS_DIR` 해석 `sms.py:56-58` 모양 |

**하이브리드 승격 — 고른 뒤에, 원문은 올리지 않는다.**

- ① **생성 태스크 md**: 분류 요약 + 신뢰하지 않는 데이터 펜스 안의 발췌 ≤ 4000자(`advote_bridge.py:69-75,112`
  모양). 발췌는 run-event-stream §4.2 패턴으로 마스킹한다(세부는 §5).
- ② **런 결과**는 이미 `Memory/episodes/` 와 PR 에 남는다 — 중복하지 않는다.
- ③ (선택) **채널별 일일 요약** `Memory/sessions/<date>-<channel-id>.<host>.md` — `session_log` 경유(git 추적
  저널 `session_log.py:18-24`, allowed-dirty `worker.py:2583-2584`). 건수, 만든 slug, 결정 요지만 적는다.

<!-- §3 이하 이어 씀 -->
