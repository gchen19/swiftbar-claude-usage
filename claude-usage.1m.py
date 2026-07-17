#!/usr/bin/env python3
# <xbar.title>Claude Usage</xbar.title>
# <xbar.version>1.4</xbar.version>
# <xbar.author>George Chen</xbar.author>
# <xbar.desc>Claude session (5h) + weekly (7d) usage limits + status.claude.com health in the menu bar.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
#
# Reads the OAuth token Claude Code stores in the macOS keychain and calls the
# same /api/oauth/usage endpoint the app uses. Read-only; your own account.
#
# /api/oauth/usage is no-store + rate-limited (it's meant for occasional checks,
# not polling). So we fetch utilization only every FETCH_INTERVAL seconds, cache
# it to disk, and recompute the reset countdown LOCALLY every minute from the
# cached resets_at. On a 429 / network error we keep showing the cached value
# instead of an error.

import json
import os
import random
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CACHE_PATH = os.path.expanduser("~/.cache/claude-usage.json")
LOG_PATH = os.path.expanduser("~/.cache/claude-usage.log")
LOG_MAX_LINES = 2000    # trim the log once it grows past this many lines
PLUGIN_PATH = os.path.abspath(__file__)  # for the non-destructive Force update button
FETCH_INTERVAL = 600    # seconds between live fetches (countdown still ticks every minute)
BACKOFF_429 = 900       # fallback 429 backoff when the server sends no Retry-After
BACKOFF_429_MAX = 3600  # cap on honoring a server Retry-After, in case it's absurd
BACKOFF_ERROR = 120     # seconds to wait after a non-429 fetch failure (auth/network/no-token)

# status.claude.com is a standard Atlassian Statuspage. summary.json is public,
# unauthenticated, and gives both the top-level indicator and per-component
# status in one call. It's not rate-limited like the usage endpoint, but we
# still cache it (own short interval) so an outage shows promptly without
# polling on every 1-minute menu-bar refresh.
STATUS_ENDPOINT = "https://status.claude.com/api/v2/summary.json"
STATUS_CACHE_PATH = os.path.expanduser("~/.cache/claude-status.json")
STATUS_INTERVAL = 120   # seconds between status fetches

# ---- SwiftBar helpers --------------------------------------------------------
SEP = "---"

def line(text, **attrs):
    if attrs:
        parts = " ".join(f"{k}={v}" for k, v in attrs.items())
        print(f"{text} | {parts}")
    else:
        print(text)

# SwiftBar accepts "lightcolor,darkcolor" and picks per system appearance.
GRAY = "#777777,#9a9a9a"

def color_for(util):
    if util is None:
        return GRAY
    if util >= 90:
        return "#c00000,#ff453a"   # red
    if util >= 75:
        return "#b25000,#ff9f0a"   # orange
    if util >= 50:
        return "#8a6d00,#ffd60a"   # amber
    return "#1a8a3a,#30d158"       # green

def fmt_countdown(iso):
    """Compact time-to-reset for the menu bar, e.g. '4h12m', '47m', '1d3h'."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        secs = (dt - datetime.now(timezone.utc).astimezone()).total_seconds()
        if secs <= 0:
            return "now"
        mins = int(secs // 60)
        h, m = divmod(mins, 60)
        if h >= 24:
            d, h = divmod(h, 24)
            return f"{d}d{h}h"
        if h > 0:
            return f"{h}h{m:02d}m" if m else f"{h}h"
        return f"{m}m"
    except Exception:
        return None

def fmt_reset(iso):
    if not iso:
        return "no reset scheduled"
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        now = datetime.now(timezone.utc).astimezone()
        delta = dt - now
        hrs = delta.total_seconds() / 3600
        if hrs < 0:
            rel = "now"
        elif hrs < 1:
            rel = f"in {int(delta.total_seconds() / 60)}m"
        elif hrs < 24:
            rel = f"in {hrs:.1f}h"
        else:
            rel = f"in {int(hrs / 24)}d {int(hrs % 24)}h"
        return f"{dt.strftime('%a %-I:%M%p').lower()} ({rel})"
    except Exception:
        return iso

def fmt_age(secs):
    if secs is None:
        return "unknown"
    secs = int(secs)
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"

def fmt_retry(next_after, now=None):
    """Compact 'in ~Nm' until the persisted backoff lifts, so a force click that
    lands in a rate-limit window can say when a retry will actually work."""
    if not next_after:
        return "shortly"
    secs = int(next_after - (now if now is not None else time.time()))
    if secs <= 0:
        return "now"
    if secs < 60:
        return f"in ~{secs}s"
    return f"in ~{secs // 60}m"

# ---- token + request ---------------------------------------------------------
def get_token():
    raw = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True, timeout=10,
    )
    if raw.returncode != 0:
        return None, "keychain read failed"
    try:
        data = json.loads(raw.stdout)
        oauth = data.get("claudeAiOauth", data)
        return oauth.get("accessToken"), None
    except Exception as e:
        return None, f"token parse: {e}"

def fetch_usage(token):
    req = urllib.request.Request(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.load(r)

# ---- cache -------------------------------------------------------------------
def load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return None

def save_cache(c):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(c, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass

def log_event(msg):
    """Append a timestamped line so multi-hour update gaps can be diagnosed after the fact."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        with open(LOG_PATH, "a") as f:
            f.write(f"{stamp} {msg}\n")
        if os.path.getsize(LOG_PATH) > 4 * LOG_MAX_LINES * 80:  # cheap size check before reading
            with open(LOG_PATH) as f:
                lines = f.readlines()
            if len(lines) > LOG_MAX_LINES:
                with open(LOG_PATH, "w") as f:
                    f.writelines(lines[-LOG_MAX_LINES:])
    except Exception:
        pass

