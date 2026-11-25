"""Claude Code plugin — ``.claude-plugin/marketplace.json`` + ``plugins/macp``.

``docs/design/lite-mode.md`` ⑦ (D13): one marketplace, one plugin, one skill that
runs the lite CLI (``macp init | add | up | status | dashboard | memory``) and
nothing else. Two things are pinned here.

* **The documented format.** ``claude plugin validate`` is the authoritative check
  (``plugins/macp/README.md`` shows the two commands), but ``claude`` is not on
  every host or stage that runs this suite, so its rules are restated from the
  official docs — code.claude.com/docs/en/plugins/manifest-reference,
  …/marketplace-reference, …/create-marketplace and …/en/skills: required fields,
  the name rules, ``./`` sources with no ``..``, entry name = manifest name, no
  unknown keys (validate warns), ``version`` in one place, the skill frontmatter.
* **No logic in the plugin.** It ships exactly three files (no scripts, hooks, MCP
  servers or ``bin/``); every command the skill tells Claude to run is ``macp …``
  or the README's install line; and the command line the skill builds from
  ``$ARGUMENTS`` reaches ``todo_system.lite.main`` unchanged through the console
  script ``macp`` (``todo_system._entry.main``).

The GitHub owner/repo is read from ``pyproject.toml`` (``[project.urls]``) and never
written here: this file is inside the public-identifier gate's scope.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from todo_system import _entry, lite

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGINS_DIR = REPO_ROOT / "plugins"
README = REPO_ROOT / "README.md"
README_KO = REPO_ROOT / "README.ko.md"

# The engine export tree (scripts/export_engine_tree.sh) and the sdist carry neither
# directory; a checkout that has only one of them fails below instead of skipping.
pytestmark = pytest.mark.skipif(
    not MARKETPLACE.exists() and not PLUGINS_DIR.exists(),
    reason="no Claude Code plugin in this tree (engine export / sdist)")

# marketplace-reference, "Top-level fields" / "Plugin entries": a marketplace or plugin
# id uses letters, digits, ".", "_" and "-", and starts with a letter or digit.
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# manifest-reference, "name": kebab-case (validate warns otherwise).
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Every key the docs list. Claude Code ignores an unknown key and `claude plugin
# validate` warns about it, so an unknown key here is a typo that loads silently.
MARKETPLACE_KEYS = {"$schema", "name", "owner", "plugins", "description", "version",
                    "metadata", "forceRemoveDeletedPlugins",
                    "allowCrossMarketplaceDependenciesOn", "renames"}
PERSON_KEYS = {"name", "email", "url"}
PLUGIN_KEYS = {"$schema", "name", "displayName", "version", "description", "author",
               "homepage", "repository", "license", "keywords", "metadata", "icon",
               "documentationUrl", "supportUrl", "privacyPolicyUrl", "termsOfServiceUrl",
               "defaultEnabled", "engines", "dependencies", "network", "userConfig",
               "types", "channels", "skills", "commands", "agents", "hooks", "mcpServers",
               "lspServers", "outputStyles", "workflows", "experimental"}
# Directory-listing fields belong in plugin.json only; in an entry validate reports them.
LISTING_KEYS = {"icon", "documentationUrl", "supportUrl", "privacyPolicyUrl",
                "termsOfServiceUrl"}
ENTRY_KEYS = ({"name", "source", "description", "version", "category", "tags", "strict",
               "relevance", "dependencies", "engines", "defaultEnabled", "displayName",
               "metadata", "headers", "headersHelper"}
              | (PLUGIN_KEYS - LISTING_KEYS - {"$schema"}))
# skills, "Frontmatter reference" (Claude Code ignores any other field without a word).
SKILL_KEYS = {"name", "description", "when_to_use", "argument-hint", "arguments",
              "disable-model-invocation", "user-invocable", "allowed-tools",
              "disallowed-tools", "model", "effort", "context", "agent", "background",
              "hooks", "paths", "shell", "metadata", "license", "compatibility"}
SKILL_DESCRIPTION_CAP = 1536      # description + when_to_use, as listed to Claude

# The whole plugin. A script, hook, MCP server, agent or bin/ added here would be logic
# outside the CLI (the task: "슬래시 명령이 2단계 CLI를 그대로 부르고 별도 로직이 없다").
PLUGIN_FILES = [".claude-plugin/plugin.json", "README.md", "skills/macp/SKILL.md"]
# Words that would mean the skill does the CLI's work itself instead of calling it.
ENGINE_INTERNALS = ("Todo/", "todo.db", "frontmatter", "todo_system", "python -m",
                    "claude -p", "MACP_", "USAGE_GUARD", "git commit", "git push",
                    "worker_mode", "auto_merge", "~/.macp")
CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
COMMAND_RE = re.compile(r"^[A-Za-z][\w.+-]*\s")      # "word …": something one could run


# --- helpers ----------------------------------------------------------------------


def _json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path}: not a JSON object"
    return data


def _marketplace() -> dict:
    return _json(MARKETPLACE)


def _entry_root(entry: dict) -> Path:
    return (REPO_ROOT / entry["source"]).resolve()


def _the_plugin() -> tuple[dict, Path, dict]:
    entries = _marketplace()["plugins"]
    assert len(entries) == 1, "one plugin: macp"
    root = _entry_root(entries[0])
    return entries[0], root, _json(root / ".claude-plugin" / "plugin.json")


def _skill() -> tuple[dict, str]:
    _, root, _ = _the_plugin()
    text = (root / "skills" / "macp" / "SKILL.md").read_text(encoding="utf-8")
    # skills: the frontmatter is read only when the opening --- is the first line.
    assert text.startswith("---\n"), "SKILL.md must open with its frontmatter"
    head, sep, body = text[len("---\n"):].partition("\n---\n")
    assert sep, "SKILL.md frontmatter is not closed"
    meta = yaml.safe_load(head)
    assert isinstance(meta, dict), "SKILL.md frontmatter is not a YAML mapping"
    return meta, body


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _repository_url() -> str:
    return _pyproject()["project"]["urls"]["Repository"]


def _github_slug() -> str:
    """``owner/repo``: the form ``/plugin marketplace add`` takes for GitHub."""
    url = urlparse(_repository_url())
    assert url.scheme == "https" and url.netloc == "github.com", url
    return url.path.strip("/").removesuffix(".git")


def _cli_install_line() -> str:
    return f"uv tool install git+{_repository_url()}"


def _lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


def _commands(body: str) -> list[str]:
    """What the skill text could ask Claude to run: code spans and fenced-block lines
    shaped like ``word …``. Single words (`macp`, `run_in_background`) and slash
    commands (`/macp status`) are references, not commands."""
    spans = CODE_SPAN_RE.findall(FENCE_RE.sub("", body))
    for block in FENCE_RE.findall(body):
        spans += [ln.strip() for ln in block.splitlines() if ln.strip()]
    return [s for s in spans if COMMAND_RE.match(s)]


def _readme_macp_commands() -> list[str]:
    """The README's "Try it in five minutes" commands (comments dropped) — the block
    scripts/check_engine_isolation.sh holds to exactly three."""
    text = README.read_text(encoding="utf-8")
    section = text[text.index("## Try it in five minutes"):]
    block = section[section.index("```bash\n") + len("```bash\n"):]
    block = block[:block.index("```")]
    return [re.sub(r"\s+#.*$", "", ln).strip() for ln in block.splitlines() if ln.strip()]


# --- the documented format (what `claude plugin validate` checks) -----------------


def test_marketplace_file_has_the_documented_fields():
    m = _marketplace()
    assert set(m) <= MARKETPLACE_KEYS, f"unknown top-level keys: {set(m) - MARKETPLACE_KEYS}"
    name = m["name"]
    assert ID_RE.match(name) and ".." not in name, name
    # Reserved names: the official/community ones, claudeai-*, and any name that
    # passes for Anthropic's own. Keeping both words out keeps clear of all of them.
    assert not re.search(r"(?i)claude|anthropic|official", name), name
    assert isinstance(m.get("description"), str) and m["description"].strip(), \
        "validate warns when the marketplace has no description"
    owner = m["owner"]
    assert isinstance(owner, dict) and set(owner) <= PERSON_KEYS, owner
    assert isinstance(owner.get("name"), str) and owner["name"].strip(), "owner.name is required"
    plugins = m["plugins"]
    assert isinstance(plugins, list) and plugins, "validate warns on an empty plugins list"
    names = [e.get("name") for e in plugins]
    assert len(names) == len(set(names)), "duplicate plugin name"


def test_each_entry_is_a_relative_source_inside_the_marketplace():
    for i, entry in enumerate(_marketplace()["plugins"]):
        assert set(entry) <= ENTRY_KEYS, f"plugins[{i}] unknown keys: {set(entry) - ENTRY_KEYS}"
        assert isinstance(entry.get("name"), str) and ID_RE.match(entry["name"]), entry
        src = entry["source"]
        # Written from the marketplace root (the directory holding .claude-plugin/),
        # starting with ./, with forward slashes and no "..".
        assert isinstance(src, str) and src.startswith("./"), src
        assert ".." not in src and "\\" not in src, src
        root = _entry_root(entry)
        assert root.is_relative_to(REPO_ROOT) and root.is_dir(), f"{src}: no such directory"
        manifest = _json(root / ".claude-plugin" / "plugin.json")
        # The install id is <entry name>@<marketplace>; skills are namespaced by the
        # manifest name. Different names break installs by the manifest name.
        assert manifest["name"] == entry["name"]
        # version in both places: plugin.json silently wins and validate warns.
        assert not ("version" in entry and "version" in manifest)
        if "description" in entry:      # what /plugin lists before and after install
            assert entry["description"] == manifest["description"]


def test_plugin_manifest_has_the_documented_fields():
    _, root, manifest = _the_plugin()
    assert set(manifest) <= PLUGIN_KEYS, f"unknown keys: {set(manifest) - PLUGIN_KEYS}"
    name = manifest["name"]
    assert KEBAB_RE.match(name), name
    # Reserved: claude-/anthropic-/anthropics-/cc-plugin- prefixes and the bare names
    # are errors; claude/anthropic as a whole word anywhere is a warning.
    assert not name.startswith("cc-plugin-"), name
    assert not {"claude", "anthropic", "anthropics"} & set(name.split("-")), name
    # Missing version, description or author → "Validation passed with warnings".
    for key in ("version", "description"):
        assert isinstance(manifest.get(key), str) and manifest[key].strip(), key
    author = manifest["author"]
    assert isinstance(author, dict) and set(author) <= PERSON_KEYS, author
    assert isinstance(author.get("name"), str) and author["name"].strip(), "author.name"
    home = urlparse(manifest["homepage"])           # "Must parse as a URL"
    assert home.scheme in ("http", "https") and home.netloc, manifest["homepage"]
    assert all(isinstance(k, str) and k for k in manifest.get("keywords", [])), "keywords"
    # A CLAUDE.md at the plugin root is not loaded, and validate warns about it.
    assert not (root / "CLAUDE.md").exists()


def test_plugin_version_is_the_engine_version():
    """Installed copies update only when ``version`` changes. Tying it to the engine
    release keeps the skill and the CLI it calls moving together."""
    _, _, manifest = _the_plugin()
    assert manifest["version"] == _pyproject()["project"]["version"]


def test_skill_frontmatter_has_the_documented_fields():
    meta, body = _skill()
    assert set(meta) <= SKILL_KEYS, f"unknown frontmatter: {set(meta) - SKILL_KEYS}"
    # skills/<dir>/SKILL.md in plugin "macp" → /macp:macp, and bare /macp wherever no
    # other command already uses that name.
    assert meta["name"] == "macp"
    desc = meta["description"]
    assert isinstance(desc, str) and desc.strip()
    assert len(desc) + len(meta.get("when_to_use", "")) <= SKILL_DESCRIPTION_CAP
    assert body.strip(), "SKILL.md has no instructions"


# --- the skill only calls the CLI -------------------------------------------------


def test_plugin_ships_one_skill_and_no_code():
    _, root, _ = _the_plugin()
    found = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                   if p.is_file() or p.is_symlink())
    assert found == PLUGIN_FILES


def test_skill_runs_nothing_but_the_macp_cli():
    meta, body = _skill()
    # Only the CLI is pre-approved, and the user starts it: `/macp …`, never Claude
    # on its own (up starts a headless worker that pushes a branch).
    assert meta["allowed-tools"] == "Bash(macp *)"
    assert meta["disable-model-invocation"] is True
    commands = _commands(body)
    assert "macp $ARGUMENTS" in commands, "the skill must run `macp $ARGUMENTS`"
    verbs = set(_entry.LITE_VERBS)
    install = _cli_install_line()
    for cmd in commands:
        word = cmd.split()[0]
        # `macp …` is the call; `up --once` names a verb the user typed; the install
        # line is what to tell a user who has no macp yet.
        assert word == "macp" or word in verbs or cmd == install, f"not the CLI: {cmd!r}"
    for marker in ENGINE_INTERNALS:
        assert marker not in body, f"the skill re-states CLI internals: {marker!r}"


def test_argument_hint_names_exactly_the_lite_verbs():
    meta, _ = _skill()
    parts = [part.strip() for part in meta["argument-hint"].split(" | ")]
    verbs = [part.split()[0] for part in parts]
    assert sorted(verbs) == sorted(_entry.LITE_VERBS)


def test_console_script_macp_is_the_entry_point():
    assert _pyproject()["project"]["scripts"]["macp"] == "todo_system._entry:main"


@pytest.mark.parametrize("typed", [
    "init",
    'add "Add an install section to the README"',
    "add Add an install section to the README",
    "up --once",
    "up",
    "up --service",
    "status",
    "dashboard",
    "memory list",
])
def test_slash_command_reaches_the_lite_cli_unchanged(typed, monkeypatch):
    """`/macp <typed>` → the skill's `macp $ARGUMENTS` → the shell's argv → the console
    script → ``lite.main`` with the same words, nothing added or dropped."""
    seen: list[list[str]] = []
    monkeypatch.setattr(lite, "main", lambda argv: seen.append(list(argv)) or 0)
    _, body = _skill()
    template = next(c for c in _commands(body) if "$ARGUMENTS" in c)
    argv = shlex.split(template.replace("$ARGUMENTS", typed))
    assert argv[0] == "macp"
    assert _entry.main(argv[1:]) == 0
    assert seen == [argv[1:]] == [shlex.split(typed)]


def test_readme_commands_are_the_same_as_slash_commands(monkeypatch):
    """Each command of the README's five-minute block, typed as `/macp …`, runs exactly
    that command."""
    seen: list[list[str]] = []
    monkeypatch.setattr(lite, "main", lambda argv: seen.append(list(argv)) or 0)
    _, body = _skill()
    template = next(c for c in _commands(body) if "$ARGUMENTS" in c)
    for line in _readme_macp_commands():
        words = shlex.split(line)
        assert words[0] == "macp" and words[1] in _entry.LITE_VERBS, line
        typed = line[len("macp "):]                      # what follows `/macp `
        assert _entry.main(shlex.split(template.replace("$ARGUMENTS", typed))[1:]) == 0
        assert seen[-1] == words[1:]
    assert len(seen) == 3


# --- README: "Already using Claude Code?" -----------------------------------------


@pytest.mark.parametrize("readme", ["README.md", "README.ko.md", "plugins/macp/README.md"])
def test_readme_has_the_two_line_plugin_install(readme):
    m = _marketplace()
    entry = m["plugins"][0]
    lines = _lines(REPO_ROOT / readme)
    add = f"/plugin marketplace add {_github_slug()}"
    install = f"/plugin install {entry['name']}@{m['name']}"
    assert add in lines, f"{readme}: missing {add!r}"
    i = lines.index(add)
    assert lines[i + 1] == install, f"{readme}: {install!r} must follow {add!r}"
    # The CLI install line is the prerequisite, written above the two lines.
    cli = _cli_install_line()
    assert cli in lines[:i], f"{readme}: {cli!r} must come before the plugin lines"


def test_readme_sections_are_named_for_claude_code_users():
    assert "### Already using Claude Code?" in _lines(README)
    assert "### 이미 Claude Code를 쓰고 있다면" in _lines(README_KO)


def test_skill_install_hint_is_the_readme_install_line():
    _, body = _skill()
    assert _cli_install_line() in _lines(README)
    assert f"`{_cli_install_line()}`" in body
