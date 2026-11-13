---
title: 런 이벤트 스트림 — 설계 노트 (모든 스테이지의 도구 호출·질문·결과·상태 전이를 런별 하나의 정규화된 NDJSON 으로)
status: draft
created: 2026-11-13
related:
  - Todo/items/macp-run-event-stream-design.md            # 이 태스크
  - Todo/items/macp-inflight-approval-cards-design.md    # 자매(blocked, depends_on 이 태스크) — approval.* 이벤트의 생산자
  - Todo/items/macp-event-triggers-design.md             # 자매 — 내부 이벤트 버스가 이 스트림을 구독(event-triggers §3.2·E6)
  - Todo/items/macp-graph-memory-v2-design.md            # 자매 — 에피소드·provenance 가 넘어가는 곳(§5)
  - docs/design/graph-memory.md                          # 자매 설계 노트 — 정본 형식 · R2 provenance · R5 개인정보 경계
  - docs/design/event-triggers.md                        # 자매 설계 노트 — 이 스트림을 내부 이벤트 소스로 기대(§3.2, E6)
  - docs/design/state-store.md                           # 자매 — external SQLite · ⑦(a) 본부 HTTP 상태 API(원격 업로드 경로 후보)
  - docs/design/agent-backends.md                        # 자매 — AgentBackend 8 capability(`event_stream` 이 더해질 자리)
  - Decisions/2026-09-11-no-github-actions-local-release-lane.md  # 완료 조건은 로컬 명령("CI green" 금지)
---

# 런 이벤트 스트림 — 설계 노트

> **이 노트의 성격**: 분석·설계 전용. 이 태스크는 코드를 한 줄도 바꾸지 않는다 —
> `docs/design/run-event-stream.md` 한 파일만 추가한다. 실제 구현은 §6 분할안의 후속 leaf 가,
> 설계 머지 뒤 **사람이 게이트를 풀 때** 맡는다. 본문의 `파일:줄` 앵커는 전부 이 리포
> (`jjkim0807/macp`) HEAD `e6154bd` 트리를 **읽어** 확인한 것이고, 볼트 쪽 파일(`.gitignore`·
> `Worker/README.md`·`docs/dashboard-parity.md`)은 볼트 체크아웃을 읽어 확인했다. 확인하지 못한
> 지점(이 체크아웃 밖의 리포 — Android 대시보드 앱·mailroom, 운영 측정이 필요한 거동)은
> '미확인'으로 남겼다. 외부 문서 URL 은 WebFetch 로 **열어 확인한 것만** '확인'(조회 2026-11-13,
> 최종 도달 URL 을 적는다), 못 연 것은 '미확인' — 자매 `graph-memory.md` §3.1 과 같은 규율이다.
> 공개 리포라 예시·스키마에 실명·토큰·엔드포인트 host·절대 홈경로·개인 데이터를 넣지 않는다 —
> 모든 예시는 합성 placeholder 다(자매 노트와 동일 규율).

## 왜 이 문서인가

사용자 의도(2026-11-13, 태스크 Spec): *"OpenMausBot 은 여러 에이전트 CLI 의 출력을 하나의 이벤트
스트림으로 정규화해 스레드별 NDJSON 으로 남기고, 화면·승인·재생이 모두 그 위에서 돈다. Grok Bot 은
감사 로그가 아직 없다고 스스로 밝힌다. 지금 macp 는 스테이지가 끝난 뒤의 산출물(run.log·verify.md·
report.md)만 남는다. 실행 중에는 무슨 일이 일어나는지 볼 수 없고, 도구 호출 단위 감사가 안 된다."*
(두 선행 봇의 서술은 태스크 Spec 의 요약을 그대로 옮긴 것이며, 이 노트는 외부 URL 로 재인용하지 않는다.)