def window_rolled_over(data, now_ts):
    """True if a tracked window's reset time has passed — fetch promptly then."""
    for key in ("five_hour", "seven_day"):
        iso = (data.get(key) or {}).get("resets_at")
        if iso:
            try:
                if datetime.fromisoformat(iso).timestamp() <= now_ts:
                    return True
            except Exception:
                pass
    return False

def get_usage(force=False):
    """Return (data, meta). meta has fetched_at, note, and on hard-fail short/detail.

    force=True re-fetches now but PRESERVES any cached data as a fallback, so a
    manual refresh that hits a 429 never wipes the last-known value.
    """
    now = time.time()
    cache = load_cache() or {}
    data = cache.get("data")
    fetched_at = cache.get("fetched_at", 0)
    next_after = cache.get("next_fetch_after", 0)

    due = force or now >= next_after
    # Rollover should get us a prompt fetch after a window resets, but must never
    # override an active post-failure backoff — otherwise a stale cached window
    # (itself a symptom of failures) forces a retry every single 1-minute tick,
    # hammering a rate-limited endpoint instead of backing off.
    if data and not due and not cache.get("error_backoff") and window_rolled_over(data, now):
        due = True

    if not due:
        if data:
            return data, {"fetched_at": fetched_at, "note": None}
        # No cached value yet but still inside a persisted backoff (e.g. after a
        # cold-start 429). Honor it instead of re-hitting the endpoint every minute.
        return None, {"short": "Claude usage…", "soft": True,
                      "detail": "Endpoint rate-limited; auto-retries shortly, "
                                "or use Force update."}

    token, err = get_token()
    if err or not token:
        # No backoff was previously persisted here, so a dead keychain read would
        # get retried every single minute forever. Back off like any other failure.
        cache["next_fetch_after"] = now + BACKOFF_ERROR
        cache["error_backoff"] = True
        save_cache(cache)
        log_event(f"no token (force={force}): {err or 'empty token'}")
        if data:
            return data, {"fetched_at": fetched_at, "note": "token unavailable — cached"}
        return None, {"short": "Claude: no token", "detail": err or "no access token in keychain"}

    try:
        fresh = fetch_usage(token)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # The endpoint's rate-limit window (~44 min observed) is much longer
            # than a blind 15-min backoff, so blind retries land inside the still-
            # active window and just 429 again — for hours during heavy use. Honor
            # the server's Retry-After instead; small jitter so the widget and the
            # app don't re-collide right at the window's end.
            try:
                retry_after = int(e.headers.get("Retry-After"))
            except (TypeError, ValueError):
                retry_after = None
            if retry_after is not None and retry_after >= 0:
                wait = min(retry_after, BACKOFF_429_MAX) + random.randint(5, 30)
            else:
                wait = BACKOFF_429 + random.randint(0, 120)
            cache["next_fetch_after"] = now + wait
            cache["error_backoff"] = True
            save_cache(cache)
            log_event(f"429 (force={force}, retry_after={retry_after}); "
                      f"next fetch at {cache['next_fetch_after']:.0f}")
            retry = fmt_retry(cache["next_fetch_after"], now)
            if data:
                # Say WHEN a retry can work — a force click during the backoff
                # otherwise looks like it did nothing.
                return data, {"fetched_at": fetched_at,
                              "note": f"rate-limited — retry {retry}"}
            # No cache yet: the backoff above is persisted, so the 1-minute refresh
            # won't hammer the endpoint. Show a calm waiting state, not an error.
            return None, {"short": "Claude usage…", "soft": True,
                          "detail": f"Endpoint rate-limited; no reading cached yet. "
                                    f"Auto-retries {retry} (Force can't beat the limit)."}
        # Non-429 failures previously left next_fetch_after untouched, so they'd get
        # retried every single minute with no backoff at all. Give them one too.
        cache["next_fetch_after"] = now + BACKOFF_ERROR
        cache["error_backoff"] = True
        save_cache(cache)
        log_event(f"HTTP {e.code} (force={force}); next fetch at {cache['next_fetch_after']:.0f}")
        if e.code in (401, 403):
            # The plugin only reads the access token; it can't safely rotate it
            # (Anthropic rotates refresh tokens, so refreshing here would knock
            # Claude Code itself back to /login). Force update can't recover this
            # — only reopening Claude Code refreshes the token. Say so plainly.
            if data:
                return data, {"fetched_at": fetched_at,
                              "note": "auth expired — reopen Claude Code (Force can't refresh)"}
            return None, {"short": "Claude: auth",
                          "detail": f"{e.code} — token expired. Reopen Claude Code to refresh it; "
                                    f"Force update can't (it can't rotate the token)."}
        if data:
            return data, {"fetched_at": fetched_at, "note": f"HTTP {e.code} — showing cached"}
        return None, {"short": "Claude: http", "detail": f"HTTP {e.code}"}
    except Exception as e:
        cache["next_fetch_after"] = now + BACKOFF_ERROR
        cache["error_backoff"] = True
        save_cache(cache)
        log_event(f"error (force={force}): {e}; next fetch at {cache['next_fetch_after']:.0f}")
        if data:
            return data, {"fetched_at": fetched_at,
                          "note": f"offline — retry {fmt_retry(cache['next_fetch_after'], now)}"}
        return None, {"short": "Claude: offline", "detail": str(e)}

    save_cache({"data": fresh, "fetched_at": now, "next_fetch_after": now + FETCH_INTERVAL,
                "error_backoff": False})
    log_event(f"ok (force={force})")
    return fresh, {"fetched_at": now, "note": None}

