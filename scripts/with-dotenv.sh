#!/usr/bin/env bash
# Run a uv-managed command with .env loaded if the file is present.
#
# Usage: scripts/with-dotenv.sh <command> [args...]
#
# When .env exists, this passes --env-file .env to uv so STT_PROXY_* values
# become visible in the subprocess environment (handy for `env | grep` and
# for tools that read env before our Python code runs). When .env is absent,
# it falls back to plain `uv run` so the task still works against shell env.
#
# pydantic-settings in app/config.py ALSO reads .env via env_file, so the
# values are loaded twice (once by uv into OS env, once by pydantic-settings
# into the Settings object). That's redundant but harmless — both layers
# agree on the same values, and shell env takes precedence either way.
set -euo pipefail

if [ -f .env ]; then
	exec uv run --env-file .env "$@"
else
	exec uv run "$@"
fi
