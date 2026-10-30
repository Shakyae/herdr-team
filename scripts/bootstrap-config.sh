#!/usr/bin/env bash
set -euo pipefail
self_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_config="$self_dir/../config/team.yaml"
target_dir="${XDG_CONFIG_HOME:-$HOME/.config}/herdr-team"
target="$target_dir/team.yaml"
mkdir -p -- "$target_dir"
if [[ -e "$target" ]]; then
  echo "Configuration already exists, not overwritten: $target"
else
  cp -- "$source_config" "$target"
  chmod 600 "$target"
  echo "Configuration installed: $target"
fi