# ---- status page -------------------------------------------------------------
# Statuspage indicator (overall) and component-status vocabularies.
STATUS_BADGE = {
    "none": "",          # all operational
    "minor": "🟡",
    "major": "🟠",
    "critical": "🔴",
    "maintenance": "🔧",
}
COMPONENT_LABEL = {
    "operational": "operational",
    "degraded_performance": "degraded",
    "partial_outage": "partial outage",
    "major_outage": "major outage",
    "under_maintenance": "maintenance",
}

def status_color(indicator):
    return {
        "none": "#1a8a3a,#30d158",      # green
        "minor": "#8a6d00,#ffd60a",     # amber
        "major": "#b25000,#ff9f0a",     # orange
        "critical": "#c00000,#ff453a",  # red
        "maintenance": GRAY,
    }.get(indicator, GRAY)

def fetch_status():
    req = urllib.request.Request(
        STATUS_ENDPOINT, headers={"User-Agent": "claude-usage-swiftbar"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

def get_status(force=False):
    """Return a compact status dict (or None). Never raises; falls back to cache.

    {indicator, description, issues: [{name, status}, ...], fetched_at}
    """
    now = time.time()
    cache = {}
    try:
        with open(STATUS_CACHE_PATH) as f:
            cache = json.load(f)
    except Exception:
        pass

    cached = cache.get("data")
    fetched_at = cache.get("fetched_at", 0)
    if cached and not force and now - fetched_at < STATUS_INTERVAL:
        return cached

    try:
        raw = fetch_status()
    except Exception:
        return cached  # may be None — caller guards on truthiness

    status = raw.get("status") or {}
    # Only surface non-operational, non-group components as "issues".
    issues = [
        {"name": c.get("name"), "status": c.get("status")}
        for c in (raw.get("components") or [])
        if c.get("status") and c.get("status") != "operational" and not c.get("group")
    ]
    out = {
        "indicator": status.get("indicator", "none"),
        "description": status.get("description", ""),
        "issues": issues,
        "fetched_at": now,
    }
    try:
        os.makedirs(os.path.dirname(STATUS_CACHE_PATH), exist_ok=True)
        tmp = STATUS_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"data": out, "fetched_at": now}, f)
        os.replace(tmp, STATUS_CACHE_PATH)
    except Exception:
        pass
    return out

