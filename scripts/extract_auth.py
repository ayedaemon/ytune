# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "yt-dlp>=2025.1.1",
#   "ytmusicapi>=1.4.0",
# ]
# ///
"""Extract a YouTube Music session from a local browser — no OAuth needed.

Runs on the HOST (needs your browser profile); containers only ever see the
two files this writes:

    auth/ytmusic.json   ytmusicapi headers (SAPISID auth)  → yt_input worker
    auth/cookies.txt    same session, Netscape format      → ytdlp_output worker

Usage:
    uv run scripts/extract_auth.py --browser firefox
    uv run scripts/extract_auth.py --browser chrome --profile "Profile 1" --authuser 1
    uv run scripts/extract_auth.py --check

Refresh when the session expires (workers report auth_expired in /healthz):
log in to music.youtube.com in the browser again, rerun this script. The
auth/ directory is bind-mounted, so workers pick the new file up on their
next task — no container restart.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from hashlib import sha1
from http.cookiejar import MozillaCookieJar
from pathlib import Path

YTM_ORIGIN = "https://music.youtube.com"
AUTH_DIR = Path(__file__).resolve().parent.parent / "auth"

BROWSERS = [
    "brave",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "opera",
    "safari",
    "vivaldi",
]

# ST-* are YouTube's per-video session-transfer tokens — hundreds of them would
# push the Cookie header over HTTP size limits without helping auth.
_SKIP_PREFIXES = ("ST-", "_g", "__utm")
_SKIP_NAMES = {
    "DV",
    "OTZ",
    "UULE",
    "_GRECAPTCHA",
    "SNID",
    "SEARCH_SAMESITE",
    "CONSISTENCY",
    "COMPASS",
}


def extract_cookiejar(browser: str, profile: str | None):
    from yt_dlp.cookies import extract_cookies_from_browser

    kwargs = {"profile": profile} if profile else {}
    try:
        return extract_cookies_from_browser(browser, **kwargs)
    except FileNotFoundError:
        sys.exit(
            f"error: no {browser} cookie database found — is the browser installed "
            "and logged in to music.youtube.com?"
        )
    except PermissionError:
        sys.exit(
            f"error: permission denied reading {browser} cookies — close the browser and retry."
        )


def filter_yt_cookies(cookiejar) -> dict[str, str]:
    """Auth/session cookies for youtube.com + google.com, most-specific domain wins."""
    by_name: dict[str, tuple[str, str]] = {}
    for c in cookiejar:
        domain = c.domain or ""
        if "youtube.com" not in domain and "google.com" not in domain:
            continue
        if any(c.name.startswith(p) for p in _SKIP_PREFIXES) or c.name in _SKIP_NAMES:
            continue
        prev = by_name.get(c.name, ("", ""))[0]
        if len(domain) >= len(prev):
            by_name[c.name] = (domain, c.value)
    return {name: val for name, (_, val) in by_name.items()}


def build_auth_headers(yt_cookies: dict[str, str], authuser: int) -> dict[str, str]:
    sapisid = yt_cookies.get("__Secure-3PAPISID") or yt_cookies.get("SAPISID", "")
    ts = str(int(time.time()))
    digest = sha1(f"{ts} {sapisid} {YTM_ORIGIN}".encode()).hexdigest()
    return {
        "Accept": "*/*",
        "Authorization": f"SAPISIDHASH {ts}_{digest}",
        "Content-Type": "application/json",
        "X-Goog-AuthUser": str(authuser),
        "x-origin": YTM_ORIGIN,
        "Cookie": "; ".join(f"{k}={v}" for k, v in yt_cookies.items()),
    }


def write_netscape_cookies(cookiejar, out_path: Path) -> int:
    """Serialize the youtube/google subset of the jar for yt-dlp's cookiefile."""
    jar = MozillaCookieJar(str(out_path))
    n = 0
    for c in cookiejar:
        if "youtube.com" in (c.domain or "") or "google.com" in (c.domain or ""):
            jar.set_cookie(c)
            n += 1
    jar.save(ignore_discard=True, ignore_expires=True)
    return n


def validate(auth_file: Path) -> tuple[bool, str]:
    """Live-probe the session. Returns (valid, detail)."""
    from ytmusicapi import YTMusic

    try:
        ytm = YTMusic(auth=str(auth_file))
        pls = ytm.get_library_playlists(limit=1)
        return True, f"session valid — {len(pls or [])}+ library playlists visible"
    except Exception as e:
        return False, str(e)


def cmd_extract(args) -> None:
    AUTH_DIR.mkdir(mode=0o700, exist_ok=True)
    cookiejar = extract_cookiejar(args.browser, args.profile)

    yt_cookies = filter_yt_cookies(cookiejar)
    if "__Secure-3PAPISID" not in yt_cookies:
        sys.exit(
            f"error: missing __Secure-3PAPISID cookie in {args.browser} — "
            "log in to music.youtube.com there, then retry."
        )

    headers = build_auth_headers(yt_cookies, args.authuser)

    json_path = AUTH_DIR / "ytmusic.json"
    json_path.write_text(json.dumps(headers, indent=2))
    json_path.chmod(0o600)

    cookies_path = AUTH_DIR / "cookies.txt"
    n = write_netscape_cookies(cookiejar, cookies_path)
    cookies_path.chmod(0o600)

    print(f"wrote {json_path}  ({len(yt_cookies)} cookies)")
    print(f"wrote {cookies_path}  ({n} cookies, Netscape format)")

    ok, detail = validate(json_path)
    if ok:
        print(f"✓ {detail}")
    else:
        sys.exit(
            f"✗ validation failed: {detail}\n"
            "  (extracted anyway — but the session likely won't work; "
            "log in to music.youtube.com and rerun)"
        )


def cmd_check(_args) -> None:
    json_path = AUTH_DIR / "ytmusic.json"
    if not json_path.exists():
        sys.exit(
            f"✗ {json_path} does not exist — run: uv run scripts/extract_auth.py --browser <name>"
        )
    ok, detail = validate(json_path)
    print(("✓ " if ok else "✗ ") + detail)
    sys.exit(0 if ok else 1)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--browser", choices=BROWSERS, help="browser to extract the session from"
    )
    p.add_argument(
        "--profile", help="browser profile name/path (default: default profile)"
    )
    p.add_argument(
        "--authuser", type=int, default=0, help="Google multi-account index (default 0)"
    )
    p.add_argument(
        "--check", action="store_true", help="validate the existing auth files and exit"
    )
    args = p.parse_args()

    if args.check:
        cmd_check(args)
    elif args.browser:
        cmd_extract(args)
    else:
        p.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
