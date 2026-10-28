#!/bin/sh
# Dedicated offline VM init. Never run OEM boot, update or formatting scripts.
set -eu
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
# This guest has no poweroff binary and init must never exit (the kernel panics: "Attempted to kill init"). Every way out
# of this script, normal or set -e, ends in sp16_poweroff: sync, then the kernel's own power-off (sysrq o, then the
# reboot(2) syscall through python); QEMU runs with -no-reboot, so either one ends the VM cleanly.
sp16_poweroff() {
  trap - EXIT
  sync
  echo o > /proc/sysrq-trigger 2>/dev/null || :
  python3 -c 'import ctypes; ctypes.CDLL(None).syscall(142, 0xfee1dead, 0x28121969, 0x4321fedc, 0)' || :
  while :; do sleep 60; done
}
trap sp16_poweroff EXIT
mkdir -p /dev /proc /sys /tmp /output /run
mount -t devtmpfs devtmpfs /dev
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t tmpfs -o mode=700 tmpfs /tmp
ip link set lo up
modprobe virtio_mmio || :
modprobe virtio_console
# Name the virtio ports by their QEMU names: with a block device present the serial controller is no longer
# virtio0, so /dev/vport0p1 (hard-coded in the bridge) must be a link to whichever node is "sp16.ui".
sp16_wait=0
until [ -e /sys/class/virtio-ports/vport0p1/name ] || ls /sys/class/virtio-ports/*/name >/dev/null 2>&1; do
  sp16_wait=$((sp16_wait + 1)); [ "$sp16_wait" -lt 50 ] || break; sleep .1
done
for sp16_port in /sys/class/virtio-ports/*; do
  [ -e "$sp16_port/name" ] || continue
  case "$(cat "$sp16_port/name")" in
   sp16.ui) [ -e /dev/vport0p1 ] || ln -s "/dev/${sp16_port##*/}" /dev/vport0p1 ;;
   sp16.ctl) ln -sf "/dev/${sp16_port##*/}" /dev/sp16-ctl ;;
   sp16.pcm) ln -sf "/dev/${sp16_port##*/}" /dev/sp16-pcm ;;
   sp16.gdb) ln -sf "/dev/${sp16_port##*/}" /dev/sp16-gdb ;;
  esac
