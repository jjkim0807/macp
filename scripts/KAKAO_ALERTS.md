# KAKAO_ALERTS — 카톡 알림 발신처 전수 표와 노이즈 등급

> 판정 기준은 하나: **이 알림을 받은 사람이 지금(수 시간 안에) 뭔가를 해야 하는가.**
> 애매하면 즉시 — 알림을 잃는 비용이 피로보다 크다. (태스크
> `macp-kakao-alert-2-noise-tiers`, 사용자 지적 2026-09-16)

## 등급 정의

| 등급 | 의미 | 전달 |
|---|---|---|
| **즉시 (immediate)** | 사람이 지금 행동해야 하거나 돈·보안·데이터 유실이 걸림 | 개별 카톡, 기존과 동일. 같은 사건 키는 10분 창 안에서 1건으로 합침("외 N건") |
| **요약 (digest)** | 알아두면 좋지만 할 일 없음 (FYI) | 개별 발송 0회. `logs/kakao_digest.jsonl`에 적재 → 하루 1회(08:35) `scripts/kakao_digest_send.py`가 한 통으로 발송. 유실 금지: 발송 성공 시에만 큐를 비우고 `logs/kakao_digest.sent.jsonl`로 이관 |
| **무음 (silent)** | 기대된 동작이거나 이미 자가 회복됨 | 발송 0회, `logs/kakao_route.log`에만 기록 |

## 옵트인 사용법 (하위호환: 등급을 안 주면 전부 즉시)

- **셸**: `KAKAO_LEVEL=digest|silent scripts/kakao_send.sh "<msg>"` — env가 없으면 코드 경로
  무변경(기존 호출 전부 즉시). `KAKAO_EVENT_KEY=<종류:대상>`를 주면 즉시 등급의 중복 합치기 적용.
- **python (macp 내부)**: `worker.kakao_notify(msg, level=..., event_key=...)`,
  `human.kakao_notifier(text, level=..., event_key=...)` — keyword-only, 기본값 None=즉시.
  orchestrator 알림은 `Orchestrator._notify_routed()` 단일 관문을 지난다.
- 라우팅 코어는 `todo_system/kakao_format.route()` (stdlib-only leaf, launchd
  `/usr/bin/python3` 안전). 상태 파일: `logs/kakao_digest.jsonl`(큐),
  `logs/kakao_dedup.json`(사건 키 창), `logs/kakao_route.log`(무음·억제 기록) — 전부 비추적.
- **요약 타이머 활성화(배포 1줄)**: `python3 scripts/systemd/gen_units.py --enable --only macp-kakao-digest`
- **채널(백엔드)**: `kakao_send.sh`는 채널 무관 디스패처다. **Slack 단독 전환 완료**: `NOTIFY_BACKENDS`
  미설정이면 `.secrets/notify-slack.json`이 있을 때 **Slack 단독**(카톡 HTTP 0회), 시크릿이 없으면
  **카톡 폴백**(알림 유실 금지)이다. 카톡으로 되돌리려면 `NOTIFY_BACKENDS=kakao`, 병행은 `=kakao,slack`.
  라우팅(등급 판정)은 팬아웃 **앞에서 1회**만 돌아 digest/silent/억제는 어느 백엔드로도 안 나간다.
  설정·채널 키(`default`/`kis`/`digest`)·Free 한계·롤백은 `scripts/KAKAO_SETUP.md` §8.

## 발신처 전수 표 — macp 코드

앵커는 `module:function`(라인 번호 없음)이다 — 줄번호는 리팩터마다 떠내려가지만
함수가 지워지거나 이름이 바뀌면 `todo_system/tests/test_kakao_alerts_doc_anchors.py`가
즉시 깨진다. 근거 열의 "자체 쿨다운"은 해당 발신처가 이미 자기 재알림 억제(1회성/일일
재나그 등)를 갖고 있어 event_key 없이도 반복 발송이 없다는 뜻.

