#!/usr/bin/env bash
# Herdr Team Manager offline installer/transactional upgrader.
# Never overwrites an existing installation without --upgrade.
set -Eeuo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source="$root"
parent="$HOME/.agents/skills"
canonical="$parent/herdr-team"
backup_root="$HOME/.agents/skills-backups/herdr-team"
mode="${1:-install}"
if [[ $# -gt 0 ]]; then shift; fi

usage() { echo "Usage: bash install-local.sh [--upgrade | --rollback BACKUP_DIRECTORY]" >&2; exit 2; }
[[ "$mode" == "install" || "$mode" == "--upgrade" || "$mode" == "--rollback" ]] || usage

mkdir -p -- "$parent" "$backup_root"
chmod 700 "$backup_root"

link_agents() {
  local dest
  mkdir -p -- "$HOME/.claude/skills" "$HOME/.codex/skills"
  for dest in "$HOME/.claude/skills/herdr-team" "$HOME/.codex/skills/herdr-team"; do
    if [[ -e "$dest" || -L "$dest" ]]; then
      echo "Existing agent entry preserved: $dest"
    else
      ln -s -- "$canonical" "$dest"
      echo "Linked: $dest -> $canonical"
    fi
  done
}

if [[ "$mode" == "--rollback" ]]; then
  [[ $# -eq 1 ]] || usage
  candidate="$1"
  [[ -d "$candidate" && ! -L "$candidate" ]] || { echo "Invalid backup directory" >&2; exit 2; }
  prefix=$(cd -- "$backup_root" && pwd -P)
  target=$(cd -- "$candidate" && pwd -P)
  [[ "$target" == "$prefix/"* ]] || { echo "Backup path outside managed backup root" >&2; exit 2; }
  [[ -f "$target/scripts/htm.py" && ! -L "$canonical" ]] || { echo "Not a valid backup or canonical is a symlink" >&2; exit 2; }
  python3 "$target/scripts/htm.py" validate >/dev/null
  displaced="$backup_root/replaced-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  if [[ -d "$canonical" ]]; then mv -- "$canonical" "$displaced"; fi
  mv -- "$target" "$canonical" || {
    if [[ -d "$displaced" ]]; then mv -- "$displaced" "$canonical"; fi
    exit 2
  }
  link_agents
  echo "Restored: $canonical; displaced installation: $displaced"
  exit 0
fi

[[ $# -eq 0 ]] || usage
[[ -d "$source" && -f "$source/scripts/htm.py" ]] || { echo "Missing package skill source" >&2; exit 2; }
[[ ! -L "$canonical" ]] || { echo "Canonical installation is a symlink: refusing overwrite" >&2; exit 2; }
if [[ "$mode" == "install" && -e "$canonical" ]]; then
  echo "Already installed: $canonical (use --upgrade with deliberate backup)" >&2
  exit 2
fi
if [[ "$mode" == "--upgrade" && ! -d "$canonical" ]]; then
  echo "No existing canonical installation to upgrade" >&2
  exit 2
fi
stage=$(mktemp -d "$parent/.htm-stage.XXXXXXXX")
backup=""
switched="0"
rollback_failure() {
  local code=$?
  trap - ERR
  if [[ "$switched" == "1" && -n "$backup" && -d "$backup" ]]; then
    if [[ -d "$canonical" ]]; then mv -- "$canonical" "$stage.failed-$$" || true; fi
    mv -- "$backup" "$canonical" || true
    echo "Upgrade failed; attempted rollback to previous installation" >&2
  fi
  if [[ -d "$stage" ]]; then rm -rf -- "$stage"; fi
  exit "$code"
}
trap rollback_failure ERR
cp -R -- "$source/SKILL.md" "$source/README.md" "$source/CHANGELOG.md" "$source/LICENSE" "$source/pyproject.toml" "$source/uv.lock" "$source/config" "$source/references" "$source/scripts" "$source/tests" "$stage/"
python3 "$stage/scripts/htm.py" validate >/dev/null
if [[ "$mode" == "--upgrade" ]]; then
  backup="$backup_root/backup-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  mv -- "$canonical" "$backup"
  switched="1"
  echo "Preserved old release: $backup"
fi
mv -- "$stage" "$canonical"
bash "$canonical/scripts/bootstrap-config.sh"
link_agents
trap - ERR
if [[ -n "$backup" ]];
then echo "Upgrade successful; rollback: bash install-local.sh --rollback '$backup'"
else echo "Installed: $canonical"
fi