done
# Network mode and optional static address (`./sp16 run --net MODE [--net-ip ADDR]`) travel in the kernel command
# line, the only host -> guest-init channel. The mode tells a bridged run (real LAN subnet) from host/mcast.
sp16_net_mode=
sp16_net_ip=
for sp16_token in $(cat /proc/cmdline); do
  case "$sp16_token" in
   sp16.net=*) sp16_net_mode=${sp16_token#sp16.net=} ;;
   sp16.netip=*) sp16_net_ip=${sp16_token#sp16.netip=} ;;
  esac
done
# Network (./sp16 run --net): QEMU adds a virtio-net device only then. The app's Pro DJ Link sockets live on eth0, so bring
# it up when the device exists (virtio device id 1); without one the guest has lo only, as before.
sp16_have_nic=
for sp16_dev in /sys/bus/virtio/devices/*/device; do
  [ -e "$sp16_dev" ] && [ "$(cat "$sp16_dev")" = 0x0001 ] && sp16_have_nic=1
done
if [ -n "$sp16_have_nic" ]; then
  modprobe virtio_net || :
  sp16_wait=0
  until [ -e /sys/class/net/eth0 ]; do
    sp16_wait=$((sp16_wait + 1)); [ "$sp16_wait" -lt 30 ] || break; sleep .1
  done
  if [ -e /sys/class/net/eth0 ]; then
    ip link set eth0 up
    # The unit's rc.local gives eth0 an address before the app starts (ifconfig from fw_printenv ipaddr); the app crashes
    # (SIGSEGV) on an eth0 that has none. So: a link-local address from the MAC, 169.254.A.B with A and B from its last
    # two bytes and kept off .0 and .255. The MAC is random per start (net/qemu-net.sh), so instances differ. This alias
    # stays even on a bridged run: Pro DJ Link peers self-assign 169.254, and the unit's own Pro DJ Link socket lives there.
    sp16_mac=$(cat /sys/class/net/eth0/address)
    sp16_b5=$(echo "$sp16_mac" | cut -d: -f5); sp16_b6=$(echo "$sp16_mac" | cut -d: -f6)
    sp16_ip="169.254.$((0x$sp16_b5 % 254 + 1)).$((0x$sp16_b6 % 254 + 1))"
    ip addr add "$sp16_ip/16" broadcast 169.254.255.255 dev eth0
    # Bridged mode: the guest sits on the real LAN, so besides the link-local alias it needs a reachable address on the
    # interface's subnet. --net-ip gives it directly (native.py validates and normalises it to a.b.c.d/prefix); without
    # one, DHCP is tried when a client is present (the base guest ships none yet, so the run stays on the alias).
    case "$sp16_net_mode" in
     bridged:*)
      if [ -n "$sp16_net_ip" ]; then
        ip addr add "$sp16_net_ip" dev eth0 || echo "SP16_NET bad --net-ip $sp16_net_ip"
        echo "SP16_NET eth0 $sp16_mac $sp16_ip + $sp16_net_ip (bridged $sp16_net_mode)"
      elif command -v udhcpc >/dev/null 2>&1; then
        udhcpc -i eth0 -q || :
        echo "SP16_NET eth0 $sp16_mac $sp16_ip (bridged $sp16_net_mode, DHCP)"
      else
        echo "SP16_NET eth0 $sp16_mac $sp16_ip (bridged $sp16_net_mode, no DHCP client; add --net-ip for a real-subnet address)"
      fi ;;
     *) echo "SP16_NET eth0 $sp16_mac $sp16_ip" ;;
    esac
  else
    sp16_have_nic=
    echo "SP16_NET no eth0 (virtio_net module missing from this guest image? rebuild with the current Dockerfile)"
  fi
fi

# Firmware import boot (decoupled base image only, docs/architecture.md M2): the user's update and an output disk
# are attached; guest-firmware.py decrypts, assembles and writes the read-only firmware image, then the VM powers off.
case " $(cat /proc/cmdline) " in
 *" sp16.mode=import "*)
  modprobe virtio_blk || :
  python3 -u /opt/sp16/guest-firmware.py import > /dev/console 2>&1 || echo 'SP16_IMPORT_ERROR importer failed' > /dev/console
  sp16_poweroff ;;
 *" sp16.mode=package "*)
  # Repacked update for the emulator only (D3): verified base update + recipes -> re-encrypted payload on the output disk.
  modprobe virtio_blk || :
  python3 -u /opt/sp16/guest-firmware.py package > /dev/console 2>&1 || echo 'SP16_PACKAGE_ERROR packager failed' > /dev/console
  sp16_poweroff ;;
esac
mkdir -m 700 /tmp/sp16-root
# The decoupled base image carries no OEM files: it verifies the firmware image disk against its manifest hash and
# mounts it read-only under a tmpfs overlay.
sp16_devlayer=0
sp16_profile_env=
# The firmware-specific runtime layout (app path and working directory, emulated CPU, library path, tmpfs
# directories, data/eMMC/USB mount points, allowed app arguments) comes from the firmware profile chosen at attach,
# never from this script.
sp16_set_runtime() {
  [ $# -eq 10 ] || return 1
  sp16_profile=$1 sp16_app_path=$2 sp16_app_cwd=$3 sp16_cpu=$4 sp16_ldpath=$5 sp16_tmpfs_dirs=$6 \
    sp16_data_dir=$7 sp16_emmc_mount=$8 sp16_usb_mount=$9 sp16_app_args=${10}
}
modprobe virtio_blk
sp16_identity=$(python3 /opt/sp16/guest-firmware.py attach 2>/dev/console) || {
  echo 'SP16_FIRMWARE_ERROR firmware refused; powering off' > /dev/console; exit 1; }
set -- $sp16_identity
sp16_variant=$1 sp16_firmware=$2 sp16_profile_how=$3
shift 3
sp16_set_runtime "$@" || { echo 'SP16_FIRMWARE_ERROR invalid runtime layout; powering off' > /dev/console; exit 1; }
sp16_data_seed=/tmp/sp16-root$sp16_data_dir
sp16_profile_env="SP16_PROFILE=$sp16_profile"
[ "$sp16_profile_how" = verified ] || echo "SP16_FIRMWARE_UNVERIFIED profile=$sp16_profile chosen=$sp16_profile_how" > /dev/console
# Our own staged files (the synthetic pad calibration), never OEM content; they land in the overlay's upper layer.
if [ -d /opt/sp16/stage ]; then cp -a /opt/sp16/stage/. /tmp/sp16-root/; fi
# Developer overlay (M6, research builds only: the app attaches the host folder read-only over 9p). Its files,
# recipes and preload libraries are applied into the overlay's upper layer before each app start.
case " $(cat /proc/cmdline) " in
 *" sp16.dev=1 "*)
  mkdir -p /opt/sp16/dev-share
  if modprobe 9pnet_virtio && modprobe 9p \
     && mount -t 9p -o trans=virtio,version=9p2000.L,ro,msize=524288 sp16dev /opt/sp16/dev-share; then
    sp16_devlayer=1 sp16_variant="$sp16_variant+dev"
  else
    echo 'SP16_DEV_ERROR developer overlay share unavailable' > /dev/console
  fi ;;
esac
cp /usr/bin/qemu-arm-static /tmp/sp16-root/qemu-arm-static
mkdir -p /tmp/sp16-root/opt/sp16 /tmp/sp16-root/dev
cp /opt/sp16/libsp16-ui.so /tmp/sp16-root/opt/sp16/
# Developer overlay only: show the read-only share to the firmware app too (it runs chrooted at /tmp/sp16-root and
# cannot see /opt/sp16/dev-share otherwise). A preload library can then read a newer library from the host folder and
# load it without an app restart. The share stays read-only (9p mount and bind); the app can read the mod folder you
# chose and nothing else. Without sp16.dev=1 nothing is mounted, so a normal build is unchanged.
if [ "$sp16_devlayer" = 1 ]; then
  mkdir -p /tmp/sp16-root/opt/sp16/dev-share
  if mount --bind /opt/sp16/dev-share /tmp/sp16-root/opt/sp16/dev-share \
     && mount -o remount,bind,ro /tmp/sp16-root/opt/sp16/dev-share; then
    echo 'SP16_DEV_SHARE app-visible=yes path=/opt/sp16/dev-share' > /dev/console
  else
    echo 'SP16_DEV_ERROR developer overlay share not visible to the app' > /dev/console
  fi
fi
: > /tmp/sp16-root/dev/null
: > /tmp/sp16-root/dev/full
# Audio transport (native.py build --audio file): give the chroot the sp16.pcm port as a character node so the
# stub's forwarder thread can stream the OEM's playback frames to the host file sink. Off unless asked for.
sp16_audio_env=
case " $(cat /proc/cmdline) " in
 *" sp16.audio=file "*)
  if [ -e /dev/sp16-pcm ]; then
    python3 -c 'import os, stat; os.mknod("/tmp/sp16-root/dev/sp16-pcm", 0o600 | stat.S_IFCHR, os.stat("/dev/sp16-pcm").st_rdev)'
    sp16_audio_env="-E SP16_PCM_PORT=/dev/sp16-pcm"
  else
    echo 'SP16_AUDIO port=missing' > /dev/console
  fi ;;
esac
printf 'pcm.null { type null }\n' > /tmp/sp16-root/etc/sp16-alsa.conf
# Persistent eMMC (fullpanel builds only: native.py adds sp16.data=1 with a virtio block device). This is our
# trusted init, not the OEM: it formats the card once (only when it has no filesystem), repairs an unclean shutdown
# with fsck.vfat -a, and mounts it at the chroot's /mnt/emmc before the app starts. The OEM's own mkfs/mount stay
# refused by the preload. /root/data lives in a dot-directory on the same card (one image, one fsck, one clean
# unmount); files the rootfs ships there (the synthetic padcalibration.xml) are only copied when missing, so a
# newer persisted file is never overwritten. /root/pdj/project, browse, scenes and packages are tmpfs (the OEM symlinks into them).
sp16_emmc=0
# virtio-blk disks are told apart by their QEMU serial (sp16emmc, sp16usb): with a virtual USB stick attached the
# probe order no longer guarantees that the eMMC is vda. Prints the device node, or fails after about 5 s.
sp16_disk() {
  sp16_n=0
  while [ "$sp16_n" -lt 50 ]; do
    for sp16_blk in /sys/block/vd*; do
      [ -e "$sp16_blk/serial" ] || continue
      if [ "$(cat "$sp16_blk/serial" 2>/dev/null)" = "$1" ] && [ -b "/dev/${sp16_blk##*/}" ]; then
        echo "/dev/${sp16_blk##*/}"; return 0
      fi
    done
    sp16_n=$((sp16_n + 1)); sleep .1
  done
  return 1
}
sp16_data_setup() {
  modprobe virtio_blk && modprobe vfat || return 1
  sp16_dev=$(sp16_disk sp16emmc) || return 1
  sp16_type=$(blkid -o value -s TYPE "$sp16_dev" 2>/dev/null || :)
  if [ -z "$sp16_type" ]; then
    mkfs.vfat -F 32 -n SP16EMMC "$sp16_dev" > /dev/console 2>&1 || return 1
    echo 'SP16_DATA formatted=yes' > /dev/console
  else
    sp16_rc=0
    fsck.vfat -a "$sp16_dev" > /dev/console 2>&1 || sp16_rc=$?
    echo "SP16_DATA fsck_rc=$sp16_rc" > /dev/console
    [ "$sp16_rc" -le 1 ] || return 1
  fi
  mkdir -p "/tmp/sp16-root$sp16_emmc_mount"
  mount -t vfat -o quiet,iocharset=utf8,shortname=mixed,umask=000,errors=remount-ro "$sp16_dev" "/tmp/sp16-root$sp16_emmc_mount" || return 1
  sp16_emmc=1
  mkdir -p "/tmp/sp16-root$sp16_emmc_mount/.sp16/data" "/tmp/sp16-root$sp16_data_dir"
  cp -an "$sp16_data_seed/." "/tmp/sp16-root$sp16_emmc_mount/.sp16/data/" 2>/dev/null || :
  mount --bind "/tmp/sp16-root$sp16_emmc_mount/.sp16/data" "/tmp/sp16-root$sp16_data_dir" || return 1
  # Profile runtime.tmpfs_dirs (1.60: the OEM symlinks Projects/Scenes/Samples into /root/pdj/project/
  # and lists /root/pdj/browse; neither directory exists in the rootfs).
  for sp16_dir in $(echo "$sp16_tmpfs_dirs" | tr , ' '); do
    [ "$sp16_dir" = - ] && continue
    mkdir -p "/tmp/sp16-root$sp16_dir"
    mount -t tmpfs -o mode=755 tmpfs "/tmp/sp16-root$sp16_dir" || return 1
  done
  echo 'SP16_DATA mounted=yes' > /dev/console
}
sp16_data=0
case " $(cat /proc/cmdline) " in
 *" sp16.data=1 "*) sp16_data=1 ;;
