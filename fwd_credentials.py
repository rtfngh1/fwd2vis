#!/usr/bin/env python3
"""
Shared credential resolution and file locations for the Forward Networks scripts
in this directory.

Two jobs:

  1. CREDENTIALS -- resolved at runtime from the macOS Keychain, then the
     environment, then whatever the script hardcodes. A script that relies on
     the first two contains no secret at all, so it can be read, edited and
     shared freely. A customer who would rather paste their key into the file
     still can; that is the third tier.

  2. FILE LOCATIONS -- every script reads its inputs from ../script_input and
     writes its outputs to ../script_output, both alongside this script's
     directory rather than inside it. Keeping real data out of the script
     directory means the scripts stay shareable and no customer data ends up
     wherever the scripts are stored or synced.

USE IN A SCRIPT
---------------
    from fwd_credentials import resolve, input_path, output_path

    # tier 3: what this script falls back to if Keychain and env are both empty.
    # Ship these blank; a customer can paste values in if they prefer.
    ACCESS_KEY = ""
    SECRET_KEY = ""
    NETWORK_ID = ""
    BASE_URL   = "https://fwd.app"

    creds = resolve(fallback={"access_key": ACCESS_KEY,
                              "secret_key": SECRET_KEY,
                              "network_id": NETWORK_ID,
                              "base_url":   BASE_URL})
    session.auth = creds.auth()

    src = input_path("devices.csv")            # ../script_input/devices.csv
    dst = output_path("report.csv")            # ../script_output/report.csv

RESOLUTION ORDER (first hit wins, evaluated per value)
------------------------------------------------------
  1. macOS Keychain   service "forward-api", accounts access-key / secret-key
                      / network-id / base-url
  2. Environment      FWD_ACCESS_KEY  FWD_SECRET_KEY  FWD_NETWORK_ID
                      FWD_BASE_URL
  3. Hardcoded        whatever the calling script passes as fallback=
  4. Prompt           interactive, secret read with getpass so it does not echo

Run `python3 fwd_credentials.py` to see what currently resolves, and
`python3 fwd_credentials.py --setup` to print the setup instructions.
"""

import os
import subprocess
import sys
from getpass import getpass

DEFAULT_BASE_URL = "https://fwd.app"

KEYCHAIN_SERVICE = "forward-api"
KEYCHAIN_ACCOUNTS = {
    "access_key": "access-key",
    "secret_key": "secret-key",
    "network_id": "network-id",
    "base_url": "base-url",
}
ENV_NAMES = {
    "access_key": "FWD_ACCESS_KEY",
    "secret_key": "FWD_SECRET_KEY",
    "network_id": "FWD_NETWORK_ID",
    "base_url": "FWD_BASE_URL",
}

# --------------------------------------------------------------------------
# File locations: ../script_input and ../script_output, i.e. SIBLINGS of the
# directory holding the scripts, never inside it.
# --------------------------------------------------------------------------
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(SCRIPTS_DIR)

INPUT_DIR = os.environ.get("FWD_SCRIPT_INPUT") or os.path.join(
    _PARENT, "script_input")
OUTPUT_DIR = os.environ.get("FWD_SCRIPT_OUTPUT") or os.path.join(
    _PARENT, "script_output")