§1 이 보여 주듯 지금의 런 기록은 **열세 가지 파일·형식이 각자의 시점에 따로** 남는다 — 스테이지 끝의
md 아티팩트, stdout 마지막 2000자, 스테이지 단위 토큰 집계, 한 줄 PROGRESS 봉투, 그리고 아무도
읽지 않는 CLI 세션 transcript. 어느 것도 "지금 이 스테이지가 무슨 도구를 부르고 있나"를 답하지
못하고, 어느 것도 다른 것과 같은 형식이 아니다. 이 노트는 그 열세 기록을 **대체하지 않고**, 그 위에
**런별 하나의 정규화된 NDJSON 이벤트 스트림** `events.ndjson` 을 더해 — 실행 중 가시성(대시보드),
사후 재생·감사(trace), 승인 카드(자매 ①), 내부 이벤트 트리거(자매 ③), 그래프 메모리 v2 입력(자매 ⑤)이
**한 파일을 읽게** 하는 설계를 정리한다. 여섯 축: (§1) 지금 남는 기록 전수표와 한계, (§2) 이벤트
스키마와 CLI stream-json 매핑, (§3) 기록 위치·보존·원격 업로드·대시보드 구독, (§4) 개인정보 경계,
(§5) 그래프 메모리 연결, (§6) 단계별 구현 분할안과 라우트 수준 완료 조건.

**핵심 전제 셋.** ① **새 상시 인프라를 신설하지 않는다** — 러너(Python)가 쓰고, 이미 있는 본부
대시보드 프로세스와 본부 HTTP 상태 API 가 읽고 받는다. 새 데몬·새 DB·새 SaaS 없음. ② **스테이지
argv 와 스테이지 루프가 읽는 stdout/stderr 의 의미는 바이트 동일을 우선한다** — claude argv 는 골든
테스트로 핀돼 있고(`todo_system/tests/test_agent_backend_golden.py:1-33`), `--output-format json` 이
배제된 이유(`stages.py:523-528`)는 그대로 이 설계의 제약이다. 도구 단위 이벤트의 1차 소스는 그래서
argv 를 바꾸지 않는 경로(§2.3)다. ③ **개인정보는 쓰는 순간 마스킹한다** — 스트림은 "지운 뒤
보여 주는" 것이 아니라 "마스킹된 것만 쓰이는" 파일이다. SMS·메일 본문·인증번호·브라우저 스크린샷·
덤프 텍스트는 어떤 경로로도 이벤트 payload 에 들어가지 않는다(§4, CLAUDE.md §11·§11b·§12).

---

## §1 현황 전수표 — 스테이지별로 지금 남는 기록 · 읽는 경로 · 한계

### 1.1 지금 남는 기록 (전수)

범례 — **시점**: 누가 언제 쓰나(에이전트 = 스테이지의 `claude -p` 프로세스, 러너 = `runner.py`
`StagedExecutor`, 오케스트레이터 = `orchestrator.py` 틱). **읽는 경로**: 지금 그 기록을 실제로
읽는 코드(`파일:줄`). 전부 이 리포 HEAD(`e6154bd`) 확인. 볼트 측 경로는 `(볼트)` 표기.