esac
if [ "$sp16_data" = 1 ]; then
  sp16_data_setup || echo 'SP16_DATA_ERROR persistent data unavailable; running without it' > /dev/console
fi
Xvfb :99 -screen 0 800x480x24 -fbdir /output -listen tcp -nolisten unix -ac > /output/xvfb.txt 2>&1 &
sp16_attempt=0
until DISPLAY=127.0.0.1:99 xdpyinfo >/dev/null 2>&1; do
  sp16_attempt=$((sp16_attempt + 1))
  test "$sp16_attempt" -lt 50
  sleep .1
done
# Research profile only: the vendor's own factory test UI, selected by an allowlisted token.
sp16_app_arg=
for sp16_token in $(cat /proc/cmdline); do
  case "$sp16_token" in
   sp16.app=*)
    # Only an argument the firmware profile allows (runtime.app_args; 1.60: testmode).
    case ",$sp16_app_args," in *",${sp16_token#sp16.app=},"*) sp16_app_arg=${sp16_token#sp16.app=} ;; esac ;;
  esac
done
# With a profile app argument (1.60: the factory test mode) the DIN MIDI ports get a virtual OUT -> IN cable for the
# DEVICE TEST page (the shim serves /dev/ttymxc1 and /dev/din_midi as one loopback). Off in normal runs, where
# looping the app's own MIDI back into it would feed it its own clock. The same page reads eth0's MAC and IP, which the
# guest does not have (only lo): SP16_FAKE_ETH0 makes the shim answer those ioctls with fake values.
sp16_midi_env=
if [ -n "$sp16_app_arg" ]; then
  sp16_midi_env="-E SP16_MIDI_LOOPBACK=1"
  # A real eth0 (--net) is answered by the kernel; the fake one is only for runs without a NIC.
  [ -n "$sp16_have_nic" ] || sp16_midi_env="$sp16_midi_env -E SP16_FAKE_ETH0=1"
fi
# Research builds only (native.py --research --trace): opt-in QEMU user-mode tracing, default off.
sp16_trace_args=
case " $(cat /proc/cmdline) " in
 *" sp16.trace=strace "*)
  mkfifo /tmp/sp16-root/sp16-strace.fifo
  # Drop the hot, uninformative syscalls: streaming everything over the serial console drops the OEM to ~0.05x.
  (grep -a --line-buffered -Ev '^[0-9]+ (_newselect|writev|rt_sigprocmask|rt_sigaction|fstat64|futex|gettid|clock_gettime|gettimeofday|clock_nanosleep|nanosleep|epoll_wait|madvise|_llseek|access|stat64|read|write)\(' \
    < /tmp/sp16-root/sp16-strace.fifo | sed -u 's/^/SP16_STRACE /' > /dev/console) &
  sp16_trace_args="-strace -D /sp16-strace.fifo" ;;
 *" sp16.trace=exec "*)
  # Research executed-block trace: qemu-user logs one line per executed translation block ("-d exec", unchained so
  # every block shows) whose address lies in the app's text range ("-dfilter"). That is far too much for the serial
  # console, so the log goes to a fifo and a small filter prints each block address once, in batches, as SP16_TB
  # lines per app instance and guest thread (cpu index; host: emulator/exectrace.py). SP16_EXEC_RANGE is the 1.60 application's text range (begin..end, inclusive);
  # change it for another firmware.
  SP16_EXEC_RANGE=0x116f8..0x831ca8
  mkfifo /tmp/sp16-root/sp16-exec.fifo
  cat > /tmp/sp16-exec-filter.py <<'PY'