SETUP_INSTRUCTIONS = f"""\
CREDENTIALS AND FILE LOCATIONS
==============================
Credentials are looked up in this order, and the first hit wins for each value:
  1. macOS Keychain    2. environment variables    3. hardcoded in the script
If none of them supply a value, the script prompts for it.

--- Option 1: macOS Keychain (recommended) --------------------------------
Set once, works in every new shell, encrypted at rest, never in shell history.
The trailing `-w` with NO value after it is what makes each command prompt for
the value and ask you to retype it. Omit that `-w` and the item is created
silently with an empty password, which then resolves as if it were not set.
`-U` updates an item that already exists, so re-run one of these to rotate a key.

    security add-generic-password -s {KEYCHAIN_SERVICE} -a access-key -U -T "" -w
    security add-generic-password -s {KEYCHAIN_SERVICE} -a secret-key -U -T "" -w
    security add-generic-password -s {KEYCHAIN_SERVICE} -a network-id -U -T "" -w

On-prem instead of fwd.app? Add:

    security add-generic-password -s {KEYCHAIN_SERVICE} -a base-url -U -T "" -w

Inspect, replace, or remove one:

    security find-generic-password   -s {KEYCHAIN_SERVICE} -a access-key -w
    security delete-generic-password -s {KEYCHAIN_SERVICE} -a access-key

--- Option 2: environment variables ---------------------------------------
Lives only in the terminal session where you set it. Use `read -rs` so the
value never appears on the command line or in your shell history:

    read -rs -p "access key: " FWD_ACCESS_KEY && export FWD_ACCESS_KEY
    read -rs -p "secret key: " FWD_SECRET_KEY && export FWD_SECRET_KEY
    export FWD_NETWORK_ID=123456
    export FWD_BASE_URL=https://fwd.app        # only if not fwd.app

Avoid `export FWD_SECRET_KEY=abc123` -- your shell records that line verbatim.

--- Option 3: hardcode in the script --------------------------------------
Each script has a CREDENTIALS block near the top with empty strings. Paste your
values in if you would rather not use the Keychain or environment. Anything you
put there is used only when the Keychain and environment have nothing.
A file with a secret pasted into it should not be shared or committed.

--- File locations --------------------------------------------------------
Scripts read inputs from and write outputs to directories NEXT TO the script
directory, so that real network data never lands among the scripts themselves:

    input   {INPUT_DIR}
    output  {OUTPUT_DIR}

The output directory is created automatically. Override either with:

    export FWD_SCRIPT_INPUT=/some/other/path
    export FWD_SCRIPT_OUTPUT=/some/other/path

Forward API tokens authenticate as HTTP Basic: access key = username,
secret = password.
"""


class Credentials:
    """Resolved credentials, plus a note on where each value came from."""

    def __init__(self, access_key, secret_key, network_id, base_url, sources):
        self.access_key = access_key
        self.secret_key = secret_key
        self.network_id = network_id
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.sources = sources

    def auth(self):
        """HTTPBasicAuth for requests. Forward uses key=user, secret=password."""
        from requests.auth import HTTPBasicAuth
        return HTTPBasicAuth(self.access_key, self.secret_key)

    def basic_auth_header(self):
        """For urllib-based scripts with no requests dependency."""
        import base64
        token = base64.b64encode(
            f"{self.access_key}:{self.secret_key}".encode()).decode()
        return f"Basic {token}"

    def describe(self):
        """One line naming where each value came from. Never prints a secret."""
        parts = [f"{k}<-{v}" for k, v in self.sources.items()]
        return f"credentials: {'  '.join(parts)}  (key {mask(self.access_key)})"

    def __repr__(self):
        # Defensive: a stray print(creds), or a traceback that renders locals,
        # must not leak the secret. Both key fields are masked.
        return (f"Credentials(base_url={self.base_url!r}, "
                f"network_id={self.network_id!r}, "
                f"access_key={mask(self.access_key)!r}, secret_key='***')")


def mask(value):
    """Show enough of a key to recognise it, never enough to use it."""
    text = str(value or "")
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-2:]}"


