"""Peloton auth: credentials from the environment + Playwright session persistence.

Credentials come from PELOTON_EMAIL / PELOTON_PASSWORD in the process
environment. The caller injects them, typically with
`op run --environment "$OP_ENVIRONMENT_ID" -- <command>`. Nothing here reads a
local secrets file.

Optional fallback: if PELOTON_OP_VAULT is set (and the env vars are not), the
credentials are read with `op read op://$PELOTON_OP_VAULT/$PELOTON_OP_ITEM/...`.
That path needs OP_SERVICE_ACCOUNT_TOKEN (or a signed-in op) from the caller.

The session cache (Playwright storage state) holds a live Peloton bearer token
that peloton_api.py reuses for API calls. It defaults to
~/.cache/peloton-skill/ and can be moved with PELOTON_CACHE_DIR.
"""

import os
import subprocess
import sys
import time

from pel_selectors import SELECTORS

CACHE_DIR = os.path.expanduser(os.environ.get("PELOTON_CACHE_DIR") or "~/.cache/peloton-skill")
STORAGE_STATE_PATH = os.path.join(CACHE_DIR, "storage_state.json")

EMAIL_VAR = "PELOTON_EMAIL"
PASSWORD_VAR = "PELOTON_PASSWORD"
OP_VAULT_VAR = "PELOTON_OP_VAULT"
OP_ITEM_VAR = "PELOTON_OP_ITEM"
DEFAULT_OP_ITEM = "www.onepeloton.com"


class MissingCredentials(RuntimeError):
    """Raised when no credential source is configured. Never carries values."""


def _op_read(op: str, vault: str, item: str, field: str) -> str:
    result = subprocess.run(
        [op, "read", f"op://{vault}/{item}/{field}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MissingCredentials(
            f"op read failed for field '{field}' in vault '{vault}': {result.stderr.strip()}"
        )
    return result.stdout.strip()


def resolve_credentials(env=None) -> tuple[str, str]:
    """Return (email, password) from the environment, else the optional op fallback.

    Raises MissingCredentials naming the missing variables. Error messages
    never include credential values.
    """
    env = os.environ if env is None else env
    email = env.get(EMAIL_VAR, "")
    password = env.get(PASSWORD_VAR, "")
    if email and password:
        return email, password

    vault = env.get(OP_VAULT_VAR, "")
    if vault:
        item = env.get(OP_ITEM_VAR) or DEFAULT_OP_ITEM
        op = env.get("OP_CLI") or "op"
        return _op_read(op, vault, item, "username"), _op_read(op, vault, item, "password")

    missing = [name for name, val in ((EMAIL_VAR, email), (PASSWORD_VAR, password)) if not val]
    raise MissingCredentials(
        f"{' and '.join(missing)} not set. Run via: "
        'op run --environment "$OP_ENVIRONMENT_ID" -- <command>'
    )


def get_credentials() -> tuple[str, str]:
    try:
        return resolve_credentials()
    except MissingCredentials as exc:
        print(f"[auth] {exc}", file=sys.stderr)
        sys.exit(2)


def has_storage_state() -> bool:
    return os.path.isfile(STORAGE_STATE_PATH)


def dismiss_cookie_banner(page) -> None:
    btn = page.locator(SELECTORS["cookie_dismiss"])
    if btn.count() > 0 and btn.first.is_visible():
        btn.first.click()
        time.sleep(0.5)


def is_logged_in(page) -> bool:
    """Check if the current page is authenticated (not a login redirect)."""
    return "login" not in page.url.lower()


def do_login(page) -> None:
    """Fill and submit the Peloton login form."""
    username, password = get_credentials()

    email_input = page.locator(SELECTORS["login_email"])
    email_input.wait_for(timeout=10000)
    email_input.fill(username)

    page.locator(SELECTORS["login_password"]).fill(password)
    page.locator(SELECTORS["login_submit"]).click()

    # Wait for redirect away from login
    page.wait_for_url("**/profile/**", timeout=30000)
    time.sleep(2)


def save_storage_state(context) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    context.storage_state(path=STORAGE_STATE_PATH)
    os.chmod(STORAGE_STATE_PATH, 0o600)