import os, select, sys, time

FIFO = sys.argv[1]
SESSIONS = 0                           # tests: stop after this many app instances (0: serve every app start)
if 'once' in sys.argv[2:]:
    SESSIONS = 1
CAP = 1000000                          # distinct addresses printed at most per app instance (the text range cannot hold more)
for argument in sys.argv[2:]:
    if argument.startswith('cap='):
        CAP = int(argument[4:])
    elif argument.startswith('sessions='):
        SESSIONS = int(argument[9:])
PER_LINE = 16
FLUSH_EVERY = 0.5
STATS_EVERY = 10.0

# One qemu-user per app start: after an app restart it numbers its guest threads from cpu 0 again, so a block is new per
# (instance, cpu, pc), where the instance counts the app starts (each reopen of the fifo), and the counts and seconds
# are kept per instance.
instance = -1                          # the current app instance, from 0
seen = set()                           # (instance, cpu, pc)
pending = []                           # new (cpu, pc) pairs of the current instance in first-execution order
per_cpu = {}                           # cpu -> distinct blocks printed or counted, this instance
first_seen = {}                        # cpu -> seconds since this instance's first log byte, at that cpu's first block
start = None                           # time of this instance's first log byte
lines = 0                              # trace lines of this instance
distinct = 0                           # distinct blocks of this instance
capped = False


def begin_instance():
    global instance, pending, per_cpu, first_seen, start, lines, distinct, capped
    instance += 1
    pending, per_cpu, first_seen, start, lines, distinct, capped = [], {}, {}, None, 0, 0, False
    print('SP16_TB_INSTANCE i%d' % instance, flush=True)


def flush():
    # One SP16_TB line per run of the same cpu (at most PER_LINE addresses), so the first-execution order holds.
    global pending
    run = []
    for cpu, pc in pending:
        if run and (run[0][0] != cpu or len(run) == PER_LINE):
            print('SP16_TB i%d c%d %s' % (instance, run[0][0], ' '.join(item[1] for item in run)), flush=True)
            run = []
        run.append((cpu, pc))
    if run:
        print('SP16_TB i%d c%d %s' % (instance, run[0][0], ' '.join(item[1] for item in run)), flush=True)
    pending = []


def elapsed():
    return time.time() - start if start is not None else 0.0


def cpu_counts():
    # "c0:123@0.0,c1:45@3.2": distinct blocks and first-seen second per guest thread of this instance, no spaces.
    return ','.join('c%d:%d@%.1f' % (cpu, per_cpu[cpu], first_seen[cpu]) for cpu in sorted(per_cpu))


def feed(chunk):
    # "Trace <cpu>: 0x<host tb> [<cs_base>/<pc>/<flags>/<cflags>] <symbol>": the guest thread's cpu index starts the
    # line, the block's guest address is the second field in the brackets. A line without both is not a block.
    global lines, capped, start, distinct
    if start is None and chunk:
        start = time.time()
    for line in chunk.split(b'\n'):
        begin = line.find(b'[')
        fields = line[begin + 1:].split(b'/') if begin >= 0 else []
        head = line[:begin].split() if begin >= 0 else []
        if len(fields) < 3 or len(head) < 2 or head[0] != b'Trace' or not head[1].rstrip(b':').isdigit():
            continue
        lines += 1
        cpu = int(head[1].rstrip(b':'))
        pc = fields[1].decode('ascii', 'replace')
        key = (instance, cpu, pc)
        if key in seen:
            continue
        seen.add(key)
        distinct += 1
        if cpu not in per_cpu:
            per_cpu[cpu] = 0
            first_seen[cpu] = elapsed()
            print('SP16_TB_CPU i%d c%d t=%.1f' % (instance, cpu, first_seen[cpu]), flush=True)
        per_cpu[cpu] += 1
        if distinct <= CAP:
            pending.append((cpu, pc))
        elif not capped:
            capped = True
            flush()
            print('SP16_TB_CAPPED i%d cap=%d' % (instance, CAP), flush=True)


