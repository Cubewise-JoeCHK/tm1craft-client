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

Releases are tagged (`v0.4.0`, …) — pin one with
`git+https://github.com/Cubewise-JoeCHK/tm1craft-client.git@v0.4.0` if you
want reproducible installs.

Describe each TM1 instance and the service in an INI credentials registry
(ADR-0003 format) — placeholder hosts shown; use your own:

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

Two commands:

```bash
tm1craft-client list                    # print the registry's instance sections (name + base URL)
tm1craft-client dump "Planning Sample"  # capture one TM1 instance and upload it to a tm1craft service
```

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
