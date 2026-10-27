"""Contract: every board PCM opens at 44.1 kHz only.

Compiles hardware_stub.c and tests/sp16_rate_contract.c for ARM in a throwaway Debian image and runs the contract under
qemu-user, as arm-contracts.sh does for the other preload contracts. Needs a working Docker; skipped without one.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
DOCKERFILE = """FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc-arm-linux-gnueabi libc6-dev-armel-cross qemu-user-static \\
 && rm -rf /var/lib/apt/lists/*
"""
SCRIPT = """set -e; mkdir -p /tmp/run; CC="arm-linux-gnueabi-gcc -Wall -Wextra"
$CC -std=c11 -O2 -fPIC -shared -nostdlib -march=armv7-a -mfpu=vfpv3 -mfloat-abi=softfp -fno-stack-protector \\
 /src/hardware_stub.c -Wl,--no-as-needed -l:libdl.so.2 -lgcc -o /tmp/run/libsp16-ui.so 2>/dev/null
$CC -DSP16_FAKE_ALSA -shared -fPIC /src/tests/sp16_rate_contract.c -o /tmp/run/libsp16-fake-alsa.so
$CC /src/tests/sp16_rate_contract.c -L/tmp/run -lsp16-fake-alsa -o /tmp/run/t
PRE="qemu-arm-static -L /usr/arm-linux-gnueabi -E LD_PRELOAD=/tmp/run/libsp16-ui.so -E LD_LIBRARY_PATH=/tmp/run"
$PRE /tmp/run/t && echo PASS-fixed-rate
$PRE -E SP16_PCM_ANY_RATE=1 /tmp/run/t any && echo PASS-any-rate
"""


def docker_works():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0


class RateContractTests(unittest.TestCase):
    def test_source_defaults_to_the_codec_rate(self):
        source = (TOOLS / "hardware_stub.c").read_text()
        self.assertIn("#define CODEC_RATE 44100u", source)
        self.assertNotIn(".rate=48000", source)

    @unittest.skipUnless(docker_works(), "docker not available")
    def test_all_three_streams_open_at_44100(self):
        with tempfile.TemporaryDirectory() as work:
            (Path(work) / "Dockerfile").write_text(DOCKERFILE)
            iid = Path(work) / "iid"
            build = subprocess.run(["docker", "build", "-q", "--iidfile", str(iid), work], capture_output=True, text=True,
                                   timeout=900)
            self.assertEqual(build.returncode, 0, build.stderr[-2000:])
            image = iid.read_text().strip()
            try:
                run = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{TOOLS}:/src:ro",
                                      "--entrypoint", "/bin/sh", image, "-c", SCRIPT], capture_output=True, text=True,
                                     timeout=600)
            finally:
                subprocess.run(["docker", "image", "rm", image], capture_output=True)
            self.assertEqual(run.returncode, 0, run.stdout[-2000:] + run.stderr[-2000:])
            self.assertIn("PASS-fixed-rate", run.stdout)
            self.assertIn("PASS-any-rate", run.stdout)


if __name__ == "__main__":
    unittest.main()
