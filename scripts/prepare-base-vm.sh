#!/usr/bin/env bash
#
# prepare-base-vm.sh: make a Debian or Ubuntu VM ready to be the base image for vmware-sandbox-mcp.
#
# Run it INSIDE the base VM, as root, with the name of the account the server logs in as:
#
#     su -
#     bash /tmp/prepare-base-vm.sh debian
#
# It first lists what is missing and asks before it changes anything. It can change:
#
#   packages    open-vm-tools, sudo and everyday tools (git, curl, build tools, python, ...)
#   sudo        passwordless sudo for that account; sudo keeps the web-mode proxy settings
#   autologin   the desktop logs that account in by itself (GNOME with GDM only)
#   desktop     no screen lock, no blank screen, no animations (GNOME only)
#   updates     no background updates that grab the package lock right after a sandbox boots
#
# Everything it does is repeatable: running it again changes nothing that is already right.
# It refuses to make changes unless the machine is a VMware VM, because it gives the account
# passwordless sudo and logs it in automatically. Never run it on your real computer.
#
set -eEuo pipefail
trap 'echo "prepare-base-vm: stopped at line $LINENO because a command failed. Fix the problem and run it again; that is safe." >&2' ERR

usage() {
  cat <<'EOF'
Usage: bash prepare-base-vm.sh [options] ACCOUNT

ACCOUNT is the login the vmware-sandbox-mcp server uses inside this VM (for example: debian).

Options:
  --check         only report what is missing; change nothing; works without root.
                  Exit code 0 means ready, 1 means something is missing.
  --yes           do not ask for confirmation (needed when there is no terminal)
  --no-tools      do not install the everyday tools (open-vm-tools and sudo are still installed)
  --no-autologin  leave the desktop login screen alone
  --keep-updates  leave background updates on
  --force         go ahead even though this does not look like a VMware VM
  -h, --help      show this text

Run it as root, inside the base VM. See docs/setup.md for the whole procedure.
EOF
}

# ---- settings and arguments ----------------------------------------------------------------

ROOT="${VMSANDBOX_ROOT:-}"   # tests only: every file read or written lives under this prefix
LOG="${VMSANDBOX_LOG:-/var/log/vmsandbox-prepare.log}"
CHECK_ONLY=0; ASSUME_YES=0; TOOLS=1; AUTOLOGIN=1; UPDATES=1; FORCE=0; ONLY=""; ACCOUNT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --no-tools) TOOLS=0 ;;
    --no-autologin) AUTOLOGIN=0 ;;
    --keep-updates) UPDATES=0 ;;
    --force) FORCE=1 ;;
    --only)   # hidden, for tests: comma-separated step names
      shift
      ONLY="${1:-}"
      [ -n "$ONLY" ] || { echo "--only needs a list of steps" >&2; exit 2; }
      ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [ -n "$ACCOUNT" ]; then echo "only one account name is allowed" >&2; exit 2; fi
      ACCOUNT="$1"
      ;;
  esac
  shift
done

if [ -z "$ACCOUNT" ]; then
  echo "Give the account the server logs in as, for example: bash prepare-base-vm.sh debian" >&2
  exit 2
fi
case "$ACCOUNT" in
  root|*[!a-z0-9_-]*|[!a-z_]*)
    echo "ACCOUNT must be a normal lowercase login name (not root), got: $ACCOUNT" >&2
    exit 2
    ;;
esac

die() { echo "prepare-base-vm: $*" >&2; exit 1; }

getent passwd "$ACCOUNT" >/dev/null || die "there is no account named $ACCOUNT in this VM"
if [ "$CHECK_ONLY" -eq 0 ] && [ -z "$ROOT" ] && [ "$(id -u)" -ne 0 ]; then
  die "run this as root (su -), or add --check to only look"
fi
export DEBIAN_FRONTEND=noninteractive

selected=()
for step in packages sudo autologin desktop updates; do
  if [ "$step" = autologin ] && [ "$AUTOLOGIN" -eq 0 ]; then continue; fi
  if [ "$step" = updates ] && [ "$UPDATES" -eq 0 ]; then continue; fi
  if [ -n "$ONLY" ]; then
    case ",$ONLY," in *",$step,"*) ;; *) continue ;; esac
  fi
  selected+=("$step")
done
[ "${#selected[@]}" -gt 0 ] || die "no steps selected"

# ---- reporting -----------------------------------------------------------------------------

todo=0
todo_steps=()
current=""
say()  { printf '[%s] %s\n' "$1" "$2"; }
ok()   { say " OK " "$1"; }
skip() { say "SKIP" "$1"; }
need() { say "TODO" "$1"; todo=$((todo + 1)); todo_steps+=("$current"); }

