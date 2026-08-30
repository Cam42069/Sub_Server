# Sub_Server

A Python subscription server with an HTTPS web interface.

It opens a socket subscription to a **data access node**, keeps the incoming
samples in RAM (up to a configurable ceiling, ~4 GB by default), and serves them
to browsers on the local network as live and static plots grouped into saveable,
shareable **profiles**.

```
   data access node  ──socket──▶  Sub_Server  ──HTTPS──▶  browsers on the LAN
                                  (RAM store)
```

---

## Quick start

```bash
pip install -r requirements.txt

# Terminal 1 -- a stand-in data node that publishes 12 synthetic signals
python3 mock_data_node.py --port 980

# Terminal 2 -- the server
python3 run_server.py
```

Then open **https://localhost:8443/** and choose *Create a new user* on the
sign-in page. The certificate is self-signed, so the browser warns on the first
visit; see [HTTPS certificates](#https-certificates) below.

Two example profiles are installed and visible to everyone under
**Profiles ▸ Load**: *Reactor Overview* (four live plots) and *Function Demo*
(user functions, reference lines and a static window).

To point at a real node instead of the mock:

```bash
python3 run_server.py --node-host 10.0.0.5 --node-port 980
```

---

## The web interface

Every page carries a banner toolbar:

| Menu | Items |
| --- | --- |
| **Home** | Welcome banner, subscription status, recently saved profiles |
| **Profiles** | Create New · Load · Browse Shared |
| **About** | Program name and current version |

Pages are dark grey with white text throughout.

### Profiles

A profile holds **up to four plots**, laid out 1×1, 2×1, 1×2 or 2×2, and saved
as a TOML file. Each plot is either:

* **Live** — inputs are a start time, the variables to plot, a plot title and a
  Y-axis name. It draws from the start time up to the present and keeps
  extending as new samples arrive.
* **Static** — inputs are a start *and* end time, plus the same variables,
  title and Y-axis name. It draws that fixed window and stays put.

Live plots refresh incrementally: after the first load the browser sends a
cursor and the server replies with only the samples that arrived since, so a
plot open for hours does not re-download its whole history every second.

### Saving, publishing and loading

* **Save** writes to your private space. You can save over your own private
  profiles freely.
* **Publish** copies a profile to the shared library, where **nothing is ever
  overwritten**. Publishing `bruh` when `bruh` already exists writes `bruh_1`,
  then `bruh_2`, and so on. The UI tells you the name it actually used.
* **Browse Shared** lists everything published by everyone; any of it can be
  opened, and *Copy* opens it in the editor so you can save your own version.

Because the shared space is append-only, published profiles cannot be edited or
deleted through the interface either — only added to. (An operator with
filesystem access can still remove a file directly.)

### Folder structure

```
Profiles/
├── shared/{user}/.../{profile_name}.toml     published, append-only
├── private/{user}/.../{profile_name}.toml    your own work
└── examples/.../{profile_name}.toml          shipped read-only samples
```

You can create your own folder trees under your username in **both** the
private and shared spaces, from the editor's *New folder…* button or the
*Your folders* panel on the Load page.

---

## Customisation

**Plot types** — per series: `line`, `scatter`, `step`, `area`, `bar`. A plot
sets a default type and any series can override it.

**Colours** — each series has its own colour picker. Unset series get colours
from a palette chosen to stay legible on the dark background; your own default
palette can be stored per account via `POST /api/preferences`.

**Two Y axes** — any series can be moved to a right-hand axis, which is what
makes a plot like bus voltage (13.8 kV) alongside bus current (410 A) readable.

**Functions** — a plot can route its data through a Python function you write.
Drop a module in `Functions/{your-username}/` and it appears in the editor's
*Transform function* menu, with its docstring and argument list. Modules are
reloaded automatically when you edit them.

```python
# Functions/alice/mine.py
import numpy as np

def envelope(data, window=50):
    """Rolling peak-to-peak range of every selected variable."""
    out = {}
    for name in data.names:
        values = data.v(name)
        highs = np.maximum.accumulate(values)
        out[f"{name} range"] = highs - np.minimum.accumulate(values)
    return out
```

`data` gives you `data.names`, `data.t(name)`, `data.v(name)`,
`data.series(name)`, and the window bounds `data.start` / `data.end` /
`data.now`. Return any of:

| Return value | Drawn as |
| --- | --- |
| `{"label": values}` | a series on the input's timestamps |
| `{"label": (times, values)}` | a series on its own timestamps |
| `{"label": 42.0}` | a labelled horizontal reference line |
| `42.0`, or a tuple of numbers | reference lines |
| a matplotlib `Figure` | a PNG rendered server-side, in place of the plot |

Errors are caught and shown on the plot rather than taking down the request, so
you can iterate on a function with the page open.

See `Functions/examples/statistics.py` for six worked examples.

---

## The data node protocol

`subserver/protocol.py` is the reference; this is the summary. The node is
contacted over a plain TCP socket exchanging **newline-delimited JSON** — one
complete JSON object per line, UTF-8.

**Sub_Server → node**

```json
{"op": "subscribe", "variables": ["*"]}
{"op": "history", "id": "abc", "variables": ["a.b"], "start": 1787500000, "end": 1787503600}
```

**Node → Sub_Server**

```json
{"op": "welcome", "variables": ["a.b", "c.d"]}
{"op": "data", "t": 1787503600.5, "values": {"a.b": 12.5, "c.d": 3}}
{"op": "history", "id": "abc", "series": {"a.b": {"t": [...], "v": [...]}}, "done": true}
{"op": "error", "message": "..."}
```

Timestamps are epoch seconds; values ≥ 1e11 are read as epoch milliseconds. The
parser is deliberately lenient so it can talk to a node that was not written
against this exact schema: `op` may be omitted on data messages, `values` may be
spelled `v` or `data`, `t` may be `time`/`timestamp`/`ts`, and a flat
`{"t": ..., "a.b": 1}` form is accepted.

`history` is optional. If the node does not implement it, Sub_Server notices,
stops asking, and plots whatever is in RAM — with a note on the plot saying how
far back its data actually goes. History replies are fetched on a separate
short-lived connection so a slow reply cannot stall the live stream.

The subscription reconnects on its own with exponential backoff, so a node
restart needs no operator action.

Two stand-in nodes ship with the project, both stdlib-only:

* **`mock_data_node.py`** — twelve plant-flavoured signals (core temperatures,
  coolant flow, bus voltage…) built from sines plus seeded noise. Because they
  are deterministic, history is recomputed on demand and a static plot always
  agrees with the live one.
* **`test_dan.py`** — a smaller, simpler node publishing seven **random**
  signals: five random walks, a monotonic counter and a 0/1 state. It keeps
  every sample it emits in memory and serves history straight out of that
  buffer, so history is exactly what was streamed. Start it with `--seed` for a
  repeatable run, or `--backlog` to choose how much history exists at start-up.

```bash
python3 test_dan.py --port 9000 --interval 0.5 --seed 7
python3 run_server.py --node-port 9000
```

The counter is there on purpose: it climbs by one every tick, so if ordering,
decimation or the incremental cursor ever breaks, the trace stops being a
straight line and says so.

---

## Memory

Samples live in per-variable `float64` arrays — 16 bytes per sample. Memory is
accounted for globally, and when the ceiling is crossed the oldest data is
dropped across *all* variables using a common cut-off timestamp, so what remains
stays time-aligned. The Home page shows current usage against the limit.

Queries are decimated to `max_points_per_series` (4000 by default) using
min/max bucketing, so spikes survive the reduction that plain striding would
hide. Non-numeric and non-finite samples are rejected at ingest and counted.

---

## Configuration

Copy `config.example.toml` to `config.toml`. Every key is optional. The main
ones:

| Section | Key | Default | Meaning |
| --- | --- | --- | --- |
| `server` | `host`, `port` | `0.0.0.0`, `8443` | where the web interface listens |
| `data_node` | `host`, `port` | `127.0.0.1`, `980` | the node to subscribe to |
| `data_node` | `variables` | `["*"]` | glob patterns to subscribe to |
| `data_node` | `backfill_seconds` | `3600` | history to request on connect |
| `store` | `max_bytes` | `4_000_000_000` | RAM ceiling for samples |
| `store` | `max_age_seconds` | `0` | optional time limit (0 = memory only) |
| `plots` | `max_points_per_series` | `4000` | decimation target |
| `security` | `allow_registration` | `true` | self-service account creation |
| `security` | `allow_user_functions` | `true` | see **Security** below |

Command-line flags override the file:

```
python3 run_server.py --help
  --config PATH        --host HOST        --port PORT
  --node-host HOST     --node-port PORT   --no-subscriber
  --create-user NAME   --verbose
```

### HTTPS certificates

On first run the server generates a self-signed certificate into `data/certs/`,
valid for `localhost`, the machine's hostname and every non-loopback IPv4
address it can find. Add more names with `cert_hostnames` in `config.toml`.

Browsers warn about the unknown issuer. Either accept the warning, or import
`data/certs/server.crt` as a trusted root on the client machines. To use a
certificate from your own CA instead, point `cert_file` and `key_file` at it.

Certificate generation uses the `cryptography` package, falling back to the
`openssl` command line if that is not installed.

---

## Security

This is a local-network tool. Read this before putting it on a network you do
not control.

* **User functions run as the server.** They are ordinary Python executed in the
  server process, with the server's privileges — writing one is equivalent to
  running code on the host. Only give accounts to people you would trust with a
  shell there, or set `allow_user_functions = false` in `config.toml`. There is
  no sandbox, and adding one is not on the roadmap; the feature exists because
  arbitrary transforms were the requirement.
* **Registration is open by default.** Anyone who can reach the login page can
  create an account. Set `allow_registration = false` and pre-create accounts
  with `--create-user` for a shared network.
* Passwords are stored as PBKDF2-HMAC-SHA256 with a per-user random salt and
  240,000 iterations. `data/users.json` and the session key are written `0600`.
* Session cookies are `HttpOnly`, `SameSite=Lax` and `Secure`; every mutating
  request carries a CSRF token, and the session id is regenerated on login.
* Every user-supplied path component is validated against an allow-list and then
  re-checked for containment inside its root, so neither `../` nor a symlink can
  reach outside `Profiles/`.
* Responses set a CSP that blocks inline scripts and any off-origin script,
  style, image or connection. Inline *styles* are permitted because series
  colours are set from data at runtime.

---

## Running the tests

```bash
python3 -m pytest tests/ -q
```

175 tests cover the RAM store and its eviction, the wire protocol and
subscriber reconnection, TOML round-tripping, profile validation, path safety,
account handling, user functions, and the HTTP layer end to end.

---

## Layout

```
run_server.py               entry point
mock_data_node.py           stand-in data access node (deterministic signals)
test_dan.py                 stand-in data access node (random signals)
config.example.toml         annotated configuration
pytest.ini                  pins the suite to tests/
subserver/
├── app.py                  Flask routes, sessions, JSON API
├── auth.py                 accounts and password hashing
├── certs.py                self-signed certificate generation
├── config.py               configuration loading
├── models.py               profile schema and validation
├── paths.py                path allow-listing and containment
├── profiles.py             profile storage, the publish rule
├── protocol.py             NDJSON wire protocol
├── query.py                plot specs -> data payloads
├── ringstore.py            the in-RAM sample store
├── subscriber.py           the background subscription
├── timeutil.py             absolute and relative time parsing
├── tomlio.py               TOML reading and writing
├── userfuncs.py            user function loading and execution
├── templates/              Jinja2 pages
└── static/
    ├── css/app.css         the dark theme
    └── js/plot.js          the canvas plotting engine
Functions/examples/         worked example transform functions
Profiles/                   shared / private / examples
tests/                      pytest suite
```

The plotting engine is hand-written against `<canvas>` rather than pulling a
charting library from a CDN: the server is meant to run on a local network that
may have no route to the internet, where a CDN dependency would leave every
page blank.

## Version

1.0.0
