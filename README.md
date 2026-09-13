# tm1craft-client

Headless TM1 capture client for the tm1craft ecosystem: it downloads TM1 model
objects (processes, cubes incl. Rules, dimensions metadata-only, chores) with
TM1py and hands them to a tm1craft arc-service model-dump job, which does all
staging, graph building, and provenance. The client itself never builds a
database.

**Work in progress** — the [dump client v0.1 milestone](https://github.com/Cubewise-JoeCHK/tm1craft-client/milestone/1)
tracks what is in and what is next: packaging and capture (#2), the chunked
upload client (#3), and the CLI (#4) are merged; what remains lands under the
same milestone.

## Usage

```
tm1craft-client dump <instance>     # capture one TM1 instance and upload it to a tm1craft service
tm1craft-client list                # print the registry's instance sections (name + base URL)
```

Connections come from an INI credentials registry (the ADR-0003 format):
one `[<instance name>]` section per TM1 instance with `base` (the full API
base, e.g. `http://tm1host:12354/api/v1`), `user`, and `password` (may be
empty); an optional `[service]` section carries `upload_url`. Without
`--credentials <path>` the client tries `./tm1-client.ini` then
`~/.tm1-client.ini`. The `--base --user --password --service` flags override
the INI for a one-off; `--license-key` (or env
`TM1CRAFT_CLIENT_LICENSE_KEY`) rides every service request as a bearer
token when present. Progress and build stages render on stderr; the final
summary (job id, status, node/edge counts) goes to stdout.

Exit codes: `0` success, `1` capture/upload/service failure (one-line
message; a traceback only under `--debug`), `2` usage/config problems
(missing INI, unknown instance, missing `--service`). The password is never
printed. Protect the registry like the secret it is — owner-readable only
(`chmod 600`).