run_logged() {
  mkdir -p "$(dirname "$LOG")"
  if ! "$@" >>"$LOG" 2>&1; then
    echo "prepare-base-vm: this failed: $*" >&2
    tail -n 15 "$LOG" >&2
    return 1
  fi
}

# ---- step: packages ------------------------------------------------------------------------

REQUIRED_PKGS=(open-vm-tools sudo)
TOOL_PKGS=(curl wget git ca-certificates build-essential python3 python3-pip python3-venv unzip zip jq xz-utils procps iproute2 less)

# shellcheck disable=SC2016  # ${Status} is dpkg's own format field, not a shell variable
pkg_installed() { [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = "install ok installed" ]; }

missing_pkgs() {
  local wanted=("${REQUIRED_PKGS[@]}") pkg
  if [ "$TOOLS" -eq 1 ]; then wanted+=("${TOOL_PKGS[@]}"); fi
  for pkg in "${wanted[@]}"; do
    if ! pkg_installed "$pkg"; then printf '%s\n' "$pkg"; fi
  done
}

check_packages() {
  if [ -n "$ROOT" ]; then skip "packages: not touched in test mode"; return 0; fi
  if ! command -v dpkg-query >/dev/null 2>&1; then skip "packages: this is not a Debian or Ubuntu system"; return 0; fi
  local missing
  missing="$(missing_pkgs | tr '\n' ' ')"
  if [ -z "$missing" ]; then ok "packages: everything is installed"; else need "packages: missing ${missing% }"; fi
}

apply_packages() {
  local missing
  missing="$(missing_pkgs | tr '\n' ' ')"
  [ -n "$missing" ] || return 0
  say "WORK" "installing: ${missing% } (details: $LOG)"
  run_logged apt-get update
  # shellcheck disable=SC2086  # the package names are fixed words from the lists above
  run_logged apt-get install -y --no-install-recommends $missing
}

# ---- step: sudo ----------------------------------------------------------------------------

SUDOERS_FILE="$ROOT/etc/sudoers.d/vmsandbox"

sudoers_text() {
  cat <<EOF
# Written by vmware-sandbox-mcp prepare-base-vm.sh, for the throwaway sandbox account only.
Defaults:$ACCOUNT env_keep += "http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY"
$ACCOUNT ALL=(ALL:ALL) NOPASSWD:ALL
EOF
}

# Can the account use sudo without a password, and does sudo keep the proxy settings?
sudo_works_for_account() {
  # shellcheck disable=SC2016  # the text is meant to be expanded by the inner shell, not this one
  local probe='[ "$(http_proxy=probe sudo -n printenv http_proxy 2>/dev/null)" = probe ]'
  if [ "$(id -u)" -eq 0 ]; then
    su -s /bin/sh "$ACCOUNT" -c "$probe"
  else
    sh -c "$probe"
  fi
}

check_sudo() {
  if [ -n "$ROOT" ]; then
    if [ -r "$SUDOERS_FILE" ] && [ "$(cat "$SUDOERS_FILE")" = "$(sudoers_text)" ]; then
      ok "sudo: the rules for $ACCOUNT are in place"
    else
      need "sudo: $ACCOUNT has no passwordless sudo yet"
    fi
    return 0
  fi
  if [ "$(id -u)" -ne 0 ] && [ "$(id -un)" != "$ACCOUNT" ]; then
    skip "sudo: cannot be checked for another account without root"
    return 0
  fi
  if sudo_works_for_account; then
    ok "sudo: $ACCOUNT can use sudo without a password, and sudo keeps the proxy settings"
  else
    need "sudo: $ACCOUNT cannot use sudo without a password"
  fi
}

apply_sudo() {
  local tmp
  if [ -z "$ROOT" ]; then
    grep -Eqs '^[#@]includedir[[:space:]]+/etc/sudoers\.d' /etc/sudoers \
      || die "/etc/sudoers does not include /etc/sudoers.d, so the rule would have no effect. Add the line '@includedir /etc/sudoers.d' with visudo and run this again."
  fi
  tmp="$(mktemp)"
  sudoers_text >"$tmp"
  chmod 0440 "$tmp"
  if command -v visudo >/dev/null 2>&1; then
    visudo -cf "$tmp" >/dev/null || { rm -f "$tmp"; die "the sudo rule did not pass visudo's syntax check; nothing was installed"; }
  elif [ -z "$ROOT" ]; then
    rm -f "$tmp"; die "visudo was not found even though sudo should be installed"
  else
    say "WARN" "visudo is not available here, so the rule was not syntax-checked (test mode only)"
  fi
  mkdir -p "$(dirname "$SUDOERS_FILE")"
  if [ -n "$ROOT" ]; then
    install -m 0440 "$tmp" "$SUDOERS_FILE"
  else
    install -m 0440 -o root -g root "$tmp" "$SUDOERS_FILE"
  fi
  rm -f "$tmp"
}

