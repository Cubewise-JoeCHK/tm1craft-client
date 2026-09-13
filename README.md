# tm1craft-client

Headless TM1 capture client for the tm1craft ecosystem: it downloads TM1 model
objects (processes, cubes incl. Rules, dimensions metadata-only, chores) with
TM1py and hands them to a tm1craft arc-service model-dump job, which does all
staging, graph building, and provenance. The client itself never builds a
database.

**Work in progress** — packaging is bootstrapped; capture, upload, and CLI
modules land in the [dump client v0.1 milestone](https://github.com/Cubewise-JoeCHK/tm1craft-client/milestone/1).
