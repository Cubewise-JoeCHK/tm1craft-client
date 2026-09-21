# tm1craft-client

**tm1craft-client downloads the TM1 objects that matter and hands them to a
tm1craft service. It captures and uploads — it never builds a database.**

One command connects to a running TM1 instance over its REST API, gathers the
model's processes, cubes (Rules included), dimensions and chores, and posts
them to a [tm1craft](https://github.com/Cubewise-JoeCHK/tm1craft) arc service,
which stages the entities, builds the model graph, and records provenance.
From there, a tm1craft MCP server can answer questions about the model. The
client is deliberately narrow: a capture belt, not a warehouse.

## Where it fits

```
┌────────────────────────────┐
│            TM1             │  running instance, REST API
└─────────────┬──────────────┘
              │  TM1py · read-only (GETs only) · metadata-only dimensions
              │  · passwords stripped before anything leaves the box
              ▼
┌────────────────────────────┐
│      tm1craft-client       │  capture → upload      (this tool)
└─────────────┬──────────────┘
              │  POST /model-dump-job · entity chunks · build · poll
              ▼
┌────────────────────────────┐
│    tm1craft arc service    │  staging → graph build → provenance
└─────────────┬──────────────┘
              │  writes
              ▼
┌────────────────────────────┐
│   dumps dir (per instance) │  *.db graph store + analysis artifacts
└─────────────┬──────────────┘
              │
              ▼
┌────────────────────────────┐
│       tm1craft MCP         │  begin_session(instance=…) → graph Q&A
└────────────────────────────┘
```

## Quickstart

Install from this repository (Python 3.11+):

```bash
pip install git+https://github.com/Cubewise-JoeCHK/tm1craft-client.git
# or, as an isolated tool:
uv tool install git+https://github.com/Cubewise-JoeCHK/tm1craft-client.git
```

Releases are tagged (`0.4.0`, …) — pin one with
`git+https://github.com/Cubewise-JoeCHK/tm1craft-client.git@0.4.0` if you
want reproducible installs.

### Windows exe (no Python)

Every release also ships a self-contained Windows x64 build: grab
`tm1craft-client-<tag>-windows-x64.zip` from
[releases](https://github.com/Cubewise-JoeCHK/tm1craft-client/releases/latest),
unzip it anywhere, and run the single `tm1craft-client.exe` inside — a
onefile build, no `_internal/` folder, no Python, no pip, no
dependencies (expect a one-time Windows SmartScreen prompt on first
run; the README in the zip covers it). Beside the exe the zip ships a
README plus a ready-to-edit `tm1-client.ini.example` covering the
credentials INI, exit codes, and the license-key variable.

Describe each TM1 instance and the service in an INI credentials registry
(ADR-0003 format) — start from [`tm1-client.ini.example`](tm1-client.ini.example)
in the repo root (the same file ships in the Windows zip); placeholder
hosts shown, use your own:

```ini
[Planning Sample]
base = http://tm1.example.com:12354/api/v1
user = admin
password =

[service]
upload_url = http://craft.example.com
```

Save it as `./tm1-client.ini` or `~/.tm1-client.ini` (or pass
`--credentials <path>`) and protect it like the secret it is:

```bash
chmod 600 ~/.tm1-client.ini
```

**Extra connection parameters.** Any additional keys in an instance
section are passed straight through to TM1py's `TM1Service` — so TM1py
connection options work right in the registry:

```ini
[Planning Sample]
base = https://tm1.example.com:12354/api/v1
user = admin
password =
namespace = LDAP
verify_ssl = false
```

Values are scalar-coerced on the way in: `true`/`false` (any casing)
become booleans, an all-digits value becomes an integer, anything else
stays a string. `ssl` defaults from the `base` URL's scheme
(`https://` → `ssl = true`) unless the section sets `ssl` itself, and
the `--base`/`--user`/`--password` flags still beat the INI. No key is
required: the client passes whatever the section (or flags) carries to
`TM1Service` and TM1py validates it — a section may legitimately use a
different parameter set (e.g. `address` + `port` instead of `base`, or
namespace auth), and a missing or unrecognized key surfaces as TM1py's
own connection error (exit code 1).

Three commands:
```bash
tm1craft-client list                    # print the registry's instance sections (name + base URL)
tm1craft-client dump "Planning Sample"  # capture one TM1 instance and upload it to a tm1craft service
tm1craft-client upload dump.json        # replay a bundle file written by 'dump --out'
```

**Two-phase dump** for boxes that cannot reach the service: capture to a
local file on the TM1 machine, move the file, upload from anywhere.

```bash
tm1craft-client dump "Planning Sample" --out planning-sample.json   # no service is contacted
tm1craft-client upload planning-sample.json --service http://craft.example.com
```

The bundle file is the capture verbatim — instance name plus the four
password-stripped entity kinds — so it carries no credentials and is safe
to move. `upload` resolves the service from `--service` or the registry's
`[service] upload_url`; with no registry at all, `--service` is required.

Progress (per-kind capture counts, build stages) renders on **stderr**; the
final report goes to **stdout**, so `tm1craft-client dump prod 2>capture.log`
pipes a clean summary:

```
job 79c026a3… done (instance: Planning Sample, attached: no)
path: /srv/tm1craft/dumps/Planning%20Sample.db
nodes: 161
edges: 94
unchanged: 159
```

### Flags

| Flag | Meaning |
| --- | --- |
| `<instance>` | INI section name, matched exactly |
| `--credentials PATH` | registry path; default: `./tm1-client.ini` then `~/.tm1-client.ini` |
| `--base`, `--user`, `--password` | one-off overrides of the instance's connection (password may be empty) |
| `--service URL` | service base URL; wins over the `[service] upload_url` |
| `--out PATH` | write the captured bundle to a file instead of uploading — no service is contacted; replay with `upload` |
| `--arc-origin URL` | Arc origin forwarded to the service (rides the job start) |
| `--license-key KEY` | service license key; default: env `TM1CRAFT_CLIENT_LICENSE_KEY` |
| `--debug` | on failure, show the traceback instead of one line |
| `--version` | print the installed version |

### Exit codes

- `0` — success
- `1` — capture/upload/service failure (one-line message; a traceback only
  under `--debug`)
- `2` — usage/config problems (missing INI, unknown instance, missing
  `--service`)

## Design boundaries

The edges of the tool are deliberate; they are what make it safe to point at
a production box:

- **Bundle-only.** The client has no idea how the graph is built. It hands
  the service per-kind entity chunks and the instance name; staging, graph
  building, fact packs, and provenance are entirely the service's business.
- **Read-only by construction.** Every TM1 call in the client is a GET or a
  service read — it cannot write to TM1 even by accident. Each kind's fetched
  count is cross-checked against the server's own `/$count`; a mismatch
  aborts the capture instead of dumping a partial model.
- **Metadata-only dimensions.** Dimension fetches carry hierarchies,
  cardinalities, element/edge counts, attribute definitions and subset
  definitions — never element payloads. The heavy full-dimension expansion is
  never requested.
- **Control objects included.** Everything the server reports rides along —
  `}CubeProperties`, `}Clients`, `}ElementAttributes_*` and their kin. No
  filtering anywhere; what the model is is what gets dumped.
- **Passwords never travel.** Any `password` key inside a fetched entity (TI
  ODBC data sources, any spelling, any depth) is stripped before the bundle
  leaves the machine. Your registry password is never printed, never logged,
  and scrubbed from error messages.
- **License key optional.** Against a service in open mode you ship nothing;
  when the service issues keys, `--license-key` (or the environment variable)
  rides every request as a bearer token.

## License

MIT — see [LICENSE](LICENSE).