| 발신처 (module:function) | 종류 / 예시 문구 | 등급 | 근거 |
|---|---|---|---|
| `todo_system/orchestrator/orchestrator.py:_notify_review` | ⛔ 선행 조건 미충족 / ✅ PR 열림 리뷰 요청 / ✅ 0-commit 완료 / ❌ 실패 의심 (구 `[검토요청]`) | 즉시 | 사람 판단 필요. event_key `review:<framing>:<task>` + 쿨다운 6h — 같은 태스크가 같은 게이트에 반복 막히는 재알림(2026-08-18 하루 10건)을 합침 |
| `todo_system/orchestrator/orchestrator.py:_notify_orphaned_commits` | 🧨 미푸시 커밋 되감김 (구 `[커밋유실]`) | 즉시 | 데이터 유실 — 되살릴지 판단 필요. event_key `orphaned:<task>` |
| `todo_system/orchestrator/orchestrator.py:_notify_poison` | 🚧 N회 재실패로 격리 | 즉시 | 자동 재시도 중단 — 수동 처리 필요. event_key `poison:<task>` |
| `todo_system/orchestrator/orchestrator.py:_notify_timeout_park` | ⏱️ 연속 stage timeout 대기 전환 | 즉시 | long_job 전환 판단 필요. event_key `timeout-park:<task>` |
| `todo_system/orchestrator/orchestrator.py:_notify_blocker_cancelled` | 🔗 상류 취소로 진행 불가 | 즉시 | 태스크 존폐 판단 필요. event_key `blocker-cancelled:<task>` |
| `todo_system/orchestrator/orchestrator.py:_notify_parent_complete` | 🧭 하위 완료된 상위 태스크 open (구 `[상위태스크 점검]`) | 즉시 | 닫을지 판단 필요. `_record_parent_surfaced`가 1회성 보장(자체 쿨다운) |
| `todo_system/orchestrator/orchestrator.py:_notify_human_ready` | 🙋 사람이 할 차례 — 기다리던 선행 작업 전부 done | 즉시 | 사람이 **지금** 해야 하는 일이고, 체인에 남은 게 사람뿐이라 요약으로 미루면 최대 하루 늦는다. event_key `human-ready:<slug>` + `_human_ready_seen`의 dep 집합 fingerprint 1회성(자체 쿨다운) — ready를 벗어나면 엔트리가 prune돼 재무장하고, 첫 배포에는 24h 안에 닫힌 체인만 알린다 |
| `todo_system/orchestrator/orchestrator.py:_propagate_downstream` | 🔗 하류 통합검증 N개 생성 | **요약** | 자동 생성 FYI — 검증 결과는 별도 알림이 옴 |
| `todo_system/orchestrator/orchestrator.py:_nudge` | 🙋 파킹된 할당 재촉 (1d/3d/7d 백오프) | 즉시 | 사람 응답을 며칠째 기다리는 의도적 에스컬레이션. event_key 없음 — last_nudge_ts 백오프가 이미 페이싱(자체 쿨다운) |
| `todo_system/orchestrator/human.py:deliver_pending` TASK_ASSIGN | 🙋 새 할당 도착 (구 `[할당]`) | **요약** | 2026-09-16 감사: 5건 전부 FYI — 직전 검토요청과 중복. QUERY/CANCEL/WAKE/PING은 즉시 유지(애매하면 즉시) |
| `todo_system/orchestrator/followups.py:_notify_awaiting` | 🙋 사람 결정 대기(followups human): [id] 태스크 — planner가 `"route":"human"`으로 넘긴 report 후속 | **요약** | 결정 대기일 뿐 수 시간 안에 할 일은 아님. id당 평생 1회(`human_notified` 표식 — digest 등급엔 자체 dedup이 없음). 적재 실패 시 다음 reconcile이 재시도하고 즉시 발송으로 폴백하지 않음. 목록·결정은 `python -m todo_system.orchestrator.followups human` / `approve` / `dismiss` |
| `todo_system/worker.py:cmd_maintenance` | ⚠️ 마이그레이션 미적용 | 즉시 | DB 스키마 확인 필요. `check_unapplied_migrations`가 판정, `cmd_maintenance`가 발송 |
| `todo_system/worker.py:check_dispatch_silence` | 🤖⚠️ macp 정체 | 즉시 | 파이프라인 침묵의 유일한 감지기. SILENCE_COOLDOWN 자체 쿨다운 |
| `todo_system/worker.py:check_work_starvation` | 🤖💤 macp 유휴 | 즉시 | 새 태스크 투입 필요. 일 1회 자체 쿨다운 |
| `todo_system/worker.py:restart_stale_daemons` | 🤖⚠️ 재기동 후에도 구코드 | 즉시 | 수동 확인 필요. escalated 1회성(자체 쿨다운) |
| `todo_system/worker.py:_note_restart_deferral` | 🤖⚠️ 재기동 계속 연기 | 즉시 | 구코드로 도는 중 — 확인 필요. 일 1회 재나그(자체 쿨다운) |
| `todo_system/worker.py:record_dirty_stall` | ⚠️ Worker 정지: 미커밋 파일 | 즉시 | 커밋/정리해야 재개. 임계+12h 재나그(자체 쿨다운) |
| `todo_system/worker.py:clear_dirty_stall` | ✅ Worker 재개 | **요약** | 자가 회복 완료 FYI |
| `todo_system/worker.py:_run_next_impl` (worker start) | 🤖 Worker 시작 | **요약** | "조치 불필요, 진행 알림" — 문구 자체가 FYI |
| `todo_system/worker.py:_run_next_impl` (worker fail) | ❌ Worker 실패 | 즉시 | 런 로그 확인 필요. event_key `worker-fail:<slug>` |
| `todo_system/worker.py:_run_next_impl` (worker done) | ✅ Worker 완료 | **요약** | 후속 판단은 검토요청·리뷰 태스크가 따로 알림 |
| `todo_system/orchestrator/runner.py:_publish_beta` | ❌ 베타 발행 실패 | 즉시 | 릴리즈 파이프라인 고장 — 확인 필요 |
| `todo_system/orchestrator/beta_publish.py:maybe_publish` | 🚫 [베타 중단] 테스터 0 | 즉시 | 콘솔에서 테스터 추가 필요 |
| `scripts/health_report.py:main` | ⚠️ 이상 N건 | 즉시 | WARN일 때만 발송(정상 무음) — 판단 필요. 주 1회 |
| `scripts/claude_auth_watch.py:check_accounts` | 🔐 Claude Code 인증 이상 | 즉시 | 방치하면 자동화 전체 정지 — /login 필요 |
| `scripts/macp_dashboard.py:_run_ingest_alert` | 📵 SMS 릴레이 수신 거부 (HTTP 401/429/400/413) | 즉시 | 사람 조치 필요(토큰 재적용·앱 확인). 종류별(auth/bad_payload/too_large) 6h 자체 쿨다운(`sms.should_alert`, 파일 상태라 재시작에도 유지). 호출원은 `do_POST` |
| `scripts/claude_auth_watch.py:check_remote_control_binary` | 🔐 자동 복구됨 | **요약** | 이미 자가 회복 — `KAKAO_LEVEL=digest` env 옵트인 |
| `.claude/commands/worker-planner.md` `[Planner] 태스크 N개 적재` | [Planner] 적재 | **요약** | FYI. 2026-10-11 적용(`KAKAO_LEVEL=digest`). 단 `planner_policy.yaml`의 `kakao_on_tasks_created: false`라 현재는 발송 안 됨. 마크다운 프롬프트 안의 셸 커맨드라 함수 앵커 없음 — grep `KAKAO_LEVEL=digest.*Planner` |
| `scripts/notify_kakao.sh` 아침 브리프 | 데일리 브리프 | (등급 밖) | 자체가 일 1회 요약 채널 — 라우팅 대상 아님. 셸 스크립트라 함수 앵커 없음 |

