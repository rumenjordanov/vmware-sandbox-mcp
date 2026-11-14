"""Background commands ("jobs") inside a sandbox.

A job lives entirely in the guest, in /tmp/.sbx-job-<id>/:

    cmd.sh   the agent's command text (copied in as a file, never on a command line)
    pid      process id of the job's shell, which leads its own session and process group
    stdout   everything the command printed
    stderr
    exit     the exit code, written when the command ends by itself

The three scripts below are fixed text written by this server. The only variable part is
the job id, which the server generates and validates (job-<8 hex digits>). The first line
of each script names the operation; nothing else depends on it.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from .guest_run import RunResult

MAX_JOBS_PER_SANDBOX = 8
JOB_OUTPUT_LIMIT_BYTES = 1_000_000  # newest output returned by a check, per stream
_JOB_ID = re.compile(r"^job-[0-9a-f]{8}\Z")


class JobError(Exception):
    """A problem the agent can understand and act on."""


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    status: str  # "running", "exited" (ended by itself) or "stopped" (killed)
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool  # older output was left out; only the newest part is returned
    stderr_truncated: bool


def new_job_id() -> str:
    return f"job-{secrets.token_hex(4)}"


def valid_job_id(job_id: object) -> str:
    if not isinstance(job_id, str) or not _JOB_ID.match(job_id):
        raise JobError(f"{job_id!r} is not a valid job id.")
    return job_id


def command_path(job_id: str) -> str:
    """Where the host drops the command text before starting the job."""
    return f"/tmp/.sbx-job-{valid_job_id(job_id)}.cmd"


def start_script(job_id: str) -> str:
    return f"""# sbx-job start {valid_job_id(job_id)}
d=/tmp/.sbx-job-{job_id}
n=0
for p in /tmp/.sbx-job-*/pid; do
  [ -f "$p" ] || continue
  [ -f "${{p%pid}}exit" ] && continue
  kill -0 "$(cat "$p")" 2>/dev/null && n=$((n+1))
done
if [ "$n" -ge {MAX_JOBS_PER_SANDBOX} ]; then rm -f "$d.cmd"; echo limit; exit 0; fi
mkdir -m 700 "$d" && mv "$d.cmd" "$d/cmd.sh" || {{ echo failed; exit 0; }}
: > "$d/stdout"; : > "$d/stderr"
cd "$HOME" 2>/dev/null || cd /
nohup setsid /bin/bash -c 'echo $$ > "$0/pid"; /bin/bash "$0/cmd.sh" > "$0/stdout" 2> "$0/stderr" < /dev/null; echo $? > "$0/exit"' "$d" > /dev/null 2>&1 < /dev/null &
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do [ -s "$d/pid" ] && break; sleep 0.1; done
if [ -s "$d/pid" ]; then echo started; else echo failed; fi
"""


def check_script(job_id: str) -> str:
    return f"""# sbx-job check {valid_job_id(job_id)}
d=/tmp/.sbx-job-{job_id}
[ -d "$d" ] || {{ echo missing; exit 0; }}
if [ -f "$d/exit" ]; then s="exited $(cat "$d/exit")"
elif kill -0 "$(cat "$d/pid" 2>/dev/null)" 2>/dev/null; then s="running -"
else s="stopped -"; fi
echo "$s $(wc -c < "$d/stdout") $(wc -c < "$d/stderr")"
tail -c {JOB_OUTPUT_LIMIT_BYTES} "$d/stdout"
tail -c {JOB_OUTPUT_LIMIT_BYTES} "$d/stderr" >&2
"""


def stop_script(job_id: str) -> str:
    return f"""# sbx-job stop {valid_job_id(job_id)}
d=/tmp/.sbx-job-{job_id}
[ -d "$d" ] || {{ echo missing; exit 0; }}
pid=$(cat "$d/pid" 2>/dev/null)
if [ -n "$pid" ] && kill -0 -- "-$pid" 2>/dev/null; then
  kill -TERM -- "-$pid" 2>/dev/null
  for i in 1 2 3 4 5 6; do kill -0 -- "-$pid" 2>/dev/null || break; sleep 0.5; done
  kill -KILL -- "-$pid" 2>/dev/null
  echo stopped
else
  echo finished
fi
"""


def first_word(result: RunResult) -> str:
    return (result.stdout.split() or [""])[0]


def parse_check(job_id: str, result: RunResult) -> JobStatus:
    header, _, output = result.stdout.partition("\n")
    fields = header.split()
    if fields[:1] == ["missing"]:
        raise JobError(f"No background job {job_id} in this sandbox.")
    try:
        status, code, out_size, err_size = fields
        exit_code = int(code) if status == "exited" else None
        sizes = int(out_size), int(err_size)
    except ValueError:
        raise JobError("The sandbox gave an unreadable answer about the job.") from None
    if status not in ("running", "exited", "stopped"):
        raise JobError("The sandbox gave an unreadable answer about the job.")
    return JobStatus(
        job_id=job_id,
        status=status,
        exit_code=exit_code,
        stdout=output,
        stderr=result.stderr,
        stdout_truncated=sizes[0] > JOB_OUTPUT_LIMIT_BYTES,
        stderr_truncated=sizes[1] > JOB_OUTPUT_LIMIT_BYTES,
    )