| # | 기록 | 파일 / 형식 | 시점 (누가 · 언제) | 읽는 경로 (`파일:줄`) | 한계 |
|---|---|---|---|---|---|
| ① | **스테이지 아티팩트** | `Worker/runs/<run-id>/{requirements.md, implementation_plan.md, run.log, verify.md, report.md, delegate.md, quick.md, discover.md}` — 스테이지 → 파일명 표 `STAGE_ARTIFACT` `stages.py:409-418`; md 산문 + frontmatter(`overall_status`·`blocked_on`·`run_id`) | 에이전트가 **스테이지 종료 직전** Write(implement 의 `run.log` 만 러너·에이전트 공동 append — ②) | 러너: 존재 체크 `runner.py:1460-1464` → verdict `parse_verify_verdict` `:1546-1549`(`stages.py:708`) → `parse_blocked_on` `:1553`(`stages.py:777`) → `merge_landed` `:1608`(`stages.py:972`) → `append_followups` `:1669` → `parse_report_pr_url` `:1673`. 대시보드 `collect_run_detail` `macp_dashboard.py:420-469`(디렉토리의 `.md` 전부, `ARTIFACT_MAX` 절단). `trace.py:66-80` `artifact_origin`(own/inherited/unknown). `metrics.py:76-89`(바이트 = 비용 프록시). 재시도 상속 `inherit_artifacts` `stages.py:2626`(`inherited_from` 스탬프 `:2535`) | 스테이지가 **끝나야** 생긴다. 기계 판독은 frontmatter 몇 필드만, 나머지는 산문. 도구 호출 단위 정보 없음 |
| ② | **run.log** | `Worker/runs/<run-id>/run.log` — plain text 누적기 | 러너가 **각 스테이지 종료 후** `=== <stage> (rc=N) ===` + stdout **마지막 2000자** append `runner.py:1383-1384`(Solar 분기 `:1274-1275`); 타임아웃 시 `=== <stage> (timeout after Ns) ===` + partial 2000자 `:1328-1330`; WIP rescue 블록 `:1356-1357`·`:1440-1441`. Implement 에이전트는 자기 페이즈 블록을 Edit 로 append. **stderr 는 어디에도 보존되지 않는다**(`:1392-1394` "run.log keeps only stdout") | 대시보드: `STAGE_RE` `macp_dashboard.py:95` 로 스테이지별 rc 추출 + `LOG_TAIL_MAX` tail `:437-443`. `claims.classify` 는 run dir mtime 만 보조 증거로 `claims.py:113-117`. 그 외 소비자 없음 | 2000자 절단 — 스테이지 stdout 의 거의 전부 유실(형제 런 implement 46턴 중 마지막 요약 한 토막만 남음). 종료 후에만. 비구조화 |
| ③ | **stages.json** | `Worker/runs/<run-id>/stages.json` — JSON 1 객체(`STAGE_PROGRESS` `stages.py:2168`) | 러너의 read-modify-write `_update_progress` `stages.py:2177` 가족: `record_stage`(`completed`) `:2199` · `record_model_tier` `:2211` · `record_account` `:2218` · `record_orphaned_commits` `:2227` · `record_disk_guard` `:2243` · `record_related` `:2259` · `record_timeout` `:2286` · `record_capacity` `:2294` · `record_usage`(`usage.<stage>`) `:2429`. `usage.<stage>` 에는 **`USAGE_KEYS` 9개만**(`input_tokens`·`output_tokens`·`cache_creation_input_tokens`·`cache_read_input_tokens`·`num_turns`·`duration_ms`·`model`·`session_id`·`total_cost_usd` `:532-534`) — "그 외 아무것도 복사하지 않는다" `:530-531` | `completed_stages` 재개 `stages.py:2471`(`RESUMABLE` `:2174`); `metrics.py:85-89` 토큰·비용. 형제 런 실측 모양: `account`·`model_tier`·`model_tier_reason`·`usage.<stage>`·`completed`(볼트 `Worker/runs/20261113T132925-macp-event-triggers-design/stages.json`) | 스테이지 단위 **집계**만. 이벤트 시각(ts) 없음(`duration_ms` 만), 순서 없음, 도구 단위 없음 |
| ④ | **related.md** (입력) | `Worker/runs/<run-id>/related.md` — md | 러너가 스테이지 **시작 전** `record_related` `stages.py:2259` → `related_context.write_related` `related_context.py:428`("metadata only" R5 경계 `:28-32`, 렌더에서 집행 `:245`) | Research/Plan 프롬프트가 읽는 **입력** | 기록이 아니라 입력 — 런이 무엇을 했는지는 담지 않음 |
| ⑤ | **head_sha.txt · screens/\*.png** | `Worker/runs/<run-id>/head_sha.txt`(sha 한 줄) · `Worker/runs/<run-id>/screens/*.png` | 러너가 verify 직후 `_app_screen_check` `runner.py:781-830`(앱 리포만, 첫 화면 1장; 볼트 `Worker/README.md:84,102`) · `head_sha.txt` `runner.py:1530-1541` | `merge_hold.scan_device_evidence`(`runner.py:1530` 주석) · beta-sweep | 앱 리포 전용. 스크린샷은 **개인정보 노출면**(CLAUDE.md §12 "볼트·커밋·PR·Slack 에 옮겨 적지 않는다") |
| ⑥ | **CLI 세션 transcript** | `<CLAUDE_CONFIG_DIR>/projects/<cwd-slug>/<session_id>.jsonl` — **계정별 config dir**, 볼트 밖; `transcript_path` `stages.py:2327-2338`. 줄 종류(claude 2.1.277 실측, `tests/fixtures/claude_output/success.json:46-53`): `queue-operation` · `user` · `last-prompt` · `assistant`(**content block 당 1줄**, `message.usage` 반복) · `cost-state`(`totalCostUSD`) | CLI 가 **진행 중 append**(스테이지가 돌면서 줄이 늘어난다). 세션 id 는 러너가 `--session-id <uuid4>` 로 준다(`SESSION_ID_FLAG` `stages.py:536`, `build_stage_argv` `:559`) | `transcript_usage` `stages.py:2341-2396` 가 **usage 만** 되읽는다(assistant `message.usage` 를 `message.id` 별로 합산 + `cost-state`) → `stage_usage` `:2399-2426` → `record_stage_usage` `:2448` → ③. 도구 호출 단위 기록(`tool_use`/`tool_result` content block)은 **오직 여기에만** 있고 **어떤 소비자도 읽지 않는다** | 비공식 내부 포맷(2.1.277 로 핀, `capture.py:1-36` 재생성 레시피). 프롬프트·도구 출력 **원문** 포함 → 개인정보. 비-claude 백엔드엔 없다. 볼트·run dir 밖이라 trace/대시보드가 못 본다 |
| ⑦ | **PROGRESS 봉투** | `Worker/agents/<agent-id>/inbox/<ulid>.json` — Envelope JSON(`message.py:112`, 타입 상수 `:23-37`; 전송 `mailbox.py:1-22`, git 전송 가능) | 러너 `progress=lambda t: self._send(msg, m.PROGRESS, note=t)` `runner.py:2165` — coarse 한 줄: 계정(`:1185`), verdict(`:1548`), blocked(`:1555`), 체크포인트 노트(`:1361`·`:1445`), screen check(`:1541`) | 오케스트레이터 `apply_reply` `assignment.py:207-220`: `history` 에 `{ts,type,from}` **만** `:212`, `last_note` **200자** `:220`. 대시보드 `collect_assignments` `note[:90]` `macp_dashboard.py:329` → `/api/status` `active[]`; run 상세 ledger `_ledger_row` `:415` | **런 중 가시성의 유일한 채널**이지만 한 줄·200자·**마지막 것만**(history 는 본문을 버린다). 봉투 파일은 소비 후 삭제(`worker.py:2584`) |
| ⑧ | **Memory 에피소드** | `(볼트) Memory/episodes/<YYYY-MM-DD>-worker-<slug>.md` — md 템플릿(`.claude/commands/worker-report.md:123-154`, frontmatter `run_id` `:135`) | report 스테이지 **에이전트**가 작성(append-only) | graph_memory 색인(`graph-memory.md` §1.1), brief·planner | 에이전트 산문 요약 — 런 통계(토큰·도구 수·건드린 경로)가 없다. report 스테이지가 없는 quick/delegate 런은 에피소드도 없다 |
| ⑨ | **Gemini 사이드카** | `Worker/runs/<run-id>/<stage>.gemini-stats.json` — JSON(`GEMINI_STATS_SUFFIX` `agent_backend.py:241`, 경로 `:244-246`) | `GeminiAgentBackend.run_stage` 가 종료 시 `stats`+`session_id` **만** 저장(`response` 본문 미저장) `:368-379` | `report_usage` → `gemini_stats_usage` `:269-292` → ③ `usage.<stage>` | 종료 시 1회, 도구 단위 없음(Gemini 의 스트리밍 포맷은 §2.4) |
| ⑩ | **Solar(upstage) 루프** | 아티팩트(①)만 — 백엔드가 **직접** 쓴다(`solar_stage.py:1-28`, `agent_backend.py:524-551`) | 엔진 내 HTTP 도구 루프(서브프로세스 없음) | run.log 2000자 append `runner.py:1274-1275`; usage 채널 **없음**(`agent_backend.py:547-551`, `report_usage` None) | 루프 안의 `tool_calls` 는 **어디에도 기록되지 않는다** — 1급 소스가 될 수 있는 유일한 백엔드인데 지금은 0 |
| ⑪ | **알림(카톡·Slack)** | 메시지 — 고정 headline + 태스크 제목(+ PR 링크) | 오케스트레이터가 RESULT/FAIL/NEEDS_INPUT **봉투 payload** 로 작성: `_notify_review(a, msg)` `orchestrator.py:1186-1237` 는 `msg.payload` 만 읽고 `:1200`, 모든 경보는 `_notify_routed` `:1124` 한 게이트; `_notify_human_ready` `:2289-2310` 은 `runs_dir` 를 **링크 생성에만**. 채널 opt-in 은 `scripts/NOTIFICATIONS.md:15-28` | **런 파일을 읽지 않는다** — 봉투가 전부 | 종료·파킹 시점 1회. 내용은 headline 수준 |
| ⑫ | **런 시작 클레임** | `Worker/agents/_state/<agent-id>/claims/<msg_id>.json`(`claims.py:19`, `:129`; gitignored 런타임) | 러너 `attach_run` `claims.py:163-172` 가 `run_id`·`run_dir` 기록(`runner.py:2166`) | 재전달 분류 `classify` `claims.py:77-117` | "시작했다"는 사실만 |
| ⑬ | **(레거시) `worker.py` 큐 러너의 status.json** | `Worker/runs/<run-id>/status.json` — JSON(`write_status` `worker.py:4663-4668`; 스키마 볼트 `Worker/README.md:333`) | 레거시 큐 러너 경로(`RUNS_DIR` `worker.py:112`; 원격 호스트는 `Worker/remote/<host>/runs/` `:79-81,111`) | orphan 복구 `worker.py:1962-1989`, slug→최신 status `:4466-4470` | 오케스트레이터 러너의 ③ 과 **다른 포맷**이 같은 `Worker/runs/` 아래 공존 — "형식 상이"의 한 사례 |

