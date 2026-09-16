tm1craft-client — TM1 capture client, Windows drop-in
=====================================================

A self-contained build of the tm1craft-client command line: unzip it on
any Windows x64 box (no Python, no pip, nothing to install) and run
tm1craft-client.exe. It captures a TM1 instance's model objects with
TM1py and hands them to a tm1craft service — the client never builds a
database.

RUN
---
1. Unzip the folder anywhere (keep _internal/ next to
   tm1craft-client.exe).
2. Describe your TM1 instance and the service in a credentials INI
   (below). By default the client looks for .\tm1-client.ini in the
   current directory, then %USERPROFILE%\.tm1-client.ini — or pass
   --credentials <path> explicitly.
3. Capture and upload:

    tm1craft-client.exe dump "Planning Sample" --service http://craft.example.com

   Progress renders on stderr; the final summary (job id, status,
   server-reported counts) prints on stdout, so
   `tm1craft-client.exe dump prod 2>capture.log` leaves a clean report.

   tm1craft-client.exe list            prints the registry's instances.

TWO-PHASE DUMP (no route to the service from the TM1 box)
---------------------------------------------------------
Capture to a local file on the TM1 machine, move the file, upload from
anywhere that can reach the service:

    tm1craft-client.exe dump "Planning Sample" --out planning-sample.json
    tm1craft-client.exe upload planning-sample.json --service http://craft.example.com

The bundle file is the capture verbatim — instance name plus the four
password-stripped entity kinds — so it carries no credentials and is
safe to move. upload resolves the service from --service or the INI's
[service] upload_url; with no INI at all, --service is required.

CREDENTIALS INI
---------------
A ready-to-edit copy ships beside this README: tm1-client.ini.example
— copy it to tm1-client.ini here, fill in your hosts, done. The format:

    [Planning Sample]
    base = http://tm1.example.com:12354/api/v1
    user = admin
    password =

    [service]
    upload_url = http://craft.example.com

One [<instance>] section per TM1 instance (base/user/password; the
password may be empty). The optional [service] section's upload_url is
the tm1craft service the dump goes to — a --service flag overrides it
per run. The file is read per invocation and never written; the
password is never printed and argv is never logged. Keep the file's
permissions to yourself.

EXIT CODES
----------
0 success; 1 capture/upload/service failure (one-line redacted message,
traceback only under --debug); 2 usage/config problems (missing INI,
unknown instance, missing --service).

LICENSE KEY
-----------
If the service runs with license keys, set the client's key first:

    set TM1CRAFT_CLIENT_LICENSE_KEY=<key>

or pass --license-key <key> on the dump command.
