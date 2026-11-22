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
| D8 | `macp up`은 단일 스레드 루프. 틱 인자는 틱 스크립트와 같고 러너는 `poll_once`다 | `scripts/orchestrator_tick.sh:54-63`, `todo_system/orchestrator/runner.py:3292` | ⑤ |
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
| 콘솔 스크립트 `macp` | todo CLI. `pyproject.toml:60` → `todo_system/_entry.py:23` → `cli.main` (`todo_system/cli.py:2170`) | wheel을 설치하면 PATH에 놓인다 | `init-db` `import-md` `validate` `new` `list` `today` `audit` `weekly-review` `queue` `complete` `reopen` `set` `daemon` `now` `focus` `calendar-sync` `calendar-fetch` `unified` `session-end` `resume` `tracker` (`todo_system/cli.py:1991-2164`) |
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
| `macp up --once` | 동기화 → 틱 → 러너 한 번 → 틱 → 상태 | `cli.snapshot_mtime` (`todo_system/cli.py:1122`) · `orchestrator._main`의 `auto` (`todo_system/orchestrator/orchestrator.py:3603`, `:3756-3805`) · `runner._main`의 `--once` (`todo_system/orchestrator/runner.py:3231`, `:3292`) | 순서, 볼트로 chdir, 요약 출력 |
| `macp up` | 위를 반복 | 위 + `Runner.poll_once` (`todo_system/orchestrator/runner.py:2705`) · `Runner._usage_blocked` (`:3033`) · `make_executor` (`:2165`) | 루프와 간격 |
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
     `Worker/queue.lock`이 추적되지 않은 채 남는다(확인 실행에서 본 목록). 2단계가 이
     목록과 `Worker/runs/`, `logs/`를 템플릿의 `.gitignore`에 넣는다. 본부 볼트는 템플릿을
     다시 복사하지 않으므로 영향이 없다.
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