def drain(fd):
    """Read one writer's output to its end; print new addresses at least every FLUSH_EVERY seconds."""
    rest = b''
    last_flush = last_stats = time.time()
    shown = 0
    while True:
        if select.select([fd], [], [], FLUSH_EVERY)[0]:
            data = os.read(fd, 1 << 16)
            if not data:
                break
            data = rest + data
            cut = data.rfind(b'\n')
            rest = data[cut + 1:]
            feed(data[:cut + 1])
        now = time.time()
        if pending and now - last_flush >= FLUSH_EVERY:
            flush()
            last_flush = now
        if distinct != shown and now - last_stats >= STATS_EVERY:
            print('SP16_TB_STATS i%d distinct=%d lines=%d t=%.1f cpus=%s' % (instance, distinct, lines, elapsed(), cpu_counts()), flush=True)
            last_stats = now
            shown = distinct
    feed(rest)
    flush()


while True:
    fd = os.open(FIFO, os.O_RDONLY)    # blocks until qemu-user opens its log; again after each app restart
    begin_instance()
    try:
        drain(fd)
    finally:
        os.close(fd)
    print('SP16_TB_END i%d distinct=%d lines=%d t=%.1f cpus=%s' % (instance, distinct, lines, elapsed(), cpu_counts()), flush=True)
    if SESSIONS and instance + 1 >= SESSIONS:
        break
PY
  python3 -u /tmp/sp16-exec-filter.py /tmp/sp16-root/sp16-exec.fifo > /dev/console 2>&1 &
  sp16_trace_args="-d exec,nochain -dfilter $SP16_EXEC_RANGE -D /sp16-exec.fifo" ;;
 *" sp16.trace=pcmtap "*)
  # Research PCM tap: the stub copies each playback stream into a bounded file under /tmp; this helper
  # prints every finished capture to the console as gzip+base64 lines (SP16_PCM_DUMP) for host-side analysis.
  # No audio ever reaches a host device.
  sp16_pcmtap=1 ;;
 *" sp16.trace=gdb "*)
  # qemu-user's gdbstub blocks the app until a debugger attaches. The guest has no NIC beyond lo, so a small
  # forwarder tunnels the stub out over the dedicated sp16.gdb virtio-serial port (host end {socket}.gdb). It waits
  # for the host (lldb, via emulator/gdb-bridge.py) to connect to the vport first, THEN dials the in-guest stub:
  # qemu-arm -g 1234 does not listen until the app starts (below), and dialing it is what lets the app run, so the
  # order keeps the app halted at entry until the debugger is actually attached.
  sp16_trace_args="-g 1234"
  if [ -e /dev/sp16-gdb ]; then
    cat > /tmp/sp16-gdb-forward.py <<'PY'
import os, select, socket, time

VPORT = '/dev/sp16-gdb'
HOST, PORT = '127.0.0.1', 1234


def wait_for_host(vfd):
    # A virtio-serial port read returns EOF (0 bytes) while no host is attached, so poll until the host
    # (lldb via the bridge) connects and sends its first gdb-remote bytes; return them to forward first.
    while True:
        if select.select([vfd], [], [], 0.2)[0]:
            data = os.read(vfd, 4096)
            if data:
                return data
        time.sleep(0.05)


def dial_stub():
    # qemu-arm -g 1234 starts after this script (the app launch is later in init), so retry until it listens.
    while True:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((HOST, PORT))
            return sock
        except OSError:
            sock.close()
            time.sleep(0.1)


def pump(vfd, sock, initial):
    sock.sendall(initial)
    sfd = sock.fileno()
    while True:
        for fd in select.select([vfd, sfd], [], [])[0]:
            if fd == vfd:
                data = os.read(vfd, 4096)
                if not data:
                    return            # host (lldb) detached
                sock.sendall(data)
            else:
                data = sock.recv(4096)
                if not data:
                    return            # stub gone (app continued or exited)
                os.write(vfd, data)


def main():
    while not os.path.exists(VPORT):
        time.sleep(0.1)
    vfd = os.open(VPORT, os.O_RDWR)
    while True:                       # one gdb session at a time; loop so a re-attach works
        initial = wait_for_host(vfd)
        sock = dial_stub()
        try:
            pump(vfd, sock, initial)
        except OSError:
            pass
        finally:
            sock.close()


main()
PY
    python3 -u /tmp/sp16-gdb-forward.py > /dev/console 2>&1 &
    echo 'SP16_GDB forwarder=ready port=1234 vport=/dev/sp16-gdb' > /dev/console
  else
    echo 'SP16_GDB vport=missing; stub reachable only inside the guest' > /dev/console
  fi ;;
esac
sp16_tap_env=
if [ "${sp16_pcmtap:-0}" = 1 ]; then
  sp16_tap_env="-E SP16_PCM_TAP=/tmp -E SP16_PCM_TAP_SECONDS=4"
  cat > /tmp/sp16-pcm-dump.py <<'PY'
import base64, glob, os, struct, sys, time, zlib

def make_test_wav():
    # Research stimulus for the OEM sampler (offline, generated here): 1 s mono 44.1 kHz 16-bit, 440 Hz with a decay.
    import math
    samples = b''.join(struct.pack('<h', int(20000 * math.exp(-3.0 * i / 44100) * math.sin(2 * math.pi * 440 * i / 44100)))
                       for i in range(44100))
    head = b'RIFF' + struct.pack('<I', 36 + len(samples)) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 44100, 88200, 2, 16)
    return head + b'data' + struct.pack('<I', len(samples)) + samples


