# macp — Maestro Agent Control Plane

**태스크만 쌓아 두면, macp가 코딩 에이전트를 24시간 일하게 합니다.**

[English README](README.md) · [시작하기](docs/getting-started.md) ·
[동작 원리](docs/concepts.md) · [태스크 파일](docs/tasks.md) ·
[상시 운영](docs/running.md) · [설정](docs/configuration.md) ·
[문제 해결](docs/troubleshooting.md) · [보안](SECURITY.md)

> 자세한 문서는 현재 영어로만 있습니다. 이 페이지는 개요와 빠른 시작입니다.

코딩 에이전트는 눈앞의 태스크 하나는 잘 풉니다. 하지만 여러 프로젝트를 계속 굴리려면
그 이상이 필요합니다. 다음에 무엇을 할지, 모델을 얼마나 쓸지, 어떤 도구를 허용할지,
정말 끝났는지, 언제 사람이 결정해야 하는지를 누군가 정해야 합니다. macp가 그 계층입니다.

태스크를 작은 마크다운 파일로 적어 두면 macp가 나머지를 합니다.

- **분석·분할** — 모든 태스크는 조사 → 계획 → 구현 → 검증 → 보고 단계를 거칩니다.
  구현 한 단계에 너무 큰 태스크는 의존 관계가 있는 하위 태스크 2~5개로 자동 분할됩니다.
- **자원 배분** — 난이도에 따라 모델 규모(heavy·standard·light), 허용 도구, 전용 git
  worktree, 저장소별 동시 실행 슬롯을 배정합니다. 사용량 페이싱으로 자동화가 구독 한도를
  넘지 않게 합니다.
- **24시간 운영** — 데몬이 사람 없이 디스패치·결과 수집·재시도·멈춘 실행 정리를 합니다.
  사람에게는 사람이 결정할 일만 묻습니다.
- **큐 보충** — 태스크 풀이 비면 플래너가 프로젝트를 직접 돌려 보고(테스트·빌드·문서)
  다시 읽어서 필요한 일을 찾아 새 태스크로 등록합니다.

macp는 **특정 코딩 에이전트에 묶이지 않는 운영 계층**을 지향합니다. 코드는 에이전트가
쓰고, 무엇을 어떤 자원으로 언제 할지는 macp가 정합니다. 에이전트는 `Executor`
인터페이스 뒤에 있고, 스테이지 실행은 특정 회사에 묶이지 않는 `AgentBackend` 뒤에 있습니다.
[Claude Code](https://code.claude.com)(`claude -p`)가 모든 스테이지를 돌립니다. 태스크의
`agent:` frontmatter나 계정으로 스테이지를 다른 에이전트에 보낼 수 있습니다. 현재는
`quick` 레인에서 Gemini CLI를 쓸 수 있습니다. 디스패치 때 능력 게이트가 스테이지에
필요한 것(예: 읽기 전용 스테이지가 기대는 허용 도구 목록)을 지킬 수 없는 백엔드를
거절합니다. 조용히 제약을 약하게 만들지 않습니다.

**실사용** — 2026-07-20부터 2026-10-20까지 저자의 인스턴스가 **22개 독립 저장소**에서
**태스크 1,080개**를 자동으로 완료했습니다. 2026년 10월 기준으로 하루 평균 약 25개입니다
(작업 원장 기준).

## 구조

- **control plane**(결정): 오케스트레이터, 플래너, 작업 원장, 사용량 가드
- **data plane**(실행): 코딩 에이전트를 구동하는 러너, 그리고 사람

둘은 파일 메일박스의 메시지와 원장으로만 대화합니다. 개인 데이터(태스크·메모리·실행
기록·비밀값)는 모두 사용자가 소유한 **볼트(vault)** 디렉터리에 있고, 이 저장소는 개인
데이터가 없는 엔진입니다. 그림과 상세는 [동작 원리](docs/concepts.md)에 있습니다.

## 5분 체험

에이전트를 호출하지 않고 외부로 아무것도 보내지 않습니다. Linux 또는 macOS,
Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/), `git`이 필요합니다.

