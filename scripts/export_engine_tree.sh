#!/usr/bin/env bash
#
# export_engine_tree.sh — public split 10: reproduce running the engine tree on its own
#
# Restores **the same set** as `check_public_identifiers.sh --export` into <dest>, to
# reproduce locally the future state of the public engine export repo (only the engine,
# split off from the vault).
# set = the 3 engine-tree paths (todo_system · scripts · .claude/commands)
#        − the gate's 3 self-excludes (check_public_identifiers.sh · public_identifiers_baseline.txt
#          · todo_system/tests/test_check_public_identifiers.py)
#        − the vault-bound entries in scripts/public_export_exclude.txt
#      + pyproject.toml · uv.lock
#      + vault-template/ (identifier-free Todo/templates + empty Todo/items·Memory·Notes·Decisions·Worker).
#
# dest is initialized as a git repo and seeded with a .gitignore holding the coverage
# rules — this blocks the public repo's gitignore gap (inventory §11.8 c-2: htmlcov/·
# coverage.xml missing) up front in this reproduction tree (fixing the public repo's own
# .gitignore is a separate human-gated leaf). test_gitignore_coverage_artifacts·
# test_public_gates_real_tree assume dest is a git checkout, so git init is required.
#
# Follows the skeleton of the sibling precedent scripts/check_engine_isolation.sh
# (REPO=git rev-parse, git archive HEAD, scoped cleanup, a persistent dest with no trap).
# This file is inside the scripts/ scope, so it holds no personal-identifier literals
# ([[public-split-standing-gates]]) — the identifier pattern is extracted at runtime
# from the canonical gate scripts/check_public_identifiers.sh to filter the templates.
#
# Usage:
#   bash scripts/export_engine_tree.sh <dest>          # dest must be absent or empty
# Standalone run check (0 failed / 0 errors, skips only for vault-only reasons):
#   uv run --env-file "<dest>/standalone.env" --directory "<dest>" --with pytest \
#     python -m pytest -q -rsfE todo_system scripts/systemd
#   (standalone.env, one line: MACP_VAULT_ROOT=<dest>/vault-template)
#
set -euo pipefail

dest="${1:-}"
if [ -z "$dest" ]; then
  echo "usage: $0 <dest>" >&2
  exit 2
fi
# Safety: refuse a dest that exists and is not empty (prevents broad overwrites). Only a
# new/empty dest.
if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
  echo "ERROR: dest exists and is not empty: $dest" >&2
  exit 2
fi
mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"   # normalize to an absolute path

REPO="$(git rev-parse --show-toplevel)"
EXPORT_EXCLUDE="$REPO/scripts/public_export_exclude.txt"
if [ ! -f "$EXPORT_EXCLUDE" ]; then
  echo "ERROR: export exclude list not found: $EXPORT_EXCLUDE" >&2
  exit 2
fi

echo "== export_engine_tree =="
echo "repo: $REPO"
echo "dest: $dest"
echo

# --- Phase 1: restore the 3 engine-tree paths + manifest from the committed tree (HEAD) ----
# git archive reflects only the committed tree — uncommitted edits are not reproduced.
echo "[1] archiving engine tree (todo_system scripts .claude/commands + manifest)"
git -C "$REPO" archive HEAD todo_system scripts .claude/commands pyproject.toml uv.lock \
  | tar -x -C "$dest"

# --- Phase 2: remove the gate's 3 self-excludes + the exclude list (scoped, no globs) -------
remove_one() {
  # One at a time, no globs — no broad deletes.
  local rel="$1"
  if [ -e "$dest/$rel" ]; then
    rm -f -- "$dest/$rel"
  fi
}

# Byte-identical to the 3 self-excludes in check_public_identifiers.sh's SCOPE.
remove_one "scripts/check_public_identifiers.sh"
remove_one "scripts/public_identifiers_baseline.txt"
remove_one "todo_system/tests/test_check_public_identifiers.py"

# Exclude list (public split 9): one repo-relative path per line, a comment after `#`,
# blank lines ignored, globs (`*` `?` `[`) forbidden — the same rules as the canonical
# gate's --export parsing.
n_excluded=0
while IFS= read -r raw || [ -n "$raw" ]; do
  p="${raw%%#*}"
  p="${p#"${p%%[![:space:]]*}"}"
  p="${p%"${p##*[![:space:]]}"}"
  [ -n "$p" ] || continue
  case "$p" in
    *'*'* | *'?'* | *'['*)
      echo "ERROR: glob in exclude entry is not allowed (list files one by one): $p" >&2
      exit 2
      ;;
  esac
  remove_one "$p"
  n_excluded=$(( n_excluded + 1 ))
