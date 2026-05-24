# agent-wake core

This directory contains the shared wire format specification for agent-wake.

Both adapters (Claude Code and opencode) read and emit events conforming to
the canonical schema described in [`schema.md`](schema.md).

For v0, `core/` is docs and examples only. If a shared runtime library
becomes useful, it will be added here in a later version.