seen = {}
last = {}
wav_dir = '/tmp/sp16-root' + os.environ.get('SP16_EMMC_MOUNT', '') + '/PIONEER DJ SAMPLER/Samples'
wav_done = False
while True:
    time.sleep(.5)
    if not wav_done and os.path.isdir(wav_dir):
        try:
            with open(wav_dir + '/sp16-tap-test.wav', 'wb') as f:
                f.write(make_test_wav())
                f.flush()
                os.fsync(f.fileno())
            wav_done = True
        except OSError:
            pass
    for path in glob.glob('/tmp/sp16-root/tmp/sp16-pcm-*.raw'):
        try:
            with open(path, 'rb') as f:
                head = f.read(64)
                if len(head) < 64 or head[:8] != b'SP16PCM1':
                    continue
                done = struct.unpack_from('<I', head, 12)[0]
                captured = struct.unpack_from('<Q', head, 40)[0]
                # full (done=2), or some signal captured and no growth for 3 s (the sound ended early)
                count, since = last.get(path, (-1, time.time()))
                if captured != count:
                    last[path] = (captured, time.time())
                if not (done == 2 or (captured and time.time() - since > 3)):
                    continue
                body = zlib.compress(f.read(captured * struct.unpack_from('<I', head, 28)[0]), 6)
        except OSError:
            continue
        count = seen.get(path, 0) + 1
        seen[path] = count
        last.pop(path, None)
        name = os.path.basename(path)[len('sp16-pcm-'):-4] + '.%d' % count
        text = base64.b64encode(head + body).decode()
        pieces = [text[i:i + 2000] for i in range(0, len(text), 2000)]
        print('SP16_PCM_DUMP %s begin pieces=%d bytes=%d' % (name, len(pieces), len(head) + len(body)), flush=True)
        for index, piece in enumerate(pieces):
            print('SP16_PCM_DUMP %s %d %s' % (name, index, piece), flush=True)
        print('SP16_PCM_DUMP %s end' % name, flush=True)
        # re-arm for the next sound: zero captured/first/start, then done (the stub waits for fresh signal)
        with open(path, 'r+b') as f:
            f.seek(40)
            f.write(bytes(24))
            f.seek(12)
            f.write(bytes(4))
PY
  SP16_EMMC_MOUNT=$sp16_emmc_mount python3 -u /tmp/sp16-pcm-dump.py > /dev/console 2>&1 &