done < "$EXPORT_EXCLUDE"
echo "[2] removed self-exclude 3 + $n_excluded exclude-list entries"

# --- Phase 3: vault-template (empty skeleton + identifier-free templates) ----------------------
vt="$dest/vault-template"
mkdir -p \
  "$vt/Todo/items" \
  "$vt/Todo/templates" \
  "$vt/Memory/sessions" \
  "$vt/Memory/episodes" \
  "$vt/Notes" \
  "$vt/Decisions" \
  "$vt/Worker"

# Extract the personal-identifier pattern from the canonical gate at runtime — so that
# this file holds no literals (it is in the scripts/ scope, so the gate checks itself).
# Fail loud if extraction fails.
PATTERN="$(git -C "$REPO" show HEAD:scripts/check_public_identifiers.sh \
           | grep -oP "^PATTERN='\K[^']*" || true)"
if [ -z "$PATTERN" ]; then
  echo "ERROR: could not extract identifier PATTERN from check_public_identifiers.sh" >&2
  exit 2
fi

# Copy only the Todo/templates with zero identifier hits (vault-template ships as-is in
# the public repo, so it must hold no identifiers). auto-worker-task-template.md has zero
# hits, so it is included, and test_task_validate requires it.
n_tmpl=0
while IFS= read -r tmpl; do
  [ -n "$tmpl" ] || continue
  if git -C "$REPO" grep -I -P -q "$PATTERN" HEAD -- "$tmpl"; then
    echo "    skip (identifier hit): $tmpl"
    continue
  fi
  git -C "$REPO" show "HEAD:$tmpl" > "$vt/$tmpl"
  n_tmpl=$(( n_tmpl + 1 ))
done < <(git -C "$REPO" ls-files 'Todo/templates/*.md')
echo "[3] vault-template ready ($n_tmpl identifier-free template(s) copied)"

# --- Phase 4: dest .gitignore (includes the 4 coverage rules → blocks c-2 up front) -----------
cat > "$dest/.gitignore" <<'GITIGNORE_EOF'
# Python / uv 런타임
.venv/
__pycache__/
*.pyc
.pytest_cache/
# coverage 산출물 — test_gitignore_coverage_artifacts 가 이 4규칙을 요구한다.
# 공개 리포 gitignore 갭(인벤토리 §11.8 c-2: htmlcov/·coverage.xml 누락)을 이
# 재현 트리에서 사전 차단한다.
.coverage
.coverage.*
htmlcov/
coverage.xml
# 개발 체크아웃에 wheel 레이아웃 마커가 섞이면 paths.engine_root()가 설치본으로 오판한다
# (test_gitignore_blocks_a_stray_engine_marker_in_the_checkout).
todo_system/_engine/
# 파생 캐시 · 실행 산출물
Todo/todo.db
standalone-junit.xml
standalone.env
# 체크아웃 설치의 런타임 스크래치(beta 잠금·스윕 상태·gradle 레지스트리 — beta_publish._SCRATCH_ROOT,
# stages._GRADLE_SCRATCH_ROOT). 볼트에서와 같이 커밋되지 않게.
Worker/_state/
# 자격증명·로컬 설정 — 엔진 체크아웃에서 실행하다 생겨도 커밋되지 않게(볼트 .secrets/ 와 같은 부류)
.secrets/
.env
*.env
.macp/
GITIGNORE_EOF

# --- Phase 5: make dest a git repo (the isolation·gitignore tests assume a git checkout) --
# dest is a persistent artifact supplied by the caller, so it is not deleted by an EXIT trap.
git -C "$dest" init -q
git -C "$dest" add -A
git -C "$dest" -c user.email=macp@example.com -c user.name=macp \
  commit -q -m "export engine tree (public-split-10 standalone reproduction)"

# Create standalone.env (gitignored) — the verify command references it via --env-file.
printf 'MACP_VAULT_ROOT=%s/vault-template\n' "$dest" > "$dest/standalone.env"

echo "[5] dest git-initialized + committed; standalone.env written"
echo
echo "RESULT: engine export tree ready at $dest"
echo "verify: uv run --env-file $dest/standalone.env --directory $dest --with pytest \\"
echo "          python -m pytest -q -rsfE todo_system scripts/systemd"
