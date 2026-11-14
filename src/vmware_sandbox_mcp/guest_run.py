"""Running a command in the guest and getting its output back.

vmrun cannot return a guest program's output, so one script file goes in and one
tar file comes out:

  1. The host writes <wrapper><command text> to a temp file and copies it into the guest.
  2. The guest runs `/bin/bash <script> <result.tar> <seconds>`. The wrapper splits the
     command text off its own tail, runs it under `timeout`, and tars stdout, stderr
     and the exit code.
  3. The host copies the tar back and unpacks it in memory.

The agent's command text only ever travels as file content. It is never placed on a
command line, on the host or in the guest, so no quoting can go wrong.
"""

from __future__ import annotations

import io
import shlex
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass

OUTPUT_LIMIT_BYTES = 1_048_576

_WRAPPER_BODY = f"""#!/bin/bash
out="$1"; secs="$2"
work="$(mktemp -d)" || exit 70
tail -n +__FIRST_COMMAND_LINE__ "$0" > "$work/cmd.sh"
cd "$HOME" 2>/dev/null || cd /
timeout -k 2 "$secs" /bin/bash "$work/cmd.sh" > "$work/stdout.full" 2> "$work/stderr.full" < /dev/null
echo $? > "$work/exit"
for s in stdout stderr; do
  wc -c < "$work/$s.full" > "$work/$s.size"
  head -c {OUTPUT_LIMIT_BYTES} "$work/$s.full" > "$work/$s"
done
tar -C "$work" -cf "$out" stdout stderr stdout.size stderr.size exit
status=$?
rm -rf "$work" "$0"
exit $status
"""
_WRAPPER = _WRAPPER_BODY.replace("__FIRST_COMMAND_LINE__", str(_WRAPPER_BODY.count("\n") + 1))


@dataclass(frozen=True)
class RunResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


def build_script(command: str, env: Mapping[str, str] | None = None) -> bytes:
    """env holds server-chosen variables (never agent text) exported before the command."""
    exports = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in (env or {}).items())
    return (_WRAPPER + exports + command.replace("\r\n", "\n") + "\n").encode("utf-8")


def parse_result(tar_bytes: bytes) -> RunResult:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                files[member.name] = tar.extractfile(member).read()  # type: ignore[union-attr]

    def text(name: str) -> str:
        return files.get(name, b"").decode("utf-8", errors="replace")

    def size(name: str) -> int:
        try:
            return int(text(name).strip())
        except ValueError:
            return 0

    exit_code = int(text("exit").strip())
    return RunResult(
        stdout=text("stdout"),
        stderr=text("stderr"),
        exit_code=exit_code,
        # GNU timeout: 124 = time limit hit, 137 = had to be killed after the limit.
        timed_out=exit_code in (124, 137),
        stdout_truncated=size("stdout.size") > OUTPUT_LIMIT_BYTES,
        stderr_truncated=size("stderr.size") > OUTPUT_LIMIT_BYTES,
    )
