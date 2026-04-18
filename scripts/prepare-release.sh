#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHANGELOG="$REPO_ROOT/CHANGELOG.md"
MANIFEST="$REPO_ROOT/custom_components/git_ha_ppens/manifest.json"
SENSOR="$REPO_ROOT/custom_components/git_ha_ppens/sensor.py"
BINARY_SENSOR="$REPO_ROOT/custom_components/git_ha_ppens/binary_sensor.py"

# ── helpers ──────────────────────────────────────────────────────────────────

ask() {
  local prompt="$1" var="$2" default="${3:-}"
  if [[ -n "$default" ]]; then
    read -rp "$prompt [$default]: " "$var"
    [[ -z "${!var}" ]] && printf -v "$var" '%s' "$default"
  else
    while [[ -z "${!var:-}" ]]; do
      read -rp "$prompt: " "$var"
    done
  fi
}

collect_entries() {
  local section="$1"
  local varname="$2"
  eval "${varname}=()"
  echo "  Einträge für '$section' (leer lassen zum Beenden):"
  while true; do
    read -rp "    - " entry
    [[ -z "$entry" ]] && break
    eval "${varname}+=(\"\$entry\")"
  done
}

# ── current version ───────────────────────────────────────────────────────────

CURRENT=$(jq -r '.version' "$MANIFEST")
echo ""
echo "Aktuelle Version: $CURRENT"
echo ""

# ── new version ───────────────────────────────────────────────────────────────

ask "Neue Version (z.B. 0.5.0)" NEW_VERSION
TODAY=$(date +%Y-%m-%d)

# ── changelog entries ─────────────────────────────────────────────────────────

echo ""
echo "Changelog-Einträge für Version $NEW_VERSION:"
echo "(Abschnitt leer lassen → wird weggelassen)"
echo ""

collect_entries "Added"    added_entries
collect_entries "Changed"  changed_entries
collect_entries "Fixed"    fixed_entries
collect_entries "Removed"  removed_entries

# ── build changelog block ─────────────────────────────────────────────────────

NEW_BLOCK="## [$NEW_VERSION] - $TODAY"$'\n'

for section_info in "Added:added_entries" "Changed:changed_entries" "Fixed:fixed_entries" "Removed:removed_entries"; do
  label="${section_info%%:*}"
  ref="${section_info##*:}"
  eval "local_count=\${#${ref}[@]}"
  if [[ $local_count -gt 0 ]]; then
    NEW_BLOCK+=$'\n'"### $label"$'\n'
    eval "local_arr=(\"\${${ref}[@]}\")"
    for e in "${local_arr[@]}"; do
      NEW_BLOCK+="- $e"$'\n'
    done
  fi
done

# ── update CHANGELOG.md ───────────────────────────────────────────────────────

python3 - "$CHANGELOG" "$NEW_VERSION" "$CURRENT" "$NEW_BLOCK" <<'PYEOF'
import sys, re

path, new_ver, old_ver, new_block = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

with open(path) as f:
    content = f.read()

# Insert new version block after the [Unreleased] line
content = re.sub(
    r'(## \[Unreleased\]\n)',
    r'\1\n' + new_block,
    content,
    count=1
)

# Update [Unreleased] comparison link
content = re.sub(
    rf'\[Unreleased\]: .+/{re.escape(old_ver)}\.\.\.HEAD',
    f'[Unreleased]: https://github.com/manuveli/git-ha-ppens/compare/v{new_ver}...HEAD',
    content
)

# Add new version comparison link after the [Unreleased] link
new_link = f'[{new_ver}]: https://github.com/manuveli/git-ha-ppens/compare/v{old_ver}...v{new_ver}'
content = re.sub(
    rf'(\[Unreleased\]: .+\n)',
    r'\1' + new_link + '\n',
    content,
    count=1
)

with open(path, 'w') as f:
    f.write(content)

print("  ✓ CHANGELOG.md")
PYEOF

# ── update manifest.json ──────────────────────────────────────────────────────

sed -i '' "s/\"version\": \"$CURRENT\"/\"version\": \"$NEW_VERSION\"/" "$MANIFEST"
echo "  ✓ manifest.json"

# ── update sensor.py + binary_sensor.py ──────────────────────────────────────

sed -i '' "s/\"sw_version\": \"$CURRENT\"/\"sw_version\": \"$NEW_VERSION\"/" "$SENSOR"
echo "  ✓ sensor.py"

sed -i '' "s/\"sw_version\": \"$CURRENT\"/\"sw_version\": \"$NEW_VERSION\"/" "$BINARY_SENSOR"
echo "  ✓ binary_sensor.py"

# ── summary ───────────────────────────────────────────────────────────────────

echo ""
echo "Version $CURRENT → $NEW_VERSION — alle Dateien aktualisiert."
echo ""
echo "Nächste Schritte:"
echo "  1. git diff     — Änderungen prüfen"
echo "  2. git add CHANGELOG.md custom_components/git_ha_ppens/manifest.json \\"
echo "         custom_components/git_ha_ppens/sensor.py \\"
echo "         custom_components/git_ha_ppens/binary_sensor.py"
echo "  3. git commit -m \"chore: prepare release v$NEW_VERSION\""
echo "  4. Push auf release-Branch → GitHub Actions erstellt das Release"
