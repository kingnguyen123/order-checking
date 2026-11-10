# Proxy Speed Checker

Tests a pasted list of `host:port:username:password` proxies concurrently and
keeps the fast ones, fastest first.

## Install

Requires Python 3.8+.

```bash
cd tools/proxy_speed_checker
pip install -r requirements.txt
```

## Run

```bash
python proxy_speed_checker.py
```

Paste your proxies, one per line, then press **Enter on an empty line** to start:

```
mix.nexaproxies.com:8888:nt_xxxx__session-abc123__lifetime-120:PASSWORD
mix.nexaproxies.com:8888:nt_xxxx__session-def456__lifetime-120:PASSWORD
```

Every full line is a separate proxy, even when they share host and port: the
session ID in the username is what makes each one different. Blank lines, extra
spaces and exact duplicates are ignored.

You can also pipe a file in: `python proxy_speed_checker.py < proxies.txt`

## Options

| Option | Default | Meaning |
|---|---|---|
| `--url` | `https://www.fastly.com` | URL requested through each proxy |
| `--timeout` | `5` | Seconds per request before it counts as failed |
| `--threshold` | `1000` | Average ms below which a proxy counts as fast |
| `--concurrency` | `100` | Proxies tested at the same time |
| `--fast-file` | `fast_proxies.txt` | Where the fast list is saved |
| `--csv-file` | `results.csv` | Where all results are saved |
| `--show-full` | off | Also print the fast list as full lines (with passwords) in the terminal |

Example: `python proxy_speed_checker.py --threshold 600 --timeout 8 --concurrency 50`

## How it tests

1. GET the test URL through the proxy and time it until the response headers
   arrive. The page body isn't downloaded, so a test uses very little proxy data.
2. Working proxies are tested a second time and the two times are averaged.
3. `https://ipinfo.io/json` is requested through each working proxy to record
   its exit IP and country.

## Output

- **Terminal:** a progress counter, then the fast proxies as a table with
  session ID, average ms, exit IP and country, then a summary (total, working,
  fast, failed, fastest/average/slowest). Passwords are not shown unless you pass
  `--show-full`.
- **fast_proxies.txt:** the fast proxies as full lines, exactly as pasted,
  fastest first, ready to copy into your tools.
- **results.csv:** every proxy with `session_id, status, avg_ms, exit_ip,
  country, error`. Status is `fast`, `slow` (working but over the threshold) or
  `failed`.

`fast_proxies.txt` contains your passwords, so don't share it.