## 발신처 전수 표 — 외부 리포 (권고 등급, 코드는 후속 태스크로)

이 리포에서는 코드를 못 고친다. 후속 태스크: `Todo/items/ai-stock-trading-kakao-noise-tiers.md`,
`Todo/items/mailroom-kakao-noise-tiers.md` (`depends_on: macp-kakao-alert-2-noise-tiers`).

| 발신처 (추정 위치) | 종류 / 예시 문구 | 권고 등급 | 근거 |
|---|---|---|---|
| ai-stock-trading `trading_loop.py` (KIS_ALERT_SCRIPT 경유, `scripts/systemd/gen_units.py:68`이 배선) | 🤖📈 KIS 페이퍼트레이딩: 1주일 트레이딩 시작 | 요약 | 페이퍼 시작 FYI |
| ai-stock-trading `watchdog.py` | 🤖📈 KIS 페이퍼트레이딩: 연속 N회 에러 | **요약까지만 — 무음 금지** | "연속 N회 에러"는 리스크 감시 정전을 가릴 수 있음(2026-09-16 VTS 타임아웃 6연속은 외부 장애·자가 회복이었지만, 같은 문구가 리스크 정전일 수도). `KIS_ENV=real`은 **전부 즉시** |
| ai-stock-trading `scripts/live_sync.py` | 🤖📈 KIS 라이브 동기화: SYNCED: synced | 요약 | 정상 동기화 FYI |
| ai-stock-trading `scripts/live_sync.py` | SYNCED: synced_no_restart | 무음 | 루프를 일부러 안 켠 실계좌 클론의 기대된 동작 |
| ai-stock-trading — 실계좌(`KIS_ENV=real`) 알림 전부 | 주문/에러/동기화 무엇이든 | **즉시** | 실계좌는 돈 — 등급 하향 금지 |
| mailroom tick (`KAKAO_PROFILE=mailroom`) | 📬 새 로그인 감지 | 즉시 + 같은 사건 합치기 | 보안 알림. 단 같은 발신자·같은 종류·짧은 창의 다계정 중복(2026-09-15 Windows 로그인 1건이 3계정 3통)은 1건으로 |
| mailroom tick | 📮 일일 요약 | 요약(현행 유지 가능) | 이미 일 1회 요약 — macp digest에 실어도, 자체 발송 유지해도 무방 |
| kakao_send.sh 프로필 토큰 없음 WARN | `WARN profile 'mailroom' token missing` | 무음(로그 전용, 현행 그대로) | 이미 발송이 아니라 로그 — 변경 없음 |