**읽는 법.** 열세 기록 중 **런이 돌고 있는 동안 갱신되는 것은 ⑥(CLI 가 append, 그러나 미독)과
⑦(한 줄 봉투) 둘**뿐이다. 나머지는 전부 스테이지(또는 런)가 끝난 뒤의 스냅샷이다. 도구 호출 단위
정보를 가진 것은 ⑥ 하나이고, 그것을 읽는 코드는 usage 합산기(`transcript_usage`) 하나다.

### 1.2 대시보드와 알림이 그 기록을 읽는 경로

| 소비자 | 읽는 것 | 경로 (`파일:줄`) | 갱신 주기 |
|---|---|---|---|
| **웹 대시보드 `/api/status`** | ledger(⑦ 의 `last_note`), todo.db, run 디렉토리 목록, 에이전트 liveness | `do_GET` `macp_dashboard.py:2619-2647` → `_StatusCache.get` 단일 비행 `:2460-2497`(2026-10-22 110 스레드 폭주 사고의 수정 `:2464-2470`) → `collect_status` `:1632`; `active[]` 행 `note` = `last_note[:90]` `:329` | 페이지가 **3초마다 폴링**(`:2464` "Each client polls every 3 s", 셸 JS `fetch('/api/status')` `:2368`) |
| **웹 대시보드 `/api/run?id=`** | ② run.log(rc + tail) · ① md 아티팩트 · ledger 행 | `:2656-2663` → `collect_run_detail` `:420-469`; 셸 JS `:2331` | 탭 시 1회. **③ stages.json·⑥ transcript·⑨ 사이드카는 읽지 않는다** |
| **웹 대시보드 `/api/task?slug=`** | todo 필드·ledger·태스크 본문·그 slug 의 run 목록 | `:2648-2655` | 탭 시 1회 |
| **Android 앱** (`macp-dashboard-android`, **이 리포 밖 — 미확인**) | 같은 `/api/status`·`/api/run` 계약 — 판정은 서버 필드, 클라이언트는 렌더만(볼트 `docs/dashboard-parity.md` 전 절의 공통 규칙) | 웹과 동일 라우트 | 폴링(주기 미확인) |
| **알림** (⑪) | 봉투 payload 만 | `orchestrator.py:1124`, `:1186-1237`, `:2289-2310` | 런 종료·파킹 시 1회 |
| **`trace` CLI** | ①(출처 판정)·③·ledger·브랜치/PR | `trace.py:1-25`(읽기 전용 설계), `:66-80` | 사람이 부를 때 |
| **`metrics`** | ①(바이트)·③(`usage`)·ledger | `metrics.py:76-89` | 사람이 부를 때 |