def render_status(status):
    """Dropdown section for status.claude.com health."""
    print(SEP)
    if not status:
        line("Status: unavailable", size=12, color=GRAY)
        line("Open status.claude.com", href="https://status.claude.com", size=11)
        return
    ind = status.get("indicator", "none")
    desc = status.get("description") or "Status unknown"
    if ind == "none":
        line(f"✓ {desc}", size=12, color=status_color(ind))
    else:
        line(f"{STATUS_BADGE.get(ind, '⚠️')} {desc}", size=12, color=status_color(ind))
        for it in status.get("issues", []):
            lbl = COMPONENT_LABEL.get(it["status"], it["status"])
            line(f"   {it['name']}: {lbl}", size=11, color=GRAY)
    line("Open status.claude.com", href="https://status.claude.com", size=11)

# ---- render ------------------------------------------------------------------
def render_error(short, detail, soft=False, badge=""):
    prefix = f"{badge} " if badge else ""
    if soft:
        # Transient / waiting state (e.g. cold-start 429): stay neutral, no warning.
        line(f"{prefix}⏳ {short}", color=GRAY, size=13)
    else:
        line(f"{prefix}⚠️ {short}", color="#b25000,#ff9f0a", size=13)
    print(SEP)
    line(detail, color=GRAY)
    if not soft:
        line("Open Claude Code to refresh the token", color=GRAY)
    line(f"Force update | bash={PLUGIN_PATH} param1=--force terminal=false refresh=true")

def pct(block):
    if not block or block.get("utilization") is None:
        return None
    return float(block["utilization"])

def main():
    force = "--force" in sys.argv[1:]
    data, meta = get_usage(force=force)
    status = get_status(force=force)
    badge = STATUS_BADGE.get((status or {}).get("indicator", "none"), "")
    if data is None:
        render_error(meta["short"], meta["detail"], soft=meta.get("soft", False), badge=badge)
        render_status(status)
        return

    five = pct(data.get("five_hour"))
    week = pct(data.get("seven_day"))
    opus = pct(data.get("seven_day_opus"))
    sonnet = pct(data.get("seven_day_sonnet"))

    five_s = f"{five:.0f}%" if five is not None else "—"
    week_s = f"{week:.0f}%" if week is not None else "—"

    # menu bar: neutral text for normal usage; color only when near a limit.
    # Session (5h) reset countdown shown inline, recomputed locally each minute.
    cd = fmt_countdown((data.get("five_hour") or {}).get("resets_at"))
    reset_s = f" ↻{cd}" if cd else ""
    prefix = f"{badge} " if badge else ""
    menu = f"{prefix}⏱ {five_s}{reset_s} · :chart.bar: {week_s}"
    binding = max([x for x in (five, week) if x is not None], default=0)
    if badge:
        # An active outage/maintenance takes visual precedence over usage color.
        line(menu, size=13, color=status_color(status.get("indicator")))
    elif binding >= 75:
        line(menu, size=13, color=color_for(binding))
    else:
        line(menu, size=13)

    print(SEP)
    line("Claude usage", size=12, color=GRAY)
    line(f"Session (5h):  {five_s}", color=color_for(five),
         tooltip=f"resets {fmt_reset((data.get('five_hour') or {}).get('resets_at'))}")
    line(f"   resets {fmt_reset((data.get('five_hour') or {}).get('resets_at'))}",
         size=11, color=GRAY)
    line(f"Weekly (7d):  {week_s}", color=color_for(week))
    line(f"   resets {fmt_reset((data.get('seven_day') or {}).get('resets_at'))}",
         size=11, color=GRAY)
    if opus is not None:
        line(f"Weekly Opus:  {opus:.0f}%", color=color_for(opus))
    if sonnet is not None:
        line(f"Weekly Sonnet:  {sonnet:.0f}%", color=color_for(sonnet))

    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled"):
        eu = extra.get("utilization")
        line(f"Extra usage:  {eu:.0f}%" if eu is not None else "Extra usage: on",
             color=color_for(eu))

    render_status(status)

    print(SEP)
    age = (time.time() - meta["fetched_at"]) if meta.get("fetched_at") else None
    line(f"Updated {fmt_age(age)}", size=11, color=GRAY)
    if meta.get("note"):
        line(meta["note"], size=11, color="#8a6d00,#ffd60a")

    print(SEP)
    line(f"Force update | bash={PLUGIN_PATH} param1=--force terminal=false refresh=true")
    line("Open claude.ai/settings/usage | href=https://claude.ai/settings/usage")

if __name__ == "__main__":
    main()
