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

## §2 이벤트 스키마 — 공통 봉투 · 종류별 payload · 소스 선택 · CLI stream-json 매핑

### 2.1 공통 봉투(envelope)와 스키마 버전

파일은 **런별 하나** `events.ndjson`(위치는 §3.1), 한 줄 = 한 이벤트 = JSON 객체 하나. 모든 줄이
같은 봉투를 쓰고 `payload` 만 종류별로 다르다. 합성 예시(실 세션 id·경로·계정·host 없음):

```json
{"v":1,"run_id":"20260101T000000-demo-task","seq":1,"ts":"2026-01-01T00:00:00.000Z","stage":null,"kind":"stream.meta","src":"runner","payload":{"schema_version":1,"producer":"macp-runner","producer_version":"<engine-sha>","task_id":"demo-task","host_label":"hq","backend":"claude","tool_events":true,"inherited_from":null,"redaction_profile":"v1"}}
{"v":1,"run_id":"20260101T000000-demo-task","seq":2,"ts":"2026-01-01T00:00:00.120Z","stage":null,"kind":"run.started","src":"runner","payload":{"task_id":"demo-task","branch":"worker/demo-task","repo":"https://example.invalid/demo.git","worker_mode":"auto","model_tier":"heavy","account_name":"<profile-name>","stages_planned":["research","plan","implement","verify","report"]}}
{"v":1,"run_id":"20260101T000000-demo-task","seq":3,"ts":"2026-01-01T00:00:00.300Z","stage":"research","kind":"stage.started","src":"runner","payload":{"stage":"research","backend":"claude","model":"<model-id>","effort":"<effort>","max_turns":40,"session_id":"00000000-0000-4000-8000-000000000000","cwd_kind":"vault","tools_count":2}}
{"v":1,"run_id":"20260101T000000-demo-task","seq":41,"ts":"2026-01-01T00:04:12.345Z","stage":"implement","kind":"tool.called","src":"claude","payload":{"tool":"Bash","tool_use_id":"toolu_<id>","category":"shell","input_excerpt":"git -C <checkout> status --short","input_bytes":41,"input_sha256":"<hex>","sensitive":false}}
```

| 필드 | 타입 | 의미 · 규칙 |
|---|---|---|
| `v` | int | **스키마 버전**. 필드 **추가**는 같은 `v`(소비자는 모르는 필드·모르는 `kind` 를 무시한다 — 공식 headless 문서가 자기 출력에 권하는 "값을 모르면 무시하고 feature-detect 하라"와 같은 태도). 필드 **제거·의미 변경·`kind` 개명**은 `v+1`. 소비자는 자기가 아는 `v` 보다 크면 라이프사이클 kind(`stream.*`·`run.*`·`stage.*`·`state.*`)만 읽고 나머지는 "미지" 로 표시한다. 첫 줄 `stream.meta.schema_version` 과 중복이지만 **줄 단위 자기서술**이 NDJSON 의 요점이라 둘 다 둔다 |
| `run_id` | str | run 디렉토리 이름과 동일 `<YYYYMMDDTHHMMSS>-<slug>`(대시보드 `RUN_ID_RE` 가 받는 모양) |
| `seq` | int | **파일 내 단조 증가**(1 = `stream.meta`). 시간축이 아니라 **런 내 순서**다 — 같은 `ts` 의 tie-break 이자 커서(§3.4 `since=<seq>`) |
| `ts` | str | UTC ISO 8601, 밀리초, `Z`. CLI transcript 의 `timestamp`(`success.json:48` `"2026-09-19T10:06:40.203Z"`) 와 같은 형식이라 그대로 옮긴다. 러너 이벤트는 러너 시계, 백엔드 이벤트는 원문의 timestamp(없으면 수신 시각) |
| `stage` | str · null | 스테이지 이름(`STAGE_ARTIFACT` 의 키) 또는 런 수준 이벤트의 `null` |
| `kind` | str | `<domain>.<event>` 소문자 점 표기(§2.2) |
| `src` | str | 생산자: `runner` · `orchestrator` · `claude` · `gemini` · `solar` · `hook` |
| `payload` | obj | 종류별 객체(§2.2). 발췌(`*_excerpt`) 필드는 **§4 마스킹을 거친 뒤에만** 들어간다 |
| `src_host` | str (선택) | 원격 러너의 이벤트를 본부가 ingest 할 때 **본부가** 붙이는 호스트 라벨(§3.3). 로컬 이벤트엔 없다 |

**단일 writer.** 한 파일의 writer 는 **그 런의 러너 프로세스**(`StagedExecutor`) 하나다 — `seq` 단조성이
거기서 나온다. 러너와 같은 프로세스 안의 생산자(라이프사이클 층, Solar 루프, transcript tail 스레드)는
writer 객체를 직접 부르고, **다른 프로세스**의 생산자(스테이지 서브프로세스 안의 hook, 자매 ①)는
`Worker/runs/<run-id>/events.in/<ulid>.json` 드롭박스에 1이벤트 1파일로 두면 러너가 다음 flush 에서
자기 `seq` 를 붙여 본 파일로 접는다(mailbox 의 "불변 파일 1통" 모양 `mailbox.py:5`). 스테이지
에이전트는 `--add-dir` 로 run dir 를 **읽을** 수 있어도 `events.ndjson` 을 쓰지 않는다 — 쓰는 순간
마스킹(§4)이 한 곳(writer)에서 집행되기 때문이다. 오케스트레이터(`src: orchestrator`, 다른 프로세스·
다른 틱)의 `state.transition` 은 러너가 끝난 뒤에도 생기므로 **같은 드롭박스**에 두고, 러너가 없으면
오케스트레이터 틱이 접는다(파일 잠금 1개 — `sms.py:85` `_locked` 선례). 원격 ingest 는 `(run_id,
src_host, seq)` 로 멱등, 병합 순서는 `ts` 다음 `(src_host, seq)`(§3.3).

**첫 줄 `stream.meta`**(`seq=1`)는 파일의 자기서술이다: `schema_version` · `producer`(`macp-runner`) ·
`producer_version`(엔진 체크아웃 단축 sha) · `task_id` · `host_label`(합성 라벨, hostname 아님) ·
`backend` · `tool_events`(도구 단위 층이 켜졌나 — §2.5) · `inherited_from`(재시도가 상속한 이전 run_id,
`inherit_artifacts` `stages.py:2626` 와 같은 의미) · `redaction_profile`(§4 규칙표의 버전).

### 2.2 종류별 payload

열 — **src** 생산자 · **시점** 러너 코드에서 쓰이는 위치(`파일:줄` 은 지금 코드의 해당 지점; 구현 leaf 는
그 옆에 emit 을 둔다) · **마스킹** §4 규칙 적용 여부 · **소비자** 누가 읽나(D 대시보드 · R 재생/trace ·
G 그래프 메모리 · A 승인 브로커(자매 ①) · T 내부 트리거(자매 ③)).