**스트리밍 엔드포인트는 없다.** `do_GET` 의 라우트는 `/api/status`·`/api/task`·`/api/run`·
`/sms-setup`·그 외 `/api/*` 404·HTML 셸 여섯 분기(`macp_dashboard.py:2644-2671`)이고 `do_POST` 는
`/api/sms` 하나(`:2699-2752`)다. SSE·WebSocket·long-poll 어느 것도 없고, 서버는 stdlib
`ThreadingHTTPServer`(`:50`, `:2775`) — 연결당 스레드다(§3.4 의 비교 축).

### 1.3 한계 — 이 설계가 닫는 세 공백 (+ 부수 한계)

1. **실행 중에는 안 보인다.** ⑦ 외 전부 스테이지 종료 시 기록된다. 그 구조적 이유는
   `run_stage_subprocess` 가 stdout/stderr 를 PIPE 로 잡고 `communicate()` 로 **종료까지 버퍼링**하기
   때문이다(`stages.py:2949-2958`) — 실시간 reader 스레드가 없다. 형제 런의 implement 는 46턴·
   963,528 ms(약 16분) 동안 돌았고(③ 실측), 그 사이 화면에 보인 것은 `last_note` 한 줄이었다.
2. **형식이 스테이지·소스마다 다르다.** md 산문(①⑧) · plain tail(②) · JSON 집계(③⑨⑬) · 비공식
   JSONL(⑥) · Envelope(⑦) · 메시지(⑪) · 클레임 JSON(⑫) — 한 런을 재생하려면 **일곱 파서**가 필요하고,
   `trace.py` 가 생긴 경위(`:3-8` "run dir 5개를 손으로 열어 오진")가 그 비용의 실측이다. ③ 과 ⑬ 은
   같은 디렉토리 아래 다른 스키마로 공존한다.
