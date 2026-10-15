#!/usr/bin/env bash
# Isolated updater installation must preserve all unrelated installed files.
set -euo pipefail
if [[ $(id -u) == 0 ]]; then
  echo 'Refusing installer test as root' >&2
  exit 2
fi
REPO=$(cd "$(dirname "$0")/.." && pwd)
SB=$(mktemp -d)
trap 'rm -rf "$SB"' EXIT
mkdir -p "$SB/bin" "$SB/root/usr/local/sbin" "$SB/root/etc/mihomo" "$SB/root/etc/systemd/system"
for file in usr/local/sbin/mihomo-build-config usr/local/sbin/mihomo-refresh etc/mihomo/config.yaml etc/systemd/system/mihomo.service etc/systemd/system/mihomo-refresh.timer; do
  printf 'existing fixture %s\n' "$file" > "$SB/root/$file"
done
(cd "$SB/root" && find . -type f -exec sha256sum {} + | sort) > "$SB/before"
cat > "$SB/bin/systemctl" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >> "$TEST_SYSTEMCTL_LOG"
exit 0
STUB
cat > "$SB/bin/curl" <<'STUB'
#!/bin/sh
echo 'Unexpected network access in local updater installation' >&2
exit 99
STUB
chmod +x "$SB/bin/systemctl" "$SB/bin/curl"
export TEST_SYSTEMCTL_LOG="$SB/systemctl.log"
PATH="$SB/bin:$PATH" DESTDIR="$SB/root" SOURCE_DIR="$REPO" bash "$REPO/install.sh" --step updater > "$SB/output" 2>&1 || { cat "$SB/output"; exit 1; }
(cd "$SB/root" && sha256sum -c "$SB/before")
for file in usr/local/sbin/mihomo-update etc/systemd/system/mihomo-update.service etc/systemd/system/mihomo-update.timer; do
  test -f "$SB/root/$file"
  cmp "$REPO/$file" "$SB/root/$file"
done
test -x "$SB/root/usr/local/sbin/mihomo-update"
grep -q '^daemon-reload$' "$SB/systemctl.log"
if grep -Eq 'enable|restart|reload mihomo|mihomo-refresh' "$SB/systemctl.log"; then
  echo 'Updater-only installation changed running services or timer choice' >&2
  exit 1
fi
grep -Eq '^[[:space:]]*Persistent=true' "$SB/root/etc/systemd/system/mihomo-update.timer"
grep -Eq '^[[:space:]]*RandomizedDelaySec=1h' "$SB/root/etc/systemd/system/mihomo-update.timer"
echo 'PASS isolated updater installation'