| kind | payload 필드 | src | 시점 | 마스킹 | 소비자 |
|---|---|---|---|---|---|
| `stream.meta` | §2.1 첫 줄 | runner | 파일 생성 시 | — | 전부 |
| `run.started` | `task_id` · `branch` · `repo`(URL) · `worker_mode` · `model_tier` · `account_name`(프로필 **이름**만, config dir 경로 아님) · `stages_planned[]` | runner | run dir 생성·클레임 부착 직후(`runner.py:2166` `attach_run`) | — | D R |
| `stage.started` | `stage` · `backend` · `model` · `effort` · `max_turns` · `session_id`(UUID) · `cwd_kind`(`vault` 또는 `checkout` — 경로 아님) · `tools_count` | runner | `backend.run_stage` 호출 직전(`runner.py:1303`, Solar `:1268`) | 프롬프트 본문 **비수록** | D R G |
| `stage.finished` | `stage` · `rc` · `outcome`(`success` · `exhausted` · `error` · `timeout` — `agent_backend.py:65-67` 의 3값 + timeout) · `duration_ms` · `artifact`(`{name, bytes, sha256}` 또는 null) · `stdout_bytes` · `stderr_bytes` | runner | run.log append 와 같은 지점(`:1383-1384`; 타임아웃 `:1328-1330`; Solar `:1274-1275`) | stdout/stderr 는 **길이만** | D R G T |
| `stage.usage` | `USAGE_KEYS` 9개(`stages.py:532-534`) 또는 `{unavailable: <reason>}`(`:2411-2424` 의 사유 문자열 그대로) | runner | `report_usage` 직후(`:1380-1382`) | — | D G |
| `tool.called` | `tool` · `tool_use_id` · `category`(`shell` · `file_read` · `file_write` · `web` · `mcp` · `agent` · `other`) · `input_excerpt` · `input_bytes` · `input_sha256` · `sensitive`(bool) | claude · solar · gemini(§2.4 ⑬) | 도구 호출 블록을 본 순간(transcript tail · stream-json · Solar 루프) | **§4 도구별 규칙** | D R G(집계만) A |
| `tool.result` | `tool_use_id` · `tool` · `is_error` · `output_excerpt`(또는 null) · `output_bytes` · `output_sha256`(또는 null) · `duration_ms`(알 때) | 같음 | 도구 결과 블록을 본 순간 | **§4** | D R |
| `agent.message` | `text_excerpt` · `text_bytes` · `text_sha256` | claude · solar | assistant `text` 블록 | 마스킹 + 2 KiB 절단 | D |
| `agent.compacted` | `trigger`(`manual` · `auto`) · `pre_tokens` | claude | `system/compact_boundary` | — | D R |
| `api.retry` | `attempt` · `max_retries` · `retry_delay_ms` · `error`(`rate_limit` 등 분류값) | claude | `system/api_retry` | — | D(운영) |
| `agent.question` | `question_id` · `text_excerpt` · `options[]`(또는 null) | claude · solar | 에이전트가 사람 답을 요구하는 순간 — 지금은 NEEDS_INPUT 봉투의 전조 | 마스킹 | A D |
| `approval.requested` | `approval_id` · `kind`(`permission` · `question` · `confirm`) · `tool` · `summary` · `options[]` · `expires_at` | hook · orchestrator | **자매 ① 이 생산** | 마스킹(summary) | A D |
| `approval.resolved` | `approval_id` · `decision` · `by` · `note` | orchestrator | **자매 ① 이 생산** | 마스킹(note) | A D R |
| `state.transition` | `from` · `to` · `reason` · `via`(`PROGRESS` · `RESULT` · `FAIL` · `NEEDS_INPUT` · `tick`) — 상태 집합 = `assignment.py:18-25` 8종(`dispatched` · `accepted` · `in_progress` · `done` · `failed` · `rejected` · `awaiting_human` · `needs_input`; `docs/concepts.md:80-81`), 표시 전용 `deferred` 는 **상태가 아니라서 제외**(`assignment.py:29`) | orchestrator | `apply_reply`(`assignment.py:207-220`) 와 틱의 상태 변경 지점 | — | D T R |
| `progress.note` | `text` | runner | PROGRESS 봉투 미러(`runner.py:2165` 람다 안) — **병존 단계**(§6 S1), 봉투는 그대로 보낸다 | 마스킹 | D |
| `artifact.written` | `path`(run dir **상대**) · `bytes` · `sha256` · `by`(`agent` · `runner`) · `stage` | runner | 아티팩트 존재 체크 지점(`:1460-1464`)에서 해시; run.log 는 스테이지 끝에 1회 | — | R G(§5.4) |
| `checkpoint.pushed` | `sha` · `branch` · `reason`(`implement_done` · `timeout` · `capacity`) | runner | `checkpoint_push` 결과(`:1359`, `:1443`, implement 완료 `:1467` 이하) | — | R |
| `capacity.limited` | `stage` · `rc` · `account_switch`(`{from, to}` 또는 null) | runner | 용량 게이트 안(`:1417-1459`) | 마커 원문 비수록 | D(운영) T |
| `run.finished` | `outcome`(`ok` · `fail`) · `summary` · `review_required` · `pr_url` · `merged` · `no_op` · `ui_merge_hold` · `blocked_on` · `stages_completed[]` | runner | `ExecOutcome` 생성 지점(`:1716`, 각 실패 return) | summary 마스킹 | 전부 |
| `stream.truncated` | `reason`(`run_cap` · `line_cap`) · `dropped_kinds[]` · `dropped_count` | runner | 상한 도달 시(§3.2) | — | 전부("불완전" 표시) |

**승인 두 종류의 경계.** `approval.requested` / `approval.resolved` 는 **자매 설계
`macp-inflight-approval-cards-design`(depends_on 이 태스크)이 생산자**다. 이 노트는 kind 이름과
필드만 **예약**하고, 대기·만료·재개·브로커(어느 프로세스가 스테이지를 멈추고 어떻게 답을 되돌리나)의
의미는 그 문서에 둔다. 여기서 정한 접점은 둘뿐이다 — ① hook 생산자는 `events.in/` 드롭박스로 쓴다(§2.1
단일 writer), ② `result.permission_denials`(§2.4 ⑥)와 `system/permission_denied` 는 지금 코드가 버리는
"승인이 필요했던 순간"의 **사후 증거**라 `approval.requested{kind: permission}` 의 전조로 매핑만 적어
둔다(브로커가 생기기 전까지는 이벤트를 **만들지 않는다** — 거부된 사실만 `tool.result{is_error}` 에 남는다).

### 2.3 이벤트 소스 비교 — 다섯 후보, 권장 하나

열 — **argv 동일**: claude 스테이지 argv 가 바이트 동일한가(골든 테스트 `test_agent_backend_golden.py`
무수정) · **stdout 의미**: 스테이지 루프가 읽는 stdout/stderr(run.log tail `runner.py:1383-1384`,
`is_capacity_limit(r.stdout)` `:1417-1418`, 모델캐시 무효화 `:1392-1394`, 계정 회전 `:1454-1455`)가
그대로인가 · **공식 스키마**: 벤더 공식 문서가 형식을 보증하나 · **실시간** · **도구 입도** · **원문
노출**: 프롬프트·도구 I/O 원문이 들어와 §4 마스킹이 필수인가 · **중립**: 세 백엔드에 같은가 · **kill
switch**: env 하나로 완전히 끌 수 있나(`MACP_STAGE_USAGE=0` `stages.py:539-544` 패턴) · **주입 경로**:
켜려면 무엇을 바꾸나.