# ---- step: autologin (GNOME / GDM) ---------------------------------------------------------

GDM_CONF="$ROOT/etc/gdm3/daemon.conf"

# The last active value of a key in the [daemon] section.
gdm_value() {
  awk -v key="$1" '
    /^\[/ { indaemon = ($0 ~ /^\[daemon\][[:space:]]*$/); next }
    indaemon && $0 ~ ("^[[:space:]]*" key "[[:space:]]*=") {
      v = $0; sub(/^[^=]*=[[:space:]]*/, "", v); sub(/[[:space:]]+$/, "", v); val = v
    }
    END { print val }
  ' "$GDM_CONF"
}

check_autologin() {
  if [ ! -f "$GDM_CONF" ]; then skip "autologin: GDM, the GNOME login screen, is not installed here"; return 0; fi
  local enabled login
  enabled="$(gdm_value AutomaticLoginEnable)"
  login="$(gdm_value AutomaticLogin)"
  case "$enabled" in true|True|TRUE|1) enabled=1 ;; *) enabled=0 ;; esac
  if [ "$enabled" -eq 1 ] && [ "$login" = "$ACCOUNT" ]; then
    ok "autologin: the desktop logs in $ACCOUNT by itself"
  else
    need "autologin: the desktop waits at the login screen instead of logging in $ACCOUNT"
  fi
}

