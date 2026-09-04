"""リポジトリ基盤（エージェント環境・仕様書・フック）の健全性を検証する。

実装コードが入るまでの間、CI が検証する対象はこのレイアウト整合性のみ。
M1 着手後もこのテストは残し、環境の破損を検知し続ける。
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    "SPEC.md",
    "SPEC_PHASE1.md",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "README.md",
    "CHANGELOG.md",
    ".claude/settings.json",
    ".claude/hooks/auto-open-review.sh",
    ".claude/agents/implementer-sonnet.md",
    ".claude/agents/implementer-opus.md",
    ".claude/commands/pdca.md",
    ".claude/commands/codebase-review.md",
]

# retrospective (agents-rules/) を SSoT とする共通ルール。
# 編集は retrospective 側で行い、sync-agent-rules.sh で同期する。
SHARED_RULES = [
    "agent-orchestration",
    "git-commit-rules",
    "indexing-codebase",
    "language-strategies",
    "plan-before-modify",
    "senior-engineer-conduct",
]


@pytest.mark.parametrize("relative_path", REQUIRED_FILES)
def test_required_file_exists(relative_path):
    assert (REPO_ROOT / relative_path).is_file(), f"{relative_path} が見つからない"


@pytest.mark.parametrize("rule_name", SHARED_RULES)
def test_shared_rule_is_present(rule_name):
    assert (REPO_ROOT / ".agents/rules" / f"{rule_name}.md").is_file()


def test_claude_entrypoint_imports_shared_knowledge():
    """CLAUDE.md が AGENTS.md と共通ナレッジの両方を参照していること。"""
    content = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "./AGENTS.md" in content
    assert ".agents/rules/*" in content
    assert "@~/.claude/shared/rules/common.md" in content


def test_settings_hook_commands_point_at_existing_scripts():
    """settings.json のフックが実在するスクリプトを指していること。"""
    settings = json.loads((REPO_ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entries in settings.get("hooks", {}).values()
        for entry in entries
        for hook in entry.get("hooks", [])
    ]
    assert commands, "フックが1つも登録されていない"

    for command in commands:
        # 共通ナレッジ側のフック (~/.claude/shared/...) は link-repo.sh が
        # 各マシンで登録するもので、リポジトリ内には存在しないため対象外。
        if not command.startswith("$CLAUDE_PROJECT_DIR/"):
            continue
        script = REPO_ROOT / command.removeprefix("$CLAUDE_PROJECT_DIR/")
        assert script.is_file(), f"{command} が指すスクリプトが無い"


def test_version_has_exactly_one_source():
    """版の出どころを `persona_sim.__version__` 1箇所に保つこと。

    リテラルで二重に持っていたときに 0.1.0 と 0.3.0 で食い違い、
    `run_metadata.json` の `persona_sim_version` と `--version` が実装を指していなかった。
    版がずれると「どの版のコードで走ったのか」を後から判別できない（`SPEC_PHASE1.md` §9.1）。

    **`pyproject.toml` 側を SSoT にはしない。** `importlib.metadata` から引くと、
    リポジトリを未インストールで使う経路（CI の `pythonpath`、ノートブックの sys.path 追加）で
    `PackageNotFoundError` になり版が丸ごと失われる。記録が要るのはまさにその経路。
    """
    import tomllib

    import persona_sim

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]

    assert "version" not in project, "pyproject に版を直書きすると二重定義になる"
    assert "version" in project.get("dynamic", []), "版は dynamic で解決すること"
    assert (
        pyproject["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        == "persona_sim.__version__"
    )
    # 未インストールでも版が読めること（ここが `0+unknown` に化けない）。
    assert persona_sim.__version__ and persona_sim.__version__[0].isdigit()


def test_attribution_text_carries_both_obligations():
    """帰属表示（`SPEC.md` §15.3）と免責（§15.1）を1つにまとめて運ぶこと。

    別々に運ぶと片方だけ付いた出力ができる（実際、CLI と成果物には
    帰属表示しか付いておらず、免責は Web UI のフッタにしか出ていなかった）。
    """
    from persona_sim import DATASET_ATTRIBUTION, OUTPUT_DISCLAIMER, attribution_text

    text = attribution_text()
    assert DATASET_ATTRIBUTION in text
    assert OUTPUT_DISCLAIMER in text


def test_shell_scripts_are_executable():
    scripts = list((REPO_ROOT / ".claude/hooks").glob("*.sh"))
    scripts += list((REPO_ROOT / ".agents/skills").glob("*/scripts/*.sh"))
    assert scripts, "検証対象のシェルスクリプトが見つからない"

    for script in scripts:
        assert script.stat().st_mode & 0o111, f"{script} に実行権限が無い"