| 소스 | argv 동일 | stdout 의미 | 공식 스키마 | 실시간 | 도구 입도 | 원문 노출 | 중립 | kill switch | 주입 경로 |
|---|---|---|---|---|---|---|---|---|---|
| **(a) claude `--output-format stream-json`** | ✗ 플래그 추가 → 골든 테스트 의식적 갱신 | **✗** stdout 이 NDJSON 이 된다 → run.log tail·`is_capacity_limit` 스캔·모델캐시 훅이 읽는 텍스트가 바뀜. 실측: JSON 모드는 stderr 의 설정 경고를 **없앴다**(`success.json:67` `""` vs 텍스트 모드 `:30`) — `stages.py:523-528` 이 `--output-format json` 을 배제한 바로 그 이유. 게다가 `communicate()` 버퍼링(`:2949-2958`)이라 실시간엔 **reader 스레드**가 필요 | ✓ 확인(headless·CLI 레퍼런스; 단 **스키마 안정성 선언은 없음** — "feature-detect, 모르는 값 무시" 권고만) | ✓(reader 전제) | ✓ `tool_use` / `tool_result` 블록 | ✓ 마스킹 필수 | ✗ | ✓ `MACP_RUN_EVENTS_SOURCE` | argv + stdout **재구성 어댑터**(`result.result` 를 텍스트 stdout 으로 복원해 기존 소비자에 넘김) |
| **(b) claude 세션 transcript tail** | **✓** `--session-id` 는 이미 있다(`stages.py:536`, `:696-700` 근방) | **✓** 서브프로세스·파이프 무변경 | ✗ 비공식 내부 포맷 — 그러나 **이미** `transcript_usage`(`:2341-2396`)가 같은 포맷에 의존하고 `capture.py` 로 버전을 핀·재생성한다(2.1.277) | ◐ CLI 가 진행 중 append → tail 스레드로 **근실시간**(세션 파일은 첫 메시지 뒤 생성 — `transcript_path` `:2327-2338` 글롭 재시도) | ✓ 같은 블록(assistant 줄 `message.content[]` 블록당 1줄 `:2346-2349`; `tool_use` 줄 모양은 픽스처에 아직 없음 → §2.4 ②) | ✓ 마스킹 필수(프롬프트·도구 출력 원문) | ✗ | ✓ | 없음 — 러너 스레드 하나 |
| **(c) Claude Code hooks** (PreToolUse·PostToolUse…) | ◐ `.claude/settings.json` 은 헤드리스 하드 가드(§6.3) → `--settings '<inline json>'` 가 후보: `--settings` 가 "파일 경로 또는 인라인 JSON, 세션 한정 오버라이드"인 것은 **확인**, 거기에 `hooks` 키를 실을 수 있다는 **명시는 없음 → 미확인** | ✓(hook 은 stdout 을 안 바꿈) | ✓ 확인(stdin JSON: `session_id`·`transcript_path`·`cwd`·`hook_event_name`·`tool_name`·`tool_input`·`tool_use_id`·`tool_response`·`duration_ms`) | ✓ 동기 | ✓ | ✓ | ✗ | ✓ `disableAllHooks`(확인) | settings 주입(미확인) — 그리고 hook 은 **실행 경로에 끼어든다**(exit 2 = 블록, 기본 timeout 600 s, `async:true` 는 결정 무효) → 관측이 실행을 바꿀 수 있는 **통제 수단** |
| **(d) 러너 라이프사이클 층** | ✓ | ✓ | (우리 스키마) | ✓ 스테이지 경계 입도 | ✗ | ✗ 메타만 | **✓** 세 백엔드 공통 | ✓ `MACP_RUN_EVENTS=0` | 없음 — `runner.py` 스테이지 루프에 emit 추가 |
| **(e) Solar 루프 직접 emit** | 해당 없음(서브프로세스 없음 `solar_stage.py:1-28`) | 해당 없음 | (우리 스키마) | ✓ | ✓ 루프가 `tool_calls` 를 안다 | ✓ 마스킹 | Solar 한정 | ✓ | `solar_stage.run` 에 콜백 하나 |
| **(f) Gemini `--output-format stream-json`** | ✗ 지금은 usage 용 `--output-format json` 을 붙인다(`agent_backend.py:453`) — 바꾸면 stats 사이드카(`:368-379`) 경로도 바뀜 | ✗ stdout 이 JSONL | ◐ 공식 페이지가 JSONL 스트림과 이벤트 **종류 6개**(`init`·`message`·`tool_use`·`tool_result`·`error`·`result`)를 **확인**해 주지만 **필드명·플래그 리터럴은 미기재(미확인)** | ✓(reader) | ✓(종류 확인·필드 미확인) | ✓ | ✗ | ✓ | argv + 어댑터 — 필드 확인(실측 캡처) 선행 |

**권장 = 두 층.** ⓐ **러너 라이프사이클 층 (d)** 를 **항상, 모든 백엔드에** 켠다 — `run.started` ·
`stage.started` · `stage.finished` · `stage.usage` · `artifact.written` · `checkpoint.pushed` ·
`capacity.limited` · `progress.note` · `run.finished` 는 러너가 이미 아는 사실이라 CLI 변경 0, 백엔드
중립, 원문 노출 0 이다. §1.3 의 공백 ①(실행 중 안 보임)의 **절반**(스테이지 경계·usage·체크포인트)과
②(형식 상이)·부수 (e)(시각 없음)는 이 층만으로 닫힌다. ⓑ **도구 단위 층**은 백엔드별 어댑터
(capability `event_stream`, §2.5)로 — **claude 1차 = (b) transcript tail**, **2차 = (a) stream-json**
(kill switch `MACP_RUN_EVENTS_SOURCE=transcript|stream-json`, 기본 `transcript`; §6 S6 의 선결 조건 —
골든 테스트 의식적 갱신 + stdout 재구성 어댑터 + transcript 를 fallback 으로 — 가 갖춰진 뒤에만),
**Solar = (e) 직접**, **Gemini = (f) 후속 leaf**(필드를 실측 캡처로 확인한 뒤; 그때까지 ⓐ + `stage.usage`
로 degrade). **hooks (c) 는 이 스트림의 소스로 쓰지 않는다** — 관측과 통제의 경계 때문이다: hook 은
도구 실행을 **막을 수** 있는 수단이고(exit 2·`permissionDecision`), 그 힘이 필요한 곳은 승인 카드
(자매 ①)다. 자매가 hook 으로 `approval.*` 를 **쓸** 때의 접점(`events.in/` 드롭박스)만 §2.1 에 예약했다.

(b) 를 (a) 보다 먼저 두는 이유를 명시한다. 전제 ②(argv·stdout 의미 바이트 동일)를 **플래그 0개·파이프
0개 변경**으로 지키고, `--session-id`·`transcript_path`·`capture.py` 라는 **이미 지고 있는** 비공식 포맷
리스크에 올라타며, 관측 스레드가 죽어도 스테이지 결과엔 아무 영향이 없다(`transcript_usage` 가 그렇듯
"never raises"). 비용은 (ⅰ) 세션 파일 생성 지연(첫 메시지 뒤)과 (ⅱ) 포맷 드리프트 — 둘 다 S2 가
픽스처(`tool_use` 가 든 캡처)로 못박는다. (a) 가 주는 것은 **공식 형식**과 `system/init`·`api_retry` 같은
transcript 에 없는 줄인데, 그 값어치는 어댑터·골든 갱신 비용을 치른 뒤의 S6 에서 사람이 판정한다.

**공식 문서 확인 결과(2026-11-13)가 Plan 기본안에 준 변화.** 전제는 유지됐다 — stream-json 은
공식·실시간이지만 stdout 의미를 바꾼다. 바뀐 것 둘: ① Gemini 는 공식 JSONL 스트림이 **있고** 이벤트
종류에 `tool_use`·`tool_result` 가 **있다**(필드 미확인) — "영구 degrade" 가 아니라 **후속 leaf** 로
적는다. ② hooks 는 `-p` 에서 실행된다는 것과 `--settings` 인라인 JSON 이 확인됐지만 hooks 주입 가능
여부는 미확인 — 자매 소유라는 결론은 그대로다. 보강 하나: Agent SDK overview 페이지에는 메시지 타입이
없어 TypeScript 레퍼런스로 대체 확인했다(§2.4 출처표).

### 2.4 매핑표 — Claude Code stream-json(및 동형 transcript) · Gemini · Solar → macp kind

**출처(모두 WebFetch 로 열어 확인, 조회 2026-11-13; 1차 후보가 301 이면 최종 도달 URL 을 적는다).**

