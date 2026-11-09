"""Guards proving the demo can run without spending provider tokens.

These tests are static (AST/text) plus one configuration check, so they need no
network and no OpenAI key.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from demo.replay import resolve_chat_mode

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = REPO_ROOT / "demo"
APP_UI = DEMO_DIR / "app_ui.py"
METRICS_MODULES = (DEMO_DIR / "metrics_page.py", DEMO_DIR / "metrics_data.py")


def _contains_chat_completions(node: ast.AST) -> bool:
    """True for a plain or f-string argument that names the chat endpoint."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and "/chat/completions" in node.value
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and "/chat/completions" in value.value
            for value in node.values
        )
    return False


def _chat_post_calls(node: ast.AST) -> bool:
    """True for ``requests.post(<string containing /chat/completions>, ...)``."""
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    if not isinstance(function, ast.Attribute) or function.attr != "post":
        return False
    owner = function.value
    if not isinstance(owner, ast.Name) or owner.id != "requests":
        return False
    if not node.args:
        return False
    return _contains_chat_completions(node.args[0])


def _chat_post_owners(source: str) -> list[str]:
    """Names of the functions that contain a chat POST call, in source order."""
    tree = ast.parse(source)
    owners: list[str] = []

    def visit(node: ast.AST, owner: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
            else:
                if owner is not None and _chat_post_calls(child):
                    owners.append(owner)
                visit(child, owner)

    visit(tree, None)
    return owners


@pytest.mark.parametrize("path", METRICS_MODULES, ids=lambda path: path.name)
def test_metrics_modules_never_reach_the_chat_endpoint(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()

    assert "chat/completions" not in text
    assert "requests.post" not in text


def test_only_chat_live_posts_to_chat_completions() -> None:
    source = APP_UI.read_text(encoding="utf-8")

    owners = _chat_post_owners(source)

    assert owners == ["_chat_live"]
    assert "_chat_replay" not in owners


def test_chat_live_exists_and_replay_is_separate() -> None:
    tree = ast.parse(APP_UI.read_text(encoding="utf-8"))
    names = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert {"_chat_live", "_chat_replay"} <= names


def test_demo_mode_without_explicit_mode_defaults_to_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCMIND_DEMO_MODE", "1")
    monkeypatch.delenv("DOCMIND_CHAT_MODE", raising=False)

    assert resolve_chat_mode(os.getenv("DOCMIND_CHAT_MODE"), demo_mode=True) == "replay"


def test_local_mode_without_explicit_mode_defaults_to_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOCMIND_CHAT_MODE", raising=False)

    assert resolve_chat_mode(os.getenv("DOCMIND_CHAT_MODE"), demo_mode=False) == "live"