apply_autologin() {
  local tmp
  if [ ! -f "$GDM_CONF.vmsandbox-original" ]; then cp -p "$GDM_CONF" "$GDM_CONF.vmsandbox-original"; fi
  tmp="$(mktemp)"
  # Drop active copies of the two keys inside [daemon], then add them right under the header.
  # Commented-out lines stay. If there is no [daemon] section, add one at the end.
  awk -v account="$ACCOUNT" '
    BEGIN { found = 0 }
    /^\[/ {
      indaemon = ($0 ~ /^\[daemon\][[:space:]]*$/)
      print
      if (indaemon) { found = 1; print "AutomaticLoginEnable=true"; print "AutomaticLogin=" account }
      next
    }
    indaemon && /^[[:space:]]*(AutomaticLoginEnable|AutomaticLogin)[[:space:]]*=/ { next }
    { print }
    END { if (!found) { print ""; print "[daemon]"; print "AutomaticLoginEnable=true"; print "AutomaticLogin=" account } }
  ' "$GDM_CONF" >"$tmp"
  cat "$tmp" >"$GDM_CONF"   # write through the old file so its owner and mode stay as they were
  rm -f "$tmp"
}

# ---- step: desktop (GNOME defaults) --------------------------------------------------------

DCONF_PROFILE_FILE="$ROOT/etc/dconf/profile/user"
DCONF_KEYFILE="$ROOT/etc/dconf/db/local.d/00-vmsandbox"

has_gnome() { [ -f "$GDM_CONF" ] || command -v gnome-shell >/dev/null 2>&1; }

dconf_text() {
  cat <<'EOF'
# Written by vmware-sandbox-mcp prepare-base-vm.sh: a desktop that stays awake and holds still,
# so a screenshot shows the real screen and nothing locks the agent out. Per-user settings win.
[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'

[org/gnome/desktop/interface]
enable-animations=false

[org/gnome/software]
download-updates=false
EOF
}

check_desktop() {
  if ! has_gnome; then skip "desktop: GNOME is not installed here"; return 0; fi
  local good=1
  if [ ! -f "$DCONF_KEYFILE" ] || [ "$(cat "$DCONF_KEYFILE")" != "$(dconf_text)" ]; then good=0; fi
  if [ ! -f "$DCONF_PROFILE_FILE" ] || ! grep -qx 'system-db:local' "$DCONF_PROFILE_FILE" \
     || ! grep -qx 'user-db:user' "$DCONF_PROFILE_FILE"; then good=0; fi
  if [ -z "$ROOT" ] && [ ! -f /etc/dconf/db/local ]; then good=0; fi
  if [ "$good" -eq 1 ]; then
    ok "desktop: no screen lock, no blank screen, no animations"
  else
    need "desktop: the screen may lock or blank, and animations make screenshots unreliable"
  fi
}

apply_desktop() {
  mkdir -p "$(dirname "$DCONF_PROFILE_FILE")" "$(dirname "$DCONF_KEYFILE")"
  if [ ! -f "$DCONF_PROFILE_FILE" ]; then
    printf 'user-db:user\nsystem-db:local\n' >"$DCONF_PROFILE_FILE"
  else
    if [ -n "$(tail -c1 "$DCONF_PROFILE_FILE")" ]; then echo >>"$DCONF_PROFILE_FILE"; fi
    if ! grep -qx 'system-db:local' "$DCONF_PROFILE_FILE"; then echo 'system-db:local' >>"$DCONF_PROFILE_FILE"; fi
    if ! grep -qx 'user-db:user' "$DCONF_PROFILE_FILE"; then sed -i '1i user-db:user' "$DCONF_PROFILE_FILE"; fi
  fi
  dconf_text >"$DCONF_KEYFILE"
  chmod 0644 "$DCONF_KEYFILE"
  if [ -z "$ROOT" ]; then
    if ! command -v dconf >/dev/null 2>&1; then run_logged apt-get install -y --no-install-recommends dconf-cli; fi
    dconf update
  fi
}

# ---- step: updates -------------------------------------------------------------------------

UPDATE_UNITS=(apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service unattended-upgrades.service)

# list-unit-files also works when systemd is not running (a container, a chroot); "systemctl cat" does not.
unit_exists() { systemctl list-unit-files --no-legend "$1" 2>/dev/null | grep -q .; }

check_updates() {
  if [ -n "$ROOT" ]; then skip "updates: not touched in test mode"; return 0; fi
  if ! command -v systemctl >/dev/null 2>&1; then skip "updates: systemd was not found"; return 0; fi
  local unit state active=""
  for unit in "${UPDATE_UNITS[@]}"; do
    if ! unit_exists "$unit"; then continue; fi
    state="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    case "$state" in masked|masked-runtime) ;; *) active="$active $unit" ;; esac
  done
  if [ -z "$active" ]; then
    ok "updates: no background updates that could hold the package lock"
  else
    need "updates: background updates are still on (${active# })"
  fi
}

apply_updates() {
  local unit
  if [ -d /run/systemd/system ]; then
    systemctl stop apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
  fi
  for unit in "${UPDATE_UNITS[@]}"; do
    if unit_exists "$unit"; then systemctl mask "$unit" >/dev/null 2>&1; fi
  done
}

# ---- is this really a VMware VM? ----------------------------------------------------------

is_vmware() {
  local virt=""
  if command -v systemd-detect-virt >/dev/null 2>&1; then virt="$(systemd-detect-virt 2>/dev/null || true)"; fi
  if [ "$virt" = vmware ]; then return 0; fi
  grep -qis vmware /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name
}

# ---- main ----------------------------------------------------------------------------------

if [ -n "$ROOT" ]; then
  say "SKIP" "TEST MODE: files are read and written under $ROOT; packages and services are not touched"
elif is_vmware; then
  ok "this is a VMware virtual machine"
elif [ "$FORCE" -eq 1 ]; then
  say "WARN" "this does not look like a VMware virtual machine; going on because of --force"
elif [ "$CHECK_ONLY" -eq 1 ]; then
  say "WARN" "this does not look like a VMware virtual machine"
else
  die "this does not look like a VMware virtual machine. The script gives $ACCOUNT passwordless sudo and logs it in automatically, so it must never run on your real computer. Use --force only if you are sure."
fi

run_checks() {
  todo=0
  todo_steps=()
  for current in "${selected[@]}"; do "check_$current"; done
  current=""
}

run_checks

if [ "$todo" -eq 0 ]; then
  echo
  echo "Nothing to do: this VM is ready."
  exit 0
fi
if [ "$CHECK_ONLY" -eq 1 ]; then
  echo
  echo "$todo thing(s) to set up. Run this again without --check, as root, to do it."
  exit 1
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  [ -t 0 ] || die "there is no terminal to ask on; add --yes to go ahead without asking"
  echo
  echo "The TODO items above will be changed on THIS virtual machine."
  printf 'Is this the base VM, and should I go ahead? [y/N] '
  read -r reply
  case "$reply" in y|Y|yes|YES) ;; *) echo "Nothing was changed."; exit 1 ;; esac
fi

for current in "${todo_steps[@]}"; do
  "apply_$current"
done
current=""

echo
echo "Checking the result..."
run_checks
if [ "$todo" -ne 0 ]; then
  die "$todo thing(s) are still not right, see above"
fi

cat <<EOF

Done. Next:
  1. Shut this VM down completely (poweroff), not suspend.
  2. In VMware Workstation take a snapshot of it, for example named "clean".
  3. Point VMSANDBOX_SNAPSHOT at that name and run: vmware-sandbox-mcp doctor

Every sandbox starts as a copy of this VM, so do not log in to personal accounts in it, and
keep in mind that code running in a sandbox can now use sudo.
EOF