| # | 출처 | 최종 URL | 확인 요지 |
|---|---|---|---|
| U1 | Claude Code — 프로그램적 실행(headless) | `https://code.claude.com/docs/en/headless` (1차 후보 `docs.claude.com/en/docs/claude-code/sdk/sdk-headless` 는 301 체인 → `platform.claude.com/docs/en/agent-sdk/headless`; 2차 후보가 200) | `--output-format text`·`json`·`stream-json`; stream-json = 줄당 JSON 객체 1이벤트, 마지막 줄 `result`; `--verbose`+`--include-partial-messages` → `stream_event`; `system/init`(model·tools·MCP servers·plugins), `system/api_retry`, `permission_denied` → `result.permission_denials`; `assistant`/`user` 에 `parent_tool_use_id`; **스키마 안정성 선언 없음** |
| U2 | Claude Code — CLI 레퍼런스 | `https://code.claude.com/docs/en/cli-reference` (`docs.claude.com/…/cli-reference` 301) | `--output-format` {text, json, stream-json} · `--input-format` {text, stream-json} · `--include-partial-messages`("requires --print and --output-format stream-json") · `--settings`(파일 경로 또는 인라인 JSON, 세션 한정 오버라이드) · `--session-id`(UUID) · `--include-hook-events`(stream-json 필요) · `--bare` · `--setting-sources` · `--restricted` |
| U3 | Claude Code — Hooks 레퍼런스 | `https://code.claude.com/docs/en/hooks` (`docs.claude.com/…/hooks` 301) | 이벤트 33종(PreToolUse·PostToolUse·PostToolUseFailure·Notification·PermissionRequest·PermissionDenied·Stop·SessionStart·SessionEnd …); 설정 위치 7곳; stdin 공통 `session_id`·`transcript_path`·`cwd`·`hook_event_name`·`permission_mode`; PreToolUse `tool_name`·`tool_input`·`tool_use_id`; PostToolUse +`tool_response`·`duration_ms`; `-p` 에서 실행됨; `async:true`; 기본 timeout 600 s |
| U4 | Claude Agent SDK — 개요 | `https://code.claude.com/docs/en/agent-sdk/overview` (`docs.claude.com/en/api/agent-sdk/overview` 301→307 체인) | **메시지 타입·블록 타입 없음**(capability 표만; TS/Python 레퍼런스로 위임) |
| U5 | Claude Agent SDK — TypeScript 레퍼런스 | `https://code.claude.com/docs/en/agent-sdk/typescript.md` | `SDKMessage` union; `SDKSystemMessage{type:"system", subtype:"init", session_id, cwd, tools[], mcp_servers[], model, permissionMode, claude_code_version, capabilities?}`; `SDKAssistantMessage{type:"assistant", message: BetaMessage, parent_tool_use_id}`(블록 스키마는 **열거 없음**); `SDKUserMessage{type:"user", message, tool_use_result?}`; `SDKResultMessage{type:"result", subtype: success · error_max_turns · error_during_execution · error_max_budget_usd · error_max_structured_output_retries, duration_ms, duration_api_ms, is_error, num_turns, result, total_cost_usd, usage, modelUsage, permission_denials[{tool_name, tool_use_id, tool_input}]}`; `SDKPartialAssistantMessage{type:"stream_event"}`; `SDKCompactBoundaryMessage{subtype:"compact_boundary", compact_metadata{trigger, pre_tokens}}` |
| U6 | Gemini CLI — headless | `https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md` (200, 리다이렉트 없음; `agent-backends.md:194` 가 2026-10-22 에 확인한 것과 같은 URL) | JSON 출력 `{response, stats, error}`; **스트리밍 JSONL** 이벤트 `init`·`message`·`tool_use`·`tool_result`·`error`·`result`; exit 0/1/42/53. **플래그 리터럴·`stats` 모양·이벤트 필드명 미기재(미확인)** |
| F1 | 실측 픽스처(claude 2.1.277, 2026-09-19) | `todo_system/orchestrator/tests/fixtures/claude_output/success.json` · `error_max_turns.json` | `result` 객체 `:66`; JSON 모드 stderr `""` `:67`(텍스트 모드 `:30` 과 대조); transcript 줄 종류 `:46-53`; `error_max_turns.json:73` `subtype:"error_max_turns"`·`is_error:true`·`num_turns:2`; 재생성 `capture.py:1-36` |

**매핑.** `→` 오른쪽이 macp kind. "transcript 동형" = 세션 transcript 에도 같은 모양의 줄이 있어 (b) 가
같은 어댑터를 쓰는가.

| # | CLI 메시지(`type` / `subtype` / content block) | 쓰는 필드 | → macp kind | transcript 동형 | 출처 · 확인 |
|---|---|---|---|---|---|
| ① | `system` / `init` | `session_id` · `model` · `tools[]`(→ `tools_count`) · `mcp_servers[]`(→ 개수) · `claude_code_version` · `capabilities[]`; **`cwd` 는 비수록**(절대 경로, §4) | `stage.started` 보강(이미 러너가 낸 이벤트에 `claude_code_version`·`tools_count` 를 더하는 두 번째 `stage.started` 는 내지 않고 `agent.session{…}` 1건으로) | ✗ transcript 엔 init 줄이 없다(`F1:46-53` 부재) | U1(산문) · U5(필드) — 확인 |
| ② | `assistant` + content block `tool_use` | 블록의 `id` → `tool_use_id`, `name` → `tool`, `input` → `input_excerpt`/`input_bytes`/`input_sha256`(§4 규칙 뒤) | `tool.called` | ✓ assistant 줄 `message.content[]`, 블록당 1줄(`stages.py:2346-2349`) | U1(블록 존재, 서브에이전트 절) · U5(`message: BetaMessage` — **블록 필드는 페이지에 열거 없음**) · F1 은 `thinking`·`text` 블록만 담음(`:50-51`) → `tool_use` 블록의 `id/name/input` 은 **미확인(S2 가 캡처로 확정)** |
| ③ | `user` + content block `tool_result` | `tool_use_id` · `content`(→ `output_excerpt`/`output_bytes`/`output_sha256`, §4) · `is_error` | `tool.result` | ✓ user 줄 | U5(`tool_use_result` 서술; `tool_use_id`·`is_error` 는 지나가는 언급) → 필드 **미확인(S2 캡처)** |
| ④ | `assistant` + `text` | `text` | `agent.message` | ✓(`F1:51`) | F1 — 확인 |
| ⑤ | `assistant` + `thinking` | — | **기록 안 함**(블록 수만 `agent.message.blocks` 에) | ✓(`F1:50`) | F1 — 확인 |
| ⑥ | `result` | `subtype`·`is_error` → `stage.finished.outcome`(`success`→`success`; `error_max_turns`→`error`; `error_during_execution`→`error`; 용량 마커는 러너의 `is_capacity_limit` 판정을 따라 `exhausted`) · `duration_ms` · `num_turns`·`total_cost_usd`·`usage`·`modelUsage` → `stage.usage` · `permission_denials[]` → `approval.requested{kind: permission}` **전조**(자매 ①; 지금은 매핑만) · `result`(텍스트) → 비수록(run.log 가 2000자) | `stage.finished` + `stage.usage` | ✗ transcript 엔 `result` 줄이 없고 `cost-state` 가 그 자리(⑪) | F1 `:66`·`:73` · U5 — 확인 |
| ⑦ | `stream_event`(`--include-partial-messages`) | `event.delta` | **미채택** — 토큰 델타는 기록 가치가 없고 양만 늘린다 | ✗ | U1(jq 예) · U2 — 확인 |
| ⑧ | `system` / `api_retry` | `attempt` · `max_retries` · `retry_delay_ms` · `error` | `api.retry` | ✗ | U1 — 확인 |
| ⑨ | `system` / `permission_denied` | — | `approval.requested{kind: permission}` 전조(자매 ①) | ✗ | U1 — 확인(필드 표는 없음) |
| ⑩ | `system` / `hook_started` · `hook_progress` · `hook_response`(`--include-hook-events`) · `system` / `compact_boundary` | — / `compact_metadata{trigger, pre_tokens}` | hook 계열 **비채택**(자매 소유) / `agent.compacted` | ✗ | U1·U2 / U5 — 확인 |
| ⑪ | transcript 전용 `queue-operation` · `last-prompt` · `cost-state` | `queue-operation.content`·`lastPrompt` = **프롬프트 원문 → 비수록**; `cost-state.totalCostUSD` → `stage.usage.total_cost_usd`(`transcript_usage` 와 같은 규칙 `stages.py:2356-2360`) | (`stage.usage` 보강) | transcript 전용 | F1 `:46-47`, `:49`, `:53` — 확인 |
| ⑫ | `assistant` / `user` 의 `parent_tool_use_id` | 서브에이전트 구분자 | `tool.called.parent_tool_use_id`(선택 필드) — 서브에이전트의 도구 호출을 부모 `Agent` 호출에 매단다 | ✗(미확인) | U1·U5 — 확인 |
| ⑬ | **Gemini** stream-json `init` · `message` · `tool_use` · `tool_result` · `error` · `result` | **미확인** — 종류만 확인 | `stage.started` 보강 · `agent.message` · `tool.called` · `tool.result` · `progress.note` · `stage.finished`+`stage.usage` | 해당 없음 | U6 — 종류 확인 · **필드 미확인**(후속 leaf 가 실측 캡처로) |
| ⑭ | **Gemini** `--output-format json` `stats` | `stats.models.<model>.{api, tokens}` | `stage.usage`(`gemini_stats_usage` `agent_backend.py:269-292` 의 매핑 그대로) | 해당 없음 | 엔진 코드 — 확인(사이드카 `:368-379`) |
| ⑮ | **Solar** 루프의 `tool_calls` / 도구 결과 / `finish` | 엔진 내부 객체 | `tool.called` · `tool.result` · `stage.finished` — 어댑터 없이 **직접** | 해당 없음 | `solar_stage.py:1-28` — 확인(외부 문서 불필요) |