fi
# Decoupled builds (M6): a supervisor restarts only the OEM app on request (control port "app-restart", research
# socket op app-restart), re-applying the developer overlay first; the VM, Xvfb, bridge and mounts stay up. Stale shim
# endpoints of a killed app are removed (the shim refuses an existing endpoint and only unlinks on a clean close).
: > /output/application.txt
(
  set +e
  while :; do
    sp16_preload=/opt/sp16/libsp16-ui.so
    # The real firmware's apl_start is not run in the emulator. Honor the marked
    # preload launcher carried by an imported update, using its own
    # preload library from the hash-checked read-only firmware image.
    sp16_app_dir=${sp16_app_path%/*}
    if [ -f "/tmp/sp16-root$sp16_app_dir/sp16-preload.so" ] &&
       grep -q '^# SP16_PRELOAD_LAUNCHER$' "/tmp/sp16-root$sp16_app_dir/apl_start"; then
      sp16_preload="$sp16_preload:$sp16_app_dir/sp16-preload.so"
      echo "SP16_FW_LAUNCHER preload=$sp16_app_dir/sp16-preload.so" > /dev/console
    fi
    if [ "$sp16_devlayer" = 1 ]; then
      if sp16_extra=$(env $sp16_profile_env python3 /opt/sp16/guest-firmware.py dev-apply 2>/dev/console); then
        sp16_preload="$sp16_preload${sp16_extra:+:$sp16_extra}"
      else
        echo 'SP16_DEV_ERROR developer overlay not applied; starting the firmware unchanged' > /dev/console
      fi
    fi
    # The firmware's apl_start is not run, but the one line it carries after a `# SP16_APL_HOOK` marker is: the unit's
    # apl_start runs such a line just before it starts the app (a mod loaded from /root/data), so it runs here the same
    # way, after the overlay, in the same chroot, under the firmware's own busybox sh. Bounded to 10 s so a bad mod can
    # never keep the app from starting; its output and exit status go to the console as SP16_APL_HOOK lines.
    sp16_hook=$(python3 /opt/sp16/guest-firmware.py apl-hook "/tmp/sp16-root$sp16_app_dir/apl_start" 2>/dev/console) || sp16_hook=
    if [ -n "$sp16_hook" ]; then
      echo "SP16_APL_HOOK run: $sp16_hook" > /dev/console
      # A mod's script starts child programs (cp, rm, sleep): qemu-user passes those to the guest kernel, which cannot run
      # ARM code, so each fails with "Exec format error" unless a binfmt_misc handler hands ARM ELF files to
      # qemu-arm-static (flag F: the interpreter is opened now, so it works inside the chroot). Registered once, here,
      # because only a hook runs child programs; /proc is mounted in the chroot for the hook only.
      if ! [ -e /proc/sys/fs/binfmt_misc/sp16-arm ]; then
        modprobe binfmt_misc 2>/dev/null || :
        grep -q ' binfmt_misc ' /proc/mounts || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc 2>/dev/null || :
        printf '%s' ':sp16-arm:M::\x7fELF\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x28\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/bin/qemu-arm-static:F' \
          > /proc/sys/fs/binfmt_misc/register 2>/dev/null || echo 'SP16_APL_HOOK binfmt_misc unavailable: child programs will not run' > /dev/console
      fi
      mkdir -p /tmp/sp16-root/proc
      mount -t proc proc /tmp/sp16-root/proc 2>/dev/null || :
      (timeout -k 2 10 env SP16_APP_CWD=$sp16_app_cwd python3 /opt/sp16/enter.py /tmp/sp16-root /qemu-arm-static -cpu $sp16_cpu \
         -E LD_LIBRARY_PATH=$sp16_ldpath /bin/busybox sh -c "$sp16_hook" 2>&1; echo "rc=$?") \
        | sed -u 's/^/SP16_APL_HOOK /' > /dev/console
      umount /tmp/sp16-root/proc 2>/dev/null || :
    fi
    rm -f /tmp/sp16-root/dev/sp16-panel-input /tmp/sp16-root/dev/sp16-cdc-input /tmp/sp16-root/dev/sp16-usb1 \
      /tmp/sp16-root/dev/sp16-usbctn1
    SP16_APP_CWD=$sp16_app_cwd python3 /opt/sp16/enter.py /tmp/sp16-root /qemu-arm-static $sp16_trace_args -cpu $sp16_cpu \
     -E DISPLAY=127.0.0.1:99 -E LD_LIBRARY_PATH=$sp16_ldpath \
     -E LD_PRELOAD=$sp16_preload -E ALSA_CONFIG_PATH=/etc/sp16-alsa.conf \
     -E SP16_PANEL_INPUT=1 $sp16_tap_env $sp16_audio_env $sp16_midi_env \
     $sp16_app_path $sp16_app_arg >> /output/application.txt 2>&1 &
    echo $! > /run/sp16-app.pid
    echo "SP16_APP_STARTED pid=$!" > /dev/console
    wait $!
    [ -e /run/sp16-app.restart ] || break
    rm -f /run/sp16-app.restart
  done
) &
sp16_app_pid=$!
# Control watcher (clean stop and virtual USB stick), one trusted script for both triggers: "watch" answers the
# host's requests on the control port (shutdown, usb-attach, usb-eject); "once" runs when the bridge exits (host gone:
# quit, crash or kill -9) and does the same stop without a host. Stop kills the OEM app, syncs, unmounts the stick and
# the data filesystem and reports clean=yes|no. The USB image (second virtio disk, serial sp16usb) is formatted by this
# script only when blank (never by the OEM), fsck'd with fsck.vfat -a otherwise, mounted with the udev options at the
# chroot's /media/usb1/sda1 and only then announced to the app (usbctn1 "connect", then usb1 "mount <path>") through
# the preload's queue sockets. autoexec.bin on the stick is never run. The app's own USB STOP umount is noticed by
# polling /proc/mounts and followed by the udev "remove" messages (usb1 "umount", usbctn1 "disconnect").
sp16_watch=$sp16_emmc
for sp16_blk in /sys/block/vd*; do
  [ "$(cat "$sp16_blk/serial" 2>/dev/null)" = sp16usb ] && sp16_watch=1
done
if [ "$sp16_watch" = 1 ]; then
  cat > /tmp/sp16-clean-stop.py <<'PY'
import os, select, socket, subprocess, sys, time

ROOT = '/tmp/sp16-root'
# Mount points from the firmware profile's runtime layout (passed by the init).
DATA = ROOT + os.environ['SP16_DATA_DIR']
EMMC = ROOT + os.environ['SP16_EMMC_MOUNT']
USB = ROOT + os.environ['SP16_USB_MOUNT']
USB_APP_PATH = os.environ['SP16_USB_MOUNT']
USB_OPTIONS = 'rw,dmask=000,fmask=000,shortname=mixed,utf8,noatime'
USB_NODES = {'usb1': ROOT + '/dev/sp16-usb1', 'usbctn1': ROOT + '/dev/sp16-usbctn1'}


def run(*command):
    return subprocess.run(list(command), capture_output=True, text=True)


def mounted(path):
    try:
        with open('/proc/mounts') as table:
            return any(line.split()[1] == path for line in table if line.strip())
    except OSError:
        return False


def umount(path, attempts=10):
    for _ in range(attempts):
        if not mounted(path) or run('umount', path).returncode == 0:
            return True
        time.sleep(.2)
    return not mounted(path)


def find_usb_disk():
    for _ in range(50):
        for name in sorted(os.listdir('/sys/block')):
            try:
                with open('/sys/block/%s/serial' % name) as handle:
                    if name.startswith('vd') and handle.read().strip() == 'sp16usb' and os.path.exists('/dev/' + name):
                        return '/dev/' + name
            except OSError:
                pass
        time.sleep(.1)
    return None


def usb_attach():
    """Format a blank stick, repair an unclean one, mount it. True once the stick mount point is mounted."""
    if mounted(USB):
        return True
    device = find_usb_disk()
    if not device:
        return False
    run('modprobe', 'vfat')
    if not run('blkid', '-o', 'value', '-s', 'TYPE', device).stdout.strip():
        if run('mkfs.vfat', '-F', '32', '-n', 'SP16USB', device).returncode != 0:
            return False
        print('SP16_USB formatted=yes', flush=True)
    else:
        rc = run('fsck.vfat', '-a', device).returncode
        print('SP16_USB fsck_rc=%d' % rc, flush=True)
        if rc > 1:
            return False
    os.makedirs(USB, exist_ok=True)
    result = run('mount', '-t', 'vfat', '-o', USB_OPTIONS, device, USB)
    if result.returncode != 0:
        print('SP16_USB mount_error=%s' % result.stderr.strip(), flush=True)
        return False
    print('SP16_USB mounted=yes', flush=True)
    return True


def inject(node, text):
    """One datagram = one message on the app's /proc/udev_<node> queue; False until the app has opened the node."""
    sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sender.sendto(text.encode(), USB_NODES[node])
        return True
    except OSError:
        return False
    finally:
        sender.close()


def announce():
    if not all(os.path.exists(path) for path in USB_NODES.values()):
        return False
    ok = inject('usbctn1', 'connect') and inject('usb1', 'mount ' + USB_APP_PATH)
    if ok:
        print('SP16_USB announced=yes', flush=True)
    return ok


def remove_messages():
    inject('usb1', 'umount ' + USB_APP_PATH)
    inject('usbctn1', 'disconnect')
    print('SP16_USB removed=yes', flush=True)


def supervised_app():
    """Decoupled builds: the current OEM app pid written by the restart supervisor, or None."""
    try:
        with open('/run/sp16-app.pid') as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def clean_stop(app):
    """Kill the OEM app, sync and unmount; True only on a confirmed unmount of the stick and the data card."""
    child = supervised_app()
    for pid in (app, child):  # the supervisor first, so it cannot start the app again
        if pid is None:
            continue
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    time.sleep(.2)
    os.sync()
    ok = umount(USB)
    if mounted(DATA):
        run('umount', DATA)
    return umount(EMMC) and ok


def watch(app):
    for _ in range(100):
        if os.path.exists('/dev/sp16-ctl'):
            break
        time.sleep(.1)
    fd = os.open('/dev/sp16-ctl', os.O_RDWR)
    print('SP16_DATA watcher=ready', flush=True)
    pending = usb_attach()     # mounted before the app is told about it
    present = False            # the app has been told the stick is there
    buffer = b''

    def reply(text):
        try:
            os.write(fd, (text + '\n').encode())
        except OSError:
            pass

    while True:
        if pending and announce():
            pending, present = False, True
        elif present and not mounted(USB):
            # The app unmounted by itself (USB STOP): flush, then behave like the physical removal.
            os.sync()
            print('SP16_USB unmounted_by_app=yes', flush=True)
            remove_messages()
            present = False
        # A virtio port read returns EOF while no host is connected, so poll instead of blocking.
        chunk = b''
        if select.select([fd], [], [], .2)[0]:
            try:
                chunk = os.read(fd, 64)
            except OSError:
                chunk = b''
            if not chunk:
                time.sleep(.2)
        buffer += chunk
        while b'\n' in buffer or b'shutdown' in buffer:
            if b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
            else:
                line, buffer = buffer, b''
            command = line.strip().decode(errors='replace')
            if command == 'shutdown':
                print('SP16_DATA stop=requested', flush=True)
                clean = clean_stop(app)
                print('SP16_DATA clean=' + ('yes' if clean else 'no'), flush=True)
                if clean:
                    reply('done')
            elif command == 'usb-attach':
                if mounted(USB) and present:
                    reply('usb-attached')
                elif usb_attach():
                    pending = True
                    reply('usb-attached')
                else:
                    reply('usb-error')
            elif command == 'app-restart':
                # Decoupled builds: restart only the OEM app (the supervisor re-applies the developer overlay).
                old = supervised_app()
                if old is None:
                    reply('app-restart-error')
                    continue
                open('/run/sp16-app.restart', 'w').close()
                try:
                    os.kill(old, 15)
                    for _ in range(15):
                        time.sleep(.1)
                        os.kill(old, 0)
                    os.kill(old, 9)
                except OSError:
                    pass
                os.sync()
                for _ in range(100):
                    if supervised_app() not in (None, old):
                        break
                    time.sleep(.1)
                if present:  # the new app has not been told about the stick yet
                    pending, present = True, False
                print('SP16_APP_RESTARTED', flush=True)
                reply('app-restarted' if supervised_app() not in (None, old) else 'app-restart-error')
            elif command == 'usb-eject':
                os.sync()
                if not mounted(USB):
                    if present:
                        remove_messages()
                        present = False
                    reply('usb-ejected')
                elif umount(USB, 15):
                    print('SP16_USB ejected=yes', flush=True)
                    if present or pending:
                        remove_messages()
                    present = pending = False
                    reply('usb-ejected')
                else:
                    print('SP16_USB eject=busy', flush=True)
                    reply('usb-busy')


def once(app):
    if not (mounted(EMMC) or mounted(USB)):
        print('SP16_DATA already_clean=yes', flush=True)
        return
    print('SP16_DATA host_lost=yes stop=requested', flush=True)
    clean = clean_stop(app)
    print('SP16_DATA clean=' + ('yes' if clean else 'no'), flush=True)


if __name__ == '__main__':
    (watch if sys.argv[1] == 'watch' else once)(int(sys.argv[2]))
PY
  SP16_DATA_DIR=$sp16_data_dir SP16_EMMC_MOUNT=$sp16_emmc_mount SP16_USB_MOUNT=$sp16_usb_mount \
    python3 -u /tmp/sp16-clean-stop.py watch "$sp16_app_pid" > /dev/console 2>&1 &
fi
echo "SP16_NATIVE_READY firmware=$sp16_firmware display=800x480 app_sha256_verified=yes variant=$sp16_variant app_arg=${sp16_app_arg:-none}" > /dev/console
env $sp16_profile_env python3 -u /opt/sp16/guest-bridge.py > /dev/console 2>&1
echo 'SP16_BRIDGE_STOPPED' > /dev/console
if [ "$sp16_watch" = 1 ]; then
  SP16_DATA_DIR=$sp16_data_dir SP16_EMMC_MOUNT=$sp16_emmc_mount SP16_USB_MOUNT=$sp16_usb_mount \
    python3 -u /tmp/sp16-clean-stop.py once "$sp16_app_pid" > /dev/console 2>&1 || :
fi
sp16_poweroff
