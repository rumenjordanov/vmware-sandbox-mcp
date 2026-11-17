"""Test scripts/prepare-base-vm.sh on a clean Debian 13 container. Needs Docker; takes a few minutes.

    python scripts/test_prepare_base_vm.py

The container stands in for the base VM: it gets the real login-screen config that the Debian
gdm3 package ships, an ordinary account, and then the script is run for real against it.
Exits 0 when every check passed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

IMAGE = "debian:trixie"

INNER = r"""
set -u
fail=0
t() {  # t "what is checked" command...
  local what="$1"; shift
  if "$@" >/tmp/t.out 2>&1; then echo "  [ok] $what"
  else echo "  [FAIL] $what"; tail -n 12 /tmp/t.out | sed 's/^/         /'; fail=$((fail + 1)); fi
}
has() { grep -q -- "$1" /tmp/last.out; }          # the last run printed this
run() { bash "$S" "$@" >/tmp/last.out 2>&1; echo $? >/tmp/last.rc; }   # remember output and exit code
rc() { [ "$(cat /tmp/last.rc)" = "$1" ]; }
export DEBIAN_FRONTEND=noninteractive
S=/scripts/prepare-base-vm.sh

echo "preparing the container"
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq --no-install-recommends shellcheck systemd dconf-cli procps >/dev/null 2>&1
( cd /tmp && apt-get download gdm3 >/dev/null 2>&1 && dpkg-deb -x gdm3_*.deb /tmp/gdm )
mkdir -p /etc/gdm3
if [ -f /tmp/gdm/etc/gdm3/daemon.conf ]; then cp /tmp/gdm/etc/gdm3/daemon.conf /etc/gdm3/daemon.conf
else printf '[daemon]\n# Uncomment the line below to force the login screen to use Xorg\n#WaylandEnable=false\n\n#  AutomaticLoginEnable = true\n#  AutomaticLogin = user1\n\n[security]\n' >/etc/gdm3/daemon.conf; echo "  (using a stand-in daemon.conf: the package file was not found)"; fi
cp /etc/gdm3/daemon.conf /tmp/daemon.conf.shipped
useradd -m tester
useradd -m other

echo "static"
t "shellcheck finds nothing" shellcheck -s bash "$S"
t "no Windows line endings" bash -c "! grep -q $'\r' $S"

echo "arguments"
run --help;               t "--help works" rc 0
run --check;              t "no account is refused" rc 2
run --check root;         t "root is refused" rc 2
run --check 'Bad;Name';   t "a bad name is refused" rc 2
run --check --bogus tester; t "an unknown option is refused" rc 2
run --check nobodyhere;   t "an unknown account is refused" rc 1
run --yes tester;         t "a machine that is not VMware is refused" rc 1
t "  ...with the reason" has "does not look like a VMware"
run --force tester </dev/null; t "no terminal and no --yes is refused" rc 1
t "  ...with the reason" has "no terminal"

echo "a fresh machine"
run --check --force tester;   t "--check says it is not ready (exit 1)" rc 1
for step in packages sudo autologin desktop; do t "  ...$step is listed as TODO" has "TODO\] $step"; done
run --check --force other >/dev/null; sha256sum /etc/gdm3/daemon.conf >/tmp/before.sum
t "--check changed nothing" bash -c "[ ! -e /etc/sudoers.d/vmsandbox ] && sha256sum -c --quiet /tmp/before.sum"

echo "applying"
run --force --yes tester;     t "it applies cleanly (exit 0)" rc 0
t "  ...and ends with the next steps" has "Shut this VM down"
tail -n 25 /tmp/last.out | sed 's/^/         /'
t "sudoers file has mode 0440" bash -c '[ "$(stat -c %a /etc/sudoers.d/vmsandbox)" = 440 ]'
t "sudoers passes visudo" visudo -c
t "tester can use sudo without a password" su -s /bin/sh tester -c 'sudo -n true'
t "sudo keeps the proxy variables" su -s /bin/sh tester -c '[ "$(http_proxy=probe sudo -n printenv http_proxy)" = probe ]'
t "the other account got nothing" su -s /bin/sh other -c '! sudo -n true'
t "tools: git curl gcc jq python venv" bash -c 'git --version && curl --version && gcc --version && jq --version && python3 -m venv --help'
t "autologin: one active copy of each key" bash -c '[ "$(grep -c "^AutomaticLoginEnable=true" /etc/gdm3/daemon.conf)" = 1 ] && [ "$(grep -c "^AutomaticLogin=tester" /etc/gdm3/daemon.conf)" = 1 ]'
t "autologin: keys sit under [daemon]" bash -c 'awk "/^\[/{s=\$0} /^AutomaticLogin=tester/{print s}" /etc/gdm3/daemon.conf | grep -qx "\[daemon\]"'
t "autologin: the shipped comments are still there" bash -c 'diff <(grep "^[#]" /tmp/daemon.conf.shipped) <(grep "^[#]" /etc/gdm3/daemon.conf)'
t "autologin: original saved" test -f /etc/gdm3/daemon.conf.vmsandbox-original
t "desktop: dconf profile and database" bash -c 'grep -qx system-db:local /etc/dconf/profile/user && grep -qx user-db:user /etc/dconf/profile/user && test -f /etc/dconf/db/local'
t "desktop: the setting is readable" su -s /bin/sh tester -c 'test "$(dconf read /org/gnome/desktop/session/idle-delay)" = "uint32 0"'
t "updates: timers are masked" bash -c '[ "$(systemctl is-enabled apt-daily.timer)" = masked ] && [ "$(systemctl is-enabled apt-daily-upgrade.timer)" = masked ]'

