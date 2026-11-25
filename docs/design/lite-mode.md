---
title: 1인 모드(lite) — 설계 노트
status: draft
created: 2026-11-22
related:
  - Todo/items/macp-lite-1-design.md              # 이 노트를 만든 태스크
  - Todo/items/macp-lite-2-cli.md                 # 구현: 콘솔 명령·README·격리 게이트
  - Todo/items/macp-lite-3-claude-code-plugin.md  # 구현: Claude Code 플러그인
  - Notes/macp-onboarding-review/ledger.md        # '첫 결과까지' 측정 원장
  - Decisions/2026-11-20-human-gate-irreversible-only.md  # 가드를 더할 때의 기준
---

# 1인 모드(lite) — 설계 노트

> **이 노트의 성격**: 설계 전용. 이 태스크는 코드를 바꾸지 않고
> `docs/design/lite-mode.md` 한 파일만 더한다. 구현은 후속 두 태스크가 맡는다
> ([후속 태스크 지도](#후속-태스크-지도)). `파일:줄` 앵커는 전부 이 리포 HEAD 트리
> (`4093ed6`)를 `git grep -n`과 직접 읽기로 확인한 것이다. 코드를 읽어 안 것과 실제로
> 돌려 본 것을 구분했다: 돌려 본 것은 [확인 실행](#확인-실행) 절에만 있고, 그 밖의 동작
> 서술은 읽은 결과다. 확인하지 못한 것은 '미확인'이라고 적고
> [부록](#미확인-목록)에 모았다.

## 왜 이 문서인가

### 요구사항

**새 사용자가 자기 기존 리포에서, 명령 3개 이내·5분 안에, 실제 Claude Code 워커가
태스크 하나를 끝내 PR(원격이 없으면 로컬 브랜치)을 여는 것을 본다.**

조건은 셋이다.

- 기존 엔진 경로(오케스트레이터·러너·스테이지)를 그대로 쓴다. 새 파이프라인을 만들지
  않는다.
- 새 스위치(환경변수·설정 키)를 만들지 않는다. 기존 값의 기본값만 바꾼다.
- 지금의 운영 형태는 바이트 단위로 그대로 둔다.

이 문서에서 쓰는 말:

| 말 | 뜻 |
|---|---|
| 볼트 | 태스크 파일·원장·런 기록·메모리가 사는 디렉터리 (`docs/getting-started.md:26-27`) |
| 워커 루트 | `repo:` 태스크의 클론과 런별 워크트리가 놓이는 곳. 기본 `~/Worker` (`todo_system/paths.py:377`) |
| 본부 모드 | 지금의 운영 형태: 사용자가 만든 볼트 리포 + `MACP_VAULT_ROOT` + 유닛 여러 개 (`docs/running.md:3-15`) |
| 1인 모드 | 이 문서가 정의하는 형태: 볼트를 자동으로 만들고 프로세스 하나로 도는 기본값 묶음 |

다른 도구를 조사해 얻은 공통점은 세 가지였다: 설치 한 줄, 명령 1~3개, 이미 있는 로그인과
프로젝트 설정(`CLAUDE.md`)을 그대로 쓴다. 1인 모드는 엔진의 기능을 줄이는 것이 아니라
이 세 가지를 기본값으로 만드는 일이다.

### 지금의 계측

리포에서 직접 센 값이다.

| 지표 | 지금 | 근거 |
|---|---|---|
| 첫 결과까지 치는 명령 | 12줄 (`&&`로 이은 것을 풀면 13개) | `README.md:83-102` |
| 손으로 고치는 파일 | 1개 (태스크 frontmatter) | `README.md:91-92` |
| 필수 환경변수 | 2개 (`MACP_VAULT_ROOT`, `USAGE_GUARD_DISABLE`) | `README.md:87-88` |
| 그 결과 | 그림자 실행. 에이전트는 뜨지 않는다 | `README.md:79`, `README.md:100` |
| 실제 실행까지 더 필요한 것 | `gh` 준비, 태스크 하나 더 손으로 작성, 명령 3줄 | `docs/getting-started.md:134-181` |
| 계속 돌릴 때의 프로세스 | 5개 | `docs/running.md:3-12` |
| 엔진이 그리는 유닛 파일 | 13개 (서비스 8 + 타이머 5) | `scripts/systemd/gen_units.py:196-238` |

태스크 본문의 "명령 약 11개"는 대화 세션이 README를 보고 센 값이고, "유닛 약 20개"는
작성자 호스트의 수(엔진 밖 유닛 포함)다. 세는 규칙이 달라 숫자가 다르다. 이 문서는 위
표의 값만 쓴다.

### 확인 실행

이 노트를 쓰며 한 번 돌려 본 것이다(2026-11-22, git 2.43.0, Python 3.12, 소스
체크아웃). 깨끗한 임시 `HOME`에서 `MACP_*`·`USAGE_GUARD_*`·`CLAUDE_*` 환경변수를 모두
지우고, 아래 설계가 부르겠다고 한 기존 함수를 **한 프로세스 안에서** 순서대로 불렀다.
실행기는 그림자(`shadow`)였다. 에이전트는 뜨지 않았고 모델 호출도 없었다.

1. `vault-template/`을 `~/.macp/vault`로 복사 → `git init -b main` → 첫 커밋: 0.04초.
   볼트 설정에 기존 키 `worker_root = "~/.macp/work"`를 적었다.
2. `os.environ["MACP_VAULT_ROOT"]`를 프로세스 안에서 넣은 **뒤에** 엔진을 import했다
   (약 2초). `cli.ROOT`, `stages._VAULT_DATA_ROOT`, `usage_guard.ROOT`,
   `model_select._ROOT`가 모두 그 볼트였고 `paths.worker_root()`는 `~/.macp/work`였다.
3. `allowed_tools`가 없고 `repo:`가 로컬 경로이며 `auto_merge: false`,
   `worker_mode: quick`인 태스크 파일을 써서 `task_validate.validate_file`에 넣었다:
   지적 0건. `import_markdown_to_db`: 1건, 0.12초.
4. 로그인 정보가 없는 `HOME`에서 `usage_guard.check()`: 허용
   (`no usage data (RuntimeError: no Claude Code OAuth token found) — fail-open`),
   0.01초, 네트워크 호출 없음.
5. `orchestrator._main(… auto …)` → `runner._main(… --mode shadow --once)` →
   `orchestrator._main(… auto …)` → `orchestrator._main(… status)`를 같은 PID로 불렀다.
   네 번 모두 rc 0, 마지막 줄은 `done  <slug>  worker-default`. 같은 PID가 `--lease`를
   두 번 잡아도 막히지 않았다.
6. 위 4.2초 가운데 약 4.1초는 틱 두 번의 `_pull_main`이 썼다. 원격이 없는 볼트에서
   `git fetch origin main`을 세 번 시도하고 그 사이 1초씩 쉰 뒤
   `hq-host pull: non-fatal — fatal: 'origin' does not appear to be a git repository …`
   다섯 줄을 틱마다 찍는다. 동작에는 지장이 없지만 새 사용자에게는 오류로 보인다(⑤ 절에서
   다룬다).
7. 로컬 경로를 `repo`로 주고 `repos.ensure_worktree` → `stages.prepare_external_branch` →
   워크트리에서 커밋 → `stages.checkpoint_push`를 불렀다. 클론은 `~/.macp/work/<repo>`,
   워크트리는 `~/.macp/work/<repo>.wt/<slug>`에 생겼다. push가 끝나자 **사용자의 리포에
   브랜치 `worker/<slug>`가 생겼고**, 사용자의 체크아웃은 `main`에 그대로였으며 작업 트리도
   깨끗했다. `_origin_repo_arg`는 `None`(URL 아님)을 돌려줬다.
8. 사용자가 그 브랜치를 체크아웃해 둔 동안 같은 브랜치로 다시 push하면 git이 거부한다
   (`refusing to update checked out branch`).
9. 마커 충돌 세 가지를 `paths.vault_root(start=…)`로 확인했다. 프로젝트 마커에
   `vault_root` 키가 없으면 **그 리포 디렉터리가 볼트로 해석된다**. 키가
   `"~/.macp/vault"`면 볼트로 해석된다. `write_project()`를 다시 부르면 그 키는 지워진다.
   전역 `~/.macp/config.toml`이 있으면 `HOME` 아래 아무 디렉터리나 `HOME`으로 해석된다.

확인하지 **않은** 것: 실제 `claude -p` 호출 전부, `gh`, wheel 설치본, systemd, 로그인된
계정에서의 사용량 가드.

## 결정 요약

| # | 결정 | 근거 한 줄 | 절 |
|---|---|---|---|
| D1 | 여섯 동사는 콘솔 스크립트 `macp`의 선행 분기로 둔다 | `cli`가 import 시점에 볼트를 요구한다 (`todo_system/cli.py:32`) | ① |
| D2 | "명령 3개"는 `macp init`·`macp add`·`macp up --once`. 설치 줄은 선행 조건으로 따로 적는다 | 2단계 완료 조건과 같은 세 개 | ② |
| D3 | 기본 볼트는 `~/.macp/vault`, 워커 루트는 기존 키로 `~/.macp/work` | `~/.macp`는 이미 `MACP_HOME`이다 (`todo_system/macp/config.py:30`) | ② |
| D4 | 볼트는 진입점이 한 번 해석해 프로세스 환경에 넣고, 그 뒤에 엔진을 import한다 | import 시점 상수가 여럿이고 앵커가 서로 다르다 | ② |
| D5 | `macp init`은 멱등: 볼트 보장 → 리포 등록 → 로그인 확인(막지 않음) | 단계마다 기존 함수 | ② |
| D6 | 로그인은 프로세스 환경을 그대로 물려준다. 계정·프로필을 만들지 않는다 | `todo_system/orchestrator/runner.py:356-357` | ③ |
| D7 | `macp add`는 `claude -p` 한 번으로 JSON만 받고, 파일·검증·DB·커밋은 CLI가 한다 | 가짜 `claude`로 테스트할 수 있고 도구 권한이 필요 없다 | ④ |
| D8 | `macp up`은 단일 스레드 루프. 틱 인자는 틱 스크립트와 같고 러너는 `poll_once`다 | `scripts/orchestrator_tick.sh:52-61`, `todo_system/orchestrator/runner.py:3292` | ⑤ |
| D9 | `--once`는 사용량 페이싱 없이 바로 돈다. 루프와 서비스는 본부와 같이 페이싱한다 | 지켜보는 한 번의 실행을 막아 지킬 것이 없다 | ⑤ |
| D10 | 끝 상태는 PR(URL 원격 + `gh`) 또는 사용자 리포의 로컬 브랜치 `worker/<slug>`. 머지는 사람이 한다 | `auto_merge: false`를 명시한다 | ⑤ |
| D11 | 첫 태스크는 `quick` 레인을 먼저 고른다. 5분은 목표치다 | `quick`은 `claude -p` 한 번 (`todo_system/orchestrator/stages.py:165`) | ⑤ |
| D12 | `--service`는 `gen_units.service()`로 유닛 1개. `build()`는 건드리지 않는다 | 본부용 유닛 집합의 출력이 그대로 남는다 | ⑥ |
| D13 | 플러그인은 같은 리포의 `plugins/macp` + 루트 `marketplace.json`, 스킬 1개 | 로컬에서 확인한 형식만 쓴다 | ⑦ |
| D14 | 2단계가 엔진에서 바꾸는 곳은 네 군데뿐이고, 모두 본부 경로에서는 닿지 않는다 | 새 환경변수·새 설정 키 0개 | ⑧ |

## ① 명령 표면

### 지금 `macp`는 두 개다

| 이름 | 실체 | 어디에 있나 | 하위명령 |
|---|---|---|---|
| 콘솔 스크립트 `macp` | todo CLI. `pyproject.toml:60` → `todo_system/_entry.py:23` → `cli.main` (`todo_system/cli.py:2170`) | wheel을 설치하면 PATH에 놓인다 | `init-db` `import-md` `validate` `new` `list` `today` `audit` `weekly-review` `queue` `complete` `reopen` `set` `daemon` `now` `focus` `calendar-sync` `calendar-fetch` `unified` `session-end` `resume` (`todo_system/cli.py`) |
| bash 래퍼 `scripts/macp` | 프로젝트/프로필 CLI `python -m todo_system.macp` | 개발 체크아웃에만 있다 (`scripts/macp:9-15`) | `init` `status` `profile` `account` `memory` `use` `deps` `impact` `link` `propagate` (`todo_system/macp/__main__.py:649-725`) |

새 동사 `init` `add` `up` `status` `dashboard` `memory`는 todo CLI의 하위명령과 하나도
겹치지 않는다. 프로젝트 CLI와는 `init`·`status`·`memory` 셋이 겹친다.

### D1 — 여섯 동사는 콘솔 스크립트의 선행 분기다

`todo_system/_entry.py`의 `main`이 `cli`를 import하기 **전에**
(`todo_system/_entry.py:27`) `sys.argv[1]`을 본다. 여섯 동사 가운데 하나면 새 얇은 모듈
(가칭 `todo_system/lite.py`)로 넘기고, 아니면 지금 경로 그대로 `cli.main()`을 부른다.

- 순서가 이래야 하는 이유: `cli`는 import하는 순간 볼트를 해석하고
  (`todo_system/cli.py:32`), 볼트가 없으면 `_entry.main`이 exit 2로 끝난다
  (`todo_system/_entry.py:28-30`). `macp init`은 볼트가 없을 때 돌아야 한다.
- 기존 동사의 뜻은 바뀌지 않는다. 여섯 이름은 지금 argparse가 거부하는 이름이다.
- 프로젝트 CLI(`python -m todo_system.macp`)는 그대로 둔다. `lite`의 `init`은 그 CLI의
  `_cmd_init`이 부르는 세 함수를 같은 순서로 부르고, `memory`는 그 CLI의 `main`으로
  넘긴다.
- 개발 체크아웃에서 `scripts/macp`를 PATH에 복사해 둔 사람에게 `macp`는 여전히 프로젝트
  CLI다. 1인 모드 문서의 `macp`는 wheel 설치본의 콘솔 스크립트를 가리킨다고 README에
  적는다.

버린 선택지: 프로젝트 CLI를 넓히기(설치본의 PATH에 없다), 두 CLI를 합치기(범위 밖이고
본부 동작이 바뀐다).

### 명령마다 부르는 기존 함수

"새 글루"는 2단계가 `todo_system/lite.py`에 쓰는 얇은 코드다. 그 밖의 일은 전부 기존
함수 호출이다.

| 명령 | 하는 일 | 부르는 기존 함수·모듈 | 새 글루 |
|---|---|---|---|
| `macp init` | 볼트 보장, 현재 리포 등록, 로그인 확인 | `paths.engine_root` (`todo_system/paths.py:191`) · `config.write_project` (`todo_system/macp/config.py:330`) · `memory.ensure_memory` (`todo_system/macp/memory.py:31`) · `memory.link_agent_context` (`todo_system/macp/memory.py:61`) · `model_select.resolve_model` (`todo_system/orchestrator/model_select.py:304`) · `claude_auth.auth_source` (`todo_system/claude_auth.py:145`) | 템플릿 복사, `git init -b main`·첫 커밋, 마커에 `vault_root` 키 |
| `macp add "<문장>"` | 문장 → 태스크 파일 → 검증 → DB → 커밋 | `stages.stage_model`·`stage_effort` (`todo_system/orchestrator/stages.py:86`, `:96`) · `claude_auth.claude_env` (`todo_system/claude_auth.py:111`) · `task_validate.validate_file` (`todo_system/task_validate.py:497`) · `cli.db_connect`·`init_db`·`import_markdown_to_db` (`todo_system/cli.py:115`, `:132`, `:1212`; `cmd_import`가 부르는 세 줄 `:1253-1256`) | 프롬프트, JSON 검사, 파일 렌더와 원자적 쓰기, 볼트 커밋 |
| `macp up --once` | 동기화 → 틱 → 러너 한 번 → 수거 → 틱 → 상태 | `cli.snapshot_mtime` (`todo_system/cli.py:1122`) · `orchestrator._main`의 `auto`와 `collect` (`todo_system/orchestrator/orchestrator.py:3603`, `:3756-3805`, `:3726-3729`) · `runner._main`의 `--once` (`todo_system/orchestrator/runner.py:3231`, `:3292`) · `Lease` (`todo_system/orchestrator/lease.py:217`) · `stages.checkpoint_push` (`todo_system/orchestrator/stages.py:2091`) | 순서, 볼트로 chdir, 태스크 파일 커밋, 요약 출력 |
| `macp up` | 위를 반복 | 위 + `Runner.poll_once` · `Runner._announce`(슬롯 알림 — 사용량 판단은 오케스트레이터 배정 시점 한 곳) · `make_executor` (`todo_system/orchestrator/runner.py`) | 루프와 간격 |
| `macp up --service` | 유닛 1개 설치 | `gen_units.service` (`scripts/systemd/gen_units.py:119`) · `write_env` (`:262`) · `systemctl` 호출 선례 (`:321-325`) | 유닛 이름과 `ExecStart` |
| `macp status` | 볼트·등록·인증·원장을 한 화면에 | `config.resolve` (`todo_system/macp/config.py:194`) · `claude_auth.auth_source` · `orchestrator._main`의 `status` (`todo_system/orchestrator/orchestrator.py:3731-3734`) · `trace` (`:3749-3753`) | 묶어 찍기, 검토 대기 문구 |
| `macp dashboard` | 라이브 대시보드 | `engine_script.main` (`todo_system/engine_script.py:140`) → `scripts/macp_dashboard.py` (인자 `:3610-3613`) | 없음 |
| `macp memory …` | 프로젝트 메모리 | `todo_system.macp.__main__.main` (`todo_system/macp/__main__.py:644`) → `_cmd_memory` (`:533`) | 없음 |

## ② 기본 경로

### 디스크 배치

```
~/.macp/                      MACP_HOME. 전역 config.toml·profiles/·host_id의 자리 — 1인 모드는 건드리지 않는다
├── vault/                    1인 모드 볼트. git 리포, 브랜치 main, 원격 없음
│   ├── .macp/config.toml     볼트 마커. worker_root = "~/.macp/work"
│   ├── Todo/items/*.md       태스크 (정본)
│   ├── Worker/               registry.yaml · 원장 · 메일박스 · runs/
│   └── logs/
└── work/                     워커 루트
    ├── <repo>/               공유 클론
    └── <repo>.wt/<slug>/     런마다 하나씩 생기는 워크트리

<내 리포>/.macp/config.toml   프로젝트 마커. vault_root 키로 볼트를 가리킨다
<내 리포>/.macp/memory/       프로젝트 메모리
```

1인 모드가 만드는 것은 `~/.macp/` 아래와 리포의 `.macp/` 아래에만 있다. 워커는 사용자의
작업 트리에서 일하지 않는다. 워커 루트의 클론과 워크트리에서 일하고
(`todo_system/orchestrator/repos.py:388-402`, `:405`), 끝나면 브랜치만 돌려보낸다.

### D2 — "명령 3개"와 설치

세는 세 개는 `macp init`, `macp add "<문장>"`, `macp up --once`다. 설치 한 줄은 선행
조건으로 따로 적는다.

- 선행 조건(명령으로 세지 않는다): Python ≥ 3.12 (`pyproject.toml:9`), `uv`, `git`,
  로그인된 Claude Code CLI. `gh`는 PR이 끝점일 때만 필요하다.
- 설치 줄 초안은 `uv tool install git+https://github.com/jjkim0807/macp`다. **이 문법은
  이 런에서 돌려 보지 않았다 — 미확인.** 2단계가 깨끗한 `HOME`에서 확정한다.
- `vault-template/`은 지금 wheel에 들어가지 않는다. wheel에 강제로 넣는 것은
  `scripts`와 `.claude/commands` 둘뿐이고 (`pyproject.toml:83-84`), sdist 목록에도 없다
  (`pyproject.toml:90`). 2단계가 같은 방식으로 `todo_system/_engine/vault-template`에
  넣고 `paths.engine_root() / "vault-template"`로 찾는다. 체크아웃에서는 같은 식이 리포
  루트의 `vault-template/`을 가리킨다 (`todo_system/paths.py:213-220`).
- `macp add`의 프롬프트와 태스크 템플릿은 `todo_system/prompts/` 아래에 둔다. 스테이지
  프롬프트가 이미 그렇게 패키지 안에 실려 있어 (`todo_system/orchestrator/stages.py:49-51`)
  패키징을 더 바꿀 필요가 없다.

### D3 — 기본 볼트는 `~/.macp/vault`

태스크 본문은 "`~/.macp` 볼트"라고 했지만 그 경로는 이미 다른 용도다. `~/.macp`는
`MACP_HOME`이고 (`todo_system/macp/config.py:30`) 전역 `config.toml`과 `profiles/`
(`todo_system/macp/config.py:31-32`), 호스트 식별자 `host_id`
(`todo_system/orchestrator/hq_host.py:37`)가 산다. 그래서 볼트는 그 **하위 디렉터리**에
둔다.

- 볼트: `~/.macp/vault`. 볼트 자신의 마커는 `~/.macp/vault/.macp/config.toml`이다.
- 워커 루트: 새 볼트의 설정에 **기존 키** `worker_root = "~/.macp/work"`를 적는다
  (`vault-template/.macp/config.toml:18-19`, 읽는 곳 `todo_system/paths.py:403-416`).
  기본값 `~/Worker`를 쓰지 않는 이유는 홈에 디렉터리가 하나 더 생기지 않게 하려는 것이다.
- **프로필을 만들지 않는다.** 프로필을 만들면 전역 `~/.macp/config.toml`이 생기는데
  (`todo_system/macp/config.py:380-381`), 볼트 해석기의 상위 탐색은 그 파일도 마커로 읽어
  `HOME`을 볼트로 돌려준다 (`todo_system/paths.py:151-156`; 확인 실행 9). 프로젝트 쪽
  탐색만 `MACP_HOME`을 건너뛴다 (`todo_system/macp/config.py:187-188`).
- `Profile.vault` 필드는 볼트 해석에 쓰이지 않는다. 테스트 밖에서 이 필드를 읽는 곳은
  출력 두 줄과 기록 한 줄뿐이다 (`todo_system/macp/__main__.py:87`, `:101`,
  `todo_system/macp/config.py:348`).

버린 선택지: `~/.macp` 자체를 볼트로(전역 설정과 파일명이 충돌한다), `~/my-vault`(홈에
디렉터리가 늘고 사용자가 이름을 정해야 한다).

워커 루트를 고정 경로로 읽는 곳이 하나 있다. 디스크 유지보수 모듈이 `~/Worker`를 직접
쓴다 (`todo_system/maintenance_disk.py:413`). 1인 모드는 maintenance를 띄우지 않으므로
닿지 않는다. 런 시작 때의 디스크 가드는 `paths.worker_root()`를 import 시점에 한 번 읽는다
(`todo_system/orchestrator/disk_guard.py:83`) — D4의 순서를 지키면 맞는 값을 본다.

### D4 — 환경변수 없이 볼트 찾기

`lite`의 모든 동사는 엔진 모듈을 import하기 전에 볼트를 한 번 해석해
`os.environ["MACP_VAULT_ROOT"]`에 넣는다. 순서:

1. `MACP_VAULT_ROOT`가 이미 있으면 그대로 쓴다. 기존 볼트를 가진 사람과 본부가 여기에
   해당한다.
2. 없으면 `paths.vault_root()`의 상위 탐색 결과를 본다. 그 디렉터리에 `Todo/items/`가
   있을 때만 받아들인다.
3. 그 밖에는 기본 볼트 `~/.macp/vault`. (`MACP_HOME`이 있으면 그 아래 `vault`.
   `todo_system/macp/config.py:30`과 같은 식이다.)

이것은 새 스위치가 아니다. 기존 변수의 값을 프로세스 안에서 정해 주는 것이고, 밖에서 준
값이 항상 이긴다.

**왜 환경에 넣는가.** 볼트를 import 시점에 한 번 읽어 상수로 굳히는 곳이 여럿이고 출발점이
서로 다르다.

| 상수 | 읽는 곳 | 환경변수가 없을 때의 출발점 |
|---|---|---|
| `cli.ROOT` | `todo_system/cli.py:32` | 현재 디렉터리 |
| `stages._VAULT_DATA_ROOT` | `todo_system/orchestrator/stages.py:48` | 체크아웃이면 엔진 루트, wheel이면 현재 디렉터리 (`todo_system/paths.py:223-243`) |
| `usage_guard.ROOT` | `todo_system/usage_guard.py:92` | 위와 같음 |
| `model_select._ROOT` | `todo_system/orchestrator/model_select.py:90` | 위와 같음 |
| 대시보드 `VAULT` | `scripts/macp_dashboard.py:82` | 위와 같음 |
| 유닛 생성기 `VAULT` | `scripts/systemd/gen_units.py:51-52` | 고정 기본 경로 |

환경에 먼저 넣으면 여섯 곳이 같은 볼트를 보고, 스테이지가 띄우는 자식 프로세스도 물려받는다.
체크아웃과 wheel에서 결과가 같다(확인 실행 2는 체크아웃에서 했다).

**2번에 `Todo/items/` 확인이 붙는 이유.** 볼트 마커와 프로젝트 마커의 파일명이 같다
(`.macp/config.toml`). 해석기는 `vault_root` 키가 없는 마커를 만나면 그 디렉터리를 볼트로
돌려준다 (`todo_system/paths.py:155-156`). 그래서 등록된 리포 안이나, 전역 설정이 있는
`HOME` 아래에서는 예외 없이 엉뚱한 디렉터리가 나온다(확인 실행 9).

| 선택지 | 문제 |
|---|---|
| (가) wheel의 상위 탐색에만 기댄다 | 리포 밖에서 실패한다. 체크아웃에서는 엔진 루트에서 찾으므로 실패한다. 마커 충돌을 그대로 맞는다 |
| (나) lite 동사는 상위 탐색을 무시하고 환경변수 아니면 기본 볼트 | 자기 볼트 체크아웃 안에서 친 사람의 태스크가 다른 볼트로 간다 |
| (다) 채택: 환경변수 → 볼트 모양을 갖춘 상위 탐색 결과 → 기본 볼트 | — |

**보강 두 가지.** 둘 다 기존 해석기에 얹는다.

- `macp init`이 리포의 `.macp/config.toml`에 **기존 키** `vault_root = "~/.macp/vault"`를
  적는다. 물결표 표기라 커밋돼도 사용자 이름이 들어가지 않고, 해석기가 `expanduser`로 푼다
  (`todo_system/paths.py:155-156`). 이 키가 있어야 리포 안에서 치는 **기존** 동사
  (`macp validate`, `macp complete <slug>`)도 맞는 볼트를 찾는다. 키가 없으면 그 동사들은
  리포 자체를 볼트로 보고 리포 안에 `Todo/todo.db`를 만든다.
  `write_project`는 고정 스키마라 이 키를 지운다 (`todo_system/macp/config.py:330-344`;
  확인 실행 9). 2단계가 `ProjectConfig`에 `vault_root` 필드를 **맨 끝에** 더하고, 읽고
  (`todo_system/macp/config.py:158-168`), 값이 있을 때만 쓴다. 값이 있을 때만 쓰는 선례는
  `account`·`name`·`provides`다 (`todo_system/macp/config.py:334-341`). 그 키가 없는 기존
  프로젝트 파일은 한 바이트도 달라지지 않는다.
- `_entry`의 기존 동사 경로는 **`VaultRootError`가 날 때만**, 곧 지금 exit 2로 끝나는
  경우에만, 기본 볼트가 있으면 그것을 환경에 넣고 다시 import한다. 기본 볼트도 없으면
  빠른 시작 안내(`todo_system/_entry.py:16-20`)를 `macp init` 한 줄로 바꿔 찍는다.

### D5 — `macp init`

몇 번을 쳐도 결과가 같다. 이미 있는 것은 건드리지 않고 "이미 있음"이라고 말한다.

1. **볼트 보장.** D4로 해석한 볼트가 없으면 패키지의 `vault-template/`을 복사하고,
   `worker_root` 키를 적고, `git init -b main` 뒤 첫 커밋을 만든다.
   - 브랜치 이름이 `main`이어야 하는 이유: 엔진이 볼트의 브랜치를 `main`으로 가정한다
     (`todo_system/orchestrator/stages.py:2952`, `:2979-2980`;
     `todo_system/orchestrator/orchestrator.py:3405-3407`;
     `todo_system/orchestrator/repos.py:562-564`).
   - 커밋이 필요한 이유: `macp add`가 태스크를 커밋하고, 뒤에 볼트 태스크를 쓰게 되면
     워크트리를 자를 커밋이 있어야 한다 (`docs/running.md:96-98`).
   - 깨끗한 `HOME`에는 git 사용자 정보가 없어 커밋이 실패한다. 볼트 리포의 **로컬** 설정에
     이름·메일이 없을 때만 대체 값을 넣는다. 전역 설정은 건드리지 않는다.
   - `vault-template`에는 `.gitignore`가 없다. 한 바퀴 돌고 나면 `Todo/todo.db`,
     `Todo/NOW.md`, `Todo/FOCUS.md`, `Todo/Dashboard.md`, `Worker/_state/`,
     `Worker/agents/<agent>/`, `Worker/assignments/`, `Worker/orchestrator.lock`,
     `Worker/queue.lock`이 추적되지 않은 채 남는다(확인 실행에서 본 목록). `macp init`이
     **새 볼트에** `.gitignore`를 써서 이 목록과 `Worker/runs/`, `logs/`를 뺀다. 템플릿에는
     넣지 않는다: 원장과 메일박스는 본부에서 git으로 호스트 사이를 오가는 상태라
     (`README.md:68-70`), 템플릿에서 빼 버리면 템플릿으로 만든 본부식 새 볼트의 동작이
     달라진다. 1인 모드는 호스트가 하나이므로 그 볼트에서만 뺀다.
2. **리포 등록.** 현재 디렉터리가 git 리포 안이면 그 최상위에 대해 프로젝트 CLI의
   `init`과 같은 세 함수를 같은 순서로 부른다 (`todo_system/macp/__main__.py:52-55`):
   `write_project`(프로필 `default`, `vault_root` 키 포함) → `ensure_memory` →
   `link_agent_context`.
   - 사용자 작업 트리에 생기거나 바뀌는 것은 넷이다: `.macp/config.toml`,
     `.macp/memory/index.md`, 있으면 `CLAUDE.md` 끝에 `@.macp/memory/index.md` 한 줄
     (`todo_system/macp/memory.py:75-80`), `AGENTS.md`(있으면 한 줄, 없으면 새 파일 —
     `todo_system/macp/memory.py:83-97`). `init`은 이 목록을 그대로 출력하고 **커밋하지
     않는다**.
   - `_cmd_init`을 통째로 부르지 않는 이유: 프로필이 없다는 경고를 찍는다
     (`todo_system/macp/__main__.py:49-51`). 1인 모드는 일부러 프로필을 만들지 않는다.
   - "등록"의 실체는 리포 쪽 마커뿐이다. 볼트에는 리포 목록이 없고, 태스크의 `repo:`
     필드가 리포를 가리킨다 (`todo_system/orchestrator/tasks.py:163`, `:241-244`).
   - 리포 밖에서 치면 볼트만 만들고 그렇게 말한다.
   - 알려 둘 점: 새로 만드는 `AGENTS.md`와 메모리 색인의 기본 문구가 한국어다
     (`todo_system/macp/memory.py:36-40`, `:90-95`). 이 문서는 그 함수를 그대로 쓰고,
     문구 번역은 따로 다룬다.
3. **로그인 확인.** 막지 않고 알려 주기만 한다.
   - `claude`가 PATH에 있는지 본다(선례 `todo_system/worker.py:4151`).
   - `model_select.resolve_model(tier="light")`를 한 번 부른다. 이 함수는 사다리를 위에서
     내려오며 1턴짜리 실제 호출로 모델을 확인하고 결과를 24시간 캐시한다
     (`todo_system/orchestrator/model_select.py:141-177`, `:304-343`). 첫 단이 나오면
     로그인이 살아 있다는 뜻이다. 마지막 단이 나오면 — 마지막 단은 확인 없이 돌려주므로
     (`:311-312`, `:332`) — "Claude Code를 쓸 수 없는 것 같다, `claude`를 한 번 실행해
     로그인하라"고 한 줄 찍는다.
   - 이 호출은 낭비가 아니다. `add`와 `up`이 같은 캐시를 쓰므로 첫 확인을 앞으로 당긴
     것이다.
   - 엔진에는 `claude auth status`를 부르는 코드가 없다. 그래서 새 확인 수단을 들이지
     않고 이미 있는 확인을 쓴다.
   - 막지 않는 이유: 막는 단계를 더하려면 그것이 되돌릴 수 없는 무엇을 지키는지 적어야
     하는데 적을 것이 없다. 로그인이 없으면 `macp add`가 같은 이유로 곧 실패하고 그때
     같은 안내를 한다.

### 기존 리포가 태스크와 이어지는 방식

`macp add`가 태스크의 `repo:`에 적는 값은 하나의 규칙으로 정한다. 현재 리포의 `origin`이
URL이면 그 URL, 아니면 리포 최상위의 절대 경로다. URL 판정은 러너가 쓰는 것과 같다:
`://`가 들어 있거나 `git@`로 시작한다 (`todo_system/orchestrator/runner.py:700-704`).

| | `origin`이 URL | `origin`이 없거나 경로 |
|---|---|---|
| `repo:` 값 | 그 URL | 리포의 절대 경로 |
| 워커가 보는 상태 | 원격에 **push된** 커밋 | 로컬에 **커밋된** 것 |
| 기준 브랜치 | 원격의 기본 브랜치 | 워커 클론을 처음 만들 때 사용자가 체크아웃해 둔 브랜치 |
| 결과물 | 원격의 `worker/<slug>` + PR | 사용자 리포의 로컬 브랜치 `worker/<slug>` |

- 어느 쪽이든 커밋하지 않은 변경은 워커가 보지 못한다. `macp add`는 작업 트리가 더럽거나
  원격보다 앞서 있으면 그 사실을 한 줄로 알린다(막지 않는다).
- 클론은 `git clone <repo 값> <워커 루트>/<이름>`으로 만들어지고
  (`todo_system/orchestrator/repos.py:373`), 이름은 값의 마지막 조각이다
  (`todo_system/orchestrator/repos.py:321-325`).
- 기준 브랜치는 클론의 `refs/remotes/origin/HEAD`에서 읽고, 없으면 `main`으로 본다
  (`todo_system/orchestrator/stages.py:1203-1207`, `:1506`). 경로 원격에서 사용자가 나중에
  다른 브랜치로 옮겨도 기준은 그대로일 것으로 본다(git이 그 참조를 fetch로 고치지 않는다 —
  이 런에서 확인하지 않음).
- 알려진 한계(기존 엔진의 성질): 마지막 조각이 같은 두 리포는 같은 클론 디렉터리를 쓴다.
  이미 있는 클론은 원격을 확인하지 않고 재사용된다
  (`todo_system/orchestrator/repos.py:353-354`). `macp add`가 그 디렉터리의 `origin`을
  읽어 다른 리포면 태스크를 만들지 않고 이유를 말한다.

## ③ 로그인·CLAUDE.md 재사용

### D6 — 로그인: 프로세스 환경을 그대로 물려준다

1인 모드에는 키를 넣는 단계가 없다. 사용자가 평소 쓰는 Claude Code 로그인으로 스테이지가
돈다. 이것은 엔진이 계정을 설정하지 않았을 때 이미 하는 동작이다.

- 계정·프로필이 없으면 러너의 계정 해석은 `(None, None)`을 돌려주고
  (`todo_system/orchestrator/runner.py:356-357`), 스테이지 환경은 프로세스 환경의 사본이
  된다 (`todo_system/orchestrator/runner.py:1170-1183`).
- `claude_auth.claude_env`도 계정과 토큰 파일이 없으면 환경을 건드리지 않는다
  (`todo_system/claude_auth.py:129-142`).
- `auth_source`는 지금 쓰이는 자격증명을 `api-key`·`automation-token`·`keychain-oauth`
  가운데 하나로 알려 준다 (`todo_system/claude_auth.py:145-152`). `macp status`가 이 값을
  그대로 보여 준다.
- 그래서 1인 모드는 프로필·계정을 만들지 않는다(D3). 여러 계정을 쓰려는 사람은 본부와
  같은 방법(`python -m todo_system.macp account …`)을 쓰면 되고, 그때도 1인 모드 명령은
  그대로 돈다.

사용량 페이싱은 ⑤의 D9에서 다룬다.

### CLAUDE.md: 엔진이 보장하는 것과 그렇지 않은 것

엔진이 **보장하는** 것은 둘이다.

1. 리포의 `.macp/memory/*.md`가 모든 스테이지 프롬프트 끝에 "Project memory"로 붙는다.
   러너가 체크아웃에서 읽어 (`todo_system/orchestrator/runner.py:1184-1189`,
   `todo_system/macp/memory.py:170`) 프롬프트에 넣는다
   (`todo_system/orchestrator/stages.py:841-843`). 워커는 클론에서 일하므로 **커밋된**
   메모리만 본다. `macp init`이 "`.macp/`를 커밋하면 워커도 본다"고 안내하는 이유다.
2. `macp init`이 사용자의 `CLAUDE.md`에 `@.macp/memory/index.md`를 건다
   (`todo_system/macp/memory.py:19`, `:75-80`). 대화형 세션과 워커가 같은 메모리를 읽게
   된다.

**보장하지 않는** 것이 하나 있다. 리포 자신의 `CLAUDE.md`를 스테이지가 자동으로 읽는지는
확인하지 못했다. 스테이지는 볼트를 작업 디렉터리로 삼고
(`todo_system/orchestrator/runner.py:1590-1591`) 체크아웃에는 `--add-dir`로 닿는다
(`todo_system/orchestrator/stages.py:867-869`). 그 경로의 `CLAUDE.md`를 Claude Code가
스스로 읽는지는 Claude Code의 동작이라 이 리포에서 알 수 없다 — **미확인**.

그래서 자동 로드에 기대지 않고 태스크 본문에 적는다. `macp add`는 리포 최상위에
`CLAUDE.md`·`AGENTS.md`가 있으면 태스크 본문 `## Spec`의 참고 항목에 그 경로를 넣는다.
5단계 파이프라인의 첫 스테이지는 태스크 파일을 가장 먼저 읽게 돼 있고, 실행 메타데이터에
경로가 없으면 `Todo/items/<slug>.md`를 읽으라고 적혀 있다
(`todo_system/prompts/worker-research.md:12-14`). 엔진은 바뀌지 않는다.

**`quick` 레인의 틈.** `quick` 프롬프트는 실행 메타데이터에서 `outcome`·`next_action`을
읽으라고 한다 (`todo_system/prompts/worker-quick.md:9-10`). 그런데 메타데이터에는 그 둘도,
태스크 파일 경로도 없다 (`todo_system/orchestrator/stages.py:737-752`). 본부에서는
스테이지의 작업 디렉터리인 볼트에 `CLAUDE.md`가 있어 볼트 구조를 알려 준다. 작업
디렉터리의 `CLAUDE.md`가 `claude -p`에 실린다는 것은 엔진에 기록돼 있다
(`todo_system/orchestrator/model_select.py:147-151`). 1인 모드 볼트에는 그 파일이 없다
(`vault-template/`에 없다). 2단계가 `vault-template/`에 몇 줄짜리 `CLAUDE.md`를 둔다:
태스크는 `Todo/items/<slug>.md`에 있고 런 산출물은 `Worker/runs/<run-id>/`에 쓴다는 것.
데이터 파일 하나이고, 이미 만들어진 볼트에는 영향이 없다. `quick` 스테이지가 이 안내
없이도 태스크 파일을 찾아 읽는지는 확인하지 않았다 — **미확인**.

## ④ macp add

### D7 — 모델에게는 JSON만 받고, 나머지는 CLI가 한다

`macp add "<자연어>"`는 `claude -p`를 **한 번** 부른다. 모델은 파일을 쓰지 않고 JSON 객체
하나만 돌려준다. 이유는 셋이다: 가짜 `claude`로 테스트할 수 있고, 도구 권한이 필요 없고,
스키마로 검사할 수 있다.

**호출 모양.** 엔진의 모델 확인 호출과 같은 꼴을 쓴다
(`todo_system/orchestrator/model_select.py:161-164`).

- 인자: `claude -p <프롬프트> --model <모델> --effort <effort> --max-turns 1`. 모델과
  effort는 light 단계의 값이다: `stages.stage_model("quick", "light")`,
  `stages.stage_effort("quick", "light")` (`todo_system/orchestrator/stages.py:86-102`).
- 환경: `claude_auth.claude_env()` (`todo_system/claude_auth.py:111`).
- 작업 디렉터리: 빈 임시 디렉터리. 지침 파일이 있는 디렉터리에서 1턴 호출을 하면 모델이
  일을 시작해 턴 제한에 걸린다는 것이 확인 호출에서 이미 드러났다
  (`todo_system/orchestrator/model_select.py:147-151`).
- 제한 시간은 모듈 상수로 둔다. JSON이 깨졌으면 한 번만 다시 부른다.

**프롬프트에 넣는 것.** 사용자의 문장, 리포 이름, 최상위에서 추적되는 항목 이름
(`git ls-files`에서 앞부분만), `CLAUDE.md`·`AGENTS.md` 유무. 모델이 완료 조건에 그
리포에서 실제로 돌릴 명령(`uv run pytest`, `npm test` …)을 적을 수 있게 하려는 것이다.
판단 규칙은 기존 `/todo` 명령의 것을 옮긴다 (`.claude/commands/todo.md:12-29`): 슬러그,
한 줄 `outcome`, 지금 할 `next_action`, 구체적인 `if_then`, 로컬 명령으로 쓴
`acceptance_criteria`, 건드릴 파일 `touches`. 프롬프트 파일은
`todo_system/prompts/` 아래에 둔다.

**누가 어느 필드를 채우나.**

| 모델이 채운다 | CLI가 채운다 |
|---|---|
| `slug`, 제목, `outcome`, `next_action`, `if_then`, `acceptance_criteria[]`, `worker_mode`(`quick` 또는 `auto`), `estimate_p50`·`estimate_p90`, `touches[]`, 분류 태그 | `status: open`, `due: "-"`, `updated`, `review_cycle: "-"`, `priority`, `repo`, `auto_merge: false`, `worker_branch`, 본문 |

**CLI가 고정하는 값과 이유.**

- `auto_merge: false`를 **명시한다.** 이 필드가 없으면 기본이 참이다
  (`todo_system/cli.py:98`, `:1051`; `todo_system/orchestrator/tasks.py:176-179`;
  `todo_system/orchestrator/runner.py:475-486`). 첫 실행이 사용자 저장소의 기본 브랜치를
  바꾸지 않게 하려는 것이고, 문서의 기존 권고와 같다 (`docs/getting-started.md:171-172`,
  `SECURITY.md:16-17`). 사용자가 태스크 파일에서 `true`로 바꾸면 본부와 같은 자동 머지가
  된다. 새 스위치는 없다.
- `allowed_tools`를 **넣지 않는다.** 태스크가 이 필드를 주면 implement 스테이지의 기본
  목록이 통째로 바뀐다 (`todo_system/orchestrator/stages.py:510-517`). 기존 템플릿의 값
  (`vault-template/Todo/templates/auto-worker-task-template.md:37`)에는
  `npm`·`pnpm`·`node`·`npx`가 없어서, 그대로 쓰면 기본 목록
  (`todo_system/orchestrator/stages.py:379-382`)보다 좁아진다. 넣지 않으면 검증기도 이
  항목을 건너뛴다 (`todo_system/task_validate.py:294-295`).
  이 필드는 implement에만 적용된다 (`todo_system/orchestrator/stages.py:458`). `quick`
  레인은 언제나 자기 기본 목록을 쓴다 (`todo_system/orchestrator/stages.py:390-392`).
- 그래서 기존 `auto-worker` 템플릿을 쓰지 않고 1인 모드용 템플릿 파일(데이터)을 따로
  둔다. 템플릿의 자리표시를 값으로 바꾸는 방식은 `cmd_new`가 선례다
  (`todo_system/cli.py:1364-1371`).
- 새 태스크는 `blocked`가 아니라 바로 실행 가능한 모드로 만든다. 기존 템플릿이
  `blocked`로 시작하는 이유는 반쯤 쓴 파일이 디스패치되는 것을 막으려는 것인데
  (`vault-template/Todo/templates/auto-worker-task-template.md:23-26`), 아래처럼 임시
  이름에 쓰고 이름을 바꾸면 그런 순간이 없다.

**태스크 본문.** `# <제목>` 아래 `## Spec`에 다음을 적는다: 목표(`outcome`과 같은 말),
사용자가 친 문장 그대로, 참고(`CLAUDE.md`·`AGENTS.md` 경로), 그리고 한 줄 —
"`auto_merge: false`는 `macp add`의 기본값이다. 결과를 사람이 검토해 머지한다는 뜻이고,
안전·금융 경로라는 표시가 아니다." 마지막 줄이 필요한 이유: `quick` 프롬프트는
`auto_merge: false`가 안전·금융 사유로 붙은 태스크를 만나면 실패로 멈추라고 한다
(`todo_system/prompts/worker-quick.md:55-61`). 사유를 적어 두지 않으면 모델이 그 규칙을
잘못 적용할 수 있다.

**순서.**

1. 현재 디렉터리가 등록된 리포 안인지 확인한다. 아니면 "`macp init`을 먼저"라고 말하고
   끝낸다.
2. `claude -p` 호출 → 출력에서 JSON 객체를 꺼낸다.
3. 스키마 검사: 타입, `worker_mode` 값, 슬러그 형식(소문자·숫자·하이픈), 목록의 각 항목이
   빈 문자열이 아닌지, 값이 한 줄인지. 슬러그가 이미 있으면 뒤에 번호를 붙인다.
4. 파일 렌더. 문자열 값은 큰따옴표로 감싸고 이스케이프한다. 이 파서는 값 뒤의 `#`를
   주석으로 떼지 않는다 (`todo_system/task_validate.py:369-387`).
5. `Todo/items/<slug>.md.part`에 쓰고 `os.replace`로 이름을 바꾼다.
6. `task_validate.validate_file` (`todo_system/task_validate.py:497`). 오류가 있으면
   파일을 지우고 실패한다. 경고는 개수만 알린다.
7. `db_connect` → `init_db` → `import_markdown_to_db`로 `todo.db`에 넣는다
   (`todo_system/cli.py:1253-1256`).
8. 볼트에서 그 파일만 `git add`하고 커밋한다.
9. 한 줄로 알린다: 슬러그, 레인, `outcome`, 그리고 다음에 칠 명령 `macp up --once`.

**실패할 때.** 원시 예외를 보여 주지 않는다. 경우마다 한 줄이고 `i18n`의 기존 방식
(`todo_system/i18n.py:23`, `:904`; 언어는 `todo_system/paths.py:690`)으로 영어·한국어를
둔다: `claude`를 찾을 수 없음 / 로그인되지 않음 / 시간 초과 / 응답이 JSON이 아님 / 스키마
위반(어느 필드인지) / 등록된 리포 밖 / 같은 이름의 다른 리포가 이미 워커 루트에 있음.

**이번 범위 밖.** 리포 밖에서 만드는 볼트 태스크, `delegate`·`manual` 모드 태스크. 그런
태스크는 지금처럼 `macp new`로 만들 수 있다.

### 검증기가 보는 것

`validate_file`은 frontmatter가 엄격한 YAML로 읽히는지 본 뒤
(`todo_system/task_validate.py:499-501`) 워커가 읽는 필드를 검사한다
(`todo_system/task_validate.py:290`). `repo`가 있고 실행 가능한 모드인데 `touches`가
없으면 경고한다 (`todo_system/task_validate.py:408-425`) — 그래서 모델에게 `touches`를
받는다. 확인 실행 3에서 위 모양의 파일은 지적 0건이었다.

## ⑤ macp up

### D8 — 단일 스레드 루프, 전부 기존 호출

`macp up`은 시작하면서 세 가지를 한다.

- 볼트로 `chdir`한다. 틱 스크립트와 유닛이 그렇게 돈다
  (`scripts/orchestrator_tick.sh:48`; 유닛의 `WorkingDirectory`
  `scripts/systemd/gen_units.py:119`, `:129`). 현재 디렉터리 기준 상대 경로를 쓰는 곳이
  아직 있어서다 (`todo_system/orchestrator/runner.py:600`,
  `todo_system/orchestrator/mailbox.py:39`).
- `Worker/up.lock`을 기존 `Lease` 클래스로 잡는다
  (`todo_system/orchestrator/lease.py:85-89`, `:217`). 살아 있는 다른 `macp up`이 잡고
  있으면 "이미 도는 루프가 처리한다"고 말하고 0으로 끝난다. 서비스를 띄워 둔 사람이
  습관대로 `macp up --once`를 쳐도 러너가 둘이 되지 않는다. 죽은 프로세스의 잠금은 같은
  호스트에서 바로 회수된다 (`todo_system/orchestrator/lease.py:147-160`). 긴 런 동안
  갱신하지 못하므로 만료 시간은 길게 준다(생성자 인자 `ttl`).
- 메일박스 주소는 `file://<볼트>/Worker/agents`다. 본부의 유닛과 틱이 쓰는 것과 같다
  (`scripts/systemd/gen_units.py:195`, `scripts/orchestrator_tick.sh:52-56`). 엔진 CLI의
  기본값 `git://`는 메시지마다 볼트에 커밋한다
  (`todo_system/orchestrator/orchestrator.py:3608`,
  `todo_system/orchestrator/mailbox.py:358`).

**`macp up --once`가 하는 일.**

| 순서 | 하는 일 | 호출 |
|---|---|---|
| 1 | 태스크 파일이 바뀌었으면 DB에 반영 | `import_markdown_to_db` (`todo_system/cli.py:1212`). 본부에서는 todo-sync 유닛이 하는 일이다 (`todo_system/cli.py:1395`, `:1433-1447`) |
| 2 | 틱: 라우팅·디스패치 | `orchestrator._main(["--uri", <주소>, "--lease", "<볼트>/Worker/orchestrator.lock", "auto", "--db", "<볼트>/Todo/todo.db"])` — 틱 스크립트의 인자 그대로 (`scripts/orchestrator_tick.sh:52-61`) |
| 3 | 러너 한 번: 받은 태스크의 파이프라인을 끝까지 실행 | `runner._main(["--agent", "worker-default", "--mode", "live-staged", "--uri", <주소>, "--once"])` (`todo_system/orchestrator/runner.py:3283-3292`). 인자는 러너 유닛과 같고 `--daemon`만 `--once`다 (`scripts/systemd/gen_units.py:195`) |
| 4 | 수거 | `orchestrator._main(["--uri", <주소>, "collect"])` (`todo_system/orchestrator/orchestrator.py:3726-3729`) |
| 5 | 틱: 닫기·정리 | 2와 같은 호출 |
| 6 | 태스크 파일 변경 커밋 | 볼트에서 `Todo/items/`에 변경이 있을 때만 커밋 한 번 |
| 7 | 상태 | `macp status`와 같은 출력 |

**4번이 따로 있는 이유.** 틱은 수거보다 **먼저** 오래 소식이 없는 진행 중 행을 실패로
돌린다 (`todo_system/orchestrator/orchestrator.py:3190`, `:3218`). 한도는 그 태스크의
스테이지 제한 시간에 900초를 더한 값이다
(`todo_system/orchestrator/orchestrator.py:2024-2041`, `:2070-2077`). 본부에서는 런이 도는
동안에도 5분마다 틱이 돌아 스테이지 사이의 진행 메시지를 수거하므로 행이 계속 갱신된다.
단일 스레드에서는 런이 도는 동안 틱이 없다. 그래서 스테이지가 여럿인 런이 한도보다 오래
걸리면, 끝난 런의 행을 다음 틱이 죽은 것으로 보고 실패로 돌린다. 짝이 되는 진행 중 행이
없는 응답은 버려진다고 수거 코드가 적고 있다
(`todo_system/orchestrator/orchestrator.py:1390-1395`). 러너가 돌아온 직후에 `collect`를
먼저 부르면 응답이 먼저 적용되고, 그 뒤의 틱은 그 행을 진행 중으로 보지 않는다. 이 순서는
코드를 읽어 정한 것이다. 2단계가 테스트로 고정한다.

**6번이 있는 이유.** 오케스트레이터는 끝난 태스크의 파일을 닫는다(확인 실행에서
`status: done`으로 바뀐 것을 봤다). 본부에서는 maintenance가 볼트 변경을 커밋한다
(`todo_system/worker.py:2494`, `:2597`). 그 함수는 push까지 하므로 원격 없는 볼트에서는
쓰지 않고, 태스크 디렉터리만 커밋하는 몇 줄을 둔다. 원장·메일박스 같은 런타임 파일은
`.gitignore`로 뺀다(②의 D5).

**`macp up`(루프).** 같은 호출을 반복한다. 러너는 한 번 만들어 계속 쓴다
(`todo_system/orchestrator/runner.py:3220-3221`, `:3286-3287`).

- 15초마다 메일박스를 본다(러너의 기본 간격 `todo_system/orchestrator/runner.py:3251`).
  사용량 가드가 막고 있으면 `bypass_guard` 태스크만 처리한다 — 러너의 직렬 분기와 같은
  분기다 (`todo_system/orchestrator/runner.py:3181-3194`).
- 틱은 세 경우에 돈다: 태스크 파일이 바뀌었을 때(`snapshot_mtime` 비교
  `todo_system/cli.py:1122`, `:1435-1443`), 러너가 무언가를 처리했을 때(수거 먼저), 마지막
  틱에서 5분이 지났을 때(본부의 틱 간격 `scripts/systemd/gen_units.py:208`).
- 간격은 모듈 상수다. 환경변수나 설정 키로 꺼내지 않는다.

**띄우지 않는 것.**

| 본부의 유닛 | 1인 모드 | 이유 |
|---|---|---|
| todo-sync (`scripts/systemd/gen_units.py:217-222`) | 루프 안의 1번 | 같은 함수 |
| planner (`:209-212`) | 띄우지 않음 | 큐가 비면 스스로 태스크를 만든다. 새 사용자가 시키지 않은 일이다 |
| maintenance (`:213-216`) | 띄우지 않음 | 엔진 자기 갱신과 볼트 push. 설치본에는 갱신할 체크아웃이 없고 볼트에는 원격이 없다 |
| brief·health (`:232-238`) | 띄우지 않음 | 첫 PR에 필요 없다 |
| dashboard (`:226-231`) | `macp dashboard`를 칠 때만 | |

알림은 설정 파일이 없으면 침묵한다 (`README.md:131-134`). 호스트 가드는 표식 파일이 없으면
통과한다 (`todo_system/orchestrator/hq_host.py:49-50`). 일일 실행 한도와 리포별 동시 실행
수는 프로필이 없을 때의 기본값을 그대로 쓴다
(`todo_system/orchestrator/orchestrator.py:3698`; 확인 실행에서 프로필 없이 디스패치됐다).

**원격 없는 볼트와 `_pull_main`.** 틱의 `auto`는 볼트에서 `git fetch origin main`을 한다
(`todo_system/orchestrator/orchestrator.py:3684-3689`). 볼트가 git 리포가 아니면 조용히
끝나지만 (`:3405-3407`), git 리포이고 원격이 없으면 세 번 시도하고 그 사이 1초씩 쉰 뒤
오류처럼 보이는 줄을 찍는다 (`:3409-3415`, `:3450`; 확인 실행 6). 2단계가 여기에 가장 작은
변경을 넣는다: **첫 fetch가 실패했고 `origin` 원격이 없으면 재시도 없이 조용히 끝낸다.**
이 확인을 fetch 실패 뒤에 두면 원격이 있는 볼트에서는 git 호출 순서가 지금과 한 줄도
다르지 않다(이 함수의 테스트는 git 호출을 가짜로 바꿔 순서를 본다 —
`todo_system/orchestrator/orchestrator.py:3361`). 바로 뒤의 `_check_divergence`는 실패해도
아무것도 찍지 않으므로 그대로 둔다 (`:3476-3488`).

버린 선택지: 러너를 스레드로 돌리고 틱을 타이머로 돌리기. 본부와 더 비슷하지만 스레드와
종료 처리가 늘어난다. 1인 모드는 동시 실행이 1이고(`vault-template/Worker/agents/registry.yaml:24`),
위의 수거 순서로 스레드 없이 같은 결과를 얻는다.

### D9 — `--once`는 페이싱 없이 바로 돈다

사용량 가드는 Claude 구독의 5시간·주간 사용률을 읽어 자동화가 페이스를 앞서면 디스패치를
보류한다 (`docs/getting-started.md:50-52`). 1인 모드는 두 경우를 나눈다.

| 실행 | 가드 | 방법 |
|---|---|---|
| `macp up --once` | 끈다 | 사용자가 `USAGE_GUARD_DISABLE`를 직접 주지 않았을 때만, 엔진 import 전에 프로세스 환경에 `1`을 넣는다 |
| `macp up`, `--service` | 켠다 | 아무것도 하지 않는다. 본부와 같은 판정이다 |

이유:

- 가드는 쿼터가 남아 있어도 보류한다. 기준이 "창이 지난 비율보다 사용률이 얼마나
  앞섰나"이기 때문이다 (`todo_system/usage_guard.py:126-137`). Claude Code를 평소 많이 쓰는
  사람 — 1인 모드의 대상 — 일수록 첫 실행에서 `USAGE-HOLD`를 볼 가능성이 높고, 풀리는
  시각은 며칠 뒤일 수 있다. 지금 README도 이 때문에 5분 체험에서 가드를 끄게 한다
  (`README.md:88`, `README.md:107-109`).
- 런이 이미 도는 중에도 5시간 창이 80%를 넘으면 다음 스테이지 앞에서 기다린다
  (`todo_system/usage_guard.py:164`, `:178-205`). 이 대기는 최대 5시간 반이다
  (`todo_system/orchestrator/runner.py:2257-2303`). 포그라운드 명령이 그동안 멈춰 있게
  된다.
- 가드가 막으려는 것은 사람이 없는 동안 자동화가 사람 몫의 쿼터까지 다 쓰는 일이다.
  `--once`는 사람이 방금 치고 지켜보는 한 번의 실행이다. 대화형 세션과 같은 처지이고,
  막아서 지킬 되돌릴 수 없는 것이 없다.
- 한도가 실제로 찼으면 `claude`가 그렇게 말하고, 러너는 그것을 결함이 아닌 보류로 처리한다
  (`todo_system/orchestrator/runner.py:1720-1766`). `macp up --once`는 그 이유를 보여 준다.

끄는 스위치는 기존 것이다 (`todo_system/usage_guard.py:1115-1120`). 이 값이 있으면 가드는
조회도 상태 기록도 하지 않으므로, 같은 볼트를 쓰는 다른 프로세스의 판정을 흐리지 않는다.
README 초안은 이 차이를 한 문장으로 알린다.

버린 선택지: 항상 켜 두기(위 첫 두 항목 때문에 "5분 안에"를 지킬 수 없는 사용자가 생긴다),
`macp add`가 태스크마다 `bypass_guard: true`를 넣기(루프와 서비스에서도 페이싱이 꺼진다).

### D10 — 끝 상태: PR 또는 로컬 브랜치, 머지는 사람

공통 경로는 이렇다. `auto_merge: false`이면 러너는 이 태스크를 "검토 필요"로 본다
(`todo_system/orchestrator/runner.py:489-502`). 파이프라인이 성공하면 응답에
`review_required: true`가 실리고 (`:1998-1999`), 수거가 그 행을 사람 대기로 둔다
(`todo_system/orchestrator/orchestrator.py:1334-1337`). `macp status`는 이것을 실패가
아니라 **검토 대기**로 보여 준다.

| | URL 원격 + `gh` 로그인 | URL 원격, `gh` 없음 | 경로 원격(원격 없음) |
|---|---|---|---|
| 워커가 남기는 것 | 원격의 `worker/<slug>` + 열린 PR | 원격의 `worker/<slug>` | 사용자 리포의 로컬 브랜치 `worker/<slug>` |
| 근거 | `todo_system/prompts/worker-quick.md:21-22`, `:42`; `todo_system/prompts/worker-report.md:183-225`, `:343-345` | `todo_system/prompts/worker-report.md:248` | 확인 실행 7 |
| 사람이 하는 일 | PR을 검토해 머지 | 브랜치를 보고 PR을 열거나 머지 | `git diff main...worker/<slug>` → `git merge worker/<slug>` → `macp complete <slug>` |
| 태스크가 닫히는 경로 | 다음 틱이 머지된 PR을 보고 닫는다 (`todo_system/orchestrator/orchestrator.py:2263-2269`, `:2330-2331`) | 태스크 파일을 닫으면 다음 틱이 행을 정리한다 (`:2279`, `:2306`) | 왼쪽과 같음 |

- `macp complete`는 기존 동사다 (`todo_system/cli.py:861`, `:2048`). 리포 안에서 치면 D4의
  `vault_root` 키로 볼트를 찾는다.
- 두 닫힘 경로 모두 `reconcile_merged`이고 틱마다 돈다
  (`todo_system/orchestrator/orchestrator.py:2210`, `:3226`).
- 경로 원격에서 사용자가 `worker/<slug>`를 체크아웃해 둔 동안 워커가 같은 브랜치로 다시
  push하면 git이 거부한다(확인 실행 8). 검토는 `git diff`나 별도 워크트리로 하라고
  안내한다.

**2단계가 메워야 할 틈 둘.**

1. **경로 원격에서의 `gh`.** `quick` 프롬프트는 push 뒤 `gh pr create`를 전제한다
   (`todo_system/prompts/worker-quick.md:21-22`). report 프롬프트는 `gh`가 없을 때만
   다룬다 (`todo_system/prompts/worker-report.md:248`). 경로 원격에서는 `gh`가 있어도
   리포를 찾지 못한다. → `stages.build_stage_argv`의 외부 리포 분기
   (`todo_system/orchestrator/stages.py:825-831`)에 **`repo`가 URL이 아닐 때만** 붙는 줄을
   하나 더한다: "origin은 로컬 경로다. 브랜치를 push하고 멈춘다. `gh`를 부르지 않는다. PR
   주소는 없다고 적는다. push된 브랜치가 결과물이다." 메타데이터가 프롬프트 뒤에 붙어
   프롬프트의 문구를 이기는 것은 선례가 있다 (`todo_system/orchestrator/stages.py:655-668`).
   URL 판정은 러너의 것과 같은 식을 쓴다
   (`todo_system/orchestrator/runner.py:700-704`). URL `repo`에서는 조건이 참이 될 수 없어
   본부의 스테이지 인자는 한 글자도 달라지지 않는다.
2. **`quick` 런의 끝점 보장과 PR 주소.**
   - 5단계 파이프라인은 implement가 끝나는 즉시 러너가 브랜치를 origin으로 밀어 둔다
     (`todo_system/orchestrator/runner.py:1774-1826`). `quick`에는 그 단계가 없어 push가
     모델의 몫이다. 성공한 런의 워크트리는 지워지지만 (`:1451-1456`) 브랜치는 공유 클론에
     남는다. `macp up`은 검토 대기로 끝난 **경로 원격** 태스크에 한해, 사용자 리포에
     브랜치가 없으면 공유 클론에서 `stages.checkpoint_push`를 한 번 부른다
     (`todo_system/orchestrator/stages.py:2091`; 빨리감기만 하고 예외를 내지 않는다). URL
     원격에는 쓰지 않는다 — 머지된 브랜치를 다시 밀어 올리는 사고가 기록돼 있다
     (`todo_system/orchestrator/runner.py:1635-1640`).
   - `quick` 런의 응답에는 PR 주소가 실리지 않는다. 러너는 `report.md`에서만 주소를 읽고
     (`todo_system/orchestrator/runner.py:1981-1997`), `quick`은 `quick.md`에 적는다
     (`todo_system/prompts/worker-quick.md:82`). `macp status`는 검토 대기 행의 런
     디렉터리에서 그 줄을 읽어 보여 준다. 태스크 하나의 현재 상태를 찍는 기존 명령 `trace`
     (`todo_system/orchestrator/orchestrator.py:3630-3634`, `:3749-3753`)가 이것을 이미
     보여 주는지는 읽지 못했다 — **미확인**.

실패했을 때: 검증이 실패하면 오케스트레이터가 한도 안에서 다시 보낸다
(`todo_system/orchestrator/orchestrator.py:1385-1387`). `--once`에서는 다음 `macp up` 때다.
모델이 사람의 결정을 요청하면 사람 대기가 된다 (`:1332-1333`).

### D11 — 첫 태스크의 파이프라인과 5분

레인은 둘이고 둘 다 기존 것이다 (`todo_system/orchestrator/runner.py:292-309`).

| | `quick` | `auto` |
|---|---|---|
| `claude -p` 호출 수 | 1 (`todo_system/orchestrator/stages.py:165`) | 5 (`todo_system/orchestrator/stages.py:163`) |
| 모델 단계 | light (`todo_system/orchestrator/model_select.py:394-396`) | 태그·추정 시간으로 정해지고 기본은 standard (`:399-406`) |
| 턴 한도 | 120 | 스테이지마다 40~200 (`todo_system/orchestrator/stages.py:405-409`) |
| 스테이지 제한 시간 | 60분 | 단계에 따라 60~180분 (`todo_system/orchestrator/stages.py:126`) |
| 문서가 말하는 소요 | — | "몇 분에서 한 시간 이상" (`docs/getting-started.md:183-185`) |

`macp add`의 프롬프트는 작고 목적이 하나인 변경은 `quick`, 설계가 필요하거나 여러 파일에
걸친 변경은 `auto`로 고르게 한다. `quick`으로 받은 일이 작지 않으면 그 스테이지가 실패로
멈추고 5단계로 보내라고 적는다 (`todo_system/prompts/worker-quick.md:52-54`).

`quick`의 도구 목록에는 `gh`와 몇 가지 빌드 실행기만 있다
(`todo_system/orchestrator/stages.py:276-279`, `:390-392`). `cargo`·`go`·`make`는 없다.
README 초안의 첫 태스크를 문서 한 줄 고치기로 잡은 이유다.

5분은 **목표**다. 이 문서를 쓰며 잰 것은 엔진이 쓰는 시간뿐이다.

| 구간 | 목표 | 잰 것 |
|---|---|---|
| `macp init` | 10초 이내 | 볼트 만들기 0.04초 + 엔진 import 약 2초. 로그인 확인의 실제 호출 1번은 재지 않았다 |
| `macp add` | 60초 이내 | 검증·DB 반영 0.12초. 모델 호출 1번은 재지 않았다 |
| `macp up --once` | 4분 이내 (`quick`, 문서 수정 크기) | 엔진 호출 4번 4.2초(그 가운데 약 4.1초는 위에서 없애기로 한 `_pull_main` 대기). 스테이지의 모델 시간은 재지 않았다 |

실제 값을 재는 방법은 ⑩에 있다.

## ⑥ --service

### D12 — 유닛 하나, 기존 렌더러

`macp up --service`는 `systemd --user` 유닛 `macp-up.service` 하나를 쓰고 켠다.

1. D4대로 볼트를 환경에 넣은 뒤 `scripts/systemd/gen_units.py`를 경로로 import한다. 이
   파일은 패키지 모듈이 아니다. 엔진 루트 아래 `scripts/systemd/`에 있고
   (`todo_system/paths.py:191`), 볼트·로그·유닛 디렉터리를 import 시점에 환경에서 읽는다
   (`scripts/systemd/gen_units.py:51-52`, `:59-63`).
2. `write_env()`를 부른다 (`scripts/systemd/gen_units.py:262-276`). `service()`가 그리는
   유닛은 `EnvironmentFile=~/.config/macp/env`를 항상 갖고 (`:129`), 그 파일이 없으면
   유닛이 뜨지 않는다. `write_env()`는 없을 때만 만든다.
3. `service("macp-up", …, kind="simple", restart="always")`로 유닛 본문을 그린다
   (`scripts/systemd/gen_units.py:119-147`).
   - `ExecStart`: 지금 도는 인터프리터의 절대 경로로 `-P -m todo_system._entry up`. 설치
     모드 유닛과 같은 꼴이다 (`scripts/systemd/gen_units.py:179`, `:189`).
   - `Environment=MACP_VAULT_ROOT=<볼트>` 한 줄을 붙인다. 설치 모드 유닛이 그렇게 한다
     (`:178`, `:188`).
   - 작업 디렉터리는 볼트, 로그는 `<볼트>/logs/macp-up.out.log`와 `.err.log`다
     (`:129`, `:133-134`).
4. 유닛 파일을 쓰고 `systemctl --user daemon-reload`, `enable --now macp-up.service`를
   부른다 (같은 호출 `scripts/systemd/gen_units.py:314-325`).
5. 안내 두 줄을 찍는다. 로그아웃 뒤에도 돌리려면 `loginctl enable-linger`가 필요하다
   (`docs/running.md:42`) — 실행하지는 않는다. 끄는 법은 `systemctl --user disable --now
   macp-up.service`다.

`build()`에는 넣지 않는다 (`scripts/systemd/gen_units.py:163-239`). 본부용 유닛 집합은
지금 그대로 13개 파일이고 출력도 그대로다.

주의할 점 둘:

- `write_env()`가 만드는 환경 파일의 `PATH`는 `~/.local/bin:/usr/local/bin:/usr/bin:/bin`
  이다 (`scripts/systemd/gen_units.py:102`). `claude`·`gh`가 다른 곳에 설치돼 있으면
  서비스는 찾지 못한다. `--service`는 `claude`의 위치가 그 `PATH`에 없으면 환경 파일을
  고치라고 알려 준다(고쳐 주지는 않는다 — 그 파일에는 사람이 넣은 값이 있을 수 있어
  `write_env()`도 덮어쓰지 않는다 `:245-246`).
- 서비스는 루프이므로 사용량 가드가 켜져 있다(D9).

macOS: launchd 설치 스크립트가 있지만 프로세스마다 하나씩이다
(`scripts/install_runner_launchagent.sh` 등, `docs/running.md:53-59`). 단일 루프용으로 다시
쓸 수 있는지는 본문을 읽지 못했다 — **미확인**, 후속 과제다. 그동안 macOS에서는
포그라운드 `macp up`을 쓴다.

## ⑦ Claude Code 플러그인

### 로컬에서 확인한 형식

Claude Code가 설치해 둔 공식 마켓플레이스 체크아웃
(`~/.claude/plugins/marketplaces/claude-plugins-official/`)에서 읽은 것만 적는다. 이
런에서는 `claude` 실행도 웹 접근도 할 수 없었다.

| 무엇 | 확인한 내용 | 읽은 곳 (그 체크아웃 기준) |
|---|---|---|
| 플러그인의 구조 | `.claude-plugin/plugin.json`(필수), `skills/`·`commands/`·`agents/`·`.mcp.json`(선택) | `README.md:30-43` |
| `plugin.json`의 필드 | `name`, `description`, `author{name,…}` | `plugins/example-plugin/.claude-plugin/plugin.json` |
| 사용자가 부르는 스킬 | `skills/<이름>/SKILL.md`, frontmatter `name`·`description`·`argument-hint`·`allowed-tools`. `/<이름>`으로 부른다 | `plugins/example-plugin/README.md:23-46` |
| `commands/*.md` | 옛 형식. 스킬과 똑같이 읽히고 파일 배치만 다르다. 본문에서 `$ARGUMENTS`를 쓴다 | `plugins/example-plugin/README.md:48-50`, `plugins/example-plugin/commands/example-command.md:1-15` |
| 마켓플레이스 | 리포 루트의 `.claude-plugin/marketplace.json`. 머리에 `$schema`·`name`·`description`·`owner`, 그리고 `plugins[]` | `.claude-plugin/marketplace.json:1-21` |
| `plugins[]` 항목 | `name`·`description`·`author`·`category`·`source`·`homepage`. 같은 리포 안의 플러그인은 `"source": "./plugins/<이름>"` | `.claude-plugin/marketplace.json:54-64` |
| 설치 | `/plugin install <플러그인>@<마켓플레이스>` | `README.md:16` |
| 이름 | 플러그인 `name`은 바꿀 수 없는 식별자다 | `README.md:45-58` |
| 스킬의 등록 이름 | `<플러그인>:<스킬>` | `README.md:87` |

공식 문서 주소는 그 README가 가리키는 둘이다: `https://code.claude.com/docs/en/plugins`,
`https://code.claude.com/docs/en/plugin-marketplaces`.

### D13 — 배치와 스킬 하나

```
.claude-plugin/marketplace.json              마켓플레이스 "macp". plugins[0] = { "name": "macp", "source": "./plugins/macp", … }
plugins/macp/.claude-plugin/plugin.json      name · description · author
plugins/macp/skills/macp/SKILL.md            사용자가 부르는 스킬 하나
plugins/macp/README.md
```

- 로컬에서 본 "같은 리포의 상대 경로" 꼴을 그대로 쓴다. 리포 루트 자체를 플러그인으로
  쓰는 꼴은 보지 못해 고르지 않는다.
- 스킬은 하나(`macp`)다. 본문은 "`macp $ARGUMENTS`를 실행하고 출력을 그대로 전한다"뿐이다.
  여섯 동사를 그대로 넘기므로 플러그인에는 로직이 없다(3단계 완료 조건). `macp`가 PATH에
  없으면 ②의 설치 줄을 안내한다. 플러그인은 Python을 싣지 않는다.
- `up`은 오래 걸린다. 스킬은 그것을 백그라운드로 실행하고 `macp status`로 확인하라고
  안내한다. 세션에서 긴 명령을 다루는 방법이지 CLI 로직의 복제가 아니다. 세션의 명령 제한
  시간이 실제로 어떻게 걸리는지는 **미확인**이라 3단계가 정한다.
- 이 리포에는 이미 `/macp` 프로젝트 명령이 있다 (`.claude/commands/macp.md`: 프로젝트
  등록과 진행 기록). 그대로 둔다. 플러그인 쪽은 `/macp:macp …`로 항상 닿는다. 다른
  프로젝트에서 맨이름 `/macp add …`가 플러그인 스킬로 풀리는지, 둘 다 있을 때 무엇이
  이기는지는 **미확인**.
- 3단계가 건드릴 곳: `.claude-plugin/**`, `plugins/macp/**`, `README.md`·`README.ko.md`,
  스킬 본문에 CLI 호출만 있는지 보는 테스트 하나. 3단계 태스크의 `touches`는 지금
  `.claude-plugin/`뿐이라 넓혀야 한다. `plugins/`는 격리 게이트의 아카이브 목록
  (`scripts/check_engine_isolation.sh:81-83`) 밖이다.

**미확인 — 3단계가 CLI와 공식 문서로 확정한다.**

- `/plugin marketplace add <소유자>/<리포>` 문법. 3단계 태스크의 완료 조건이 이 꼴을
  전제한다.
- `claude plugin validate`가 있는지와 그 출력. 없으면 위 표의 필드로 매니페스트를 검사하는
  테스트가 대신한다.
- 스킬 frontmatter의 `allowed-tools`에 `Bash(macp:*)` 같은 패턴을 쓸 수 있는지. 예제는
  도구 이름만 나열한다.

## ⑧ 본부 모드와의 경계

### 기본값만 다른 것

| 항목 | 본부 모드 | 1인 모드 | 같은 코드 |
|---|---|---|---|
| 볼트 위치 | `MACP_VAULT_ROOT`. 없으면 오류 | 같은 변수. 없으면 진입점이 `~/.macp/vault`로 채운다 | `paths.vault_root` (`todo_system/paths.py:140`) |
| 볼트를 만드는 주체 | 사람이 `cp -r` (`docs/getting-started.md:29-32`) | `macp init` | 같은 `vault-template/` |
| 워커 루트 | `~/Worker` | 볼트 설정의 기존 키로 `~/.macp/work` | `paths.worker_root` (`todo_system/paths.py:380`) |
| 프로세스 | 5개 이상, 유닛 파일 13개 | 1개, 유닛 파일 1개 | 같은 `_main`들 |
| 메일박스 주소 | `file://Worker/agents` | 같음 | `open_mailbox` (`todo_system/orchestrator/mailbox.py:397`) |
| 태스크 작성 | 손으로, 또는 `macp new` | `macp add` | 같은 스키마와 `validate_file` |
| `auto_merge` | 생략하면 참 | 거짓을 명시 | 같은 판정 (`todo_system/orchestrator/runner.py:489-502`) |
| 사용량 가드 | 켬 | 루프·서비스는 켬, `--once`만 끔 | `usage_guard.check` (`todo_system/usage_guard.py:1101`) |
| 볼트 원격 | 있음. maintenance가 push | 없음. push하지 않는다 | — |
| planner·maintenance·brief·health | 뜬다 | 뜨지 않는다 | — |
| 계정·프로필 | 설정한다 | 만들지 않는다. 프로세스의 로그인을 쓴다 | `_account_env` (`todo_system/orchestrator/runner.py:323`) |

### 그대로인 것

태스크 스키마와 검증기 · `todo.db` 캐시 · 틱 전체(라우팅, 원장, 재시도, 오래된 행 정리,
머지된 PR 닫기) · 러너와 `StagedExecutor` · 스테이지 프롬프트, 도구 허용 목록, 턴 한도,
제한 시간 · 런별 워크트리 격리와 push 가드 · 모델 단계 선택과 확인 호출 · 사용자 권한으로
도는 `claude -p`(`SECURITY.md:5-9`) · 기존 CLI 동사와 모듈 진입점 전부 · 모든 `MACP_*`
변수의 뜻과 우선순위(밖에서 준 값이 이긴다).

### D14 — 엔진에서 바뀌는 네 군데, 본부에서는 닿지 않는다

2단계가 기존 엔진 코드를 고치는 곳은 넷이다. 나머지는 새 모듈과 데이터 파일이다.

| 바뀌는 곳 | 바뀌는 내용 | 본부 경로가 닿지 않는 이유 |
|---|---|---|
| `todo_system/_entry.py` | 여섯 동사의 선행 분기, `VaultRootError` 때 기본 볼트로 다시 시도 | 여섯 이름은 지금 argparse가 거부한다. 다시 시도는 지금 exit 2로 끝나는 경우에만 일어난다 |
| `todo_system/macp/config.py` | `ProjectConfig.vault_root`(맨 끝 필드). 값이 있을 때만 읽고 쓴다 | 그 키가 없는 프로젝트 파일은 한 바이트도 달라지지 않는다 |
| `todo_system/orchestrator/stages.py` | 외부 리포 분기에 "origin이 로컬 경로" 메타데이터 한 줄 | `repo`가 URL이면 붙지 않는다 |
| `todo_system/orchestrator/orchestrator.py` | `_pull_main`: 첫 fetch 실패 + `origin` 원격 없음 → 조용히 끝냄 | `origin`이 있는 볼트에서는 git 호출 순서가 그대로다 |

- 새 환경변수 0개, 새 설정 키 0개. `vault_root`와 `worker_root`는 해석기가 이미 읽는
  키다 (`todo_system/paths.py:155`, `:411`).
- `gen_units.build()`는 건드리지 않는다. 13개 유닛의 출력이 그대로다.
- 데이터 파일: `vault-template/CLAUDE.md`, `todo_system/prompts/`의 프롬프트·템플릿,
  `pyproject.toml`의 wheel 포함 목록. 이미 만들어진 볼트는 템플릿을 다시 복사하지 않는다.
  `.gitignore`는 템플릿이 아니라 `macp init`이 새 볼트에 쓴다(②의 D5).
- 주의: 테스트에는 경로를 `repo`로 쓰는 것이 있다
  (`todo_system/orchestrator/runner.py:691-692`가 그렇게 적는다). 셋째 줄의 변경이 그런
  테스트의 기대 인자를 바꾸는지 2단계가 확인한다.

## ⑨ 지울 것

1인 모드에서 필요 없어지는 것의 목록이다. "지금"의 근거는 모두 이 리포의 문서와 코드다.

### 필수 단계

| # | 지금 해야 하는 일 | 근거 | 1인 모드 |
|---|---|---|---|
| 1 | 엔진을 클론하고 `uv sync` | `README.md:83-84` | 설치 한 줄(선행 조건) |
| 2 | `vault-template`을 손으로 복사 | `README.md:86` | `macp init`이 만든다 |
| 3 | 볼트를 따로 git 리포로 만들기 | `docs/getting-started.md:34-36` | `init`이 `git init`. 원격은 필요 없다 |
| 4 | `cli new`로 틀을 만들고 frontmatter를 손으로 채우기 | `README.md:90-92`, `docs/getting-started.md:62-76` | `macp add "<문장>"` |
| 5 | `worker_mode: blocked`를 `auto`로 손으로 바꾸기 | `README.md:92`, `docs/getting-started.md:78-80` | 파일을 한 번에 써서 필요 없다 |
| 6 | `cli validate` | `README.md:93` | `add` 안에서 |
| 7 | `cli import-md` | `README.md:94` | `add`와 `up` 안에서 |
| 8 | `orchestrator auto` → `runner … --once` → `orchestrator auto` → `orchestrator status`, 매번 `uv run --project ~/macp …` | `README.md:96-102` | `macp up --once` |
| 9 | 실제 실행용 태스크에 `repo:` 주소와 `auto_merge: false`를 손으로 적기 | `docs/getting-started.md:161-162` | `add`가 채운다 |
| 10 | 실제 실행 전에 `gh`를 설치하고 인증하기 | `docs/getting-started.md:142-143` | PR이 끝점일 때만. 없으면 브랜치가 결과물이다 |
| 11 | 실제 실행 전에 `unset USAGE_GUARD_DISABLE` | `docs/getting-started.md:145` | 필요 없다 |

### 환경변수

| 지금 필수 | 근거 | 1인 모드 |
|---|---|---|
| `MACP_VAULT_ROOT` | `README.md:87`, `docs/configuration.md:3-4` | 진입점이 프로세스 안에서 채운다 |
| `USAGE_GUARD_DISABLE=1` | `README.md:88`, `docs/getting-started.md:54-58` | `--once`가 프로세스 안에서 채운다 |
| 셸 프로필에 `export MACP_VAULT_ROOT=…` 넣기 | `docs/getting-started.md:34` | 필요 없다 |

필수 환경변수는 2개에서 0개가 된다. 선택 변수(`MACP_WORKER_ROOT`, `MACP_LANGUAGE` …)는
지금처럼 동작한다.

### 유닛과 프로세스

| 지금 | 근거 | 1인 모드 |
|---|---|---|
| 계속 살려 둘 프로세스 5개 | `docs/running.md:3-12` | 1개 (`macp up`) |
| 터미널 네 개에 한 줄씩 | `docs/running.md:17-27` | 터미널 하나 |
| 유닛 파일 13개 | `scripts/systemd/gen_units.py:196-238` | 1개 (`macp-up.service`) |
| — runner 서비스 | `:196-204` | 루프 안의 러너 호출 |
| — orchestrator 서비스 + 타이머 | `:205-208` | 루프 안의 틱 |
| — todo-sync 서비스 | `:219-222` | 루프 안의 DB 반영 |
| — dashboard 서비스 | `:227-231` | 볼 때만 `macp dashboard` |
| — planner·maintenance·brief·health 서비스 4 + 타이머 4 | `:209-216`, `:232-238` | 띄우지 않는다 |
| 유닛을 깔기 전에 `export MACP_VAULT_ROOT` | `docs/running.md:38-40` | `--service`가 처리한다 |

### 지우지 않는 것

- 본부의 유닛, `gen_units.build()`, `docs/running.md`. 여러 리포를 24시간 돌리는 사람은
  지금 방식을 그대로 쓴다.
- 수동 루프. `docs/getting-started.md`에 남긴다. 각 조각이 무엇을 하는지 설명하는 자리다.
- 기존 CLI 동사와 그림자 실행기(`--mode shadow`).
- README에서 빠지는 문장 하나: "No agent is called and nothing leaves your machine."
  (`README.md:79`). 새 5분 체험에서는 사실이 아니다.

## ⑩ 측정

### 지표

주간 온보딩 점검이 쓰는 다섯 가지를 그대로 쓴다.

| 지표 | 기준 (2026-11-21 원장) | 이 문서가 센 지금 | 목표 |
|---|---|---|---|
| 첫 결과까지 친 명령 수 | 약 11 | 12줄 | 3 이하 |
| 손으로 고친 파일 수 | 1 | 1 | 0 |
| 필수 환경변수 수 | 2 | 2 | 0 |
| 걸린 분 | 기록 없음 | 재지 않음 | 5 이하 |
| 결과의 종류 | 그림자 실행 | 그림자 실행 | 워커가 만든 커밋이 있는 브랜치(또는 PR) |

### 재는 방법

1. **게이트가 매번 잰다.** 2단계의 경로 테스트와 `scripts/check_engine_isolation.sh`의
   README 구간이 깨끗한 `HOME`에서 세 명령을 그대로 친다. 가짜 `claude`를 쓰므로 여기서
   나오는 것은 명령 수·손수정·환경변수·결과의 종류, 그리고 **엔진이 쓰는 시간**이다. 지금
   그 구간은 8단계에 기대 출력 5개다 (`scripts/check_engine_isolation.sh:214-215`,
   `:277-292`). 명령이 하나라도 늘면 이 구간이 실패하게 한다.
2. **주간 점검이 원장에 한 줄을 남긴다.** 깨끗한 `HOME`에서 README 첫 절을 글자 그대로
   따라 하고 다섯 값을 적는다. 직전 회차보다 나빠지면 그 원인을 먼저 고친다.
3. **모델이 쓰는 시간은 실제 실행에서만 나온다.** 스테이지마다 러너가
   `Worker/runs/<run-id>/stages.json`의 `usage.<stage>`에 `duration_ms`를 남긴다
   (`todo_system/orchestrator/stages.py:527-546`). `macp add`의 호출 시간은 `add`가 끝날 때
   한 줄로 찍는다.

"분"은 둘로 나눠 적는다: 엔진 시간과 모델 시간. 앞의 것은 게이트가 지키고, 뒤의 것은
모델·계정·태스크 크기에 달려 있어 목표치로만 둔다(⑤의 D11 표).

## README '5분 체험' 초안

`README.md:77-112`를 대신할 초안이다. README 자체는 2단계가 바꾼다. 펜스 안의 명령은
3개이고, 설치 줄은 펜스 밖의 선행 조건이다.

````markdown
## Try it in five minutes

A real Claude Code worker finishes one small task in a repository of yours and leaves
you a pull request — or a local branch, if the repository has no GitHub remote.
Nothing is merged for you.

You need Linux or macOS, Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/), `git`, and
the [Claude Code](https://code.claude.com) CLI, logged in. Install macp once:

    uv tool install git+https://github.com/jjkim0807/macp

Then, inside a git repository you own:

```bash
macp init                                   # makes ~/.macp/vault, registers this repository
macp add "Add an install section to the README"
macp up --once                              # dispatch → worker → result, then exit
```

- `macp init` creates your vault (tasks, run records, memory) under `~/.macp/` and
  marks this repository as managed. There are no environment variables to set and no
  vault repository to create.
- `macp add` turns the sentence into a validated task file. You never edit frontmatter.
- `macp up --once` runs one pass of the same orchestrator and runner the 24/7 setup
  uses. The worker works in its own clone under `~/.macp/work`, on the branch
  `worker/<slug>`. Your checkout is not touched.

When it finishes, `macp status` shows the task waiting for your review, with the pull
request link. Without a GitHub remote the result is the branch `worker/<slug>` in your
repository: look at it with `git diff main...worker/<slug>`, merge it, then run
`macp complete <slug>`.

`macp up` keeps the loop running in the foreground and paces itself against your
Claude subscription usage; `macp up --once` runs what you queued right away.
`macp up --service` installs the loop as one systemd user unit.

The worker is a headless agent with your user account's permissions — read
[SECURITY.md](SECURITY.md) first. [Getting started](docs/getting-started.md) explains
each step and still documents the manual loop.
````

초안과 함께 넘기는 것:

- 설치 줄의 문법은 확인하지 않았다(②). 각 명령의 출력 문구는 2단계가 실제로 돌려 정한다.
  엔진에 이미 있는 문자열만 그대로 인용한다.
- 지금 절의 마지막 문장은 격리 게이트가 이 루프를 돌린다고 말한다 (`README.md:111-112`).
  게이트의 README 구간이 새 세 명령으로 바뀐 뒤에 2단계가 그 문장을 다시 넣는다.
- 한국어판 `README.ko.md`는 2단계가 같은 내용으로 맞춘다. 이 런에서는 그 파일을 읽지
  않았다.
- 워커가 사용자 권한으로 돈다는 안내와 `SECURITY.md` 링크는 지금 README의 Status 절에
  있다 (`README.md:129-130`). 새 5분 체험은 실제 에이전트를 띄우므로 그 안내를 이 절
  안으로 올렸다.

## 후속 태스크 지도

### 2단계 — 콘솔 명령 (`macp-lite-2-cli`)

**새로 생기는 것.** `todo_system/lite.py`(얇은 글루) · `todo_system/prompts/`의 `add`
프롬프트와 태스크 템플릿 · `vault-template/CLAUDE.md` · 테스트.

**바뀌는 것.** ⑧의 네 군데 · `todo_system/i18n.py`(문구) · `pyproject.toml`(wheel과
sdist에 `vault-template` 넣기) · `README.md` · `README.ko.md` · `docs/getting-started.md` ·
`scripts/check_engine_isolation.sh`의 README 구간.

2단계 태스크의 `touches`는 지금 `todo_system/`뿐이다. 위 목록은 그보다 넓으므로 2단계의
계획 스테이지가 넓힌다.

**한 스테이지에 들어가지 않으면 이렇게 나눈다.** 각 조각은 그 자체로 게이트가 green이다.

1. 볼트 해석(D4) · `macp init` · `ProjectConfig.vault_root` · `_entry` 분기 · wheel에
   `vault-template` 넣기.
2. `macp add`(프롬프트, 템플릿, 검사, 문구).
3. `macp up`·`status`·`dashboard`·`memory`·`--service` · 스테이지 메타데이터 한 줄 ·
   `_pull_main` · 경로 테스트 · README · 격리 게이트의 README 구간.

**경로 테스트의 계약** (2단계 완료 조건 1번).

- 환경: 임시 `HOME`. `MACP_*`·`USAGE_GUARD_*`·`CLAUDE_*` 변수를 모두 지운다. `PATH` 앞에
  가짜 `claude`를 둔다. 깨끗한 `HOME`에는 git 사용자 정보가 없으므로 가짜가 만드는 커밋은
  이름·메일을 직접 준다.
- 준비: 커밋 하나가 있고 원격이 없는 임시 git 리포. 그 안에서 `macp init` →
  `macp add "README에 설치 한 줄 추가"` → `macp up --once`.
- 단언:
  - 세 명령 모두 rc 0. 테스트가 설정한 엔진 환경변수는 0개.
  - 리포의 `.macp/config.toml`에 `vault_root` 키가 있다.
  - `~/.macp/vault/Todo/items/<slug>.md`가 있고 `macp validate <slug>`가 통과한다.
  - **사용자의 리포에** `worker/<slug>` 브랜치가 있고 가짜가 만든 커밋이 들어 있다.
  - 사용자의 체크아웃 브랜치와 작업 트리는 그대로다.
  - 원장의 그 행은 검토 대기다.
  - 러너 뒤에 `collect`가 `auto`보다 먼저 불린다(⑤).
- 가짜 `claude`가 답해야 하는 호출은 세 가지다. 프롬프트 내용으로 구분한다.

  | 호출 | 모양 | 가짜가 할 일 |
  |---|---|---|
  | 모델 확인 | `claude -p "Reply with just: ok" --model … --effort … --max-turns 1` (`todo_system/orchestrator/model_select.py:161-162`) | 아무 글자나 찍고 rc 0 |
  | `add` | `claude -p <add 프롬프트> …` | JSON 객체를 찍고 rc 0 |
  | 스테이지 | 프롬프트 끝에 "Run metadata" 블록 (`todo_system/orchestrator/stages.py:737-752`, `:825-831`) | `run_dir`·`working_dir`·`branch`를 읽어 워크트리에 커밋하고 `git push origin <branch>`, `<run_dir>/quick.md`에 `overall_status: pass`를 쓴다 (`todo_system/orchestrator/stages.py:421-430`, 판정 `todo_system/orchestrator/runner.py:1890-1896`) |

  가짜의 출력에는 사용량 한도를 뜻하는 문구가 없어야 한다. 러너가 그런 문구를 보면 런을
  보류로 돌린다 (`todo_system/orchestrator/runner.py:1720-1721`).

**격리 게이트의 README 구간.** 같은 세 명령으로 바꾼다. 지금은 `MACP_VAULT_ROOT`와
`USAGE_GUARD_DISABLE=1`을 넣고 돈다 (`scripts/check_engine_isolation.sh:195-196`). 새
구간은 둘 다 넣지 않는다. 기대 출력은 엔진에 이미 있는 문자열을 그대로 쓴다 — 예:
`dispatched <slug> -> worker-default` (`todo_system/orchestrator/orchestrator.py:3804`).
이 게이트는 `git archive`로 뽑은 트리에서 돈다 (`scripts/check_engine_isolation.sh:81-83`).
가짜 `claude`는 그 목록에 드는 경로(`todo_system/`나 `scripts/` 아래)에 둬야 한다.

**본부가 그대로라는 증명.** 기존 골든과 스모크를 그대로 통과한다: 유닛 생성기의 골든
(`scripts/systemd/testdata/default_units.golden`), 스테이지 인자와 오케스트레이터 CLI
출력을 고정한 기존 테스트. 여기에 하나를 더한다 — 여섯 동사가 아닌 인자로 들어오면
`todo_system.lite`가 import되지 않는다.

**가짜로는 잡을 수 없는 것.** 아래는 실제 `claude`로만 확인된다. 사람의 승인을 기다릴
일은 아니다. 2단계는 가짜로 통과하면 머지하고, 아래는 '확인 못 함'으로 기록한다. 첫 실제
사용이 채운다.

1. 한 번도 열어 본 적 없는 볼트 디렉터리에서 `claude -p`가 도구 허용 목록을 지키며 묻지
   않고 도는지. 러너는 볼트를 "신뢰된 작업 공간"으로 전제한다
   (`todo_system/orchestrator/runner.py:1103-1109`).
2. `quick` 스테이지가 태스크 파일을 찾아 읽는지(③).
3. 태스크 본문의 사유 한 줄이 `quick`의 안전 규칙 오적용을 막는지(④).
4. `add`의 응답이 스키마를 지키는 비율.
5. 실제로 걸리는 분(⑩).

### 3단계 — Claude Code 플러그인 (`macp-lite-3-claude-code-plugin`)

⑦의 배치대로 매니페스트 둘과 스킬 하나를 만든다. 스킬 본문에는 CLI 호출만 둔다. 먼저
⑦의 미확인 세 가지를 `claude` CLI와 공식 문서로 확정한다. README에는 "이미 Claude Code를
쓰고 있다면" 두 줄을 더한다: 마켓플레이스 추가와 플러그인 설치. CLI 설치 줄은 선행
조건으로 그 위에 둔다.

## 부록

### 미확인 목록

이 런에서 확인하지 못했거나 코드를 읽기만 한 것이다. 앞의 숫자는 다루는 절이다.

| 절 | 확인하지 못한 것 | 누가 확정하나 |
|---|---|---|
| ② | `uv tool install git+…` 문법, 설치 뒤 `macp`가 PATH에 놓이는지 | 2단계 (깨끗한 `HOME`) |
| ② | wheel 설치본에서의 전체 흐름. 확인 실행은 체크아웃에서만 했다 | 2단계 |
| ② | 볼트 환경을 넣기 전에 `todo_system.macp` 패키지를 import해도 되는지. 그래서 `lite`는 `paths`만 먼저 import한다 | 2단계 |
| ② | 경로 원격에서 사용자가 브랜치를 옮긴 뒤의 기준 브랜치 | 2단계 |
| ③ | 리포의 `CLAUDE.md`가 `--add-dir` 경로에서 자동으로 읽히는지 | 첫 실제 사용 |
| ③ | `quick` 스테이지가 안내 없이 태스크 파일을 찾는지 | 첫 실제 사용 |
| ④ | 실제 `claude -p` 호출 전부: `add`의 JSON, 사유 줄의 효과 | 첫 실제 사용 |
| ⑤ | 수거를 먼저 부르는 순서. 코드로만 확인했다 | 2단계 (테스트) |
| ⑤ | 로그인된 계정에서의 사용량 가드(조회가 되는 경우) | 첫 실제 사용 |
| ⑤ | 경로 원격에서 `gh`가 실제로 보이는 반응, `gh_pr_merged`가 경로 `repo`에 돌려주는 값 (`todo_system/orchestrator/stages.py:1024`) | 2단계 |
| ⑤ | `trace`가 `quick` 런의 PR 주소를 보여 주는지 | 2단계 |
| ⑤ | 새 볼트 디렉터리에서 `claude -p`의 도구 허용과 신뢰 처리 | 첫 실제 사용 |
| ⑤ | 실제로 걸리는 시간 | 주간 점검, 실제 사용 |
| ⑥ | 유닛의 실제 기동, `uv tool install`로 깔린 인터프리터 경로가 `ExecStart`에 맞는지 | 2단계 |
| ⑥ | macOS launchd | 후속 과제 |
| ⑦ | 마켓플레이스 추가 문법, `claude plugin validate`, 스킬의 `allowed-tools` 패턴, 맨이름 `/macp`의 해석, 세션의 명령 제한 시간 | 3단계 |
| README | `README.ko.md`·`docs/concepts.md` 본문. 이 런에서 읽지 않았다 | 2단계 |

### 열린 질문과 답한 곳

조사 단계가 남긴 여덟 가지다.

| 질문 | 답 |
|---|---|
| 겹치는 동사를 어디에 둘까 | ① D1 — 콘솔 스크립트의 선행 분기 |
| 기본 볼트 위치 | ② D3 — `~/.macp/vault` |
| 환경변수 없이 볼트 찾기, 설치 형태, "3개"를 세는 법 | ② D2·D4 |
| 로컬 리포의 `repo:` 값과 원격·`gh`가 없을 때의 끝점 | ② 끝 절, ⑤ D10 |
| 첫 태스크의 파이프라인 | ⑤ D11 — `quick` 먼저, 5분은 목표 |
| 기존 `/macp` 명령과 플러그인의 관계 | ⑦ — 기존 명령은 그대로, 플러그인은 `/macp:macp` |
| 세 태스크 공용 문구 "앞 단계 산출물을 읽고" | 이 노트가 그 산출물이다. 1단계에는 앞 산출물이 없었다 |
| README 초안의 언어 | 초안은 영어, 이 노트는 한국어. 한국어판 반영은 2단계 |
