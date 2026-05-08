#!/bin/zsh
# Local cron entry point used by launchd. Logs to /tmp/string-theory.log.
cd "$(dirname "$0")" || exit 1
export PYTHONPATH=src
exec /usr/bin/python3 -m string_theory.main "$@"