echo "running it again"
run --check --force tester;   t "--check now says ready (exit 0)" rc 0
sha256sum /etc/sudoers.d/vmsandbox /etc/gdm3/daemon.conf /etc/dconf/db/local.d/00-vmsandbox /etc/dconf/profile/user >/tmp/before.sum
run --force --yes tester;     t "a second run has nothing to do" rc 0
t "  ...and says so" has "Nothing to do"
t "  ...and changes no file" sha256sum -c --quiet /tmp/before.sum

echo "repairing"
rm -f /etc/sudoers.d/vmsandbox
sed -i 's/^AutomaticLogin=tester/AutomaticLogin=other/' /etc/gdm3/daemon.conf
run --check --force tester;   t "damage is noticed" rc 1
t "  ...sudo" has "TODO\] sudo"
t "  ...autologin" has "TODO\] autologin"
run --force --yes tester;     t "damage is repaired" rc 0
run --check --force tester;   t "and it is ready again" rc 0
cp /tmp/daemon.conf.shipped /etc/gdm3/daemon.conf
run --force --yes tester;     t "works on the shipped file again" rc 0
t "  ...with one active copy of each key" bash -c '[ "$(grep -c "^AutomaticLogin" /etc/gdm3/daemon.conf)" = 2 ]'
printf '[security]\nDisallowTCP=true\n' >/etc/gdm3/daemon.conf
run --force --yes tester;     t "works on a file without a [daemon] section" rc 0
t "  ...and keeps the other section" bash -c 'grep -qx DisallowTCP=true /etc/gdm3/daemon.conf && grep -qx "\[daemon\]" /etc/gdm3/daemon.conf'
mv /etc/dconf/profile/user /tmp/profile.bak; printf 'system-db:site\n' >/etc/dconf/profile/user
run --force --yes tester;     t "adds the missing lines to an existing dconf profile" rc 0
t "  ...keeping the line that was there" bash -c 'grep -qx system-db:site /etc/dconf/profile/user && [ "$(head -n1 /etc/dconf/profile/user)" = user-db:user ] && grep -qx system-db:local /etc/dconf/profile/user'

echo "test mode, as an ordinary user, under a fake root"
mkdir -p /tmp/fr/etc/gdm3 && cp /tmp/daemon.conf.shipped /tmp/fr/etc/gdm3/daemon.conf && chmod -R a+rwX /tmp/fr
t "writes only below the fake root" su -s /bin/sh tester -c "VMSANDBOX_ROOT=/tmp/fr bash $S --yes --only sudo,autologin,desktop other >/tmp/tm.out 2>&1; cat /tmp/tm.out"
t "  ...sudo rule" test -f /tmp/fr/etc/sudoers.d/vmsandbox
t "  ...autologin names the account" grep -qx AutomaticLogin=other /tmp/fr/etc/gdm3/daemon.conf
t "  ...dconf file" test -f /tmp/fr/etc/dconf/db/local.d/00-vmsandbox
t "  ...and a second check there is clean" su -s /bin/sh tester -c "VMSANDBOX_ROOT=/tmp/fr bash $S --check --only sudo,autologin,desktop other"

echo
if [ "$fail" -eq 0 ]; then echo "ALL PASSED"; else echo "$fail FAILED"; fi
exit "$fail"
"""


def main() -> int:
    scripts = Path(__file__).resolve().parent
    done = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{scripts}:/scripts:ro", IMAGE, "bash", "-c", INNER]
    )
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
