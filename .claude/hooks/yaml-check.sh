#!/bin/bash
# PostToolUse (Write|Edit): YAML-parse edited .yaml files and helm-lint the chart after any
# edit under chart/ or values/. Exit 2 feeds the error back to Claude.
f=$(jq -r '.tool_response.filePath // .tool_input.file_path // empty')
[ -f "$f" ] || exit 0
root="${CLAUDE_PROJECT_DIR:-$PWD}"

# Helm templates are Go templates, not YAML; helm lint covers them below.
case "$f" in
  */templates/*) ;;
  *.yaml|*.yml)
    if ! out=$(python3 -c '
import sys, yaml
try:
    list(yaml.safe_load_all(open(sys.argv[1])))
except yaml.YAMLError as e:
    sys.exit(str(e))' "$f" 2>&1); then
      echo "YAML syntax check failed for $f:" >&2
      echo "$out" | tail -n 8 >&2
      exit 2
    fi ;;
esac

case "$f" in
  "$root"/chart/*|"$root"/values/*) ;;
  *) exit 0 ;;
esac

# Lint the recipe against every site, so a chart change cannot break one of them unnoticed.
for site in "$root"/values/sites/*.yaml; do
  [ -f "$site" ] || continue
  if ! out=$(helm lint "$root/chart" -f "$root/values/glm53-b200.yaml" -f "$site" 2>&1); then
    echo "helm lint failed for site $(basename "$site") after editing $f:" >&2
    echo "$out" | grep -vE '^\[INFO\]|^$' | tail -n 12 >&2
    exit 2
  fi
done
exit 0
