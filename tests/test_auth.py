"""Unit tests for credential resolution in auth.py.

Credentials must come from the process environment (injected by `op run`),
with an optional, explicitly configured `op read` fallback. Error messages
must name the missing variable and never echo a credential value.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth  # noqa: E402


def test_env_credentials_win():
    env = {"PELOTON_EMAIL": "a@example.com", "PELOTON_PASSWORD": "pw-secret"}
    assert auth.resolve_credentials(env) == ("a@example.com", "pw-secret")


def test_env_beats_op_fallback(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("op must not be called when env vars are set")

    monkeypatch.setattr(subprocess, "run", boom)
    env = {
        "PELOTON_EMAIL": "a@example.com",
        "PELOTON_PASSWORD": "pw-secret",
        "PELOTON_OP_VAULT": "Some-Vault",
    }
    assert auth.resolve_credentials(env) == ("a@example.com", "pw-secret")


def test_missing_both_names_both_vars():
    with pytest.raises(auth.MissingCredentials) as exc:
        auth.resolve_credentials({})
    msg = str(exc.value)
    assert "PELOTON_EMAIL" in msg and "PELOTON_PASSWORD" in msg
    assert "op run --environment" in msg


def test_missing_password_does_not_leak_email():
    with pytest.raises(auth.MissingCredentials) as exc:
        auth.resolve_credentials({"PELOTON_EMAIL": "leaky@example.com"})
    msg = str(exc.value)
    assert "PELOTON_PASSWORD" in msg
    assert "PELOTON_EMAIL" not in msg
    assert "leaky@example.com" not in msg


def test_no_hardcoded_vault_without_config(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("op must not be called without PELOTON_OP_VAULT")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(auth.MissingCredentials):
        auth.resolve_credentials({})


def test_op_fallback_uses_configured_vault_and_item(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        field = cmd[-1].rsplit("/", 1)[-1]
        return subprocess.CompletedProcess(cmd, 0, stdout=f"val-{field}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    env = {"PELOTON_OP_VAULT": "Bot-Vault", "PELOTON_OP_ITEM": "peloton", "OP_CLI": "/opt/op"}
    assert auth.resolve_credentials(env) == ("val-username", "val-password")
    assert calls == [
        ["/opt/op", "read", "op://Bot-Vault/peloton/username"],
        ["/opt/op", "read", "op://Bot-Vault/peloton/password"],
    ]


def test_op_fallback_default_item(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="x\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("OP_CLI", raising=False)
    auth.resolve_credentials({"PELOTON_OP_VAULT": "V"})
    assert calls[0] == ["op", "read", "op://V/www.onepeloton.com/username"]


def test_op_failure_raises_missing_credentials(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(auth.MissingCredentials) as exc:
        auth.resolve_credentials({"PELOTON_OP_VAULT": "V"})
    assert "username" in str(exc.value)


def test_get_credentials_exits_2_when_missing(monkeypatch, capsys):
    monkeypatch.delenv("PELOTON_EMAIL", raising=False)
    monkeypatch.delenv("PELOTON_PASSWORD", raising=False)
    monkeypatch.delenv("PELOTON_OP_VAULT", raising=False)
    with pytest.raises(SystemExit) as exc:
        auth.get_credentials()
    assert exc.value.code == 2
    assert "PELOTON_EMAIL" in capsys.readouterr().err
