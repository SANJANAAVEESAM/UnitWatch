#!/usr/bin/env python3
"""
Watches three Bellevue apartment communities and says when something new opens.

Run it every five minutes. It compares what each community offers now against
what it offered last time and pushes a notification only for what changed, so a
quiet market is silent.

The two landlords give away very different amounts, and the script is honest
about that rather than pretending otherwise:

  Avalon    — the page carries the whole unit list as JSON: unit number, floor
              plan, beds, size, price and the exact date it is free. Alerts can
              therefore be filtered to a move-in window, and say everything.

Layouts on EXCLUDED_LAYOUTS are dropped before any of that, so they never
reach a notification.

  Essex     — sits behind Vercel's bot check, so it needs a real browser engine
              to reach anything. Behind that it has a proper availability API,
              found by logging what its own page requests, and that returns a
              per-unit availability_date. So Essex is filtered by date too.

              Its date parameters turn out to affect pricing, not which units
              come back: asking for December returns exactly what asking for
              October does. Essex simply does not list further than about sixty
              days out, so a unit free in October appears only once someone
              gives notice. Which is the whole reason to keep watching.

No third-party packages. Fetching uses the Chrome already on this Mac, because
both sites need JavaScript and one of them needs a browser fingerprint.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def _topic() -> str:
    """The ntfy topic, from the environment or an untracked file beside this one.

    Deliberately not a literal in the source. The topic string is the only
    secret here — anyone holding it can read every alert or post fakes — and
    this file is meant to live in a repository.
    """
    env = os.environ.get("UNITWATCH_TOPIC")
    if env:
        return env.strip()
    local = Path(__file__).with_name("topic.txt")
    if local.exists():
        return local.read_text().strip()
    raise SystemExit(
        "No ntfy topic. Either set UNITWATCH_TOPIC or write one line into "
        f"{local}"
    )


NTFY_TOPIC = _topic()
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

# Unattended for months, silence is ambiguous: it means either nothing has come
# up, or the scraper broke and nobody noticed. These make the difference audible.
FAILS_BEFORE_ALERT = 3          # consecutive bad runs before crying wolf
HEARTBEAT_HOUR = 8              # local hour for the daily "all well" report
GAP_ALERT_MINUTES = 25          # report a silence longer than this on waking


def _ping_url() -> str | None:
    """A dead-man's switch, if one is configured.

    Everything else here is self-reported, and a process cannot report that it
    has stopped. If the machine is off, the job unloaded, the disk full or the
    script broken on import, no alert of ours will ever fire and the resulting
    silence is indistinguishable from a quiet market.

    So each completed run pings an outside service, and that service is what
    shouts when the pings stop. healthchecks.io does this on a free tier;
    anything with the same shape works.

    Optional. Without it everything else still runs, just without the safety
    net — so the absence of a ping URL is not an error.
    """
    env = os.environ.get("UNITWATCH_PING")
    if env:
        return env.strip()
    local = Path(__file__).with_name("ping.txt")
    if local.exists():
        text = local.read_text().strip()
        return text or None
    return None


PING_URL = _ping_url()


def ping_alive() -> None:
    """Tell the outside world this run finished."""
    if not PING_URL:
        return
    try:
        r = subprocess.run(
            ["curl", "-sS", "-m", "15", "-o", "/dev/null", "-w", "%{http_code}", PING_URL],
            capture_output=True, text=True, timeout=25)
        code = (r.stdout or "").strip()
        # Logged either way. A silent success is indistinguishable from a ping
        # that never happened, and this is the one alarm with no other witness.
        if code == "200":
            log("  dead-man ping ok")
        else:
            log(f"  ! dead-man ping returned {code or 'nothing'}")
    except Exception as exc:  # noqa: BLE001
        log(f"  ! dead-man ping failed: {exc}")

# Only Avalon can be filtered by date; see the module docstring.
WINDOW_START = date(2026, 10, 10)
WINDOW_END = date(2026, 10, 17)

# Layouts you do not want to hear about, as (bedrooms, bathrooms). Applied to
# both landlords: to individual units at Avalon, and to whole floorplans at
# Essex, which is the only granularity Essex offers.
EXCLUDED_LAYOUTS = {(0.0, 1.0), (2.0, 2.0), (3.0, 2.0)}


def wanted(beds, baths) -> bool:
    """False for a layout on the exclusion list.

    Bedroom and bathroom counts arrive as ints from one site and floats from
    the other, and half-baths exist, so both are normalised to float before
    comparing — (2, 2) and (2.0, 2.0) must not be treated as different layouts.
    """
    try:
        return (float(beds), float(baths)) not in EXCLUDED_LAYOUTS
    except (TypeError, ValueError):
        return True  # unknown layout: let it through rather than hide it

STATE_PATH = Path(__file__).with_name("state.json")
LOG_PATH = Path(__file__).with_name("watch.log")

def _find_chrome() -> str:
    """Whatever browser this machine has.

    Written so the same file runs on the Mac and on a Linux host without edits —
    the only thing that differs between them is where Chrome lives and what it
    is called.
    """
    from shutil import which
    env = os.environ.get("UNITWATCH_CHROME")
    if env:
        return env
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(mac):
        return mac
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome"):
        found = which(name)
        if found:
            return found
    return mac  # let it fail loudly rather than silently doing nothing


CHROME = _find_chrome()

AVALON = {
    "key": "avalon-meydenbauer",
    "name": "Avalon Meydenbauer",
    "url": "https://www.avaloncommunities.com/washington/bellevue-apartments/avalon-meydenbauer/",
}
ESSEX = [
    {
        "key": "essex-belcarra",
        "name": "Essex Belcarra",
        "property_id": "510860",
        "url": "https://www.essexapartmenthomes.com/apartments/bellevue/belcarra/floor-plans-and-pricing",
    },
    {
        "key": "essex-bellcentre",
        "name": "Essex Bellcentre",
        "property_id": "510867",
        "url": "https://www.essexapartmenthomes.com/apartments/bellevue/bellcentre/floor-plans-and-pricing",
    },
]


def essex_api(property_id: str) -> str:
    """Essex's own availability endpoint.

    The range is asked for generously even though it makes no difference to
    which units come back — Essex lists about sixty days ahead regardless. It
    costs nothing and means the day they extend that horizon, this sees it.
    """
    start = date.today().isoformat()
    end = date.today().replace(year=date.today().year + 1).isoformat()
    return (f"https://www.essexapartmenthomes.com/api/properties/{property_id}"
            f"/availability?start_date={start}&end_date={end}&format=spa")


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    with LOG_PATH.open("a") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def fetch_plain(url: str) -> str:
    """Straight HTTP. Enough for Avalon, and about seventy times faster.

    Avalon server-renders its unit list into the page, so there is nothing to
    wait for — curl returns the full payload in well under a second. Reaching
    for a browser here was a habit, not a requirement, and it is the difference
    between this needing a 2GB host and running anywhere.
    """
    out = subprocess.run(
        ["curl", "-sS", "--compressed", "-L", "--max-time", "40", "-A", UA, url],
        capture_output=True, text=True, timeout=60,
    )
    return out.stdout


def fetch_browser(url: str, budget_ms: int = 30000) -> str:
    """The page as a browser leaves it.

    Only Essex needs this. It sits behind Vercel's bot check, which answers
    anything without a browser fingerprint with 429 forever — no combination of
    headers or cookies gets past it.
    """
    out = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--no-sandbox",
         # Flags for small hosts. /dev/shm is tiny on most cheap VMs and
         # containers, and Chrome will crash rather than fall back on its own;
         # the rest turn off machinery a scraper never uses. Together they take
         # a page load from roughly half a gigabyte to something a 1GB free-tier
         # box can serve without swapping itself to death.
         "--disable-dev-shm-usage", "--disable-extensions",
         "--disable-background-networking", "--disable-sync",
         "--disable-default-apps", "--no-first-run", "--mute-audio",
         "--blink-settings=imagesEnabled=false",
         f"--virtual-time-budget={budget_ms}", "--dump-dom", url],
        capture_output=True, text=True, timeout=budget_ms / 1000 + 45,
    )
    return out.stdout


def balanced(text: str, start: int, opener: str, closer: str) -> str | None:
    """The complete bracketed span beginning at `start`, quotes respected."""
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return text[start:j + 1]
    return None


# --------------------------------------------------------------------------
# Avalon — full unit detail, dates included
# --------------------------------------------------------------------------


def parse_avalon(html: str) -> list[dict]:
    units: list[dict] = []
    seen: set[str] = set()
    marker = '{"unitId":'
    at = 0
    while True:
        i = html.find(marker, at)
        if i == -1:
            break
        at = i + 1
        chunk = balanced(html, i, "{", "}")
        if not chunk:
            continue
        try:
            u = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        uid = u.get("unitId")
        if not uid or uid in seen:
            continue
        seen.add(uid)

        raw = u.get("availableDateUnfurnished") or u.get("availableDateFurnished")
        try:
            avail = datetime.fromisoformat(raw.replace("Z", "+00:00")).date() if raw else None
        except ValueError:
            avail = None
        price = ((u.get("startingAtPricesUnfurnished") or {}).get("prices") or {})

        units.append({
            "id": uid,
            "label": uid.split("-")[-1],
            "plan": (u.get("floorPlan") or {}).get("name"),
            "beds": u.get("bedroomNumber"),
            "baths": u.get("bathroomNumber"),
            "sqft": u.get("squareFeet"),
            "price": price.get("totalPrice") or price.get("price"),
            "available": avail.isoformat() if avail else None,
            "url": u.get("url"),
        })
    return units


def in_window(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return False
    return WINDOW_START <= d <= WINDOW_END


# --------------------------------------------------------------------------
# Essex — floorplan counts only
# --------------------------------------------------------------------------


def parse_essex(dom: str) -> list[dict]:
    """Units from the availability API.

    Chrome renders a JSON response inside a <pre>, with the entities escaped,
    so it has to be dug back out rather than parsed straight.
    """
    m = re.search(r"<pre[^>]*>(.*?)</pre>", dom, re.S)
    body = (m.group(1) if m else dom)
    body = (body.replace("&quot;", '"').replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")).strip()
    try:
        units = json.loads(body)["result"]["units"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []

    out = []
    for u in units:
        raw = u.get("availability_date") or ""
        out.append({
            "id": str(u.get("unit_id")),
            "label": u.get("name"),
            "plan": u.get("floorplan_name"),
            "beds": u.get("beds"),
            "baths": u.get("baths"),
            "sqft": u.get("sqft"),
            "price": u.get("minimum_rent"),
            "available": raw[:10] or None,
        })
    return out


# --------------------------------------------------------------------------
# Notifying
# --------------------------------------------------------------------------


def notify(title: str, body: str, url: str | None = None, priority: str = "default") -> None:
    """Push through curl rather than urllib.

    The Python shipped in /Library/Frameworks has no root certificate bundle of
    its own, so urllib raises CERTIFICATE_VERIFY_FAILED against ntfy while every
    other tool on the machine is fine. curl uses the system store and needs no
    install, which matters for something meant to run unattended for months.
    """
    cmd = [
        "curl", "-sS", "--max-time", "20",
        "-H", f"Title: {title}",
        "-H", f"Priority: {priority}",
        "-H", "Tags: house",
    ]
    if url:
        cmd += ["-H", f"Click: {url}"]
    cmd += ["-d", body, NTFY_URL]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log(f"  ! could not push: {r.stderr.strip()[:200]}")
    except Exception as exc:  # noqa: BLE001 — a failed push must not kill the run
        log(f"  ! could not push: {exc}")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            log("  ! state file unreadable, starting over")
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


# --------------------------------------------------------------------------


def health(state: dict, key: str, name: str, ok: bool, detail: str = "") -> None:
    """Count consecutive bad runs per source, and speak up once when it sticks.

    Alerts on the transition only, not every run — a broken source should be one
    notification, not one every five minutes for a week.
    """
    h = state.setdefault("_health", {})
    entry = h.setdefault(key, {"fails": 0, "alerted": False})
    if ok:
        if entry["alerted"]:
            notify(f"{name} is readable again", "Back to normal; watching resumed.")
        h[key] = {"fails": 0, "alerted": False}
        return
    entry["fails"] += 1
    if entry["fails"] >= FAILS_BEFORE_ALERT and not entry["alerted"]:
        entry["alerted"] = True
        notify(
            f"Unit watch: {name} cannot be read",
            f"{entry['fails']} checks in a row have failed.\n{detail}\n"
            "Availability for this community is unknown until it recovers — "
            "silence is not the same as nothing available.",
            priority="high",
        )


def maybe_heartbeat(state: dict) -> None:
    """Once a day, at a set hour, say everything is well and what it sees.

    Pinned to a clock hour rather than "24 hours since the last one", which
    drifts a little further into the day every day until it arrives at an
    unhelpful time. Sent by the first run at or after the hour, so if the
    machine was off at eight it still reports when it wakes rather than
    skipping the day.
    """
    now = datetime.now()
    today = now.date().isoformat()
    if now.hour < HEARTBEAT_HOUR or state.get("_heartbeat_day") == today:
        return
    state["_heartbeat_day"] = today

    lines = []
    for key, label in (("avalon-meydenbauer", "Avalon Meydenbauer"),
                       ("essex-belcarra", "Essex Belcarra"),
                       ("essex-bellcentre", "Essex Bellcentre")):
        d = state.get(key) or {}
        hit = len(d.get("window_ids", []))
        far = d.get("furthest")
        # The horizon is the useful number: your window is only reachable once
        # a community starts listing that far ahead.
        tail = f", listed to {far}" if far and far != "?" else ""
        lines.append(f"{label}: {d.get('total', 0)} units, {hit} matching{tail}")

    bad = [k for k, v in (state.get("_health") or {}).items() if v.get("alerted")]
    status = "All three readable." if not bad else f"PROBLEM with: {', '.join(bad)}"

    notify(
        "Watcher healthy",
        status + "\n\n" + "\n".join(lines) +
        f"\n\nWindow: {WINDOW_START} to {WINDOW_END}"
        "\nExcluded: studios, 2bd/2ba, 3bd/2ba",
        priority="low",
    )


def report_gap(state: dict) -> None:
    """Say so if a long time passed since the last check.

    A watcher cannot warn you while it is not running, and on a laptop that is
    the likeliest failure by far — the lid closes and everything simply stops.
    Silence then looks exactly like silence from a watcher that has found
    nothing, which is the one thing this must never be confused with. So the
    first run after a gap reports the gap.
    """
    last = state.get("_last_run")
    now = datetime.now()
    state["_last_run"] = now.isoformat(timespec="seconds")
    if not last:
        return
    try:
        gap = (now - datetime.fromisoformat(last)).total_seconds() / 60
    except ValueError:
        return
    if gap < GAP_ALERT_MINUTES:
        return
    hours = gap / 60
    span = f"{gap:.0f} minutes" if hours < 1.5 else f"{hours:.1f} hours"
    log(f"gap since last check: {span}")
    notify(
        f"Unit watch was down for {span}",
        "Nothing was checked in that time, so a unit could have been listed "
        "and taken without you hearing about it.\n"
        "Watching again now.",
        priority="high",
    )


def main() -> int:
    state = load_state()
    first_run = not state
    changes: list[str] = []
    if not first_run:
        report_gap(state)
    else:
        state["_last_run"] = datetime.now().isoformat(timespec="seconds")

    # ---- Avalon -----------------------------------------------------------
    try:
        html = fetch_plain(AVALON["url"])
        units = parse_avalon(html)
        if not units:
            log(f"{AVALON['name']}: parsed 0 units — page shape may have changed")
            health(state, AVALON["key"], AVALON["name"], False,
                   "The page loaded but no units could be parsed.")
        else:
            health(state, AVALON["key"], AVALON["name"], True)
            in_win = [u for u in units if in_window(u["available"])]
            matching = [u for u in in_win if wanted(u["beds"], u["baths"])]
            skipped = len(in_win) - len(matching)
            log(f"{AVALON['name']}: {len(units)} units, {len(in_win)} in window"
                + (f", {skipped} excluded by layout" if skipped else ""))

            prev = set(state.get(AVALON["key"], {}).get("window_ids", []))
            now_ids = {u["id"] for u in matching}
            fresh = [u for u in matching if u["id"] not in prev]

            if fresh and not first_run:
                for u in fresh:
                    changes.append(f"{AVALON['name']} · {u['label']}")
                    notify(
                        f"{AVALON['name']} — {u['label']} available {u['available']}",
                        f"{u['plan']} · {u['beds']}bd/{u['baths']}ba · {u['sqft']} sqft\n"
                        f"${u['price']}/mo · move-in {u['available']}",
                        url=u["url"],
                        priority="high",
                    )
            # The horizon matters as much as the count: a window in October is
            # unreachable until a community starts listing that far ahead.
            furthest = max((u["available"] for u in units if u["available"]), default="?")
            state[AVALON["key"]] = {
                "window_ids": sorted(now_ids),
                "total": len(units),
                "furthest": furthest,
            }
    except Exception as exc:  # noqa: BLE001
        log(f"{AVALON['name']}: FAILED — {exc}")
        health(state, AVALON["key"], AVALON["name"], False, str(exc)[:200])

    # ---- Essex ------------------------------------------------------------
    for prop in ESSEX:
        try:
            dom = fetch_browser(essex_api(prop["property_id"]), budget_ms=20000)
            if "Security Checkpoint" in dom:
                log(f"{prop['name']}: blocked by the bot check this run")
                health(state, prop["key"], prop["name"], False,
                       "Blocked by the site's bot check.")
                continue
            units = parse_essex(dom)
            if not units:
                log(f"{prop['name']}: API returned no units — shape may have changed")
                health(state, prop["key"], prop["name"], False,
                       "The availability API returned nothing parseable.")
                continue
            health(state, prop["key"], prop["name"], True)

            in_win = [u for u in units if in_window(u["available"])]
            matching = [u for u in in_win if wanted(u["beds"], u["baths"])]
            furthest = max((u["available"] for u in units if u["available"]), default="?")
            log(f"{prop['name']}: {len(units)} units, {len(matching)} in window "
                f"(listed out to {furthest})")

            prev = set(state.get(prop["key"], {}).get("window_ids", []))
            fresh = [u for u in matching if u["id"] not in prev]
            if fresh and not first_run:
                for u in fresh:
                    changes.append(f"{prop['name']} · {u['label']}")
                    notify(
                        f"{prop['name']} — {u['label']} available {u['available']}",
                        f"{u['plan']} · {u['beds']}bd/{u['baths']}ba · {u['sqft']} sqft\n"
                        f"from ${u['price']}/mo · move-in {u['available']}",
                        url=prop["url"],
                        priority="high",
                    )
            state[prop["key"]] = {
                "window_ids": sorted(u["id"] for u in matching),
                "total": len(units),
                "furthest": furthest,
            }
        except Exception as exc:  # noqa: BLE001
            log(f"{prop['name']}: FAILED — {exc}")
            health(state, prop["key"], prop["name"], False, str(exc)[:200])

    save_state(state)

    if first_run:
        watched = sum(v.get("total", 0) for v in state.values())
        log(f"first run — baseline saved, {watched} units being watched (no alerts sent)")
        notify(
            "Unit watch is running",
            f"Watching 3 Bellevue communities, {watched} units on the board.\n"
            f"Avalon alerts are filtered to {WINDOW_START} – {WINDOW_END}.\n"
            "You will hear from me only when something new opens.",
        )
    elif changes:
        log(f"pushed {len(changes)} alert(s): {', '.join(changes)}")
    else:
        log("nothing new")

    maybe_heartbeat(state)
    save_state(state)

    # Last thing, on every path: the run finished. Whatever it found, the
    # outside world now knows this is still alive.
    ping_alive()
    return 0


if __name__ == "__main__":
    sys.exit(main())