3. **도구 호출 단위 감사가 안 된다.** `tool_use`/`tool_result` 는 ⑥ 에만 있고 아무도 읽지 않는다.
   "이 런이 `macp-sms` 를 불렀나", "어느 파일을 Write 했나", "어떤 명령이 몇 번 실패했나"를 사후에
   답할 수 없다. 아티팩트 authorship 조차 frontmatter `run_id` 추정이라는 **약한 증거**다
   (`trace.py:49-54`). Solar(⑩)는 도구 루프를 엔진이 직접 돌리는데도 기록이 0 이다.

**부수 한계.** (a) **stderr 유실** — run.log 는 stdout 만(`runner.py:1392-1394`); 모델캐시 무효화와
계정 회전만 stderr 를 즉석에서 본다(`:1392-1394`, `:1454-1455`). (b) **호스트 로컬·ephemeral** —
`Worker/runs/` 는 볼트 `.gitignore:75` 로 디렉토리 전체 비추적(`*.log` 는 전역 `:25`), `metrics.py:81-83`
가 "gitignored and ephemeral … only a recent local SAMPLE" 로 명시; 클라우드 본부 인수가 상속하지
않고 디스크 가드가 지운다. (c) **M5 원격 호스트**는 `Worker/remote/<host>/runs/` 를 **git 추적**하되
`*.log` 는 제외(`.gitignore:142-145`, `worker.py:79-81`) — 즉 원격 run.log 는 본부에 오지 않는다.
(d) **비-claude 백엔드**(Gemini·Solar)는 ⑥ 조차 없다 — 도구 단위 기록이 0 인 채 스테이지를 돈다.
(e) **시각이 없다** — ③ 은 `duration_ms` 만, ⑦ 은 봉투 `ts` 만 있어 "어느 도구 호출이 몇 분을
먹었나"를 재구성할 수 없다.

---
