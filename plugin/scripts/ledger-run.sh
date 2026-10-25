#!/bin/sh
# Runs the `aglium-ledger` client for the plugin: the push hook, the MCP server
# and the slash commands all come through here, so there is one place that
# knows how to find it.
#
#   LEDGER_BIN       an explicit path to the `aglium-ledger` executable
#   (PATH)           `aglium-ledger`, however it was installed
#   (uvx)            otherwise fetched and run with `uvx`, from LEDGER_PACKAGE
#                    if set (a wheel, a path, a git URL), or else from this
#                    plugin's own release, pinned below, with the `mcp` extra
#                    the MCP server needs
#
# Optional, read by the client itself:
#   LEDGER_CONFIG    a config file; otherwise the one `aglium-ledger init` wrote
#   LEDGER_URL       your organisation's Ledger server, when there is one
#   LEDGER_API_TOKEN its read token

# The client release this plugin was built with. Kept equal to plugin.json and
# the package version by the tests.
LEDGER_VERSION=0.3.0
PACKAGE=${LEDGER_PACKAGE:-"aglium-ledger[mcp]==${LEDGER_VERSION}"}

find_ledger() {
  if [ -n "${LEDGER_BIN:-}" ] && [ -x "$LEDGER_BIN" ]; then
    printf '%s\n' "$LEDGER_BIN"; return 0
  fi
  if command -v aglium-ledger >/dev/null 2>&1; then
    command -v aglium-ledger; return 0
  fi
  return 1
}

run() {
  if bin=$(find_ledger); then
    exec "$bin" "$@"
  fi
  if command -v uvx >/dev/null 2>&1; then
    exec uvx --quiet --from "$PACKAGE" aglium-ledger "$@"
  fi
  return 127
}

case "${1:-}" in
push-hook)
  # A hook must never be the reason a command cannot run: on any failure,
  # print nothing and let it through.
  payload=$(cat)
  # Most Bash calls are not a push. Leave before starting anything.
  printf '%s' "$payload" | grep -qE 'git( +-C +[^ ]+)? +push|gh +pr +create|glab +mr +create|git( +-C +[^ ]+)? +(checkout +-b|switch +-c)' || exit 0
  if bin=$(find_ledger); then
    printf '%s' "$payload" | "$bin" hook 2>/dev/null
  elif command -v uvx >/dev/null 2>&1; then
    printf '%s' "$payload" | uvx --quiet --from "$PACKAGE" aglium-ledger hook 2>/dev/null
  fi
  exit 0
  ;;
"")
  echo "usage: ledger-run.sh push-hook | <aglium-ledger arguments>" >&2
  exit 2
  ;;
*)
  run "$@"
  echo "aglium-ledger is not installed, and uvx is not here to fetch it." >&2
  echo "Install uv (https://docs.astral.sh/uv/) and start a new session, or install the client:" >&2
  echo "  uv tool install '$PACKAGE'" >&2
  echo "or set LEDGER_BIN to an aglium-ledger executable." >&2
  exit 127
  ;;
esac
