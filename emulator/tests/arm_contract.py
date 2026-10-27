"""Run an ARM preload contract (sp16_*_contract.c) against hardware_stub.c in a throwaway Debian image under qemu-user.

Same compile and preload lines as arm-contracts.sh, without the packaged guest image: the image holds only the ARM
cross compiler and qemu-user, so it builds in about a minute and is removed afterwards. Needs a working Docker.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile

TOOLS = Path(__file__).resolve().parents[1]
DOCKERFILE = """FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc-arm-linux-gnueabi libc6-dev-armel-cross qemu-user-static \\
 && rm -rf /var/lib/apt/lists/*
"""
PREPARE = """set -e; mkdir -p /tmp/run; CC="arm-linux-gnueabi-gcc -Wall -Wextra"
$CC -std=c11 -O2 -fPIC -shared -nostdlib -march=armv7-a -mfpu=vfpv3 -mfloat-abi=softfp -fno-stack-protector \\
 /src/hardware_stub.c -Wl,--no-as-needed -l:libdl.so.2 -lgcc -o /tmp/run/libsp16-ui.so 2>/dev/null
$CC -DSP16_FAKE_ALSA -shared -fPIC /src/tests/%(test)s.c -o /tmp/run/libsp16-fake-alsa.so
$CC /src/tests/%(test)s.c -L/tmp/run -lsp16-fake-alsa -lm -o /tmp/run/t
PRE="qemu-arm-static -L /usr/arm-linux-gnueabi -E LD_PRELOAD=/tmp/run/libsp16-ui.so -E LD_LIBRARY_PATH=/tmp/run"
"""


def docker_works():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class ArmContract:
    """Context manager: builds the image once; run(test, commands) compiles the contract and runs each command.

    commands are shell lines using $PRE (the qemu-user line with the preload) and /tmp/run/t; the combined output of
    the stub's own log lines and the test comes back as text. A non-zero exit raises CalledProcessError.
    """

    def __enter__(self):
        self.work = tempfile.TemporaryDirectory()
        (Path(self.work.name) / "Dockerfile").write_text(DOCKERFILE)
        iid = Path(self.work.name) / "iid"
        subprocess.run(["docker", "build", "-q", "--iidfile", str(iid), self.work.name], capture_output=True, check=True,
                       timeout=900)
        self.image = iid.read_text().strip()
        return self

    def __exit__(self, *exc):
        subprocess.run(["docker", "image", "rm", self.image], capture_output=True)
        self.work.cleanup()

    def run(self, test, commands):
        script = PREPARE % {"test": test} + "\n".join(commands) + "\n"
        run = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{TOOLS}:/src:ro", "--entrypoint",
                              "/bin/sh", self.image, "-c", script], capture_output=True, text=True, timeout=600)
        if run.returncode:
            raise subprocess.CalledProcessError(run.returncode, "contract " + test, run.stdout, run.stderr)
        return run.stdout + run.stderr
