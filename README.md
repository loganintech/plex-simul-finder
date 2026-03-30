# plex-simul-finder

Detect Plex account sharing using [Tautulli](https://tautulli.com/)'s API. Analyzes watch history to find users streaming from multiple devices and locations, with per-device usage breakdowns.

## Detection signals

- **Multiple devices** from different public IPs
- **Concurrent sessions** — overlapping playback from different networks
- **Teleportation** — sessions from locations farther apart than physically possible given the time gap (requires `--geo`)
- **IP diversity** — how many distinct IPs each user streams from and how much usage each gets

Private IPs (192.168.x, 10.x, etc.) are treated as the same network, so multiple devices at home won't trigger false positives.

## Prerequisites

- Python 3.10+
- [Tautulli](https://tautulli.com/) with API access enabled
- The `requests` Python package

### Install dependencies

```bash
pip install requests
```

### Configure

Set your Tautulli connection via environment variables or pass them as flags:

```bash
export TAUTULLI_HOST="tautulli.example.com"
export TAUTULLI_API_KEY="your-api-key"
```

You can find your API key in Tautulli under **Settings > Web Interface > API Key**.

## Usage

```bash
# Default sharing analysis (last 30 days)
python simul_finder.py

# Only show users with concurrent sessions from different networks
python simul_finder.py --concurrent-only

# Add chronological timeline of concurrent sessions
python simul_finder.py --timeline

# Enable geolocation + teleportation detection (more API calls)
python simul_finder.py --geo

# Per-user IP usage breakdown
python simul_finder.py --top-ips
python simul_finder.py --top-ips --geo  # with locations

# Check a specific user over 90 days
python simul_finder.py --user "SomeUser" --days 90 --timeline --geo

# Lower the flagging threshold
python simul_finder.py --min-score 10
```

## Scoring

Users are scored and ranked by suspicion level (default threshold: 20):

| Signal | Points |
|---|---|
| Each device beyond the first | +10 |
| Each unique concurrent device pair | +25 |
| Each concurrent session (capped at 50) | +2 |
| Each heavily-used device beyond the first (>5 plays) | +15 |
| Each teleportation event (with `--geo`) | +30 |

## License

[GPL-3.0](https://www.gnu.org/licenses/gpl-3.0.html)

---

> **Nix users:** A `flake.nix` is included. If you use [direnv](https://direnv.net/), just `direnv allow` and everything (Python + dependencies) is set up automatically. Otherwise `nix develop` drops you into a shell with everything you need.
