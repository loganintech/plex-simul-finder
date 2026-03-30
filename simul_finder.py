#!/usr/bin/env python3
"""
Plex account sharing detector using Tautulli API.

Finds users streaming from multiple devices/locations, with per-device
usage breakdowns and "teleportation" detection (impossible travel between
geographically distant sessions).
"""

import argparse
import math
import os
import sys
from datetime import datetime, timedelta

import requests

# ---------------------------------------------------------------------------
# Tautulli API helpers
# ---------------------------------------------------------------------------

def tautulli_api(base_url: str, api_key: str, cmd: str, **params) -> dict:
    params = {k: v for k, v in params.items() if v is not None}
    resp = requests.get(
        f"{base_url}/api/v2",
        params={"apikey": api_key, "cmd": cmd, **params},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("response", {}).get("result") != "success":
        msg = data.get("response", {}).get("message", "unknown error")
        raise RuntimeError(f"Tautulli API error ({cmd}): {msg}")
    return data["response"]["data"]


def get_users(base_url: str, api_key: str) -> list[dict]:
    data = tautulli_api(base_url, api_key, "get_users_table", length=500)
    return data.get("data", [])


def get_history(base_url: str, api_key: str, user_id: int, after: str, length: int = 1000) -> list[dict]:
    data = tautulli_api(
        base_url, api_key, "get_history",
        user_id=user_id, after=after, length=length,
    )
    return data.get("data", [])


def get_geoip(base_url: str, api_key: str, ip: str) -> dict:
    return tautulli_api(base_url, api_key, "get_geoip_lookup", ip_address=ip)


# ---------------------------------------------------------------------------
# Geo / teleportation helpers
# ---------------------------------------------------------------------------

def is_private_ip(ip: str) -> bool:
    return ip.startswith(("10.", "192.168.", "172.16.", "127.", "0."))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ~900 km/h is the fastest commercial flight
MAX_TRAVEL_SPEED_KMH = 900


def find_teleportations(
    history: list[dict],
    ip_geo_cache: dict[str, dict],
) -> list[dict]:
    """Find session transitions where the user moved faster than physically possible."""
    # Build a timeline: (timestamp, ip, machine_id, title) sorted by time
    events = []
    for rec in history:
        ip = rec.get("ip_address", "")
        if not ip or is_private_ip(ip):
            continue
        geo = ip_geo_cache.get(ip)
        if not geo or not geo.get("latitude") or not geo.get("longitude"):
            continue
        started = rec.get("started")
        stopped = rec.get("stopped")
        if started:
            events.append({
                "time": started,
                "ip": ip,
                "lat": float(geo["latitude"]),
                "lon": float(geo["longitude"]),
                "location": geo.get("city", "?"),
                "region": geo.get("region", ""),
                "machine_id": rec.get("machine_id", "?"),
                "platform": rec.get("platform", "?"),
                "title": rec.get("full_title") or rec.get("title", "?"),
                "event": "start",
            })
        if stopped:
            events.append({
                "time": stopped,
                "ip": ip,
                "lat": float(geo["latitude"]),
                "lon": float(geo["longitude"]),
                "location": geo.get("city", "?"),
                "region": geo.get("region", ""),
                "machine_id": rec.get("machine_id", "?"),
                "platform": rec.get("platform", "?"),
                "title": rec.get("full_title") or rec.get("title", "?"),
                "event": "stop",
            })

    events.sort(key=lambda e: e["time"])

    teleportations = []
    for i in range(len(events) - 1):
        a, b = events[i], events[i + 1]
        if a["ip"] == b["ip"]:
            continue
        dist_km = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        if dist_km < 50:  # same metro area, ignore
            continue
        time_diff_h = (b["time"] - a["time"]) / 3600
        if time_diff_h <= 0:
            time_diff_h = 0.001  # concurrent
        speed_kmh = dist_km / time_diff_h
        if speed_kmh > MAX_TRAVEL_SPEED_KMH:
            teleportations.append({
                "from": a,
                "to": b,
                "dist_km": dist_km,
                "time_diff_h": time_diff_h,
                "speed_kmh": speed_kmh,
            })

    return teleportations


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def same_network(ip_a: str, ip_b: str) -> bool:
    """True if both IPs are on the same network (both private = same household)."""
    if is_private_ip(ip_a) and is_private_ip(ip_b):
        return True
    return ip_a == ip_b


def effective_end(session: dict) -> int:
    """Use started + play_duration as the end time, since 'stopped' can be hours
    after playback ended (e.g. fell asleep, left app open)."""
    started = session["started"]
    duration = session.get("play_duration") or session.get("duration") or 0
    if duration > 0:
        return started + duration
    return session.get("stopped") or started


def find_concurrent_sessions(history: list[dict]) -> list[tuple[dict, dict]]:
    """Find pairs of history records that overlap in time from different devices and networks."""
    overlaps = []
    for i, a in enumerate(history):
        if not a.get("started"):
            continue
        a_end = effective_end(a)
        for b in history[i + 1:]:
            if not b.get("started"):
                continue
            b_end = effective_end(b)
            if a.get("machine_id") == b.get("machine_id"):
                continue
            if same_network(a.get("ip_address", ""), b.get("ip_address", "")):
                continue
            if a["started"] < b_end and b["started"] < a_end:
                overlaps.append((a, b))
    return overlaps


def resolve_ips(base_url: str, api_key: str, ips: set[str]) -> dict[str, dict]:
    """Geo-resolve a set of IPs, returning {ip: geo_dict}."""
    cache: dict[str, dict] = {}
    for ip in ips:
        if is_private_ip(ip):
            cache[ip] = {"city": "LAN", "region": "", "country": ""}
            continue
        try:
            cache[ip] = get_geoip(base_url, api_key, ip)
        except Exception:
            cache[ip] = {}
    return cache


def fmt_location(geo: dict) -> str:
    if not geo:
        return "Unknown"
    parts = [geo.get("city"), geo.get("region"), geo.get("country")]
    return ", ".join(p for p in parts if p) or "Unknown"


def analyze_user(base_url: str, api_key: str, user: dict, after: str, geo: bool) -> dict | None:
    uid = user["user_id"]
    name = user.get("friendly_name") or user.get("username") or str(uid)

    history = get_history(base_url, api_key, uid, after)
    if not history:
        return None

    # --- Device breakdown from history ---
    devices: dict[str, dict] = {}
    all_ips: set[str] = set()
    for rec in history:
        mid = rec.get("machine_id", "unknown")
        if mid not in devices:
            devices[mid] = {
                "machine_id": mid,
                "platform": rec.get("platform", "?"),
                "player": rec.get("player", "?"),
                "ips": set(),
                "plays": 0,
                "duration_sec": 0,
            }
        devices[mid]["plays"] += 1
        devices[mid]["duration_sec"] += rec.get("play_duration") or rec.get("duration") or 0
        ip = rec.get("ip_address")
        if ip:
            devices[mid]["ips"].add(ip)
            all_ips.add(ip)

    if len(devices) < 2:
        return None

    # --- Concurrent sessions ---
    overlaps = find_concurrent_sessions(history)
    overlap_pairs: set[tuple[str, str]] = set()
    for a, b in overlaps:
        pair = tuple(sorted([a.get("machine_id", "?"), b.get("machine_id", "?")]))
        overlap_pairs.add(pair)

    # --- Geo + teleportation (when --geo) ---
    ip_geo_cache: dict[str, dict] = {}
    ip_locations: dict[str, str] = {}
    teleportations: list[dict] = []
    if geo:
        ip_geo_cache = resolve_ips(base_url, api_key, all_ips)
        ip_locations = {ip: fmt_location(g) for ip, g in ip_geo_cache.items()}
        teleportations = find_teleportations(history, ip_geo_cache)

    # --- Scoring ---
    n_devices = len(devices)
    n_overlaps = len(overlaps)
    n_overlap_pairs = len(overlap_pairs)
    heavy_devices = sum(1 for d in devices.values() if d["plays"] > 5)

    score = (
        (n_devices - 1) * 10
        + n_overlap_pairs * 25
        + min(n_overlaps, 50) * 2
        + max(0, heavy_devices - 1) * 15
        + len(teleportations) * 30  # teleportation is a strong signal
    )

    return {
        "user_id": uid,
        "name": name,
        "history": history,
        "devices": devices,
        "n_devices": n_devices,
        "heavy_devices": heavy_devices,
        "total_plays": sum(d["plays"] for d in devices.values()),
        "total_duration_sec": sum(d["duration_sec"] for d in devices.values()),
        "n_overlaps": n_overlaps,
        "n_overlap_pairs": n_overlap_pairs,
        "teleportations": teleportations,
        "score": score,
        "ip_locations": ip_locations,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def fmt_duration(seconds: int) -> str:
    h, m = divmod(seconds // 60, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def fmt_timestamp(ts: int | float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def filter_flagged(results: list[dict], min_score: int, concurrent_only: bool) -> list[dict]:
    flagged = [r for r in results if r["score"] >= min_score]
    if concurrent_only:
        flagged = [r for r in flagged if r["n_overlaps"] > 0]
    flagged.sort(key=lambda r: r["score"], reverse=True)
    return flagged


def print_device_report(flagged: list[dict]):
    """Device-centric view: one row per device with aggregated stats."""
    print(f"\n{'='*78}")
    print(f" DEVICE BREAKDOWN  —  {len(flagged)} user(s) flagged")
    print(f"{'='*78}\n")

    for r in flagged:
        print(f"  {r['name']}  (user_id: {r['user_id']})")
        print(f"  Score: {r['score']}  |  Devices: {r['n_devices']}  |  "
              f"Heavy devices: {r['heavy_devices']}  |  "
              f"Concurrent sessions: {r['n_overlaps']}  |  "
              f"Concurrent device pairs: {r['n_overlap_pairs']}")
        print(f"  Total plays: {r['total_plays']}  |  "
              f"Total watch time: {fmt_duration(r['total_duration_sec'])}")
        print()

        sorted_devices = sorted(r["devices"].values(), key=lambda d: d["duration_sec"], reverse=True)
        print(f"    {'Platform':<16} {'Player':<24} {'Plays':>6} {'Watch Time':>11}  IPs")
        print(f"    {'-'*16} {'-'*24} {'-'*6} {'-'*11}  {'-'*30}")
        for d in sorted_devices:
            ips_display = []
            for ip in sorted(d["ips"]):
                loc = r["ip_locations"].get(ip)
                if loc:
                    ips_display.append(f"{ip} ({loc})")
                else:
                    ips_display.append(ip)
            ip_str = ", ".join(ips_display) if ips_display else "—"
            print(f"    {d['platform']:<16} {d['player']:<24} {d['plays']:>6} "
                  f"{fmt_duration(d['duration_sec']):>11}  {ip_str}")

        if r["teleportations"]:
            print(f"\n    TELEPORTATION DETECTED  ({len(r['teleportations'])} event(s)):")
            for t in r["teleportations"][:10]:
                f_ = t["from"]
                to = t["to"]
                print(f"      {fmt_timestamp(f_['time'])} {f_['location']}, {f_['region']}"
                      f"  -->  {fmt_timestamp(to['time'])} {to['location']}, {to['region']}")
                print(f"        {t['dist_km']:.0f} km in {t['time_diff_h']:.1f}h"
                      f"  ({t['speed_kmh']:.0f} km/h — max plausible: {MAX_TRAVEL_SPEED_KMH} km/h)")
                print(f"        Devices: {f_['platform']} -> {to['platform']}")

        print(f"\n{'-'*78}\n")


def print_timeline_report(flagged: list[dict]):
    """Chronological view: sessions listed by date with device/IP info and concurrency markers."""
    print(f"\n{'='*78}")
    print(f" SESSION TIMELINE  —  {len(flagged)} user(s) flagged")
    print(f"{'='*78}\n")

    for r in flagged:
        history = r.get("history", [])
        if not history:
            continue

        print(f"  {r['name']}  (user_id: {r['user_id']})  —  score: {r['score']}")
        print()

        # Sort sessions chronologically
        sessions = sorted(
            [s for s in history if s.get("started")],
            key=lambda s: s["started"],
        )

        # Pre-compute which sessions are concurrent with another from a different device + network
        concurrent_sessions: set[int] = set()
        for i, a in enumerate(sessions):
            a_end = effective_end(a)
            for j, b in enumerate(sessions[i + 1:], start=i + 1):
                b_end = effective_end(b)
                if a.get("machine_id") == b.get("machine_id"):
                    continue
                if same_network(a.get("ip_address", ""), b.get("ip_address", "")):
                    continue
                if a["started"] < b_end and b["started"] < a_end:
                    concurrent_sessions.add(i)
                    concurrent_sessions.add(j)

        if not concurrent_sessions:
            print(f"    (no concurrent sessions)")
            print(f"\n{'-'*78}\n")
            continue

        current_date = None
        for idx, s in enumerate(sessions):
            if idx not in concurrent_sessions:
                continue

            ts = datetime.fromtimestamp(s["started"])
            date_str = ts.strftime("%Y-%m-%d")
            time_str = ts.strftime("%H:%M")
            dur = s.get("play_duration") or s.get("duration") or 0
            end_ts = effective_end(s)
            end_str = datetime.fromtimestamp(end_ts).strftime("%H:%M")

            if date_str != current_date:
                current_date = date_str
                print(f"    {date_str}")

            ip = s.get("ip_address", "?")
            loc = r["ip_locations"].get(ip)
            ip_display = f"{ip} ({loc})" if loc else ip
            platform = s.get("platform", "?")
            player = s.get("player", "?")
            title = s.get("full_title") or s.get("title") or "?"
            if len(title) > 40:
                title = title[:37] + "..."

            print(f"      {time_str}-{end_str}  {fmt_duration(dur):>7}"
                  f"  {platform:<14} {player:<20} {ip_display}")
            print(f"        {title}")

        print(f"\n{'-'*78}\n")


def print_ip_report(user_ip_stats: list[dict], ip_locations: dict[str, str]):
    """Per-user breakdown of usage by IP, sorted by watch time."""
    print(f"\n{'='*78}")
    print(f" IP USAGE BY USER  —  {len(user_ip_stats)} user(s)")
    print(f"{'='*78}\n")

    for u in user_ip_stats:
        print(f"  {u['name']}  (user_id: {u['user_id']})")
        total_dur = sum(ip["duration_sec"] for ip in u["ips"].values())
        print(f"  Total: {u['total_plays']} plays, {fmt_duration(total_dur)} watch time, "
              f"{len(u['ips'])} unique IP(s)")
        print()

        sorted_ips = sorted(u["ips"].values(), key=lambda x: x["duration_sec"], reverse=True)
        print(f"    {'IP':<22} {'Plays':>6} {'Watch Time':>11} {'Pct':>5}  Devices")
        print(f"    {'-'*22} {'-'*6} {'-'*11} {'-'*5}  {'-'*30}")
        for ip_info in sorted_ips:
            ip = ip_info["ip"]
            loc = ip_locations.get(ip)
            ip_display = f"{ip} ({loc})" if loc else ip
            pct = (ip_info["duration_sec"] / total_dur * 100) if total_dur else 0
            devices = ", ".join(sorted(ip_info["platforms"]))
            print(f"    {ip_display:<22} {ip_info['plays']:>6} "
                  f"{fmt_duration(ip_info['duration_sec']):>11} {pct:>4.0f}%  {devices}")

        print(f"\n{'-'*78}\n")


def print_report(results: list[dict], min_score: int, concurrent_only: bool, timeline: bool):
    flagged = filter_flagged(results, min_score, concurrent_only)

    if not flagged:
        print("No users flagged for potential account sharing.")
        return

    print_device_report(flagged)
    if timeline:
        print_timeline_report(flagged)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_base_url(raw: str) -> str:
    """Accept a hostname, host:port, or full URL and return a base URL."""
    if raw.startswith(("http://", "https://")):
        return raw.rstrip("/")
    return f"http://{raw.rstrip('/')}"


def main():
    parser = argparse.ArgumentParser(
        description="Detect Plex account sharing via Tautulli",
    )
    parser.add_argument("--host",
                        default=os.environ.get("TAUTULLI_HOST") or os.environ.get("TAUTULLI_URL"),
                        help="Tautulli host or URL (or set TAUTULLI_HOST env var)")
    parser.add_argument("--api-key",
                        default=os.environ.get("TAUTULLI_API_KEY"),
                        help="Tautulli API key (or set TAUTULLI_API_KEY env var)")
    parser.add_argument("--days", type=int, default=30,
                        help="Look back N days (default: 30)")
    parser.add_argument("--min-score", type=int, default=20,
                        help="Minimum suspicion score to flag (default: 20)")
    parser.add_argument("--geo", action="store_true",
                        help="Enable IP geolocation + teleportation detection (more API calls)")
    parser.add_argument("--concurrent-only", action="store_true",
                        help="Only show users with concurrent sessions from different devices")
    parser.add_argument("--timeline", action="store_true",
                        help="Show a chronological session timeline in addition to the device breakdown")
    parser.add_argument("--top-ips", action="store_true",
                        help="Show per-user IP usage breakdown ranked by watch time")
    parser.add_argument("--user", type=str, default=None,
                        help="Analyze a single user by friendly name")
    args = parser.parse_args()

    if not args.host or not args.api_key:
        print("Error: provide --host and --api-key (or set TAUTULLI_HOST / TAUTULLI_API_KEY).",
              file=sys.stderr)
        sys.exit(1)

    base_url = build_base_url(args.host)
    after = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

    print(f"Fetching users from Tautulli ({base_url})...")
    users = get_users(base_url, args.api_key)
    if args.user:
        users = [u for u in users if (u.get("friendly_name") or "").lower() == args.user.lower()
                 or (u.get("username") or "").lower() == args.user.lower()]
        if not users:
            print(f"User '{args.user}' not found.", file=sys.stderr)
            sys.exit(1)

    print(f"Analyzing {len(users)} user(s) over the last {args.days} days...")
    if args.geo:
        print("  (geo lookups enabled — teleportation detection active)")
    print()

    if args.top_ips:
        all_ips_seen: set[str] = set()
        user_ip_stats = []
        for i, user in enumerate(users):
            uid = user["user_id"]
            name = user.get("friendly_name") or user.get("username") or "?"
            print(f"  [{i+1}/{len(users)}] {name}...", end="", flush=True)
            try:
                history = get_history(base_url, args.api_key, uid, after)
            except Exception as e:
                print(f" error: {e}")
                continue
            if not history:
                print(" no history")
                continue

            ips: dict[str, dict] = {}
            for rec in history:
                ip = rec.get("ip_address", "")
                if not ip:
                    continue
                if ip not in ips:
                    ips[ip] = {"ip": ip, "plays": 0, "duration_sec": 0, "platforms": set()}
                ips[ip]["plays"] += 1
                ips[ip]["duration_sec"] += rec.get("play_duration") or rec.get("duration") or 0
                platform = rec.get("platform", "?")
                ips[ip]["platforms"].add(platform)
                all_ips_seen.add(ip)

            total_plays = sum(d["plays"] for d in ips.values())
            user_ip_stats.append({"user_id": uid, "name": name, "ips": ips, "total_plays": total_plays})
            print(f" {total_plays} plays, {len(ips)} IP(s)")

        ip_locations: dict[str, str] = {}
        if args.geo:
            print(f"\n  Resolving {len(all_ips_seen)} IP(s)...")
            geo_cache = resolve_ips(base_url, args.api_key, all_ips_seen)
            ip_locations = {ip: fmt_location(g) for ip, g in geo_cache.items()}

        # Sort users by total watch time descending
        user_ip_stats.sort(
            key=lambda u: sum(ip["duration_sec"] for ip in u["ips"].values()), reverse=True
        )
        print_ip_report(user_ip_stats, ip_locations)
        return

    results = []
    for i, user in enumerate(users):
        name = user.get("friendly_name") or user.get("username") or "?"
        print(f"  [{i+1}/{len(users)}] {name}...", end="", flush=True)
        try:
            result = analyze_user(base_url, args.api_key, user, after, args.geo)
            if result:
                results.append(result)
                extra = ""
                if result["teleportations"]:
                    extra = f", {len(result['teleportations'])} teleportation(s)!"
                print(f" {result['n_devices']} devices, score {result['score']}{extra}")
            else:
                print(" skip (0-1 devices)")
        except Exception as e:
            print(f" error: {e}")

    print_report(results, args.min_score, args.concurrent_only, args.timeline)


if __name__ == "__main__":
    main()
