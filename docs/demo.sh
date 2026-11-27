#!/bin/sh
# A throwaway demo of owner-queue with made-up sessions. Run from the repository root:
#   sh docs/demo.sh
# It uses a temporary queue directory and never touches ~/.claude/owner-queue.
set -e
export OWNER_QUEUE_DIR="$(mktemp -d)/queue"
oq() { python3 bin/owner-queue "$@"; }
show() { echo "\$ owner-queue $*"; oq "$@"; }

# Setup, as the plugin's hooks do it: session s1 opened a pull request, session s2 waits
# for a permission answer. One more item is added by hand.
printf '%s\n' '{"hook_event_name":"PostToolUse","session_id":"s1","cwd":"/work/alpha/app","tool_name":"Bash","tool_input":{"command":"gh pr create --title \"Add retry to the uploader\""},"tool_response":{"stdout":"https://github.com/example/repo/pull/7\n"}}' | oq hook
printf '%s\n' '{"hook_event_name":"Notification","notification_type":"permission_prompt","session_id":"s2","cwd":"/work/beta/api","message":"Claude needs your permission to use Bash"}' | oq hook
oq add "Decide on the database migration plan" > /dev/null

show list
show next
show done
show statusline