def keychain_get(account, service=KEYCHAIN_SERVICE):
    """
    Read one item from the macOS Keychain.

    Returns None on any failure -- not macOS, `security` missing, item absent,
    or the user dismissing the access prompt. Never raises.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service,
             "-a", account, "-w"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def resolve(fallback=None, require_network_id=True, allow_prompt=True,
            service=KEYCHAIN_SERVICE):
    """
    Resolve credentials: Keychain -> environment -> fallback -> prompt.

    fallback            dict of the calling script's hardcoded constants, any of
                        access_key / secret_key / network_id / base_url. Blank
                        and placeholder values are ignored.
    require_network_id  False for scripts that get the network elsewhere.
    allow_prompt        False for unattended runs, so a missing value fails
                        loudly instead of blocking on a prompt.
    """
    fallback = fallback or {}
    sources = {}

    def pick(field, prompt_text, secret=False, required=True, default=None):
        env_name = ENV_NAMES[field]

        value = keychain_get(KEYCHAIN_ACCOUNTS[field], service)
        if value:
            sources[field] = "keychain"
            return value

        value = os.environ.get(env_name)
        if value and value.strip():
            sources[field] = "env"
            return value.strip()

        value = fallback.get(field)
        if value and str(value).strip() and not _is_placeholder(value):
            sources[field] = "script"
            return str(value).strip()

        if default is not None:
            sources[field] = "default"
            return default

        if not required:
            return None

        if not allow_prompt:
            raise SystemExit(
                f"ERROR: no {field} found (Keychain, {env_name}, or the "
                f"script's CREDENTIALS block) and prompting is disabled.\n"
                f"Run `python3 fwd_credentials.py --setup` for instructions.")

        if not sys.stdin.isatty():
            raise SystemExit(
                f"ERROR: no {field} found (Keychain, {env_name}, or the "
                f"script's CREDENTIALS block), and stdin is not a terminal so "
                f"it cannot be prompted for.\n"
                f"Run `python3 fwd_credentials.py --setup` for instructions.")

        value = (getpass(prompt_text) if secret else input(prompt_text)).strip()
        if not value:
            raise SystemExit(f"ERROR: no value given for {field}.")
        sources[field] = "prompt"
        return value

    access_key = pick("access_key", "Forward API access key: ")
    secret_key = pick("secret_key", "Forward API secret key: ", secret=True)
    network_id = pick("network_id", "Forward network id: ",
                      required=require_network_id)
    base_url = pick("base_url", "Forward base URL: ", required=False,
                    default=DEFAULT_BASE_URL)

    return Credentials(access_key, secret_key, network_id, base_url, sources)


def _is_placeholder(value):
    """Recognise unfilled template values so they are never used as real ones."""
    text = str(value).strip().strip("<>").lower()
    if not text:
        return True
    markers = ("put_", "your_", "your-", "xxx", "redacted", "changeme",
               "change_me", "todo", "example", "placeholder", "here")
    return any(m in text for m in markers)


# --------------------------------------------------------------------------
# Input / output paths
# --------------------------------------------------------------------------
def ensure_output_dir(subdir=None):
    """Create and return the output directory (optionally a subdirectory)."""
    path = os.path.join(OUTPUT_DIR, subdir) if subdir else OUTPUT_DIR
    os.makedirs(path, exist_ok=True)
    return path


def output_path(filename, subdir=None):
    """Absolute path under ../script_output, creating the directory."""
    return os.path.join(ensure_output_dir(subdir), filename)


def input_path(filename, must_exist=True):
    """
    Absolute path under ../script_input.

    With must_exist, a missing file exits with a message naming the directory
    to drop it into, rather than a bare FileNotFoundError from somewhere deep
    in the script.
    """
    path = os.path.join(INPUT_DIR, filename)
    if must_exist and not os.path.exists(path):
        raise SystemExit(
            f"ERROR: input file not found: {path}\n"
            f"  Put '{filename}' in {INPUT_DIR} and run again.\n"
            f"  (Override the directory with FWD_SCRIPT_INPUT=/some/path)")
    return path


def find_input(*filenames, must_exist=True):
    """First of several candidate filenames that exists in ../script_input."""
    for name in filenames:
        candidate = os.path.join(INPUT_DIR, name)
        if os.path.exists(candidate):
            return candidate
    if must_exist:
        raise SystemExit(
            f"ERROR: none of these were found in {INPUT_DIR}:\n" +
            "".join(f"    {n}\n" for n in filenames) +
            f"  Put one of them there and run again.")
    return None


def timestamped(filename):
    """'report.csv' -> 'report_20260831-142530.csv', so runs never overwrite."""
    import datetime
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base, ext = os.path.splitext(filename)
    return f"{base}_{stamp}{ext}"


if __name__ == "__main__":
    if "--setup" in sys.argv or "-h" in sys.argv or "--help" in sys.argv:
        print(SETUP_INSTRUCTIONS)
        sys.exit(0)

    print(f"scripts dir : {SCRIPTS_DIR}")
    print(f"input dir   : {INPUT_DIR}"
          f"{'' if os.path.isdir(INPUT_DIR) else '   (does not exist yet)'}")
    print(f"output dir  : {OUTPUT_DIR}"
          f"{'' if os.path.isdir(OUTPUT_DIR) else '   (created on first write)'}")
    print()
    creds = resolve(require_network_id=False)
    print(creds.describe())
    print(f"base_url    : {creds.base_url}")
    print(f"network_id  : {creds.network_id or '(not set)'}")
    print(f"secret      : resolved, {len(creds.secret_key)} chars")
    print("\nRun with --setup for setup instructions.")
