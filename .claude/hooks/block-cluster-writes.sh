#!/bin/bash
# PreToolUse (Bash): block mutating oc/kubectl commands. The target cluster is air-gapped;
# the kube contexts on this machine are unrelated sandboxes. Exit 2 blocks the call.
cmd=$(jq -r '.tool_input.command // empty')

verbs='apply|delete|create|replace|patch|edit|debug|run|exec|scale|label|annotate|drain|cordon|uncordon|taint'
re="(^|[^[:alnum:]_./-])(oc|kubectl)[[:space:]]+([^|;&]*[[:space:]])?(${verbs})([[:space:]]|\$)"

if [[ $cmd =~ $re ]]; then
  echo "Blocked: mutating ${BASH_REMATCH[2]} command (${BASH_REMATCH[4]}). The target cluster is disconnected and the local kube context is not it. Give the user the command to run on the other side instead." >&2
  exit 2
fi
exit 0
