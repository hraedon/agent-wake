#!/usr/bin/env python3
"""CLI tool to generate a shared HMAC secret for agent-wake sources."""

import secrets

print(secrets.token_hex(32))