### 2.5 백엔드 중립 degrade 규칙

- `agent_backend.CAPABILITIES`(`agent_backend.py:53-62`, 8종)에 **`event_stream`**("도구 호출 단위
  이벤트를 낼 수 있다")을 **더한다 — 서술만**(구현은 §6 S2). 지금의 집합으로 보면 claude(`_CAPS =
  CAPABILITIES` `:133`)는 S2 뒤 ✓, Solar(`:554` `{headless, model_select}`)는 (e) 로 ✓, Gemini
  (`:415-416`)는 (f) 전까지 ✗.
- **미지원은 거절 사유가 아니다.** `missing_capabilities`(`:70-81`)는 `stages.required_capabilities`
  (`stages.py:239`)가 요구하는 능력이 빠지면 디스패치를 **거절**하지만, `event_stream` 은 관측 기능이라
  `required_capabilities` 에 넣지 않는다. 없으면 ⓐ 라이프사이클 층만 쓰고 `stream.meta.tool_events:
  false` 로 **스스로 밝힌다** — 소비자(대시보드 이벤트 탭)는 그 플래그로 "이 런은 도구 단위 기록이 없다"를
  표시한다(가짜 빈 목록이 아니라).
- 세 백엔드의 기본값: claude = (b) transcript tail(`MACP_RUN_EVENTS_SOURCE=transcript`), Solar = (e)
  직접, Gemini = 없음(ⓐ + `stage.usage`). 전체 스위치 `MACP_RUN_EVENTS=0` 은 **세 백엔드 공통**으로
  파일 생성·스레드·드롭박스 접기 전부를 끈다(`usage_enabled()` `stages.py:539-544` 와 같은 "호출 시점에
  읽는 env, 명시적 0 만 끔" 규칙).

---

## §3 기록·전달 — 위치 · 크기·보존 · 원격 러너 → 본부 · 대시보드 구독

### 3.1 파일 위치 — 정본 `Worker/runs/<run-id>/events.ndjson`, 둘 다 "git 밖"

| 후보 | git 추적? | 대시보드 읽기 경로 | 스테이지 `--add-dir` 가시성 | 클라우드 인수 상속 | 개인정보 노출면 | 판정 · 근거 |
|---|---|---|---|---|---|---|
| **★ `Worker/runs/<run-id>/events.ndjson`** | **✗** — 볼트 `.gitignore:75` 가 `Worker/runs/` 디렉토리 전체를 비추적 | `RUNS / run_id` 바로(`macp_dashboard.py:83`, `collect_run_detail` `:426`) — 경로 추가 없이 **같은 폴더** | ✓ 에이전트가 자기 런의 이벤트를 **읽을** 수 있다(쓰기는 writer 만, §2.1) | ✗ 호스트 로컬 — ③ stages.json·② run.log 와 **같은 수명** | run dir 에 이미 있는 run.log·screens 와 동일 면 | **정본.** 아티팩트·stages.json·run.log 와 한 폴더라 trace(`trace.py`)·재생·대시보드가 **한 경로**를 읽고, 재시도 체인(`inherit_artifacts` `stages.py:2626`)·디스크 가드 sweep 이 디렉토리 단위로 함께 다룬다 |
| XDG `~/.local/share/macp/events/<run-id>.ndjson` | ✗ 볼트 밖 | 경로 하나 더 읽어야(`MACP_RUN_EVENTS_DIR` 해석) | ✗ add-dir 밖 — 에이전트가 **못 읽는다**(그걸 원하는 호스트가 있다) | ✗ | run dir 와 **분리** — SMS 스토어 선례 0700/0600(`sms.py:88`, `:119-123`) | **선택 가능 override.** `MACP_RUN_EVENTS_DIR` env → `.macp/config.toml` 키 `run_events_dir` → 기본값 "run dir" 의 세 단계 리졸버(`docs/configuration.md:16-42` 패턴; XDG 기본값 선례 `MACP_STATE_SQLITE_PATH` `:57`). external 모드의 `state.sqlite` 와 같은 디렉토리에 두는 대칭이 성립한다 |
| `Worker/remote/<host>/runs/<run-id>/events.ndjson` **git 추적**(M5 전송 트리) | ✓ 추적되어 버린다(`.gitignore:142-145` — `runs/` 는 전송, `*.log` 만 제외; `worker.py:79-81`) | 본부 볼트에 pull 되면 보임 | — | ✓(그래서 위험) | **영구 히스토리**에 도구 I/O 발췌가 남는다 | **기각.** 셋 다 치명: 크기(런당 MiB 급 × 하루 수십 런 → 볼트 리포 비대), 개인정보(마스킹 뒤에도 발췌가 영구 git 히스토리로), `chore: sync vault state` 커밋에 쓸린다(`worker.py:2584-2590` — remote/ 는 git 이 "백업이 아니라 전송"인 곳) + push 주기만큼 지연. §6 S4 가 볼트 `.gitignore` 에 `Worker/remote/*/runs/*/events.ndjson` 한 줄을 요구한다(볼트 몫·사람) |
| 외부 상태 SQLite(`state.sqlite`)의 events 테이블 | ✗ | 상태 API 경유 | ✗ | ✓(external 모드) | DB 안 | **기각(원본으로는).** 행 단위 append 로그는 **파일**이 맞다 — tail·바이트 커서·sha 가 자연스럽고 writer 가 하나다. `state-store.md:260-262` 는 `runs/` 를 "호스트 로컬 런타임 — 이관 대상 아님" 으로 두고, external 모드에서도 `Worker/runs/` 는 호스트 로컬로 남는다 → 동거가 자연스럽다. 색인·질의가 필요한 것은 §5 의 **요약 블록**이고 그것이 SQLite 로 간다(이벤트 원본은 아님) |
| PROGRESS 봉투로 mailbox 전송 | ✓(git mailbox 면) | ledger | — | ✓ | 봉투 | **기각.** 봉투는 ledger 상태기계의 **입력**(`apply_reply` `assignment.py:207`)이고 소비 후 삭제된다(`worker.py:2584`); 도구 이벤트 수백 건을 봉투로 보내면 git 전송 시 커밋 폭주, `last_note` 200자 설계와 충돌 |

두 후보가 **모두 "git 밖"인 이유**를 적어 둔다 — 셋이다: **크기**(§3.2 숫자), **개인정보**(마스킹해도 도구
I/O 발췌·URL host·경로 basename 이 남는다 — 공개 리포도, 사람의 영구 히스토리도 아닌 **호스트 로컬 보존**이
맞다), **`chore: sync vault state` 커밋에 쓸리지 않음**(todo-sync 데몬이 볼트 추적 파일을 자동 커밋·push 한다).
로컬(본부) 러너는 파일에 **직접** 쓰고, 원격 러너는 자기 호스트의 같은 경로에 쓴 뒤 §3.3 으로 올린다.

### 3.2 크기·보존 — 숫자와 근거 (**사람 게이트**: 숫자는 제안이고 머지 뒤 사람이 확정한다)

| 항목 | 제안 | 근거 |
|---|---|---|
| 이벤트당 발췌(`*_excerpt`) | **≤ 2 KiB** = head 1 KiB + `…[N bytes omitted]…` + tail 1 KiB; 원문 전체의 `*_bytes` 와 `*_sha256` 은 항상 | 길이·동일성(같은 명령을 두 번 돌렸나)은 해시로 답하고, 내용은 "무슨 명령이었나" 를 알아볼 발췌만. 1 KiB 는 git·pytest 출력의 첫·마지막 화면 한 장 |
| 이벤트 1줄 하드캡 | **8 KiB**(payload 직렬화 기준). 초과 시 발췌를 더 줄이고 `truncated: true` | tail·cursor 구현이 "한 줄 = 한 read" 로 단순해지는 상한. `MAX_BODY=4000` 자(`sms.py:53`)의 2배 |
| 런당 상한 | **20 MiB**. 초과 시 `stream.truncated{reason: run_cap}` 1건 뒤 **도구 payload 생략**(라이프사이클 kind 는 계속) | 형제 런 implement = 46턴(stages.json 실측); 턴당 도구 호출 3~5 → 이벤트 ~2×(3~5)×46 ≈ 300~450건 × 평균 1~2 KiB ≈ **0.5~1 MiB/스테이지**, 5 스테이지 ≈ 3~5 MiB. 최악은 `STAGE_MAX_TURNS` implement 200(`stages.py:405`) × 10 이벤트 × 2 KiB ≈ 4 MiB/스테이지 → 20 MiB 는 평균의 4~6배, 최악의 1배 여유 |
| 보존 | **30일 또는 최근 500 런 중 먼저 도달** — run dir 와 **같은 수명**(events 만 먼저 지우지 않는다) | SMS 7일(`sms.py:52`)보다 긴 이유: 실패 진단은 "최신 run 부터"이지만 재시도 체인·`inherit_artifacts` 가 **며칠 전 run** 을 되읽고, 재발 비교(같은 태스크의 지난 런)는 주 단위다. 7일로 줄여도 설계는 바뀌지 않는다 — **사람 결정**. events 만 먼저 지우면 "아티팩트는 있는데 이벤트만 없는 run" 이 생겨 trace 가 두 종류의 부재를 구분해야 하므로 비권장 |
| 집행 주체 | 러너 **run-start sweep**(디스크 가드가 run dir 를 다루는 자리 — `record_disk_guard` `stages.py:2243` 가 기록하는 그 가드) | 틱마다가 아니라 런 시작 시 1회 — 비용이 런 수에 비례 |
| 짧은 비밀 | 인증번호(4~8자리)·PIN 은 **해시도 남기지 않는다** — `<otp>` 치환만 | sha256 이라도 10^8 공간은 브루트포스로 복원된다(§4) |

### 3.3 원격 러너 → 본부 — 본부 HTTP 상태 API 확장 (1차), 기각안 둘

**1차 권장 = `state_store_remote serve`(본부 HTTP 상태 API, `docs/configuration.md:118-144`)에 엔드포인트
하나 추가**: `POST /api/runs/<run_id>/events`.

- **인증**: 기존 bearer 재사용 — `MACP_STATE_REMOTE_TOKEN`/`_TOKEN_FILE`, env-only·config 키 없음·로그 금지
  (`configuration.md:59`, `:137-139`). 토큰 값·host 는 이 문서에도 코드에도 적지 않는다.
- **본문**: NDJSON 배치 — 원격 파일의 줄을 **그대로**(이미 마스킹된 상태, `redaction_profile` 포함). 요청마다
  `X-Macp-Host: <host_label>`(합성 라벨) — 본부가 `src_host` 로 붙인다.
- **멱등**: `(run_id, src_host, seq)` 가 키. 본부는 호스트별 마지막 `seq` 를 기억하고 중복은 버린다 → 재전송이
  중복을 만들지 않는다(§6.2 S4 의 라우트 AC). 응답 `{accepted, next_seq}`.
- **본부 측 저장**: `Worker/runs/<run-id>/events.ndjson` 에 append(디렉토리 없으면 생성 + `remote_host` 마커
  파일) → **대시보드 경로가 로컬 런과 동일**(§3.4 가 그냥 읽는다). 아티팩트(①)는 지금처럼 M5 git 전송
  (`Worker/remote/<host>/runs/`)으로 오고, 이벤트만 HTTP 로 온다 — 둘의 `run_id` 가 같아 trace 가 묶는다.
- **HQ 다운**: 러너는 기존 **host-local append-only outbox**(`configuration.md:143-144`) 에 배치를 append 하고
  계속 돈다(이벤트는 관측이라 상태 API 와 달리 **러너를 멈추지 않는다**); 복구 시 순서대로 flush. 원격 파일이
  정본 사본이라 outbox 가 유실돼도 재업로드(`since=<본부 next_seq>`) 가 가능하다.
- **업로더**: 러너의 writer 가 N초(제안 5 s) 또는 N KiB(제안 64 KiB) 마다 flush 하는 훅 — "원격 런도 본부 화면에
  근실시간" 이 목적. 스테이지 종료 시 강제 flush.
- **재마스킹 없음**: 본부는 받은 줄을 다시 마스킹하지 않는다(원격 writer 가 끝냈다). `redaction_profile` 이 본부
  최소치보다 낮으면 **거부(4xx)** — 옛 엔진의 원격 러너가 덜 마스킹된 줄을 올리는 사고를 막는다. 거부 시 러너는
  라이프사이클 kind 만 올린다(degrade).

**기각안.** (ⅰ) **git 추적 전송**(§3.1 셋째 행) — 크기·개인정보·영구 히스토리·지연. (ⅱ) **git mailbox 봉투**로
이벤트 전송 — 봉투 1통 = 커밋 1개 모양이라 수백 건이 커밋 폭주를 만들고, ledger 상태기계 입력과 섞인다.
(ⅲ) **본부가 원격 파일을 pull**(ssh/scp) — 스테이지 금지 도구이고 원격 호스트의 인바운드 노출을 요구한다.

**사람 게이트.** 네트워크 노출면(테일넷 안에서만 리슨 등)·토큰 프로비저닝·`redaction_profile` 최소치는
`state-store.md` leaf ⑦(`:369-373`, human_gate)과 같은 게이트에서 사람이 정한다. §6 S4 는 그 leaf ⑦ 이
존재해야 돈다(상태 API 서버가 있어야 엔드포인트를 더할 수 있다).

### 3.4 대시보드 구독 — SSE 기각, cursor 증분 GET 권장, 웹·앱 동일 계약

**운영 전제(확인된 것).** 서버는 stdlib `ThreadingHTTPServer`(`macp_dashboard.py:50`, `:2775`) — **연결당
스레드 1개**, 연결이 살아 있는 동안 스레드가 산다. `/api/status` 는 2026-10-22 에 요청 적체로 **110 스레드**가
futex 에 걸려 앱이 본부에 닿지 못했고, 수정은 `_StatusCache` 단일 비행(`:2460-2474`)이었다. 프로덕션 모양은
loopback + `tailscale serve`(TLS 종단 뒤 plain http, `:19-28`, `:2641-2643`). **장수 연결이 그 프록시 뒤에서
어떻게 끊기는지(idle timeout)는 이 체크아웃만으로 확정할 수 없다 → 미확인.**

| 방식 | 스레드 모델 비용 | 프록시·터널 장수연결 리스크 | 재생·감사 재사용 | 웹·앱 동일성 | 2026-10-22 사고 대비 |
|---|---|---|---|---|---|
| **SSE** `GET /api/run/stream?id=` | 열린 run 상세 탭마다 **무기한 스레드 1개** — 클라이언트 수 × 런 수 | **미확인**(tailscale serve·devtunnel 의 idle timeout) — 끊기면 클라이언트 재접속 폭주 | ✗ 과거 런 재생엔 **별도** 엔드포인트 필요 | 웹은 `EventSource` 내장, 앱은 별도 SSE 클라이언트 코드 | **재현 경로**: 스레드가 요청 수가 아니라 **탭 수명**에 비례 → 단일 비행으로도 못 막는다 |
| WebSocket | 같음 + 핸드셰이크 | 같음 | ✗ | 앱 별 코드 | stdlib 서버에 없음 — 새 의존성 |
| **★ cursor 증분 GET** `GET /api/run/events?id=&since=` | `wait=0`(기본): 스레드 수명 = 응답 시간(ms) — 기존 3초 폴링과 **같은 모양**. `wait>0`(선택 long-poll): 최대 25 s, 동시 long-poll 상한(제안 8) 초과 시 **즉시 응답으로 degrade** | `wait≤25 s` 는 보수적 프록시 idle 30 s 미만; 연결은 매 응답마다 닫힌다 | **✓ 한 엔드포인트** — `since=0` 부터 `limit` 씩 페이지하면 재생이고, `since=next_since` 를 3초마다 치면 실시간이다 | **✓** 앱은 이미 `/api/status` 를 폴링한다(`docs/dashboard-parity.md`) — 같은 패턴, 새 프로토콜 없음 | 요청당 비용이 bounded(파일 오프셋부터 ≤ `limit` 줄) + 단일 비행 오프셋 캐시 → 적체 조건이 없다 |
| plain poll(`/api/run` 전체 재전송) | 같음 | 없음 | ✗ 전체 재전송 | ✓ | 런당 20 MiB 를 3초마다 — 대역 폭주 |

**권장 = cursor 증분 GET.** 계약:

```
GET /api/run/events?id=<run-id>&since=<seq>[&limit=<n, 기본 200, 최대 500>][&wait=<s, 기본 0, 최대 25>]
→ 200 {"run_id": "...", "schema_version": 1, "events": [ {...}, ... ],
       "next_since": <int>, "live": <bool>, "truncated": <bool>, "tool_events": <bool>}
→ 404 {"error": "no events"}   (옛 런 — 파일 없음; 기존 /api/run 은 그대로 200)
```

- **판정 필드는 서버가 만든다**(parity 규칙): `live` = ledger 가 `dispatched`/`in_progress` **그리고** 파일 mtime 이
  최근 N초(제안 120) 안 — 클라이언트는 bool 만 읽는다. `truncated` = `stream.truncated` 를 봤거나 `limit` 에
  걸렸다. `tool_events` = `stream.meta.tool_events`.
- **핸들러 비용**: `since` 이후만 읽는다 — run_id → `(last_seq, byte_offset)` 오프셋 캐시(`_StatusCache` 와 같은
  per-server 단일 비행 `:2773`), 응답은 `limit` 줄 상한, 한 줄 ≤ 8 KiB(§3.2) 이므로 응답 ≤ 4 MiB.
- **`/api/run?id=` 에는 필드 추가만**: `events_count` · `events_live` · `events_available`(bool). 기존 키(`stages`·
  `artifacts`·`log_tail`·`ledger` …)는 불변 — 구 앱이 깨지지 않는다.
- **렌더(웹)**: run 상세에 "이벤트" 탭. 시간순 목록, `kind` 배지, `stage` 구분선; 도구 이벤트는 `tool` · 발췌 ·
  `bytes` · `sensitive` 배지 · `is_error` 색. `live` 면 3초마다 `since=next_since` 증분 append(전체 재렌더 아님),
  `truncated` 면 "일부 생략" 표시, `tool_events:false` 면 "이 런은 도구 단위 기록 없음". **payload 는 데이터로
  escape** 한다 — 이벤트 본문은 지시가 아니고, 발췌에 HTML/JS 가 섞여 있어도 텍스트다(event-triggers.md §4.4 와
  동형; XSS 방어).

**Parity 계약 초안**(머지 뒤 볼트 `docs/dashboard-parity.md` 에 사람이 옮긴다 — 이 리포는 볼트 문서를 고칠 수 없다;
앱 짝 태스크는 `macp-dashboard-android` 별도 리포, 사람 게이트):

> **Dashboard parity contract — 런 이벤트 탭** (웹 먼저, 앱이 따른다. 판정은 전부 서버, 클라이언트는 렌더만.)
> **1. 데이터 계약(서버, 추가만)** — 새 라우트 `GET /api/run/events`(위 계약 그대로). `/api/run` 응답에
> `events_count`·`events_live`·`events_available` 추가, 기존 키 불변. `live`·`truncated`·`tool_events` 는 서버 판정.
> **2. 렌더(클라이언트)** — run 상세의 "이벤트" 탭(기본 접힘, 헤더에 `events_count`). `live` 면 3초 증분, 아니면 1회.
> 도구 행 = `tool` 칩 + 발췌 1줄 말줄임 + `bytes` + `sensitive`/`is_error` 배지; 탭하면 발췌 전체(≤ 2 KiB). 모르는
> `kind` 는 회색 배지에 값 그대로(구 클라이언트 안전). **3. 상태** — 탭 접힘 키 웹 `mdash.sec.run-events`, 앱 FoldStore
> 같은 이름. **4. 제약** — 외부 네트워크 의존 없음, payload 는 escape 된 텍스트, 390px 가로 스크롤 0. **5. 테스트** —
> `todo_system/tests/test_macp_dashboard.py` 의 `RunEventsRoute`(바인딩된 서버에 실제 GET) · `RunEventsShell`(셸 계약).

---

## §4 개인정보 경계 — 쓰는 순간 한 곳에서 마스킹, 도구별 제외표

**원칙.** ① 마스킹은 **writer 한 곳**에서, **쓰는 순간**에 집행한다(`related_context.py:28-32` 가 R5 를 "렌더
한 모듈에서 집행" `:245` 하는 것과 같은 모양; §6 S2 의 `run_events_redact.py`). 파일에는 마스킹된 것만 있으므로,
스테이지 에이전트가 `--add-dir` 로 자기 run dir 의 `events.ndjson` 을 Read 해도, 대시보드가 보내도, 본부가
ingest 해도 **같은 것**이 흐른다. ② **기본은 가장 보수적**: 규칙표에 없는 도구는 "이름 + rc + bytes + sha256" 만
기록하고 발췌는 없다. 과잉 마스킹은 감사 가치만 줄이지만 누락은 유출이다. ③ CLAUDE.md §11(SMS)·§11b(메일)·§12
(브라우저)의 "본문·코드·스크린샷·덤프를 볼트나 외부로 옮겨 적지 않는다"가 그대로 이 표의 상위 규칙이다.
④ 규칙표가 바뀌면 `redaction_profile` +1(§2.1) — 옛 파일은 재마스킹하지 않고 그 프로필로 쓰였다고 표시한다.

### 4.1 도구별 규칙표

| 도구 / 패턴 | 기록하는 것 | **절대 기록하지 않는 것** | 비고 |
|---|---|---|---|
| `Bash(macp-sms:*)` · `Bash(macp-mail:*)` | 호출됨 · 서브커맨드(`list`·`wait-code`·`list-recent`) · rc · duration · `sensitive: true` | **stdout 전부 — 해시도 없다.** 인자 중 `--from`/`--grep`/`--since` 값은 `<redacted>` | 인증번호(4~8자리)는 sha256 으로도 10^8 브루트포스로 복원된다. 은행·카드 OTP 포함 전 메시지가 들어오는 채널(CLAUDE.md §11) |
| `Bash(macp-browser:*)` `probe`·`steps`·`text` · `mcp__macp-browser__browser_navigate`·`browser_snapshot`·`browser_text`·`browser_take_screenshot`·`browser_tabs` | 서브커맨드/도구명 · URL 의 **scheme + host 만** · rc · 출력 bytes · `sensitive: true` | snapshot/text **본문**, 스크린샷 **경로와 바이트**(`last.png`), `steps` JSON 의 `fill` 값, URL 의 path·query·fragment(쿼리에 토큰·세션 id) | CLAUDE.md §12 "스크린샷·덤프 텍스트에는 개인정보가 들기 쉽다". host 도 로그인 콘솔 종류를 드러내지만 "어느 서비스를 다뤘나"는 감사 가치가 있어 **host 만** 남긴다(사람 게이트: host 마스킹 전환 가능) |
| `mcp__macp-browser__browser_type` · `browser_click` · `browser_wait_for` | 도구명 · 요소 ref id · 입력 **길이** | 입력 텍스트(비밀번호·검색어·개인정보), 대기 조건 문구 | |
| `Bash(macp-emulator:*)` | 서브커맨드 · rc · **aggregates JSON**(rc · per-step durations · ANR/FATAL counts · skipped frames) 그대로 | 시드 DB, 스크린샷 경로, 덤프 | `stages.py:656-657` "Aggregates only: never copy the seed DB, screenshots, or dumps" — 이미 enforced 규율 |
| `Bash(macp-remote-gate:*)` | 서브커맨드 · handle · rc | 출력 본문 | |
| 일반 `Bash` | 명령줄(전역 마스킹 뒤 ≤ 2 KiB) · 출력 발췌 head/tail 1 KiB · bytes · sha256 · rc · duration | 전역 패턴에 걸린 값(§4.2). 명령줄이 `.secrets/`·`.env`·`~/.ssh`·`*.pem`·`credentials`·`token` 을 포함하면 `sensitive: true` + **출력 발췌 생략** | `git`·`uv run pytest`·`./gradlew` 가 대부분 — 첫·마지막 화면 한 장이 진단 가치의 대부분 |
| `Read` · `Write` · `Edit` · `NotebookEdit` · `Glob` · `Grep` | 경로(홈 접두 → `~`, 체크아웃 상대) · bytes · sha256(Write/Edit 결과 **내용의** 해시) · Grep 의 매치 **건수** | 파일 **내용**(Read 결과, Write 본문, Edit 의 old/new string), Grep 패턴·매치 줄 | `.secrets/`·`.env`·`~/.ssh`·`*.pem`·`*credentials*` 경로는 `sensitive: true` 이고 경로도 **basename 만** |
| `WebFetch` · `WebSearch` | host + path(쿼리 제외) · status · bytes / 검색어 **길이**만 | 응답 본문, 검색어 원문, 쿼리 문자열 | |
| `Agent`(서브에이전트) | 타입 · 프롬프트 길이 · `parent_tool_use_id` | 프롬프트 본문, 결과 본문 | 서브에이전트의 도구 호출은 `parent_tool_use_id` 로 매달린다(§2.4 ⑫) |
| 프롬프트(`-p` 본문) · `thinking` 블록 · transcript `queue-operation.content`·`lastPrompt` | 프롬프트 sha256 + bytes / thinking **블록 수** | **원문 전부** | 프롬프트엔 태스크 본문·related.md·프로젝트 메모리가 들어 있다 |
| `agent.message` · `agent.question` · `progress.note` · `run.finished.summary` · `approval.*.summary/note` | 전역 마스킹 + ≤ 2 KiB 절단 | 전역 패턴에 걸린 값 | 에이전트가 본문에 비밀을 인용할 수 있다 |
| `stage.started` 의 작업 디렉토리 · `system/init.cwd` | `cwd_kind: vault 또는 checkout` | 절대 경로 | 공개 리포 식별자 규율과 같은 이유(`/home/<user>`·`/Users/<user>`) |
| `stage.usage` · `stage.finished` · `state.transition` · `checkpoint.pushed` · `artifact.written` | 숫자·sha·상대 경로 | — | 메타만 — 마스킹 대상 아님 |

### 4.2 전역 마스킹 패턴 (코드가 쓸 정규식의 **종류** — 정확한 정규식은 S2 가 테스트와 함께 확정)

| 종류 | 패턴 서술 | 치환 |
|---|---|---|
| API 키·토큰 형태 | `sk-…`, `ghp_…`/`github_pat_…`, `AIza…`, `xox[abp]-…`, JWT 3분절(`eyJ….….…`), 32자 이상의 연속 hex/base64 뭉치 | `<token>` |
| 키=값 비밀 | `token=`·`password=`·`passwd=`·`secret=`·`api_key=` 뒤의 값, `Authorization:`·`Bearer ` 뒤의 값, `MACP_*_TOKEN=` 류 env 대입 | `<token>` |
| 인증번호 | `인증`·`승인`·`확인`·`code`·`OTP`·`verification`·`PIN` 이 앞뒤 40자 안에 있는 **4~8자리 숫자** | `<otp>` — **해시 없음** |
| 이메일 · 전화 · 주민번호 형태 | `local@domain`, `01x-xxxx-xxxx`·`+82…`, `6자리-7자리` | `<email>` · `<phone>` · `<id>` |
| 홈 경로 | `/home/<user>`·`/Users/<user>` 접두, `CLAUDE_CONFIG_DIR` 하위 경로 | `~` · `<config-dir>` |
| 긴 base64 blob | 데이터 URI·이미지·바이너리 덤프(`data:…;base64,`) | `<blob N bytes>` |

마스킹기는 **보수적**이다 — 오탐(git sha 가 `<token>` 으로 가려지는 일)은 감수하고, 누락을 피한다. 패턴은
`redaction_profile` 과 함께 버전된다.

### 4.3 검증 규율 — 네거티브 단언은 양성 대조군과 함께

"`macp-sms` 의 stdout 이 어디에도 없다"를 **`! grep -q` 하나로** 단언하면 파일이 비어도 통과한다(공허 단언).
S2 의 테스트는 **같은 픽스처에서** ① `tool.called{tool:"Bash", input_excerpt:"macp-sms wait-code --from <redacted> …"}`
가 **있고** ② `tool.result{tool_use_id:…, output_excerpt: null, sensitive: true}` 가 있고 ③ 픽스처가 심은
합성 코드 문자열(예: `MACP-FIXTURE-CODE-000000`)이 **파일 전체에 0회**임을 한 테스트에서 함께 단언한다.
브라우저 픽스처도 같은 모양 — snapshot 본문의 합성 마커가 0회이고 `host` 는 `example.invalid` 로 있다.
공개 리포의 픽스처는 전부 **합성**(`example.invalid`, `<redacted>`, `00000000-0000-4000-8000-000000000000`)이고,
실 캡처(`capture.py`)로 만든 transcript 픽스처는 지금처럼 usage 가 읽는 줄만 남기고 attachment 줄은 제외한다
(`capture.py:28-33` "private context and never leave the local config dir").

---
