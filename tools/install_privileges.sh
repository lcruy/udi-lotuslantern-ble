#!/bin/sh
# One-time setup so the udi-lotuslantern-ble node server can manage the eisy's
# Bluetooth adapter. PG3 runs node servers as a normal user (admin), but
# switching LE on, scanning and cancelling a stuck connection need root.
#
#   cd ~/nodeservers/udi-lotuslantern-ble     (or wherever PG3 installed it)
#   sudo sh tools/install_privileges.sh            # for user "admin"
#   sudo sh tools/install_privileges.sh someuser   # another user
#   sudo sh tools/install_privileges.sh --remove   # undo
#
# It allows exactly two programs, both root-owned and outside the node
# server's folder, to run as root without a password:
#   /usr/sbin/hccontrol                         (FreeBSD's Bluetooth tool)
#   /usr/local/libexec/lotuslantern-hci-cancel  (copy of tools/hci_cancel.py)
# Nothing from the node server's own (user-writable) folder runs as root.

set -eu

HELPER=/usr/local/libexec/lotuslantern-hci-cancel
RULE=/usr/local/etc/sudoers.d/lotuslantern-ble
VISUDO=/usr/local/sbin/visudo
HERE=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo sh tools/install_privileges.sh" >&2
    exit 1
fi

if [ "${1:-}" = "--remove" ]; then
    rm -f "$RULE" "$HELPER"
    echo "Removed $RULE and $HELPER."
    exit 0
fi

USER_NAME=${1:-admin}
if ! id "$USER_NAME" >/dev/null 2>&1; then
    echo "No such user: $USER_NAME" >&2
    exit 1
fi

# 1. Root-owned copy of the cancel helper.
mkdir -p /usr/local/libexec
install -o root -g wheel -m 0755 "$HERE/hci_cancel.py" "$HELPER"

# 2. Scoped sudo rule, validated before it goes live.
mkdir -p /usr/local/etc/sudoers.d
TMP=$(mktemp /tmp/lotuslantern-sudoers.XXXXXX)
cat > "$TMP" <<EOF
# Installed by udi-lotuslantern-ble tools/install_privileges.sh
$USER_NAME ALL=(root) NOPASSWD: /usr/sbin/hccontrol, $HELPER
EOF
if [ -x "$VISUDO" ] && ! "$VISUDO" -cf "$TMP" >/dev/null; then
    rm -f "$TMP"
    echo "sudo rule failed validation; nothing changed." >&2
    exit 1
fi
install -o root -g wheel -m 0440 "$TMP" "$RULE"
rm -f "$TMP"

# 3. Make sure sudo reads sudoers.d (FreeBSD's default sudoers does).
if ! grep -Eq '^[@#]includedir[[:space:]]+/usr/local/etc/sudoers.d' /usr/local/etc/sudoers 2>/dev/null; then
    echo "WARNING: /usr/local/etc/sudoers has no '@includedir /usr/local/etc/sudoers.d' line;"
    echo "         the rule in $RULE will not take effect until it does."
fi

# 4. Check it works as that user, and switch LE on now.
if su -m "$USER_NAME" -c "/usr/local/bin/sudo -n /usr/sbin/hccontrol -n ubt0hci read_le_host_support" >/dev/null 2>&1; then
    echo "OK: $USER_NAME can now run hccontrol and the cancel helper without a password."
else
    echo "WARNING: test as $USER_NAME failed; check 'sudo -l -U $USER_NAME'."
fi
/usr/sbin/hccontrol -n ubt0hci write_le_host_support 1 0 >/dev/null 2>&1 || true
/usr/sbin/hccontrol -n ubt0hci le_enable enable >/dev/null 2>&1 || true
echo "Done. Restart the node server in PG3."