## 재생 결과 (2026-09-11 00:00 ~ 09-16 23:59, `logs/kakao_send.out.log`)

재생 스크립트: `Worker/runs/20260917T011944-macp-kakao-alert-2-noise-tiers/replay_classify.py`.
로그에는 이 창에 `sent (200)` 라인이 **32건** 남아 있다(태스크 본문의 "34건"은 작성 시점
집계 — 창 경계 포함 방식 차이로 2건 차이, 로그가 정본).

**before: 32건 전부 개별 즉시 발송.** → **after:**

| 등급 | 건수 | 내역 |
|---|---|---|
| 즉시 | 17건 → **15통** | 검토요청 9, mailroom 로그인 감지 3(같은 사건 → 1통, "외 2건"), macp 정체 1, 상위태스크 점검 1, 인증 이상 1, 커밋유실 1, 헬스리포트 WARN 1 |
| 요약 | 14건 → **하루 1통** (창 내 최대 6통) | 할당 FYI 5, Planner 적재 4, KIS 라이브 동기화 2, KIS 페이퍼 시작 1, KIS 페이퍼 연속에러(페이퍼) 1, mailroom 일일 요약 1 |
| 무음 | 1건 → **0통** | KIS synced_no_restart 1 |

개별 카톡 32통 → 즉시 15통 + 요약 ≤6통 = **최대 21통 (−34%)**, 그중 폰을 울릴 가치가
있는 즉시 알림 비중은 47% → 71%.