```bash
git clone https://github.com/jjkim0807/macp ~/macp && cd ~/macp
uv sync

cp -r vault-template ~/my-vault            # 볼트: 태스크·실행 기록·메모리
export MACP_VAULT_ROOT=~/my-vault
export USAGE_GUARD_DISABLE=1               # 체험 중에는 사용량 페이싱 끔

uv run python -m todo_system.cli new hello-world --template auto-worker
#   ~/my-vault/Todo/items/hello-world.md 를 열어 outcome을 쓰고
#   `worker_mode: blocked` 를 `worker_mode: auto` 로 바꿉니다
uv run python -m todo_system.cli validate hello-world   # → worker contract OK (open tasks)
uv run python -m todo_system.cli import-md              # → imported tasks: 1

uv run --project ~/macp python -m todo_system.orchestrator.orchestrator auto
#   → dispatched hello-world -> worker-default
uv run --project ~/macp python -m todo_system.orchestrator.runner \
    --agent worker-default --mode shadow --once
#   → RESULT: shadow run for 'hello-world' (no agent spawned)
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator auto
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator status
#   → done           hello-world                              worker-default
```

태스크 파일이 가져오기 → 라우팅 → 디스패치 → 실행(shadow) → 수집까지 한 바퀴 돌았습니다.
실제 저장소에 대한 첫 실행은 [시작하기](docs/getting-started.md) 2부를 보세요. 실제
실행 전에는 반드시 [SECURITY.md](SECURITY.md)를 읽으세요. 워커는 사용자 권한으로 코드를
실행하고 GitHub에 push할 수 있습니다. `bash scripts/check_engine_isolation.sh`가 이 과정을
깨끗한 HOME에서 그대로 돌립니다. 위의 명령이나 기대 출력이 하나라도 바뀌면 실패합니다.

## 누구에게 맞는가

여러 저장소에서 코딩 에이전트를 상시로 돌리려는 개인이나 소규모 운영자에게 맞습니다.
에이전트가 사용자 계정 권한으로 일해도 되는 자기 머신이나 VM이 있어야 합니다.

팀용 호스팅 SaaS를 원하면 맞지 않습니다. 사람 없이 실계좌로 거래하거나 프로덕션에
배포하기를 기대해도 맞지 않습니다.

## 상태

macp는 저자의 프로젝트들을 매일 운영하고 있습니다. 핵심(태스크 CLI·오케스트레이터·단계형
러너·사용량 가드·대시보드)은 테스트되어 있고 단독으로 동작합니다. 다만 개인 규모의
시스템을 그대로 공개한 것이라 남은 한계가 있습니다.

- 일부 연동(카카오톡/Slack 알림, 앱 베타 배포, `advote` 브리지)은 저자 환경에 맞춰져
  있습니다. 설정 파일이 있을 때만 켜집니다.
- 워커 프롬프트(`.claude/commands/`), 코드 주석, 로그 메시지 일부가 한국어입니다.

## 기여하기

이슈를 먼저 열어도 되고, 바로 PR을 보내도 됩니다. PR을 열기 전에 [개발](#개발) 절의
세 명령을 로컬에서 돌리세요. 셋 다 0으로 끝나야 합니다. 이 저장소는 공개 저장소입니다.
이메일·호스트명·개인 경로 같은 개인 식별자를 커밋하지 마세요.

## 개발

```bash
MACP_VAULT_ROOT=vault-template uv run --with pytest python -m pytest -q todo_system scripts/systemd
uv run --with ruff ruff check .
bash scripts/check_engine_isolation.sh
```

GitHub Actions는 쓰지 않습니다. 검사는 로컬에서 돌립니다. 전체 테스트는 몇 분 걸립니다.
저장소 지도는 [docs/concepts.md](docs/concepts.md#repository-map)에 있습니다.

## 라이선스

MIT — [`LICENSE`](LICENSE)를 보세요.
