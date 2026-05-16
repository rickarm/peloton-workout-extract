"""Peloton auth via 1Password CLI + Playwright session persistence."""

import os
import subprocess
import sys
import time

from pel_selectors import SELECTORS

CACHE_DIR = os.path.expanduser("~/.cache/peloton-skill")
STORAGE_STATE_PATH = os.path.join(CACHE_DIR, "storage_state.json")
OP_VAULT = "Vault-agent-mandy"
OP_ITEM = "www.onepeloton.com"


def _op_read(field: str) -> str:
    result = subprocess.run(
        ["op", "read", f"op://{OP_VAULT}/{OP_ITEM}/{field}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[auth] Failed to read {field} from 1Password: {result.stderr}", file=sys.stderr)
        sys.exit(2)
    return result.stdout.strip()


def get_credentials() -> tuple[str, str]:
    return _op_read("username"), _op_read("password")


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
