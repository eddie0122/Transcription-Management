#!/bin/sh
# Initialize volume ownership (named volumes mount as root on first use),
# then drop to the non-root application user.
set -e
chown -R appuser:appuser /app/data /app/models 2>/dev/null || true
exec gosu appuser "$@"
