/* SP-16 UI-only compatibility layer. No physical hardware or audible output.
 * SPI request layouts come from the matching GPL driver contracts; unknown
 * requests fail instead of silently pretending that a device operation worked.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <arpa/inet.h>
#include <net/if.h>
#include <net/if_arp.h>
#include <netinet/in.h>
#include <poll.h>
#include <sched.h>
#include <sys/mman.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define MAX_FDS 1024
enum device { NONE, PANEL, CDC, DAC, BACKLIGHT, MEDIA, PACKET, MIDI, GPIO, MIDI_CONTROL };
/* media: 1 = /proc/udev_usb1, 2 = /proc/udev_usbctn1 (only meaningful when kind==MEDIA). */
struct state { enum device kind; int peer; uint32_t interval, bytes, offset; uint16_t bits; uint8_t timer, endian, input, media; };
struct spi_message { uint8_t multi_id, reserved, part_id, part_num; uint32_t bytes; uint64_t tx_buf; };
_Static_assert(sizeof(struct spi_message)==16,"SPI transfer descriptor ABI must be 16 bytes");
static struct state devices[MAX_FDS];
static pid_t owner_pid;
static int (*original_open)(const char *, int, ...);
static int (*original_close)(int);
static ssize_t (*original_read)(int, void *, size_t);
static ssize_t (*original_write)(int, const void *, size_t);
static int (*original_ioctl)(int, unsigned long, ...);
static char panel_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
static char cdc_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
/* The sequencer bridge owns the sole DIN output queue. Each request is one packet and gets
 * an explicit acceptance/capacity response; no second virtio reader is opened here. */
static int din_control_request(uint32_t request) {
    const char *path=getenv("SP16_DIN_CONTROL");
    if (!path) path="/dev/sp16-din-control";
    if (!*path || strlen(path)>=sizeof(((struct sockaddr_un *)0)->sun_path)) { errno=EINVAL; return -1; }
    int fd=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0);
    if (fd<0) return -1;
    struct timeval timeout={.tv_sec=1};
    (void)setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof timeout);
    struct sockaddr_un address={.sun_family=AF_UNIX};
    strcpy(address.sun_path,path);
    int32_t result=-EIO;
    int error=0;
    if (connect(fd,(struct sockaddr *)&address,sizeof address)<0) error=errno;
    else {
        ssize_t n=send(fd,&request,sizeof request,MSG_NOSIGNAL);
        if (n!=(ssize_t)sizeof request) error=n<0 ? errno : EIO;
        else {
            n=recv(fd,&result,sizeof result,0);
            if (n!=(ssize_t)sizeof result) error=n<0 ? errno : EIO;
        }
    }
    original_close(fd);
    if (error) { errno=error; return -1; }
    if (result<0) { errno=-result; return -1; }
    return result;
}
static int din_capacity_file(void) {
    int available=din_control_request(UINT32_MAX);
    if (available<0) return -1;
    int fd=(int)syscall(SYS_memfd_create,"sp16-din-capacity",MFD_CLOEXEC);
    if (fd<0) return -1;
    char report[64];
    int length=snprintf(report,sizeof report,"Avail        : %d\n",available);
    if (length<0 || length>=(int)sizeof report || original_write(fd,report,(size_t)length)!=length ||
        lseek(fd,0,SEEK_SET)<0) {
        int error=errno ? errno : EIO; original_close(fd); errno=error; return -1;
    }
    return fd;
}
static uint64_t mono_ns(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);
    return (uint64_t)t.tv_sec*1000000000ULL+(uint64_t)t.tv_nsec;
}
static void dac_write(const void *buffer,size_t count);

/* The OEM's JUCE MIDI path uses ALSA sequencer subscriptions. Record the result at the
 * API boundary so a registered device cannot be mistaken for a connected route. */
typedef struct _snd_seq snd_seq_t;
typedef struct snd_seq_event snd_seq_event_t;
static void *midi_alsa_symbol(const char *name) {
    static void *alsa;
    void *handle=__atomic_load_n(&alsa,__ATOMIC_ACQUIRE);
    if (!handle) {
        void *candidate=dlopen("libasound.so.2",RTLD_NOW|RTLD_LOCAL);
        if (!candidate) {
            fprintf(stderr,"[sp16-ui-stub] MIDI ALSA load failed: %s\n",dlerror());
            return NULL;
        }
        void *empty=NULL;
        if (__atomic_compare_exchange_n(&alsa,&empty,candidate,0,__ATOMIC_RELEASE,__ATOMIC_ACQUIRE))
            handle=candidate;
        else { dlclose(candidate); handle=empty; }
    }
    void *symbol=dlsym(handle,name);
    if (!symbol) fprintf(stderr,"[sp16-ui-stub] MIDI ALSA symbol missing: %s (%s)\n",name,dlerror());
    return symbol;
}
int snd_seq_open(snd_seq_t **handle,const char *name,int streams,int mode) {
    int (*real)(snd_seq_t **,const char *,int,int)=midi_alsa_symbol("snd_seq_open");
    if (!real) return -ENOSYS;
    int result=real(handle,name,streams,mode);
    fprintf(stderr,"[sp16-ui-stub] MIDI seq open name=%s streams=%d result=%d\n",name,streams,result);
    return result;
}
int snd_seq_connect_from(snd_seq_t *handle,int my_port,int client,int port) {
    int (*real)(snd_seq_t *,int,int,int)=midi_alsa_symbol("snd_seq_connect_from");
    if (!real) return -ENOSYS;
    int result=real(handle,my_port,client,port);
    fprintf(stderr,"[sp16-ui-stub] MIDI seq connect_from local=%d peer=%d:%d result=%d\n",my_port,client,port,result);
    return result;
}
int snd_seq_connect_to(snd_seq_t *handle,int my_port,int client,int port) {
    int (*real)(snd_seq_t *,int,int,int)=midi_alsa_symbol("snd_seq_connect_to");
    if (!real) return -ENOSYS;
    int result=real(handle,my_port,client,port);
    fprintf(stderr,"[sp16-ui-stub] MIDI seq connect_to local=%d peer=%d:%d result=%d\n",my_port,client,port,result);
    return result;
}
int snd_seq_event_input(snd_seq_t *handle,snd_seq_event_t **event) {
    int (*real)(snd_seq_t *,snd_seq_event_t **)=midi_alsa_symbol("snd_seq_event_input");
    if (!real) return -ENOSYS;
    int result=real(handle,event);
    static unsigned events;
    if (result>=0 && __atomic_fetch_add(&events,1,__ATOMIC_RELAXED)<16)
        fprintf(stderr,"[sp16-ui-stub] MIDI seq input event=%d\n",result);
    return result;
}

/* Bind a native-only datagram endpoint (panel RX or CDC strip). `input` is 1 for the
 * panel and 2 for the CDC controller, which closes/unlinks its own path. */
static int input_socket(enum device kind, const char *env, const char *fallback, char *saved) {
    const char *path=getenv(env);
    if (!path) path=fallback;
    if (!*path || strlen(path)>=sizeof(panel_path)) { errno=EINVAL; return -1; }
    int fd=socket(AF_UNIX,SOCK_DGRAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
    if (fd<0) return -1;
    if (fd>=MAX_FDS) { original_close(fd); errno=EMFILE; return -1; }
    struct sockaddr_un address={ .sun_family=AF_UNIX };
    strcpy(address.sun_path,path);
    /* Refuse an existing endpoint; never unlink a socket we did not create. */
    if (bind(fd,(struct sockaddr *)&address,sizeof(address))) {
        int error=errno; original_close(fd); errno=error; return -1;
    }
    strcpy(saved,path);
    devices[fd]=(struct state){ .kind=kind,.peer=-1,.interval=3,.bytes=32,.bits=32,.input=kind==PANEL?1:2 };
    return fd;
}

/* OEM panel TX capture: each ioctl 0x40107000 frame is copied to a guest-only
 * datagram endpoint for the bridge. Nonblocking and bounded; drops are counted. */
static int tx_fd=-2;
static unsigned long tx_sent, tx_dropped;
static void tx_capture(const struct spi_message *m) {
    const char *enabled=getenv("SP16_PANEL_INPUT");
    if (!enabled || strcmp(enabled,"1") || !m->bytes) return;
    const char *path=getenv("SP16_PANEL_TX");
    if (!path) path="/dev/sp16-panel-tx";
    if (strlen(path)>=sizeof(((struct sockaddr_un *)0)->sun_path)) return;
    if (tx_fd==-2) {
        tx_fd=socket(AF_UNIX,SOCK_DGRAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
        if (tx_fd>=MAX_FDS) { original_close(tx_fd); tx_fd=-1; }
    }
    if (tx_fd<0) return;
    uint8_t out[8+128];
    out[0]=m->multi_id; out[1]=m->part_id; out[2]=m->part_num; out[3]=0;
    memcpy(out+4,&m->bytes,4);
    memcpy(out+8,(const void *)(uintptr_t)m->tx_buf,m->bytes);
    struct sockaddr_un address={ .sun_family=AF_UNIX };
    strcpy(address.sun_path,path);
    if (sendto(tx_fd,out,8+m->bytes,MSG_DONTWAIT,(struct sockaddr *)&address,sizeof(address))<0) {
        unsigned long dropped=++tx_dropped; /* logged at powers of two */
        if (dropped==(dropped&-dropped))
            fprintf(stderr,"[sp16-ui-stub] panel TX frames dropped=%lu errno=%d\n",dropped,errno);
    } else tx_sent++;
}

static int idle_socket(enum device kind) {
    int pair[2];
    int flags=kind==PACKET?0:SOCK_NONBLOCK;
    if (socketpair(AF_UNIX,SOCK_DGRAM|SOCK_CLOEXEC|flags,0,pair)) return -1;
    if (pair[0]>=MAX_FDS) { close(pair[0]); close(pair[1]); errno=EMFILE; return -1; }
    devices[pair[0]]=(struct state){ .kind=kind,.peer=pair[1],.interval=kind==DAC?1:3,
        .bytes=kind==DAC?16:32,.bits=kind==DAC?16:32 };
    return pair[0];
}

__attribute__((constructor)) static void initialize(void) {
    owner_pid = getpid();
    original_open = dlsym(RTLD_NEXT, "open");
    original_close = dlsym(RTLD_NEXT, "close");
    original_read = dlsym(RTLD_NEXT, "read");
    original_write = dlsym(RTLD_NEXT, "write");
    original_ioctl = dlsym(RTLD_NEXT, "ioctl");
    fprintf(stderr, "[sp16-ui-stub] enabled: simulated board devices, silent PCM\n");
}

static struct state *device(int fd) {
    return fd >= 0 && fd < MAX_FDS && devices[fd].kind ? &devices[fd] : NULL;
}

/* Media nodes (/proc/udev_usb1, /proc/udev_usbctn1) mirror the kernel's vendor FIFO message queues
 * (udev_usb.c): each node is a blocking unix datagram socket bound to a guest-local path, so one datagram is
 * one message, poll/epoll report readable iff non-empty, a read pops exactly one message (excess bytes are
 * discarded) and a read blocks while empty. Our trusted guest code injects udev-style messages by sending a
 * datagram to the path; the app's own writes loop back to the same queue and a leading '0' clears it.
 * Log the first attempts verbatim, then one count per 60 s so a polling loop cannot flood the console.
 * Counters are advisory (no lock). */
static unsigned long media_total, media_window;
static uint64_t media_report_ns;
static void media_note(const struct state *s, const char *op) {
    unsigned long total=__sync_add_and_fetch(&media_total,1);
    unsigned long window=__sync_add_and_fetch(&media_window,1);
    const char *node=s->media==2?"/proc/udev_usbctn1":"/proc/udev_usb1";
    if (total<=5) { fprintf(stderr,"[sp16-ui-stub] media %s %s (attempt %lu)\n",op,node,total); return; }
    uint64_t now=mono_ns();
    if (!media_report_ns) media_report_ns=now;
    if (now-media_report_ns>=60000000000ULL) {
        fprintf(stderr,"[sp16-ui-stub] media attempts in last 60s=%lu total=%lu last=%s %s\n",window,total,op,node);
        media_window=0; media_report_ns=now;
    }
}

static char media_path[3][sizeof(((struct sockaddr_un *)0)->sun_path)];
static int media_fd[3]={ -1,-1,-1 }, media_refs[3];

/* The app may only queue the requests it issues itself: "mount <path>", "umount <path>" and
 * "do_umount<digit> <path>", with the path under the single USB slot (no "..") because the reader thread
 * performs a real umount on it. Everything else is refused so the guest cannot be steered elsewhere. */
static int media_app_message_ok(const char *m, size_t n) {
    char text[4097];
    if (n==0 || n>=sizeof(text)) return 0;
    memcpy(text,m,n); text[n]=0;
    if (memchr(text,0,n)) return 0;
    const char *path=NULL;
    if (!strncmp(text,"mount ",6)) path=text+6;
    else if (!strncmp(text,"umount ",7)) path=text+7;
    else if (!strncmp(text,"do_umount",9) && text[9]>='0' && text[9]<='9' && text[10]==' ') path=text+11;
    if (!path || strncmp(path,"/media/usb1/",12)) return 0;
    return !strstr(path,"..");
}

static ssize_t media_write(struct state *s,int fd,const void *buffer,size_t count) {
    if (count==0) return 0;
    if (count>4096) { errno=EMSGSIZE; return -1; }
    if (*(const char *)buffer=='0') { /* kernel: leading '0' clears the whole queue */
        char sink[4096];
        while (recv(fd,sink,sizeof(sink),MSG_DONTWAIT)>=0) {}
        return (ssize_t)count;
    }
    if (!media_app_message_ok(buffer,count)) {
        fprintf(stderr,"[sp16-ui-stub] media write refused (%zu bytes, not a usb1 request)\n",count);
        errno=EINVAL; return -1;
    }
    struct sockaddr_un address={ .sun_family=AF_UNIX };
    strcpy(address.sun_path,media_path[s->media]);
    if (sendto(fd,buffer,count,MSG_DONTWAIT,(struct sockaddr *)&address,sizeof(address))<0) {
        if (errno==EAGAIN) errno=ENOSPC;
        return -1;
    }
    return (ssize_t)count;
}

static int media_socket(int node) {
    const char *env=node==2?"SP16_USBCTN1_SOCKET":"SP16_USB1_SOCKET";
    const char *path=getenv(env);
    if (!path) path=node==2?"/dev/sp16-usbctn1":"/dev/sp16-usb1";
    if (media_fd[node]>=0) { /* every opener shares the one queue, like the kernel */
        int fd=dup(media_fd[node]);
        if (fd<0) return -1;
        if (fd>=MAX_FDS) { original_close(fd); errno=EMFILE; return -1; }
        media_refs[node]++;
        devices[fd]=(struct state){ .kind=MEDIA,.peer=-1,.media=(uint8_t)node };
        return fd;
    }
    if (!*path || strlen(path)>=sizeof(media_path[0])) { errno=EINVAL; return -1; }
    int fd=socket(AF_UNIX,SOCK_DGRAM|SOCK_CLOEXEC,0);
    if (fd<0) return -1;
    if (fd>=MAX_FDS) { original_close(fd); errno=EMFILE; return -1; }
    struct sockaddr_un address={ .sun_family=AF_UNIX };
    strcpy(address.sun_path,path);
    /* Refuse an existing endpoint; never unlink a socket we did not create. */
    if (bind(fd,(struct sockaddr *)&address,sizeof(address))) {
        int error=errno; original_close(fd); errno=error; return -1;
    }
    strcpy(media_path[node],path);
    media_fd[node]=fd; media_refs[node]=1;
    devices[fd]=(struct state){ .kind=MEDIA,.peer=-1,.media=(uint8_t)node };
    return fd;
}

static enum device classify(const char *path) {
    if (!strcmp(path, "/dev/subucom_spi0.1")) return PANEL;
    if (!strcmp(path, "/dev/cdc_spi2.0")) return CDC;
    if (!strcmp(path, "/dev/dac_spi4.0")) return DAC;
    if (!strcmp(path, "/dev/backlight")) return BACKLIGHT;
    if (!strcmp(path, "/proc/udev_usb1") || !strcmp(path, "/proc/udev_usbctn1")) return MEDIA;
    if (!strcmp(path, "/dev/gpiodrv")) return GPIO;
    /* The factory cable and normal app ioctl endpoint have separate semantics. */
    if (!strcmp(path, "/dev/ttymxc1") || !strcmp(path, "/dev/din_midi")) {
        const char *loop=getenv("SP16_MIDI_LOOPBACK");
        if (loop && !strcmp(loop,"1")) return MIDI;
        return !strcmp(path,"/dev/din_midi") ? MIDI_CONTROL : NONE;
    }
    return NONE;
}

/* /dev/gpiodrv: the vendor GPIO driver. One byte per line at offset (bank - 1) * 32 + number; the app seeks, then
 * writes the level (outputs) or reads it (inputs). Lines the app drives (service manual names): GPIO5_IO05
 * SEL_xTHRU/DSF (analog filter input select), GPIO5_IO06 LMT_xOFF/ON (input limiter), GPIO1_IO05 MIDI_PASS (hardware
 * MIDI thru), GPIO4_IO15 USB-A power enable; it polls GPIO4_IO14, USB-A over-current, active low. Every line reads 1
 * (idle for the active-low inputs) until written. Changes are logged; the board model reads the filter select. */
#define GPIO_LINES (7*32)
static uint8_t gpio_level[GPIO_LINES], gpio_written[GPIO_LINES];
static int gpio_ready;
static unsigned long gpio_writes;
static const char *gpio_name(unsigned line) {
    switch (line) {
    case 4*32+5: return "SEL_xTHRU/DSF";
    case 4*32+6: return "LMT_xOFF/ON";
    case 0*32+5: return "MIDI_PASS";
    case 3*32+15: return "USB_OTG_PWR";
    case 3*32+14: return "USBPWR_OC";
    }
    return "?";
}
static int gpio_open(void) {
    if (!gpio_ready) { memset(gpio_level,1,sizeof gpio_level); gpio_ready=1; }
    int pair[2]; /* a descriptor of our own; data never travels through it */
    if (socketpair(AF_UNIX,SOCK_DGRAM|SOCK_CLOEXEC,0,pair)) return -1;
    if (pair[0]>=MAX_FDS) { original_close(pair[0]); original_close(pair[1]); errno=EMFILE; return -1; }
    devices[pair[0]]=(struct state){ .kind=GPIO,.peer=pair[1] };
    return pair[0];
}
static uint8_t usb_oc_level(void) {
    const char *path=getenv("SP16_USB_OC_PATH");
    if (!path || !*path) return 1;
    int fd=original_open(path,O_RDONLY|O_CLOEXEC);
    if (fd<0) return 1;
    char value=0;
    ssize_t count=original_read(fd,&value,1);
    original_close(fd);
    return count==1 && value=='0' ? 0 : 1;
}
static ssize_t gpio_io(struct state *s,void *buffer,size_t count,int writing) {
    uint8_t *b=buffer;
    size_t n=0;
    for (;n<count && s->offset<GPIO_LINES;n++,s->offset++) {
        unsigned line=s->offset;
        if (!writing) {
            if (line==3*32+14) {
                uint8_t level=usb_oc_level();
                if (__atomic_exchange_n(&gpio_level[line],level,__ATOMIC_ACQ_REL)!=level)
                    fprintf(stderr,"[sp16-ui-stub] gpio GPIO4_IO14=%u USBPWR_OC\n",level);
            }
            b[n]=__atomic_load_n(&gpio_level[line],__ATOMIC_ACQUIRE);
            continue;
        }
        uint8_t level=b[n]&1;
        __atomic_add_fetch(&gpio_writes,1,__ATOMIC_RELAXED);
        int first=!__atomic_exchange_n(&gpio_written[line],1,__ATOMIC_ACQ_REL);
        if (__atomic_exchange_n(&gpio_level[line],level,__ATOMIC_ACQ_REL)!=level || first)
            fprintf(stderr,"[sp16-ui-stub] gpio GPIO%u_IO%02u=%u %s\n",line/32+1,line%32,level,gpio_name(line));
    }
    return (ssize_t)n;
}
static off_t gpio_seek(struct state *s,off_t offset,int whence) {
    off_t next=whence==SEEK_SET?offset:whence==SEEK_CUR?(off_t)s->offset+offset:whence==SEEK_END?GPIO_LINES+offset:-1;
    if (next<0 || next>GPIO_LINES) { errno=EINVAL; return -1; }
    s->offset=(uint32_t)next;
    return next;
}

/* DIN MIDI loopback (SP16_MIDI_LOOPBACK=1, set for factory test mode): a cable from MIDI OUT to MIDI IN. Every open
 * of a node shares one stream socket pair, so bytes written through any descriptor are read back through any other,
 * as the factory DEVICE TEST expects (it writes a pattern through one open of /dev/ttymxc1 and reads it through a
 * second). Node 1 = /dev/ttymxc1, node 2 = /dev/din_midi. */
static int midi_pair[3][2]={ {-1,-1},{-1,-1},{-1,-1} };
static unsigned long midi_bytes[3];
static int midi_socket(int node) {
    if (midi_pair[node][0]<0) {
        int pair[2];
        if (socketpair(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0,pair)) return -1;
        midi_pair[node][0]=pair[0]; midi_pair[node][1]=pair[1];
    }
    int fd=dup(midi_pair[node][0]);
    if (fd<0) return -1;
    if (fd>=MAX_FDS) { original_close(fd); errno=EMFILE; return -1; }
    devices[fd]=(struct state){ .kind=MIDI,.peer=-1,.media=(uint8_t)node };
    return fd;
}
static ssize_t midi_write(struct state *s,const void *buffer,size_t count) {
    int out=midi_pair[s->media][1];
    if (out<0) { errno=EBADF; return -1; }
    ssize_t n=send(out,buffer,count,MSG_DONTWAIT|MSG_NOSIGNAL);
    if (n<0 && errno==EAGAIN) return (ssize_t)count; /* a full cable drops bytes, as an unread UART would */
    if (n>0 && (midi_bytes[s->media]+=(unsigned long)n)==(unsigned long)n)
        fprintf(stderr,"[sp16-ui-stub] midi loopback %s first bytes=%ld\n",s->media==2?"/dev/din_midi":"/dev/ttymxc1",(long)n);
    return n;
}

/* The rawmidi UART backend complements the native guest sequencer client. Each app uses one ALSA API.
 * The one full-duplex virtio port is the UART wire: reads are DIN IN, writes are DIN OUT. An absolute monotonic
 * deadline is kept across calls so a 31,250 baud UART consumes one 10-bit byte every 320 us. Each byte takes the
 * lock separately, allowing a real-time write from another thread to use the next wire slot. */
typedef struct _snd_rawmidi snd_rawmidi_t;
#define RAWMIDI_MAGIC 0x534d4944u
#define MIDI_BYTE_NS 320000ULL
struct fake_rawmidi { uint32_t magic; int fd, input, nonblock; };
static volatile int rawmidi_wire_lock;
static uint64_t rawmidi_next_ns;
static int rawmidi_fd=-1;
static int rawmidi_is_fake(const snd_rawmidi_t *handle) {
    return handle && ((const struct fake_rawmidi *)handle)->magic==RAWMIDI_MAGIC;
}
static int rawmidi_open_port(void) {
    if (rawmidi_fd>=0) return rawmidi_fd;
    const char *path=getenv("SP16_DIN_PORT");
    if (!path) path="/dev/sp16-din";
    int fd=original_open(path,O_RDWR|O_CLOEXEC|O_NONBLOCK);
    if (fd>=0) rawmidi_fd=fd;
    return fd;
}
int snd_rawmidi_open(snd_rawmidi_t **input,snd_rawmidi_t **output,const char *name,int mode) {
    int (*real)(snd_rawmidi_t **,snd_rawmidi_t **,const char *,int)=dlsym(RTLD_NEXT,"snd_rawmidi_open");
    if (!name || !strstr(name,"snddinmidi"))
        return real?real(input,output,name,mode):-ENOSYS;
    if ((!input && !output) || (input && output && input==output)) return -EINVAL;
    int port=rawmidi_open_port();
    if (port<0) return -errno;
    struct fake_rawmidi *in=NULL,*out=NULL;
    if (input) { in=calloc(1,sizeof(*in)); if (!in) return -ENOMEM; }
    if (output) { out=calloc(1,sizeof(*out)); if (!out) { free(in); return -ENOMEM; } }
    if (in) { in->magic=RAWMIDI_MAGIC; in->fd=port; in->input=1; in->nonblock=!!(mode&1); *input=(snd_rawmidi_t *)in; }
    if (out) { out->magic=RAWMIDI_MAGIC; out->fd=port; out->nonblock=!!(mode&1); *output=(snd_rawmidi_t *)out; }
    fprintf(stderr,"[sp16-ui-stub] rawmidi %s -> %s input=%d output=%d\n",name,
            getenv("SP16_DIN_PORT")?getenv("SP16_DIN_PORT"):"/dev/sp16-din",!!input,!!output);
    return 0;
}
int snd_rawmidi_close(snd_rawmidi_t *handle) {
    int (*real)(snd_rawmidi_t *)=dlsym(RTLD_NEXT,"snd_rawmidi_close");
    if (!rawmidi_is_fake(handle)) return real?real(handle):-ENOSYS;
    struct fake_rawmidi *fake=(struct fake_rawmidi *)handle;
    fake->magic=0; free(fake);
    /* The port remains open for the other direction and future rawmidi opens in this app process. */
    return 0;
}
int snd_rawmidi_nonblock(snd_rawmidi_t *handle,int nonblock) {
    int (*real)(snd_rawmidi_t *,int)=dlsym(RTLD_NEXT,"snd_rawmidi_nonblock");
    if (!rawmidi_is_fake(handle)) return real?real(handle,nonblock):-ENOSYS;
    __atomic_store_n(&((struct fake_rawmidi *)handle)->nonblock,!!nonblock,__ATOMIC_RELEASE);
    return 0;
}
static int rawmidi_write_byte(int fd,uint8_t byte,int nonblock) {
    while (__sync_lock_test_and_set(&rawmidi_wire_lock,1)) sched_yield();
    uint64_t now=mono_ns();
    uint64_t target=(rawmidi_next_ns>now?rawmidi_next_ns:now)+MIDI_BYTE_NS;
    struct timespec deadline={ .tv_sec=(time_t)(target/1000000000ULL),.tv_nsec=(long)(target%1000000000ULL) };
    int slept;
    do { slept=clock_nanosleep(CLOCK_MONOTONIC,TIMER_ABSTIME,&deadline,NULL); } while (slept==EINTR);
    int error=slept;
    if (!error) for (;;) {
        ssize_t wrote=original_write(fd,&byte,1);
        if (wrote==1) break;
        error=wrote==0 ? EIO : errno;
        if (error!=EAGAIN || nonblock) break;
        struct pollfd ready={ .fd=fd,.events=POLLOUT };
        if (poll(&ready,1,-1)<0 && errno!=EINTR) { error=errno; break; }
        error=0;
    }
    rawmidi_next_ns=target;
    __sync_lock_release(&rawmidi_wire_lock);
    return error ? -error : 0;
}
ssize_t snd_rawmidi_read(snd_rawmidi_t *handle,void *buffer,size_t count) {
    ssize_t (*real)(snd_rawmidi_t *,void *,size_t)=dlsym(RTLD_NEXT,"snd_rawmidi_read");
    if (!rawmidi_is_fake(handle)) return real?real(handle,buffer,count):-ENOSYS;
    struct fake_rawmidi *fake=(struct fake_rawmidi *)handle;
    if (!fake->input) return -EBADF;
    ssize_t n;
    for (;;) {
        n=original_read(fake->fd,buffer,count);
        if (n>=0 || errno!=EAGAIN || __atomic_load_n(&fake->nonblock,__ATOMIC_ACQUIRE)) break;
        struct pollfd ready={ .fd=fake->fd,.events=POLLIN };
        if (poll(&ready,1,-1)<0 && errno!=EINTR) return -errno;
    }
    if (n>0 && gpio_ready && __atomic_load_n(&gpio_level[5],__ATOMIC_ACQUIRE)==1) {
        /* MIDI_PASS is the hardware THRU gate. Echo incoming bytes to the host, not back to the app. */
        const uint8_t *bytes=buffer;
        for (ssize_t i=0;i<n;i++) (void)rawmidi_write_byte(fake->fd,bytes[i],0);
    }
    return n<0?-errno:n;
}
ssize_t snd_rawmidi_write(snd_rawmidi_t *handle,const void *buffer,size_t count) {
    ssize_t (*real)(snd_rawmidi_t *,const void *,size_t)=dlsym(RTLD_NEXT,"snd_rawmidi_write");
    if (!rawmidi_is_fake(handle)) return real?real(handle,buffer,count):-ENOSYS;
    struct fake_rawmidi *fake=(struct fake_rawmidi *)handle;
    if (fake->input) return -EBADF;
    const uint8_t *bytes=buffer;
    for (size_t i=0;i<count;i++) {
        int result=rawmidi_write_byte(fake->fd,bytes[i],__atomic_load_n(&fake->nonblock,__ATOMIC_ACQUIRE));
        if (result) return i?(ssize_t)i:result;
    }
    return (ssize_t)count;
}
int snd_rawmidi_drain(snd_rawmidi_t *handle) {
    int (*real)(snd_rawmidi_t *)=dlsym(RTLD_NEXT,"snd_rawmidi_drain");
    return rawmidi_is_fake(handle)?0:real?real(handle):-ENOSYS;
}
int snd_rawmidi_drop(snd_rawmidi_t *handle) {
    int (*real)(snd_rawmidi_t *)=dlsym(RTLD_NEXT,"snd_rawmidi_drop");
    return rawmidi_is_fake(handle)?0:real?real(handle):-ENOSYS;
}
int snd_rawmidi_poll_descriptors_count(snd_rawmidi_t *handle) {
    int (*real)(snd_rawmidi_t *)=dlsym(RTLD_NEXT,"snd_rawmidi_poll_descriptors_count");
    return rawmidi_is_fake(handle)?1:real?real(handle):-ENOSYS;
}
int snd_rawmidi_poll_descriptors(snd_rawmidi_t *handle,struct pollfd *fds,unsigned int space) {
    int (*real)(snd_rawmidi_t *,struct pollfd *,unsigned int)=dlsym(RTLD_NEXT,"snd_rawmidi_poll_descriptors");
    if (!rawmidi_is_fake(handle)) return real?real(handle,fds,space):-ENOSYS;
    if (!fds || !space) return -EINVAL;
    struct fake_rawmidi *fake=(struct fake_rawmidi *)handle;
    fds[0]=(struct pollfd){ .fd=fake->fd,.events=fake->input?POLLIN:POLLOUT };
    return 1;
}
int snd_rawmidi_poll_descriptors_revents(snd_rawmidi_t *handle,struct pollfd *fds,unsigned int count,unsigned short *revents) {
    int (*real)(snd_rawmidi_t *,struct pollfd *,unsigned int,unsigned short *)=dlsym(RTLD_NEXT,"snd_rawmidi_poll_descriptors_revents");
    if (!rawmidi_is_fake(handle)) return real?real(handle,fds,count,revents):-ENOSYS;
    if (!fds || !count || !revents) return -EINVAL;
    *revents=(unsigned short)fds[0].revents;
    return 0;
}

/* Driver coverage (hardware suite HW-45, docs/architecture.md M5): a device node the shim does not serve is reported once
 * per path, so a firmware version that uses another driver is diagnosable; the open itself is passed through. */
static int unknown_device_node(const char *path) {
    static const char *standard[]={"/dev/null","/dev/full","/dev/zero","/dev/random","/dev/urandom","/dev/tty",
                                   "/dev/console","/dev/ptmx",0};
    if (!strncmp(path,"/proc/udev_",11)) return 1;
    if (strncmp(path,"/dev/",5) || !strncmp(path,"/dev/sp16-",10) || !strncmp(path,"/dev/shm/",9)
        || !strncmp(path,"/dev/pts/",9)) return 0;
    for (int i=0; standard[i]; i++) if (!strcmp(path,standard[i])) return 0;
    return 1;
}

static void note_unknown_device(const char *path) {
    static char seen[16][64];
    static int count;
    for (int i=0; i<count; i++) if (!strncmp(seen[i],path,sizeof seen[i]-1)) return;
    if (count<16) { strncpy(seen[count],path,sizeof seen[count]-1); count++; }
    fprintf(stderr, "[sp16-ui-stub] unknown device open path=%s\n", path);
}

static int open_device(const char *path, int flags, mode_t mode) {
    if (!original_open) original_open=dlsym(RTLD_NEXT,"open");
    if (!original_close) original_close=dlsym(RTLD_NEXT,"close");
    if (path && !strcmp(path,"/proc/asound/snddinmidi/midi0")) return din_capacity_file();
    enum device kind = classify(path);
    if (!kind) {
        if (path && unknown_device_node(path)) note_unknown_device(path);
        return original_open(path, flags, mode);
    }
    /* Idle sockets can participate in epoll without an always-readable EOF. */
    const char *enabled=getenv("SP16_PANEL_INPUT");
    int native=enabled && !strcmp(enabled,"1");
    int fd=-1;
    if (native && kind==PANEL) fd=input_socket(PANEL,"SP16_PANEL_SOCKET","/dev/sp16-panel-input",panel_path);
    else if (native && kind==CDC) fd=input_socket(CDC,"SP16_CDC_SOCKET","/dev/sp16-cdc-input",cdc_path);
    else if (kind==MIDI) fd=midi_socket(!strcmp(path,"/dev/din_midi")?2:1);
    else if (kind==MIDI_CONTROL) {
        if (din_control_request(UINT32_MAX)<0) return -1;
        fd=idle_socket(kind);
    }
    else if (kind==GPIO) fd=gpio_open();
    else if (kind==MEDIA) {
        int node=!strcmp(path,"/proc/udev_usbctn1")?2:1;
        fd=media_socket(node);
        if (fd<0) { /* no queue socket available: keep the old inert behaviour rather than failing the open */
            fd=idle_socket(kind);
            if (fd>=0) devices[fd].media=(uint8_t)node;
        }
    }
    else fd=idle_socket(kind);
    fprintf(stderr, "[sp16-ui-stub] open %s => fd=%d\n", path, fd);
    return fd;
}

int open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list args; va_start(args, flags); mode = va_arg(args, mode_t); va_end(args); }
    return open_device(path, flags, mode);
}
int open64(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list args; va_start(args, flags); mode = va_arg(args, mode_t); va_end(args); }
    return open_device(path, flags, mode);
}

off_t lseek(int fd,off_t offset,int whence) {
    off_t (*real)(int,off_t,int)=dlsym(RTLD_NEXT,"lseek");
    struct state *s=device(fd);
    if (s && s->kind==GPIO) return gpio_seek(s,offset,whence);
    return real(fd,offset,whence);
}
int64_t lseek64(int fd,int64_t offset,int whence) {
    int64_t (*real)(int,int64_t,int)=dlsym(RTLD_NEXT,"lseek64");
    struct state *s=device(fd);
    if (s && s->kind==GPIO) return gpio_seek(s,(off_t)offset,whence);
    return real(fd,offset,whence);
}

int close(int fd) {
    if (!original_close) original_close=dlsym(RTLD_NEXT,"close");
    struct state *s = device(fd);
    if (s) {
        int peer=s->peer;
        if (s->input==1) { unlink(panel_path); panel_path[0]=0; }
        else if (s->input==2) { unlink(cdc_path); cdc_path[0]=0; }
        else if (s->kind==MEDIA && s->peer<0 && s->media && media_fd[s->media]>=0) {
            if (--media_refs[s->media]<=0) {
                unlink(media_path[s->media]); media_path[s->media][0]=0;
                media_fd[s->media]=-1;
            } else if (fd==media_fd[s->media]) {
                /* The bound descriptor closes first: keep the queue alive through a duplicate. */
                int keep=dup(fd);
                if (keep>=0 && keep<MAX_FDS) { devices[keep]=(struct state){ .kind=MEDIA,.peer=-1,.media=s->media }; media_fd[s->media]=keep; }
                else media_fd[s->media]=-1;
            }
        }
        memset(s, 0, sizeof(*s));
        if (peer>=0) original_close(peer);
    }
    return original_close(fd);
}

ssize_t write(int fd, const void *buffer, size_t count) {
    if (!original_write) original_write=dlsym(RTLD_NEXT,"write");
    struct state *s = device(fd);
    if (!s) return original_write(fd, buffer, count);
    if (s->kind==MEDIA) {
        media_note(s,"write");
        if (s->peer<0) return media_write(s,fd,buffer,count);
    }
    if (s->kind==MIDI) return midi_write(s,buffer,count);
    if (s->kind==MIDI_CONTROL) { errno=ENOTSUP; return -1; }
    if (s->kind==GPIO) return gpio_io(s,(void *)buffer,count,1);
    if (s->kind == MEDIA || s->kind==PACKET) { errno=ENOTSUP; return -1; }
    if (s->timer) { errno=EFAULT; return -1; }
    if (s->kind==DAC) dac_write(buffer,count);
    /* The OEM synchronous SPI write contract returns zero, not count. */
    return 0;
}

ssize_t read(int fd, void *buffer, size_t count) {
    if (!original_read) original_read=dlsym(RTLD_NEXT,"read");
    struct state *s = device(fd);
    if (!s) return original_read(fd, buffer, count);
    /* CDC reads with the scan timer off keep the earlier neutral-state contract. */
    if (s->input==2 && !s->timer) {
        if (count>128) { errno=EMSGSIZE; return -1; }
        memset(buffer,0,count);
        return (ssize_t)count;
    }
    if (s->input) {
        if (count>128) { errno=EMSGSIZE; return -1; }
        ssize_t result=original_read(fd,buffer,count);
        static unsigned logged=0;
        if (s->input==1 && result==128 && logged++<8) {
            const uint8_t *frame=buffer;
            fprintf(stderr,"[sp16-ui-stub] panel RX bytes=128 id=%u buttons=0x%02x selector=%02x%02x crc=%02x%02x\n",
                    frame[0],frame[1],frame[60],frame[61],frame[124],frame[125]);
        }
        return result;
    }
    if (s->kind==MIDI) return original_read(fd,buffer,count); /* the loopback cable */
    if (s->kind==MIDI_CONTROL) { errno=ENOTSUP; return -1; }
    if (s->kind==GPIO) return gpio_io(s,buffer,count,0);
    if (s->kind==MEDIA) {
        media_note(s,"read");
        if (s->peer<0) return original_read(fd,buffer,count); /* blocks while empty, pops one message */
    }
    if (s->kind==MEDIA || s->kind==PACKET || s->timer) { errno=EAGAIN; return -1; }
    if (count > 128) { errno=EMSGSIZE; return -1; }
    memset(buffer, 0, count); /* neutral touch/control state, not sampled hardware */
    return (ssize_t)count;
}

/* Hardware suite HW-45: the first accepted call of each request is logged once, so the run can show that every
 * request the profile declares was served (the bridge counts these lines in metrics.guest.ioctls_seen). */
static void note_ioctl(unsigned long request, enum device kind) {
    static unsigned long seen[32];
    static int count;
    static const char *names[] = {"none", "panel", "cdc", "dac", "backlight", "media", "packet", "midi", "gpio", "din-control"};
    int known = __atomic_load_n(&count, __ATOMIC_ACQUIRE);
    for (int i=0; i<known && i<32; i++) if (__atomic_load_n(&seen[i], __ATOMIC_RELAXED)==request) return;
    int slot = __atomic_fetch_add(&count, 1, __ATOMIC_ACQ_REL);  /* a racing duplicate line is harmless */
    if (slot>=32) return;
    __atomic_store_n(&seen[slot], request, __ATOMIC_RELEASE);
    fprintf(stderr, "[sp16-ui-stub] ioctl accepted request=0x%lx device=%s\n", request, names[kind]);
}

/* Fake eth0 (SP16_FAKE_ETH0=1, set with the factory test mode): the guest has only lo, but the DEVICE TEST page
 * (page 8) reads the MAC (retrying with 250 ms sleeps until bytes 3..5 are not all zero) and the IP of eth0 on the
 * UI thread, so without an interface it never returns. Every value below is fake: a locally administered MAC
 * (02:00:5e:10:00:01), 192.0.2.16/24 (TEST-NET-1) and an interface that is up. Link data (SIOCETHTOOL) is not
 * faked: it fails with EOPNOTSUPP. Off in normal runs, where eth0 stays absent. */
#define SP16_SIOCGIFFLAGS 0x8913UL
#define SP16_SIOCGIFADDR 0x8915UL
#define SP16_SIOCGIFNETMASK 0x891bUL
#define SP16_SIOCGIFHWADDR 0x8927UL
#define SP16_SIOCETHTOOL 0x8946UL
/* A networked guest has a real eth0, but QEMU user-mode can reject its carrier
 * query. Only an explicit opt-in supplies the ETHTOOL_GLINK result. */
static int network_glink_ioctl(unsigned long request, void *arg, int *result) {
    const char *value=getenv("SP16_NET_GLINK");
    if (!value || (strcmp(value,"0") && strcmp(value,"1")) ||
        request!=SP16_SIOCETHTOOL || !arg) return 0;
    struct ifreq *ifr=arg;
    if (strncmp(ifr->ifr_name,"eth0",IFNAMSIZ)) return 0;
    if (!ifr->ifr_data) { errno=EFAULT; *result=-1; return 1; }
    uint32_t *ethtool=(uint32_t *)ifr->ifr_data;
    if (ethtool[0]!=10) return 0; /* ETHTOOL_GLINK only */
    ethtool[1]=value[0]=='1' ? 1U : 0U;
    static int logged;
    if (!__atomic_exchange_n(&logged,1,__ATOMIC_RELAXED))
        fprintf(stderr,"[sp16-ui-stub] network eth0 GLINK=%u\n",ethtool[1]);
    *result=0;
    return 1;
}

static int network_arp_enabled(void) {
    const char *value=getenv("SP16_NET_ARP");
    return value && !strcmp(value,"1");
}

static int fake_eth0_enabled(void) {
    static int enabled=-1;
    if (enabled<0) { const char *v=getenv("SP16_FAKE_ETH0"); enabled = v && !strcmp(v,"1"); }
    return enabled;
}

static void fake_eth0_address(struct ifreq *ifr, uint32_t host_order_address) {
    struct sockaddr_in *in=(struct sockaddr_in *)&ifr->ifr_addr;
    memset(&ifr->ifr_addr, 0, sizeof ifr->ifr_addr);
    in->sin_family=AF_INET;
    in->sin_addr.s_addr=htonl(host_order_address);
}

/* Returns 1 and sets *result when the request is a fake-eth0 one, 0 to pass it on. */
static int fake_eth0_ioctl(unsigned long request, void *arg, int *result) {
    static const char *kinds[]={"SIOCGIFHWADDR","SIOCGIFADDR","SIOCGIFNETMASK","SIOCGIFFLAGS","SIOCETHTOOL"};
    static int logged[5];
    int kind;
    switch (request) {
    case SP16_SIOCGIFHWADDR: kind=0; break;
    case SP16_SIOCGIFADDR: kind=1; break;
    case SP16_SIOCGIFNETMASK: kind=2; break;
    case SP16_SIOCGIFFLAGS: kind=3; break;
    case SP16_SIOCETHTOOL: kind=4; break;
    default: return 0;
    }
    struct ifreq *ifr=arg;
    if (!fake_eth0_enabled() || !ifr) return 0;
    if (strncmp(ifr->ifr_name,"eth0",IFNAMSIZ)) return 0;
    if (!__atomic_exchange_n(&logged[kind], 1, __ATOMIC_RELAXED))
        fprintf(stderr, "[sp16-ui-stub] fake eth0 %s\n", kinds[kind]);
    switch (kind) {
    case 0: {
        static const uint8_t mac[6]={0x02,0x00,0x5e,0x10,0x00,0x01};
        memset(&ifr->ifr_hwaddr, 0, sizeof ifr->ifr_hwaddr);
        ifr->ifr_hwaddr.sa_family=ARPHRD_ETHER;
        memcpy(ifr->ifr_hwaddr.sa_data, mac, sizeof mac);
        *result=0; break; }
    case 1: fake_eth0_address(ifr, 0xc0000210UL); *result=0; break;
    case 2: fake_eth0_address(ifr, 0xffffff00UL); *result=0; break;
    case 3: ifr->ifr_flags=IFF_UP|IFF_RUNNING|IFF_BROADCAST; *result=0; break;
    default: errno=EOPNOTSUPP; *result=-1; break;
    }
    return 1;
}

static int device_ioctl(struct state *s, int fd, unsigned long request, void *arg);

int ioctl(int fd, unsigned long request, ...) {
    if (!original_ioctl) original_ioctl=dlsym(RTLD_NEXT,"ioctl");
    va_list args; va_start(args, request); void *arg=va_arg(args, void *); va_end(args);
    struct state *s = device(fd);
    if (!s) {
        int fake_result;
        if (network_glink_ioctl(request, arg, &fake_result)) return fake_result;
        if (fake_eth0_ioctl(request, arg, &fake_result)) return fake_result;
        return original_ioctl(fd, request, arg);
    }
    int result = device_ioctl(s, fd, request, arg);
    if (result==0) note_ioctl(request, s->kind);
    return result;
}

static int device_ioctl(struct state *s, int fd, unsigned long request, void *arg) {
    if (s->kind==BACKLIGHT && request==0x40047000UL) {
        if (!arg) { errno=EFAULT; return -1; }
        uint32_t level=*(const uint32_t *)arg;
        static uint32_t last_level=UINT32_MAX;
        if (level!=last_level) {
            last_level=level;
            fprintf(stderr,"[sp16-ui-stub] backlight level=%u\n",level);
        }
        return 0;
    }
    if (s->kind==MIDI) {
        /* A UART line setting has no meaning on the cable: TCGETS reports a zeroed kernel termios (36 bytes on
         * ARM), the TCSETS family and TCFLSH are accepted, FIONREAD is answered by the socket. */
        if (request==0x541bUL) return original_ioctl(fd,request,arg);
        if (!arg && request!=0x540bUL) { errno=EFAULT; return -1; }
        if (request==0x5401UL) { memset(arg,0,36); return 0; }
        if (request==0x5402UL || request==0x5403UL || request==0x5404UL || request==0x540bUL) return 0;
        fprintf(stderr, "[sp16-ui-stub] unsupported ioctl fd=%d request=0x%lx\n", fd, request);
        errno=ENOTTY; return -1;
    }
    if (s->kind==MIDI_CONTROL) {
        if (request!=0x40047000UL) { errno=ENOTTY; return -1; }
        if (!arg) { errno=EFAULT; return -1; }
        return din_control_request(*(const uint32_t *)arg);
    }
    if (s->kind!=PANEL && s->kind!=CDC && s->kind!=DAC) { errno=ENOTTY; return -1; }
    if (!arg) { errno=EFAULT; return -1; }
    switch (request) {
    case 0x40107000UL: {
        struct spi_message *m=arg;
        if (m->bytes>128 || (m->bytes && !m->tx_buf)) { errno=EINVAL; return -1; }
        /* Accept the validated TX configuration; no device response is invented. */
        if (s->kind==PANEL) tx_capture(m);
        return 0;
    }
    case 0x80017001UL: *(uint8_t *)arg=s->timer; return 0;
    case 0x40017001UL: s->timer=!!*(uint8_t *)arg; return 0;
    case 0x80047002UL: *(uint32_t *)arg=s->interval; return 0;
    case 0x40047002UL: s->interval=*(uint32_t *)arg; return 0;
    case 0x80027003UL: *(uint16_t *)arg=s->bits; return 0;
    case 0x40027003UL:
        if (s->timer) { errno=EBUSY; return -1; } s->bits=*(uint16_t *)arg; return 0;
    case 0x80047004UL: *(uint32_t *)arg=s->bytes; return 0;
    case 0x40047004UL:
        if (s->timer) { errno=EBUSY; return -1; }
        if (*(uint32_t *)arg>128) { errno=EINVAL; return -1; } s->bytes=*(uint32_t *)arg; return 0;
    case 0x40017005UL: if (s->kind==PANEL) return 0; break; /* dummy TX */
    case 0x40047007UL: if (s->kind==PANEL) return 0; break; /* ready pin selector */
    case 0x80017006UL: if (s->kind==PANEL) { *(uint8_t *)arg=1; return 0; } break;
    case 0x40017008UL: if (s->kind==PANEL) { *(uint8_t *)arg=s->endian; return 0; } break;
    case 0x40017009UL: if (s->kind==PANEL) { s->endian=*(uint8_t *)arg; return 0; } break;
    case 0x40017006UL: if (s->kind==CDC) return 0; break; /* capacitive converter reset */
    }
    fprintf(stderr, "[sp16-ui-stub] unsupported ioctl fd=%d request=0x%lx\n", fd, request);
    errno=ENOTTY; return -1;
}

/* Use the stock ALSA null plugin only for the three observed board PCM ports.
 * Keep real PCM handles/parameter validation, with pacing to avoid a busy loop.
 */
typedef struct _snd_pcm snd_pcm_t;
typedef struct _snd_pcm_hw_params snd_pcm_hw_params_t;
typedef unsigned long snd_pcm_uframes_t;
typedef long snd_pcm_sframes_t;
/* Diagnostic counters (logged every few seconds) so audio pacing can be measured, not guessed. */
struct ax_ring;
#define CODEC_RATE 44100u /* the only rate the codec clock offers, see codec_rate_fixed */
struct pcm_state { snd_pcm_t *handle; unsigned int rate; int stream; char name[24];
                   uint64_t calls, frames, slept_ns, reported_ns, deadline_ns; int tid;
                   uint32_t logged; uint8_t io_logged; struct pcm_tap *tap; struct ax_ring *ax;
                   int format; unsigned channels; float phase; };
static struct pcm_state pcms[16];
/* Research-only PCM tap (SP16_PCM_TAP=<dir>, set by guest-init only for --trace pcmtap builds). Playback frames
 * written by the OEM are copied with one memcpy into a preallocated, bounded, MAP_SHARED file
 * <dir>/sp16-pcm-<device>.raw, so the audio thread makes no syscall and never waits: pacing is untouched.
 * Capture starts at the first non-zero frame (SP16_PCM_TAP_ARM=0: immediately) and stops after
 * SP16_PCM_TAP_SECONDS (default 3, max 30). The 64-byte little-endian header is finalized last (done=1). */
#define PCM_TAP_HDR 64u
#define PCM_TAP_DEFAULT_S 3u
#define PCM_TAP_MAX_S 30u
struct pcm_tap { uint8_t *map; size_t cap_frames; uint32_t bpf, armed, is_float, arm_signal; uint64_t seen; };
static void tap_u32(uint8_t *h,unsigned off,uint32_t v) { memcpy(h+off,&v,4); }
static void tap_u64(uint8_t *h,unsigned off,uint64_t v) { memcpy(h+off,&v,8); }
static uint64_t tap_get64(const uint8_t *h,unsigned off) { uint64_t v; memcpy(&v,h+off,8); return v; }
static void pcm_state_init_tap(struct pcm_state *s,unsigned channels,int format);
static void pcm_tap_close(struct pcm_state *s);
static void ax_init(struct pcm_state *s,unsigned channels,int format);
static void ax_close(struct pcm_state *s);
static void ax_feed(struct pcm_state *s,const void *buffer,snd_pcm_sframes_t frames);
static void ax_report(struct pcm_state *s);
static void main_out_scale(struct ax_ring *r,uint8_t *data,uint32_t frames);
static struct pcm_state *pcm(snd_pcm_t *handle) {
    for (unsigned i=0;i<16;i++) if (pcms[i].handle==handle) return &pcms[i];
    return NULL;
}
/* Each hw_params call is logged once per handle (bit per call site). */
static int first_time(struct pcm_state *s, unsigned bit) {
    if (s->logged & (1u<<bit)) return 0;
    s->logged |= 1u<<bit; return 1;
}
/* Resolve a real libasound symbol without caching a stale pointer; NULL when libasound is absent. */
static void *real_symbol(const char *name) { return dlsym(RTLD_NEXT,name); }
/* libasound's hw_params entry points carry the ALSA_0.9.0rc4 version; the default lookup can resolve a different
 * ABI (observed as garbage return values), so use the versioned symbol and fall back to the plain name. */
static void *alsa_symbol(const char *name) {
    void *symbol=dlvsym(RTLD_NEXT,name,"ALSA_0.9.0rc4");
    return symbol?symbol:dlsym(RTLD_NEXT,name);
}
static const char *format_label(int format) {
    const char *(*name)(int)=real_symbol("snd_pcm_format_name");
    const char *text=name?name(format):NULL;
    return text?text:"?";
}
static const char *access_label(int access) {
    const char *(*name)(int)=real_symbol("snd_pcm_access_name");
    const char *text=name?name(access):NULL;
    return text?text:"?";
}
int snd_pcm_open(snd_pcm_t **handle, const char *name, int stream, int mode) {
    int (*real)(snd_pcm_t **,const char *,int,int)=dlsym(RTLD_NEXT,"snd_pcm_open");
    int matched=!strcmp(name,"hw:ak4452audio,0") || !strcmp(name,"hw:ak4452audio,1") ||
                !strncmp(name,"hw:ak4458audio,",15);
    int result=real(handle,matched?"null":name,stream,mode);
    if (matched) {
        fprintf(stderr,"[sp16-ui-stub] PCM %s -> null stream=%d result=%d\n",name,stream,result);
        if (!result) for (unsigned i=0;i<16;i++) if (!pcms[i].handle) {
            pcms[i]=(struct pcm_state){.handle=*handle,.rate=CODEC_RATE,.stream=stream};
            snprintf(pcms[i].name,sizeof(pcms[i].name),"%s",name); break; }
    }
    return result;
}
int snd_pcm_close(snd_pcm_t *handle) {
    int (*real)(snd_pcm_t *)=dlsym(RTLD_NEXT,"snd_pcm_close");
    struct pcm_state *s=pcm(handle); if (s) { pcm_tap_close(s); ax_close(s); memset(s,0,sizeof(*s)); }
    return real(handle);
}
/* hw_params wrappers: forward every call unchanged; log only handles in pcms[] (the redirected board PCMs),
 * once per call site. Values are read before and after the real call so requested and returned match. */
int snd_pcm_hw_params_set_access(snd_pcm_t *handle,snd_pcm_hw_params_t *params,int access) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,int)=alsa_symbol("snd_pcm_hw_params_set_access");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    int result=real(handle,params,access);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,0)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_access requested=%d(%s) result=%d\n",
                                      s->name,access,access_label(access),result);
    return result;
}
/* The AK4458 master codec advertises S16_LE only. The AK4452 codec driver declares S16_LE, S24_LE and S20_3LE
 * (its DAI stream tables); the
 * null plugin also takes FLOAT_LE, which the app's format search tries first, so without a restriction the board
 * streams run a sample path the unit never runs. The shim therefore refuses the formats the codec lacks by default, so
 * the app settles on S24_LE as on the unit. S20_3LE (three packed bytes) is refused too: the board model's sample
 * path (bm_width/bm_get/bm_put) carries only 2- and 4-byte layouts, so a 3-byte layout would leave the loop and input
 * buffers untouched. SP16_PCM_ANY_FORMAT=1 (research) restores the null plugin's answer. */
static int codec_refuses(const struct pcm_state *s,int format) {
    const char *any=getenv("SP16_PCM_ANY_FORMAT");
    if (!s || (any && !strcmp(any,"1"))) return 0;
    if (!strncmp(s->name,"hw:ak4458audio,",15)) return format!=2;
    if (!strncmp(s->name,"hw:ak4452audio,",15)) return format!=2 && format!=6;
    return 0;
}
/* The codec master clock is 1024 x 44.1 kHz and has no other setting (service manual clock tree), so every board PCM
 * accepts 44100 Hz only. set_rate refuses anything else as a driver restricted to one rate would; set_rate_near
 * answers 44100 whatever was asked, the way a near-search over a one-element rate set does. SP16_PCM_ANY_RATE=1
 * (research) restores the null plugin's answer. */
static int codec_rate_fixed(const struct pcm_state *s) {
    const char *any=getenv("SP16_PCM_ANY_RATE");
    return s && !(any && !strcmp(any,"1"));
}
static int codec_rate_refused(const struct pcm_state *s,unsigned int rate) { return codec_rate_fixed(s) && rate!=CODEC_RATE; }
int snd_pcm_hw_params_set_format(snd_pcm_t *handle,snd_pcm_hw_params_t *params,int format) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,int)=alsa_symbol("snd_pcm_hw_params_set_format");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    struct pcm_state *s=pcm(handle);
    int result=codec_refuses(s,format)?-EINVAL:real(handle,params,format);
    if (s && (first_time(s,1) || (!result && first_time(s,9)))) fprintf(stderr,"[sp16-ui-stub] PCM %s set_format requested=%d(%s) result=%d\n",
                                      s->name,format,format_label(format),result);
    return result;
}
int snd_pcm_hw_params_set_channels(snd_pcm_t *handle,snd_pcm_hw_params_t *params,unsigned int channels) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned int)=alsa_symbol("snd_pcm_hw_params_set_channels");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    int result=real(handle,params,channels);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,2)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_channels requested=%u result=%d\n",
                                      s->name,channels,result);
    return result;
}
int snd_pcm_hw_params_set_rate(snd_pcm_t *handle,snd_pcm_hw_params_t *params,unsigned int rate,int direction) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned int,int)=alsa_symbol("snd_pcm_hw_params_set_rate");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    struct pcm_state *s=pcm(handle);
    int result=codec_rate_refused(s,rate)?-EINVAL:real(handle,params,rate,direction);
    if (!result && s) s->rate=rate;
    if (s && first_time(s,3)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_rate requested=%u dir=%d result=%d\n",
                                      s->name,rate,direction,result);
    return result;
}
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *handle,snd_pcm_hw_params_t *params,unsigned int *rate,int *direction) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned int *,int *)=dlvsym(RTLD_NEXT,"snd_pcm_hw_params_set_rate_near","ALSA_0.9.0rc4");
    if (!real) real=alsa_symbol("snd_pcm_hw_params_set_rate_near");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    unsigned int requested=rate?*rate:0; int requested_dir=direction?*direction:0;
    struct pcm_state *s=pcm(handle);
    if (codec_rate_fixed(s) && rate) { *rate=CODEC_RATE; if (direction) *direction=0; } /* the clock offers one rate only */
    int result=real(handle,params,rate,direction);
    if (!result && s && rate && *rate) s->rate=*rate;
    if (s && first_time(s,4)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_rate_near requested=%u dir=%d returned=%u dir=%d result=%d\n",
                                      s->name,requested,requested_dir,rate?*rate:0,direction?*direction:0,result);
    return result;
}
int snd_pcm_hw_params_set_period_size_near(snd_pcm_t *handle,snd_pcm_hw_params_t *params,snd_pcm_uframes_t *frames,int *direction) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,snd_pcm_uframes_t *,int *)=alsa_symbol("snd_pcm_hw_params_set_period_size_near");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    snd_pcm_uframes_t requested=frames?*frames:0; int requested_dir=direction?*direction:0;
    int result=real(handle,params,frames,direction);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,5)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_period_size_near requested=%lu dir=%d returned=%lu dir=%d result=%d\n",
                                      s->name,(unsigned long)requested,requested_dir,(unsigned long)(frames?*frames:0),direction?*direction:0,result);
    return result;
}
int snd_pcm_hw_params_set_buffer_size_near(snd_pcm_t *handle,snd_pcm_hw_params_t *params,snd_pcm_uframes_t *frames) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *,snd_pcm_uframes_t *)=alsa_symbol("snd_pcm_hw_params_set_buffer_size_near");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    snd_pcm_uframes_t requested=frames?*frames:0;
    int result=real(handle,params,frames);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,6)) fprintf(stderr,"[sp16-ui-stub] PCM %s set_buffer_size_near requested=%lu returned=%lu result=%d\n",
                                      s->name,(unsigned long)requested,(unsigned long)(frames?*frames:0),result);
    return result;
}
/* Commit: after a successful snd_pcm_hw_params the params object holds the installed configuration, so the
 * negotiated values are read back with the real getters (not interposed). */
int snd_pcm_hw_params(snd_pcm_t *handle,snd_pcm_hw_params_t *params) {
    int (*real)(snd_pcm_t *,snd_pcm_hw_params_t *)=alsa_symbol("snd_pcm_hw_params");
    if (!real) { errno=ENOSYS; return -ENOSYS; }
    int result=real(handle,params);
    struct pcm_state *s=pcm(handle);
    if (s && first_time(s,8)) {
        int access=-1, format=-1, dir=0; unsigned int channels=0, rate=0, periods=0; snd_pcm_uframes_t period=0, buffer=0;
        int (*get_access)(const snd_pcm_hw_params_t *,int *)=alsa_symbol("snd_pcm_hw_params_get_access");
        int (*get_format)(const snd_pcm_hw_params_t *,int *)=alsa_symbol("snd_pcm_hw_params_get_format");
        int (*get_channels)(const snd_pcm_hw_params_t *,unsigned int *)=alsa_symbol("snd_pcm_hw_params_get_channels");
        int (*get_rate)(const snd_pcm_hw_params_t *,unsigned int *,int *)=alsa_symbol("snd_pcm_hw_params_get_rate");
        int (*get_period)(const snd_pcm_hw_params_t *,snd_pcm_uframes_t *,int *)=alsa_symbol("snd_pcm_hw_params_get_period_size");
        int (*get_buffer)(const snd_pcm_hw_params_t *,snd_pcm_uframes_t *)=alsa_symbol("snd_pcm_hw_params_get_buffer_size");
        int (*get_periods)(const snd_pcm_hw_params_t *,unsigned int *,int *)=alsa_symbol("snd_pcm_hw_params_get_periods");
        if (get_access) get_access(params,&access);
        if (get_format) get_format(params,&format);
        if (get_channels) get_channels(params,&channels);
        if (get_rate) get_rate(params,&rate,&dir);
        if (get_period) get_period(params,&period,&dir);
        if (get_buffer) get_buffer(params,&buffer);
        if (get_periods) get_periods(params,&periods,&dir);
        fprintf(stderr,"[sp16-ui-stub] PCM %s hw_params result=%d access=%s(%d) format=%s(%d) channels=%u rate=%u period_frames=%lu buffer_frames=%lu periods=%u stream=%s\n",
                s->name,result,access_label(access),access,format_label(format),format,channels,rate,
                (unsigned long)period,(unsigned long)buffer,periods,s->stream?"capture":"playback");
        if (!result) { pcm_state_init_tap(s,channels,format); ax_init(s,channels,format); }
    }
    if (s && !result) { /* the board model converts samples in the negotiated layout, also after a re-negotiation */
        int (*get_format)(const snd_pcm_hw_params_t *,int *)=alsa_symbol("snd_pcm_hw_params_get_format");
        int (*get_channels)(const snd_pcm_hw_params_t *,unsigned int *)=alsa_symbol("snd_pcm_hw_params_get_channels");
        int format=-1; unsigned int channels=0;
        if (get_format) get_format(params,&format);
        if (get_channels) get_channels(params,&channels);
        s->format=format; s->channels=channels;
    }
    return result;
}
/* Pace each stream against an absolute deadline on the (shared) 48 kHz board clock. A relative sleep per
 * call made one audio thread that serves several devices (the OEM engine writes three playback PCMs back to
 * back) advance at 1/N of real time, so the sequencer ran about 3.5x slow. With per-stream deadlines the
 * waits overlap, as they do on the real I2S clock. A stream that falls too far behind is resynchronised
 * instead of bursting to catch up. */
#define PCM_MAX_LAG_NS 100000000ULL
#define PCM_STATS_NS 60000000000ULL

/* Header layout: 0 magic "SP16PCM1", 8 version, 12 done, 16 channels, 20 alsa format, 24 rate, 28 bytes/frame,
 * 32 cap_frames, 40 captured_frames, 48 first_frame_index (frames written before capture started),
 * 56 start_mono_ns. */
static unsigned tap_seconds(void) {
    const char *text=getenv("SP16_PCM_TAP_SECONDS"); long v=text?strtol(text,NULL,10):PCM_TAP_DEFAULT_S;
    return v<1?PCM_TAP_DEFAULT_S:(v>(long)PCM_TAP_MAX_S?PCM_TAP_MAX_S:(unsigned)v);
}
static unsigned tap_width(int format) { /* bytes per sample: S16_LE=2, FLOAT_LE=14, S32_LE=10, S24_LE=6 -> 4 */
    return format==2?2:(format==14||format==10||format==6?4:0);
}
static void pcm_state_init_tap(struct pcm_state *s,unsigned channels,int format) {
    const char *dir=getenv("SP16_PCM_TAP");
    if (!dir || !*dir || s->stream!=0 || s->tap) return;
    unsigned width=tap_width(format);
    if (!channels || !width || channels>32) return;
    static struct pcm_tap taps[16]; struct pcm_tap *t=&taps[s-pcms];
    memset(t,0,sizeof(*t));
    t->is_float=format==14; t->bpf=channels*width; t->cap_frames=(size_t)tap_seconds()*s->rate;
    const char *dev=strchr(s->name,':'); dev=dev?dev+1:s->name;
    char path[256], clean[48]; unsigned n=0;
    for (;dev[n] && n<sizeof(clean)-1;n++) clean[n]=(dev[n]>='a'&&dev[n]<='z')||(dev[n]>='0'&&dev[n]<='9')?dev[n]:'-';
    clean[n]=0;
    snprintf(path,sizeof(path),"%s/sp16-pcm-%s.raw",dir,clean);
    size_t bytes=PCM_TAP_HDR+t->cap_frames*t->bpf;
    int fd=open(path,O_RDWR|O_CREAT|O_TRUNC,0600);
    if (fd<0) { fprintf(stderr,"[sp16-ui-stub] PCM tap %s open failed errno=%d\n",path,errno); return; }
    if (ftruncate(fd,(off_t)bytes)!=0) { close(fd); return; }
    void *map=mmap(NULL,bytes,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0); close(fd);
    if (map==MAP_FAILED) return;
    t->map=map; t->armed=getenv("SP16_PCM_TAP_ARM")&&!strcmp(getenv("SP16_PCM_TAP_ARM"),"0"); t->arm_signal=!t->armed;
    memcpy(t->map,"SP16PCM1",8); tap_u32(t->map,8,1); tap_u32(t->map,16,channels); tap_u32(t->map,20,(uint32_t)format);
    tap_u32(t->map,24,s->rate); tap_u32(t->map,28,t->bpf); tap_u64(t->map,32,t->cap_frames);
    s->tap=t;
    fprintf(stderr,"[sp16-ui-stub] PCM tap %s -> %s cap_frames=%lu bytes=%lu arm_on_signal=%d\n",
            s->name,path,(unsigned long)t->cap_frames,(unsigned long)bytes,!t->armed);
}
static void pcm_tap_close(struct pcm_state *s) {
    if (!s->tap) return;
    struct pcm_tap *t=s->tap;
    if (!t->map[12]) tap_u32(t->map,12,1);
    munmap(t->map,PCM_TAP_HDR+t->cap_frames*t->bpf); t->map=NULL; s->tap=NULL;
}
/* Called after the real write with the data the OEM just queued. Bounded and non-blocking: no syscall, no lock. */
static void pcm_tap_feed(struct pcm_state *s,const void *buffer,snd_pcm_sframes_t frames) {
    struct pcm_tap *t=s->tap;
    /* The consumer re-arms a finished capture by zeroing captured/start and then done: wait for fresh signal. */
    if (t && t->map && t->arm_signal && t->armed && !t->map[12] && !tap_get64(t->map,40)) t->armed=0;
    if (!t || !t->map || frames<=0 || !buffer || tap_get64(t->map,40)>=t->cap_frames || (t->map[12])) {
        if (t) t->seen+=frames>0?(uint64_t)frames:0;
        return;
    }
    size_t bytes=(size_t)frames*t->bpf; const uint8_t *src=buffer;
    if (!t->armed) {
        size_t i=0;
        if (t->is_float) { /* -0.0 (sign bit only) is silence */
            for (;i+4<=bytes;i+=4) { uint32_t w; memcpy(&w,src+i,4); if (w<<1) break; }
            if (i+4>bytes) i=bytes;
        } else for (;i<bytes && !src[i];i++) {}
        if (i==bytes) { t->seen+=(uint64_t)frames; return; }
        t->armed=1; tap_u64(t->map,48,t->seen); tap_u64(t->map,56,mono_ns());
        /* start at the frame holding the first non-zero byte so silence does not lead the capture */
        size_t skip=i/t->bpf; src+=skip*t->bpf; bytes-=skip*t->bpf; t->seen+=skip; frames-=(snd_pcm_sframes_t)skip;
        tap_u64(t->map,48,t->seen);
    } else if (!tap_get64(t->map,56)) { tap_u64(t->map,48,t->seen); tap_u64(t->map,56,mono_ns()); }
    uint64_t have=tap_get64(t->map,40), room=t->cap_frames-have;
    uint64_t take=(uint64_t)frames<room?(uint64_t)frames:room;
    memcpy(t->map+PCM_TAP_HDR+have*t->bpf,src,take*t->bpf);
    tap_u64(t->map,40,have+take); t->seen+=(uint64_t)frames;
    if (have+take>=t->cap_frames) tap_u32(t->map,12,2); /* done=2: full */
}

/* Audio transport (off unless SP16_PCM_PORT names the guest virtio-serial node, set by guest-init for
 * sp16.audio=file). The audio thread (snd_pcm_writei) only copies the interesting channels into a per-stream
 * lock-free SPSC ring of 256 period slots (about 370 ms): no lock, no syscall besides the clock read pace() already makes, and it
 * never waits. A full ring drops the new period and counts it (the frame index still advances, so the host sees a
 * gap). A forwarder thread (the only consumer) polls every 2 ms, coalesces contiguous slots into one framed
 * packet and writes it to the port; if the host is slow or absent only the ring fills and drops. The deadline
 * clock in pace() stays the master. The host can ping (16 bytes "SPNG", pad, u64 host ns); the pong lets it
 * measure the clock offset and so the guest-write-to-host-receipt latency.
 * Packet (little endian, 48-byte header): 0 magic "SPCM"; 4 type (1 config, 2 data, 4 pong);
 * 5 stream (0 ak4458,0; 1 ak4452,0; 2 ak4452,1); 6 ALSA format; 7 sent channels; 8 total channels; 9 first channel;
 * 10 reserved; 12 rate; 16 sequence (global, one counter for the port); 20 frames; 24 guest ns of the oldest frame
 * (pong: now); 32 absolute frame index of the first frame (pong: echoed host ns); 40 cumulative dropped frames;
 * 44 payload bytes; then the payload (interleaved sent channels). */
#define AX_SLOTS 256u
#define AX_SLOT_FRAMES 128u
#define AX_SLOT_BYTES (AX_SLOT_FRAMES*32u)
#define AX_STREAMS 3u
#define AX_HDR 48u
#define AX_MAX_SLOTS_PER_PACKET 16u
struct ax_slot { uint64_t ts_ns, frame_index; uint32_t frames; uint8_t data[AX_SLOT_BYTES]; };
struct ax_ring { struct ax_slot slots[AX_SLOTS]; uint32_t head, tail, cfg_gen, active;
                 uint32_t rate, width, total_ch, first_ch, sent_ch, fmt, src_fmt, stream, high_water;
                 uint64_t next_frame, drops, packets, frames_sent; };
static struct ax_ring ax_rings[AX_STREAMS];
static const char *ax_port_path;
static int ax_thread_started;
static void *ax_forwarder(void *unused);
static void ax_put32(uint8_t *h,unsigned o,uint32_t v) { memcpy(h+o,&v,4); }
static void ax_put64(uint8_t *h,unsigned o,uint64_t v) { memcpy(h+o,&v,8); }
static void ax_init(struct pcm_state *s,unsigned channels,int format) {
    ax_port_path=getenv("SP16_PCM_PORT");
    if (!ax_port_path || !*ax_port_path || s->stream!=0 || s->ax || !channels || channels>16) return;
    unsigned id=!strcmp(s->name,"hw:ak4458audio,0")?0:!strcmp(s->name,"hw:ak4452audio,0")?1:!strcmp(s->name,"hw:ak4452audio,1")?2:AX_STREAMS;
    unsigned width=tap_width(format);
    if (id>=AX_STREAMS || !width || width*channels>32) return;
    struct ax_ring *r=&ax_rings[id];
    unsigned first=0, count=channels;   /* by default the whole stream travels, so the host output monitor can pick any pair */
    const char *env=id==0?getenv("SP16_PCM_PORT_CHANNELS"):NULL; /* research override "first:count" */
    char from_share[32];
    if (id==0 && !env && original_open && original_read) { /* the same override from the developer share, for a build whose environment is fixed */
        int fd=original_open("/opt/sp16/dev-share/pcm-port-channels",O_RDONLY|O_CLOEXEC);
        if (fd>=0) { ssize_t n=original_read(fd,from_share,sizeof from_share-1); original_close(fd);
                     if (n>0) { from_share[n]=0; env=from_share; } }
    }
    if (env) { unsigned a,b; if (sscanf(env,"%u:%u",&a,&b)==2 && b && a+b<=channels) { first=a; count=b; } }
    __atomic_store_n(&r->active,0,__ATOMIC_RELEASE);
    /* S24_LE (24 bits low in a 32-bit word) travels as FLOAT_LE, the format the host sink already writes */
    r->rate=s->rate; r->width=width; r->total_ch=channels; r->first_ch=first; r->sent_ch=count; r->src_fmt=(uint32_t)format;
    r->fmt=format==6?14u:(uint32_t)format; r->stream=id;
    __atomic_add_fetch(&r->cfg_gen,1,__ATOMIC_RELEASE);
    __atomic_store_n(&r->active,1,__ATOMIC_RELEASE);
    s->ax=r;
    fprintf(stderr,"[sp16-ui-stub] audio transport %s -> %s stream=%u fmt=%d channels=%u..%u of %u rate=%u\n",
            s->name,ax_port_path,id,format,first,first+count-1,channels,s->rate);
    if (!ax_thread_started) {
        int (*create)(unsigned long *,const void *,void *(*)(void *),void *)=dlsym(RTLD_DEFAULT,"pthread_create");
        unsigned long tid;
        if (create && !create(&tid,NULL,ax_forwarder,NULL)) ax_thread_started=1;
        else fprintf(stderr,"[sp16-ui-stub] audio transport: no forwarder thread (pthread_create unavailable)\n");
    }
}
static void ax_close(struct pcm_state *s) {
    if (!s->ax) return;
    __atomic_store_n(&s->ax->active,0,__ATOMIC_RELEASE); s->ax=NULL;
}
static void ax_feed(struct pcm_state *s,const void *buffer,snd_pcm_sframes_t frames) {
    struct ax_ring *r=s->ax;
    if (!r || frames<=0 || !buffer) return;
    uint64_t ts=mono_ns();
    const uint8_t *src=buffer; unsigned bpf=r->width*r->total_ch, run=r->width*r->sent_ch, skip=r->width*r->first_ch;
    while (frames>0) {
        uint32_t n=frames>(snd_pcm_sframes_t)AX_SLOT_FRAMES?AX_SLOT_FRAMES:(uint32_t)frames;
        uint32_t head=r->head, tail=__atomic_load_n(&r->tail,__ATOMIC_ACQUIRE);
        if (head-tail>=AX_SLOTS) __atomic_add_fetch(&r->drops,n,__ATOMIC_RELAXED);
        else {
            struct ax_slot *slot=&r->slots[head%AX_SLOTS];
            slot->ts_ns=ts; slot->frame_index=r->next_frame; slot->frames=n;
            if (run==bpf) memcpy(slot->data,src,(size_t)n*bpf);
            else for (uint32_t i=0;i<n;i++) memcpy(slot->data+(size_t)i*run,src+(size_t)i*bpf+skip,run);
            if (r->src_fmt==6) for (uint32_t i=0;i<n*r->sent_ch;i++) {
                int32_t v; memcpy(&v,slot->data+(size_t)i*4,4); v=(int32_t)((uint32_t)v<<8)>>8;
                float f=(float)v/8388608.0f; memcpy(slot->data+(size_t)i*4,&f,4);
            }
            if (r->stream==0) main_out_scale(r,slot->data,n);
            if (head+1-tail>r->high_water) r->high_water=head+1-tail;
            __atomic_store_n(&r->head,head+1,__ATOMIC_RELEASE);
        }
        r->next_frame+=n; src+=(size_t)n*bpf; frames-=(snd_pcm_sframes_t)n;
    }
}
static void ax_report(struct pcm_state *s) {
    struct ax_ring *r=s->ax;
    fprintf(stderr,"[sp16-ui-stub] audio-tx %s packets=%lu frames_sent=%lu drops=%lu ring_high_water=%u/%u\n",s->name,
            (unsigned long)__atomic_load_n(&r->packets,__ATOMIC_RELAXED),(unsigned long)__atomic_load_n(&r->frames_sent,__ATOMIC_RELAXED),
            (unsigned long)__atomic_load_n(&r->drops,__ATOMIC_RELAXED),r->high_water,AX_SLOTS);
}
static uint8_t ax_out[AX_HDR+AX_MAX_SLOTS_PER_PACKET*AX_SLOT_BYTES];
static uint32_t ax_seq, ax_cfg_sent[AX_STREAMS];
static int ax_fd=-1;
static int ax_send(const uint8_t *data,size_t size) {
    size_t done=0;
    while (done<size) {
        ssize_t n=write(ax_fd,data+done,size-done);
        if (n>0) { done+=(size_t)n; continue; }
        if (n<0 && errno==EINTR) continue;
        if (n<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) { struct pollfd p={ .fd=ax_fd,.events=POLLOUT }; poll(&p,1,5); continue; }
        close(ax_fd); ax_fd=-1; return -1; /* port went away: reopen; a half packet is discarded */
    }
    return 0;
}
static void ax_header(uint8_t *h,unsigned type,struct ax_ring *r,uint32_t frames,uint64_t ns,uint64_t index,uint32_t payload) {
    memset(h,0,AX_HDR); memcpy(h,"SPCM",4); h[4]=(uint8_t)type;
    if (r) { h[5]=(uint8_t)r->stream; h[6]=(uint8_t)r->fmt; h[7]=(uint8_t)r->sent_ch; h[8]=(uint8_t)r->total_ch; h[9]=(uint8_t)r->first_ch;
             ax_put32(h,12,r->rate); ax_put32(h,40,(uint32_t)__atomic_load_n(&r->drops,__ATOMIC_RELAXED)); }
    ax_put32(h,16,ax_seq++); ax_put32(h,20,frames); ax_put64(h,24,ns); ax_put64(h,32,index); ax_put32(h,44,payload);
}
static void *ax_forwarder(void *unused) {
    (void)unused;
    uint8_t ping[16]; unsigned have=0;
    for (;;) {
        if (ax_fd<0) {
            ax_fd=open(ax_port_path,O_RDWR|O_NONBLOCK|O_CLOEXEC);
            if (ax_fd<0) { struct timespec t={ .tv_sec=1 }; nanosleep(&t,NULL); continue; }
            have=0; memset(ax_cfg_sent,0,sizeof ax_cfg_sent);
        }
        ssize_t got=read(ax_fd,ping+have,sizeof ping-have);
        if (got>0) have+=(unsigned)got;
        if (have==sizeof ping) {
            have=0;
            if (!memcmp(ping,"SPNG",4)) { uint8_t pong[AX_HDR]; uint64_t t0; memcpy(&t0,ping+8,8);
                ax_header(pong,4,NULL,0,mono_ns(),t0,0); if (ax_send(pong,sizeof pong)) continue; }
        }
        for (unsigned id=0;id<AX_STREAMS && ax_fd>=0;id++) {
            struct ax_ring *r=&ax_rings[id];
            uint32_t gen=__atomic_load_n(&r->cfg_gen,__ATOMIC_ACQUIRE);
            if (!gen) continue;
            if (gen!=ax_cfg_sent[id]) { uint8_t cfg[AX_HDR]; ax_header(cfg,1,r,0,mono_ns(),r->next_frame,0);
                if (ax_send(cfg,sizeof cfg)) break; ax_cfg_sent[id]=gen; }
            uint32_t tail=r->tail, head=__atomic_load_n(&r->head,__ATOMIC_ACQUIRE);
            while (tail!=head && ax_fd>=0) {
                uint32_t count=0, frames=0; uint64_t expect=r->slots[tail%AX_SLOTS].frame_index;
                size_t bytes=0; uint64_t first_ns=r->slots[tail%AX_SLOTS].ts_ns, first_index=expect;
                while (tail+count!=head && count<AX_MAX_SLOTS_PER_PACKET) {
                    struct ax_slot *slot=&r->slots[(tail+count)%AX_SLOTS];
                    if (slot->frame_index!=expect) break; /* a drop: the next packet carries the new frame index */
                    size_t size=(size_t)slot->frames*r->width*r->sent_ch;
                    memcpy(ax_out+AX_HDR+bytes,slot->data,size); bytes+=size; frames+=slot->frames; expect+=slot->frames; count++;
                }
                ax_header(ax_out,2,r,frames,first_ns,first_index,(uint32_t)bytes);
                __atomic_store_n(&r->tail,tail+count,__ATOMIC_RELEASE); tail+=count;
                if (ax_send(ax_out,AX_HDR+bytes)) break;
                __atomic_add_fetch(&r->packets,1,__ATOMIC_RELAXED); __atomic_add_fetch(&r->frames_sent,frames,__ATOMIC_RELAXED);
            }
        }
        struct timespec nap={ .tv_nsec=2000000 }; nanosleep(&nap,NULL);
    }
    return NULL;
}
/* ---------------------------------------------------------------------------------------------- analog board model
 * What the board does to audio outside the firmware, from the service manual's signal path (hardware description in
 * our own words, docs/architecture.md):
 *  - The filter is an insert loop: the "Send & Return" playback (send DAC) goes through an analog low-pass (CUTOFF,
 *    RESONANCE pots, never seen by any MCU) and a high-pass whose cutoff is a control voltage from a DAC7811 on
 *    /dev/dac_spi4.0, and comes back on the "Send & Return" capture (return ADC). Here the send frames are queued and
 *    drained by the return capture through a two-pole low-pass and a two-pole high-pass.
 *  - MAIN OUT is an analog volume after the master DAC (its pot position also reaches the firmware as an ADC word),
 *    INPUT LEVEL an analog gain before the input ADC (no MCU sees it).
 * Host-only state (MAIN OUT gain, INPUT LEVEL, CUTOFF, RESONANCE, a test tone on the input) comes from the bridge
 * through a 64-byte shared file (SP16_ANALOG, default /dev/sp16-analog) with a sequence lock. A control the host
 * never moved is neutral (unity gain, filter open), so a session that does not touch them sounds as before.
 * The shim links no libm, hence the small sine and exp2 below. */
#define BM_PI 3.14159265f
static float bm_sin(float x) { /* |error| < 1e-4 on any input */
    while (x>BM_PI) x-=2*BM_PI;
    while (x<-BM_PI) x+=2*BM_PI;
    if (x>BM_PI/2) x=BM_PI-x; else if (x<-BM_PI/2) x=-BM_PI-x;
    float x2=x*x;
    return x*(1-x2/6*(1-x2/20*(1-x2/42*(1-x2/72))));
}
static float bm_tan(float x) { float c=bm_sin(x+BM_PI/2); return bm_sin(x)/(c>1e-6f?c:1e-6f); }
static float bm_exp2(float x) { /* 2^x, relative error < 1e-4 for |x| < 60 */
    if (x<-60) return 0;
    int i=(int)x; if ((float)i>x) i--;
    float f=x-(float)i, l=f*0.69314718f;
    float r=1+l*(1+l/2*(1+l/3*(1+l/4*(1+l/5*(1+l/6)))));
    for (;i>0;i--) r*=2;
    for (;i<0;i++) r*=.5f;
    return r;
}
#define BM_DECADES3 9.965784f /* log2(1000): 20 Hz .. 20 kHz over the pot or DAC range */
static float bm_log2(float x) { /* log2 for x > 0, error < 1e-4: exponent from the bits, mantissa by an atanh series */
    uint32_t bits; memcpy(&bits,&x,4);
    int e=(int)((bits>>23)&0xff)-127;
    bits=(bits&0x7fffffu)|0x3f800000u; float m; memcpy(&m,&bits,4); /* [1,2) */
    float t=(m-1)/(m+1), t2=t*t;
    return (float)e+2.8853901f*t*(1+t2/3*(1+t2*3/5*(1+t2*5/7)));
}
/* Level scale (grade I, not measured on a unit): the converters' full scale is the rated maximum level of the jacks,
 * +16 dBu (input and output). The dBu readouts in the metrics are peak-equivalent (dBFS + 16 from the peak level);
 * dBu proper is an RMS unit, so the fields are named *_dbu_peak to keep peak and RMS distinct. */
#define FS_DBU 16.0f
static float to_dbu(float peak) { return peak>1e-6f?FS_DBU+6.0205999f*bm_log2(peak):-99.0f; }

/* DAC7811 (high-pass control voltage): the app writes one little-endian 16-bit word per change, only when the
 * HPF pot word changes. The word is control nibble (bits 15..12) + 12-bit code. Logged on change, at most one line
 * per 50 ms; the newest word is flushed later from the audio clock (pace) so the last position is always logged. */
static uint32_t dac_word=0xffffffffu; /* 0xffffffff: never written */
static unsigned long dac_writes;
static uint32_t dac_logged=0xffffffffu;
static uint64_t dac_logged_ns;
static void dac_log(uint32_t word) {
    /* several audio threads may flush at once: only the one that swaps the logged word in prints */
    if (__atomic_exchange_n(&dac_logged,word,__ATOMIC_ACQ_REL)==word) return;
    fprintf(stderr,"[sp16-ui-stub] dac hpf word=0x%04x ctrl=%u code=%u writes=%lu\n",(unsigned)word,(unsigned)(word>>12),
            (unsigned)(word&0xfff),__atomic_load_n(&dac_writes,__ATOMIC_RELAXED));
    dac_logged_ns=mono_ns();
}
static void dac_write(const void *buffer,size_t count) {
    if (count!=2 || !buffer) {
        static int warned;
        if (!warned++) fprintf(stderr,"[sp16-ui-stub] dac write of %zu bytes ignored (expected 2)\n",count);
        return;
    }
    const uint8_t *b=buffer;
    uint32_t word=(uint32_t)b[0]|(uint32_t)b[1]<<8;
    __atomic_store_n(&dac_word,word,__ATOMIC_RELEASE);
    unsigned long n=__atomic_add_fetch(&dac_writes,1,__ATOMIC_RELAXED);
    if (word!=dac_logged && (n<=8 || mono_ns()-dac_logged_ns>=50000000ULL)) dac_log(word);
}
static void dac_flush(uint64_t now) {
    uint32_t word=__atomic_load_n(&dac_word,__ATOMIC_ACQUIRE);
    if (word!=0xffffffffu && word!=dac_logged && now-dac_logged_ns>=50000000ULL) dac_log(word);
}

/* Shared host state: 0 "SP16ANA1", 8 u32 sequence (odd while the bridge writes), then u16 values 0..1000 or 0xffff
 * (never moved): 12 main_out, 14 input_level, 16 lp_cutoff, 18 lp_resonance, 20 input tone level (0 = off),
 * 22 test tone frequency in Hz, 24 filter tone level (a research stimulus at the filter input, where the send DAC plays, 0 = off). */
struct analog { float main_gain, input_gain, lp_fc, lp_k, tone_amp, tone_hz, filter_amp; int main_set, lp_set; };
static struct analog analog_now={ 1,1,20000,2,0,1000,0,0,0 };
static const uint8_t *analog_map;
static uint64_t analog_checked_ns, analog_open_ns;
static float pot(uint16_t v) { return v>1000?1.0f:(float)v/1000.0f; }
static void analog_refresh(uint64_t now) {
    if (analog_checked_ns && now-analog_checked_ns<20000000ULL) return;
    analog_checked_ns=now;
    if (!analog_map) {
        if (analog_open_ns && now-analog_open_ns<1000000000ULL) return;
        analog_open_ns=now;
        const char *path=getenv("SP16_ANALOG"); if (!path) path="/dev/sp16-analog";
        int fd=original_open?original_open(path,O_RDONLY|O_CLOEXEC):-1;
        if (fd<0) return;
        void *map=mmap(NULL,64,PROT_READ,MAP_SHARED,fd,0); original_close(fd);
        if (map==MAP_FAILED) return;
        if (memcmp(map,"SP16ANA1",8)) { munmap(map,64); return; }
        analog_map=map;
        fprintf(stderr,"[sp16-ui-stub] analog board state mapped from %s\n",path);
    }
    uint32_t seq1, seq2; uint16_t v[7];
    memcpy(&seq1,analog_map+8,4); __atomic_thread_fence(__ATOMIC_ACQUIRE);
    if (seq1&1) return;
    memcpy(v,analog_map+12,sizeof v); __atomic_thread_fence(__ATOMIC_ACQUIRE);
    memcpy(&seq2,analog_map+8,4);
    if (seq1!=seq2) return;
    struct analog a=analog_now;
    a.main_set=v[0]!=0xffff; a.main_gain=a.main_set?pot(v[0])*pot(v[0]):1;       /* audio taper, unity at full */
    a.input_gain=v[1]!=0xffff?4*pot(v[1])*pot(v[1]):1;                            /* unity at the centre, +12 dB max */
    a.lp_set=v[2]!=0xffff || v[3]!=0xffff;
    a.lp_fc=v[2]!=0xffff?20*bm_exp2(pot(v[2])*BM_DECADES3):20000;
    a.lp_k=v[3]!=0xffff?2-1.9f*pot(v[3]):2;                                       /* 1/Q: 0.5 .. 10 */
    a.tone_amp=v[4] && v[4]!=0xffff?bm_exp2((-60+60*pot(v[4]))/6.0206f):0;       /* -60 .. 0 dBFS */
    a.tone_hz=v[5] && v[5]!=0xffff?(float)v[5]:1000;
    a.filter_amp=v[6] && v[6]!=0xffff?bm_exp2((-60+60*pot(v[6]))/6.0206f):0;
    analog_now=a;
}

/* Sample conversion for the layouts the app can negotiate (ALSA format ids). */
static unsigned bm_width(int format) { return format==2?2:(format==10||format==14||format==6?4:0); }
static float bm_get(const uint8_t *p,int format) {
    if (format==14) { float f; memcpy(&f,p,4); return f; }
    if (format==2) { int16_t v; memcpy(&v,p,2); return (float)v/32768.0f; }
    int32_t v; memcpy(&v,p,4);
    if (format==6) { v=(int32_t)((uint32_t)v<<8)>>8; return (float)v/8388608.0f; }
    return (float)v/2147483648.0f;
}
static void bm_put(uint8_t *p,int format,float f) {
    if (f>1) f=1; else if (f<-1) f=-1;
    if (format==14) { memcpy(p,&f,4); return; }
    if (format==2) { int16_t v=(int16_t)(f*32767.0f); memcpy(p,&v,2); return; }
    int32_t v=format==6?(int32_t)(f*8388607.0f):(int32_t)(f*2147483520.0f);
    memcpy(p,&v,4);
}

/* Two-pole state-variable filter (trapezoidal integration, stable under fast parameter changes). */
struct svf { float fc, k, a1, a2, a3, ic1[2], ic2[2]; };
static void svf_tune(struct svf *f,float fc,float k,float rate) {
    if (fc>0.45f*rate) fc=0.45f*rate;
    if (f->fc==fc && f->k==k) return;
    float g=bm_tan(BM_PI*fc/rate);
    f->fc=fc; f->k=k; f->a1=1/(1+g*(g+k)); f->a2=g*f->a1; f->a3=g*f->a2;
}
static float svf_run(struct svf *f,unsigned c,float x,int high) {
    float v3=x-f->ic2[c], v1=f->a1*f->ic1[c]+f->a2*v3, v2=f->ic2[c]+f->a2*f->ic1[c]+f->a3*v3;
    f->ic1[c]=2*v1-f->ic1[c]; f->ic2[c]=2*v2-f->ic2[c];
    return high?x-f->k*v1-v2:v2;
}

/* The loop: the send playback is queued as stereo float and the return capture drains it on demand (a FIFO, no fixed
 * one-period delay). A capture that finds the queue empty returns silence (counted), a full queue drops the newest
 * send frames (counted). */
#define LOOP_FRAMES 8192u
static float loop_buf[LOOP_FRAMES][2];
static uint32_t loop_head, loop_tail;
static unsigned long loop_frames, loop_underruns, loop_drops;
static float loop_send_peak, loop_return_peak;
static uint64_t loop_report_ns;
static struct svf loop_lp, loop_hp;
static int is_board(const struct pcm_state *s,const char *name,int stream) { return s->stream==stream && !strcmp(s->name,name); }
static void loop_send(struct pcm_state *s,const void *buffer,snd_pcm_sframes_t frames) {
    unsigned w=bm_width(s->format);
    if (!w || !s->channels || frames<=0 || !buffer) return;
    const uint8_t *p=buffer; unsigned bpf=w*s->channels;
    for (snd_pcm_sframes_t i=0;i<frames;i++,p+=bpf) {
        uint32_t head=loop_head, tail=__atomic_load_n(&loop_tail,__ATOMIC_ACQUIRE);
        if (head-tail>=LOOP_FRAMES) { loop_drops++; continue; }
        float l=bm_get(p,s->format), r=s->channels>1?bm_get(p+w,s->format):l;
        float m=l<0?-l:l, n=r<0?-r:r; if (m>loop_send_peak) loop_send_peak=m; if (n>loop_send_peak) loop_send_peak=n;
        loop_buf[head%LOOP_FRAMES][0]=l; loop_buf[head%LOOP_FRAMES][1]=r;
        __atomic_store_n(&loop_head,head+1,__ATOMIC_RELEASE);
    }
}
static void loop_return(struct pcm_state *s,void *buffer,snd_pcm_sframes_t frames) {
    unsigned w=bm_width(s->format);
    if (!w || !s->channels || frames<=0 || !buffer) return;
    struct analog a=analog_now;
    uint32_t word=__atomic_load_n(&dac_word,__ATOMIC_ACQUIRE);
    int hp_on=word!=0xffffffffu;
    float rate=(float)(s->rate?s->rate:44100);
    svf_tune(&loop_lp,a.lp_fc,a.lp_k,rate);
    if (hp_on) svf_tune(&loop_hp,20*bm_exp2((float)(word&0xfff)/4095.0f*BM_DECADES3),1.4142f,rate);
    uint8_t *p=buffer; unsigned bpf=w*s->channels;
    float step=2*BM_PI*a.tone_hz/rate;
    for (snd_pcm_sframes_t i=0;i<frames;i++,p+=bpf) {
        float extra=0;
        if (a.filter_amp>0) { extra=a.filter_amp*bm_sin(s->phase); s->phase+=step; if (s->phase>BM_PI) s->phase-=2*BM_PI; }
        uint32_t tail=loop_tail, head=__atomic_load_n(&loop_head,__ATOMIC_ACQUIRE);
        float in[2]={0,0};
        if (tail!=head) {
            in[0]=loop_buf[tail%LOOP_FRAMES][0]; in[1]=loop_buf[tail%LOOP_FRAMES][1];
            __atomic_store_n(&loop_tail,tail+1,__ATOMIC_RELEASE); loop_frames++;
        } else loop_underruns++;
        for (unsigned c=0;c<s->channels;c++) {
            unsigned k=c<2?c:1;
            float y=in[k]+extra; /* the research tone enters where the send DAC would, ahead of both filters */
            if (a.lp_set) y=svf_run(&loop_lp,k,y,0);
            if (hp_on) y=svf_run(&loop_hp,k,y,1);
            float m=y<0?-y:y; if (m>loop_return_peak) loop_return_peak=m;
            bm_put(p+c*w,s->format,y);
        }
    }
    uint64_t now=mono_ns();
    if (!loop_report_ns) loop_report_ns=now;
    if (now-loop_report_ns>=5000000000ULL && (loop_send_peak>0 || loop_return_peak>0)) {
        fprintf(stderr,"[sp16-ui-stub] filter loop send_peak=%.4f return_peak=%.4f frames=%lu underruns=%lu drops=%lu lp_hz=%.0f hp=%s\n",
                (double)loop_send_peak,(double)loop_return_peak,loop_frames,loop_underruns,loop_drops,
                (double)(a.lp_set?loop_lp.fc:0),hp_on?"dac":"off");
        loop_send_peak=loop_return_peak=0; loop_report_ns=now;
    }
}
/* Level meters for the metrics: peaks over the report window (linear, 1.0 = converter full scale). The input meter sits after
 * INPUT LEVEL and before the limiter; the output meter is the MAIN pair after MAIN OUT. */
static float meter_in_peak, meter_out_peak;
static unsigned long meter_limited;
static int meter_source, meter_limiter_logged=-1;  /* 0 off, 1 tone, 2 wav */
static uint64_t meter_report_ns;
/* Jack map of the AK4458 master stream, measured with the factory test's AUDIO TEST (docs/audio-jack-map.md): the
 * stream is 16 S16 channels, i.e. 8 DAC slots of two channels, and only the even channel of a slot carries audio
 * (the odd one is padding). MODE3 (input -> filter -> OUT1/2 + MASTER) lit channels 12 and 14 only, MODE2 (-> OUT7-8)
 * channels 0 and 2 only, so OUT1-2 = ch12/ch14 (observed), OUT7-8 = ch0/ch2 (observed), OUT3-4 = ch8/ch10 and
 * OUT5-6 = ch4/ch6 (inferred from the slot order; MODE1 alone lights all four pairs). */
#define MAIN_PAIR_LEFT 12u
#define MAIN_PAIR_RIGHT 14u
/* MAIN OUT: an analog volume on OUT1-2 / MASTER, applied to the copy the host hears (the app's own buffer, the PCM
 * tap and the firmware's ADC word are untouched). Only the channels of that pair are scaled; the other jacks are
 * not behind this knob. The out meter reports the same pair. */
static void main_out_scale(struct ax_ring *r,uint8_t *data,uint32_t frames) {
    struct analog a=analog_now;
    unsigned w=r->width, count=frames*r->sent_ch;
    int scale=a.main_set && a.main_gain!=1;
    for (unsigned i=0;i<count;i++) {
        unsigned channel=r->first_ch+i%r->sent_ch;
        if (channel!=MAIN_PAIR_LEFT && channel!=MAIN_PAIR_RIGHT) continue;
        uint8_t *q=data+(size_t)i*w;
        float v=bm_get(q,(int)r->fmt)*(scale?a.main_gain:1);
        if (scale) bm_put(q,(int)r->fmt,v);
        float m=v<0?-v:v; if (m>meter_out_peak) meter_out_peak=m;
    }
}
/* Input jack. Source, in order: a WAV the host placed in the developer share (research builds, `./sp16 run
 * --dev-overlay DIR` with DIR/input.wav; SP16_INPUT_WAV overrides the path), else the host test tone, else silence.
 * The WAV is 16/24/32-bit PCM or 32-bit float, mono or stereo (mono feeds both channels), up to 64 MB, looped, and
 * resampled linearly to the codec rate; it is re-read when its size changes (looked at once a second, or every
 * SP16_INPUT_WAV_POLL_MS milliseconds). Chain, as on the board: INPUT LEVEL gain, then the input limiter when the
 * firmware has switched it on (GPIO5_IO06), then the converter. The null plugin leaves a capture buffer untouched, so
 * silence is written (all-zero bytes are 0 in every format the codec takes, and 0.0 in FLOAT_LE) or the app's reused
 * buffer would keep the last tone samples.
 *
 * The file is copied into private memory (a mapping of a file the host truncates or replaces would fault). Threads: the
 * capture thread holds `inject_busy` for the whole of input_fill; the refresh runs on whichever PCM thread reaches pace()
 * first (one at a time, `inject_refreshing`), does its file work without the lock and takes it only to swap the source,
 * then unmaps the old copy, which no reader can reach any more. */
struct inject { uint8_t *base; size_t size; const uint8_t *data; size_t bytes; unsigned channels, bits, is_float;
                double rate, pos; uint64_t frames; };
#define INJECT_MAX_BYTES (64u<<20)
#ifndef MAP_ANONYMOUS
#define MAP_ANONYMOUS 0x20
#endif
static struct inject inject_now;         /* the source: read and replaced only with inject_busy held */
static int inject_busy;                  /* 1 while input_fill runs or the source is being swapped */
static int inject_refreshing;            /* 1 while one thread looks at the file */
static uint64_t inject_checked_ns, inject_poll_ns;
static size_t inject_loaded, inject_rejected; /* size of the WAV in use; size of the last file that was not usable (0: none) */
static int inject_parse(struct inject *w,const uint8_t *b,size_t size) {
    if (size<44 || memcmp(b,"RIFF",4) || memcmp(b+8,"WAVE",4)) return 0;
    unsigned fmt_tag=0, channels=0, bits=0; uint32_t rate=0; size_t at=12;
    int have_fmt=0;
    while (at+8<=size) {
        uint32_t len; memcpy(&len,b+at+4,4);
        if (!memcmp(b+at,"fmt ",4) && len>=16 && at+8+16<=size) {
            uint16_t v; memcpy(&v,b+at+8,2); fmt_tag=v; memcpy(&v,b+at+10,2); channels=v;
            memcpy(&rate,b+at+12,4); memcpy(&v,b+at+22,2); bits=v;
            if (fmt_tag==0xfffe && len>=26 && at+8+26<=size) { memcpy(&v,b+at+8+24,2); fmt_tag=v; }
            have_fmt=1;
        } else if (!memcmp(b+at,"data",4) && have_fmt) {
            size_t avail=size-(at+8); if (len>avail) len=(uint32_t)avail;
            int ok=(fmt_tag==1 && (bits==16 || bits==24 || bits==32)) || (fmt_tag==3 && bits==32);
            if (!ok || (channels!=1 && channels!=2) || !rate || len<channels*bits/8) return 0;
            w->data=b+at+8; w->bytes=len; w->channels=channels; w->bits=bits; w->is_float=fmt_tag==3; w->rate=(double)rate;
            w->frames=len/(channels*bits/8); w->pos=0; return 1;
        }
        at+=8+((size_t)len+1)/2*2;
    }
    return 0;
}
static float inject_sample(const struct inject *w,uint64_t frame,unsigned c) {
    const uint8_t *p=w->data+(size_t)frame*w->channels*(w->bits/8)+(c<w->channels?c:0)*(w->bits/8);
    if (w->is_float) { float f; memcpy(&f,p,4); return f; }
    if (w->bits==16) { int16_t v; memcpy(&v,p,2); return (float)v/32768.0f; }
    if (w->bits==24) { int32_t v=(int32_t)((uint32_t)p[0]<<8|(uint32_t)p[1]<<16|(uint32_t)p[2]<<24)>>8; return (float)v/8388608.0f; }
    int32_t v; memcpy(&v,p,4); return (float)v/2147483648.0f;
}
static void inject_lock(void) {
    while (__atomic_exchange_n(&inject_busy,1,__ATOMIC_ACQUIRE)) { struct timespec t={ .tv_nsec=20000 }; nanosleep(&t,NULL); }
}
static void inject_unlock(void) { __atomic_store_n(&inject_busy,0,__ATOMIC_RELEASE); }
/* Make `next` (NULL: nothing) the source and return the old one, which no reader can reach any more. */
static struct inject inject_swap(const struct inject *next) {
    inject_lock();
    struct inject old=inject_now;
    if (next) inject_now=*next; else memset(&inject_now,0,sizeof inject_now);
    __atomic_store_n(&inject_loaded,next?next->size:0,__ATOMIC_RELEASE);
    inject_unlock();
    return old;
}
static void inject_reload(void) {
    const char *path=getenv("SP16_INPUT_WAV"); if (!path) path="/opt/sp16/dev-share/input.wav";
    size_t loaded=__atomic_load_n(&inject_loaded,__ATOMIC_ACQUIRE);
    int fd=original_open(path,O_RDONLY|O_CLOEXEC);
    if (fd<0) {
        inject_rejected=0;
        if (loaded) { struct inject old=inject_swap(NULL); munmap(old.base,old.size);
                      fprintf(stderr,"[sp16-ui-stub] input injection: %s removed\n",path); }
        return;
    }
    off_t end=lseek(fd,0,SEEK_END);
    if (end<44 || (size_t)end==loaded || (size_t)end==inject_rejected) { original_close(fd); return; }
    if (end>(off_t)INJECT_MAX_BYTES) {
        inject_rejected=(size_t)end; original_close(fd);
        fprintf(stderr,"[sp16-ui-stub] input injection: %s is larger than %u MB\n",path,INJECT_MAX_BYTES>>20); return; }
    uint8_t *copy=mmap(NULL,(size_t)end,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if (copy==(uint8_t *)MAP_FAILED) { original_close(fd); return; }
    size_t got=0;
    if (lseek(fd,0,SEEK_SET)==0) while (got<(size_t)end) {
        ssize_t n=original_read(fd,copy+got,(size_t)end-got); if (n<=0) break; got+=(size_t)n; }
    off_t again=lseek(fd,0,SEEK_END); original_close(fd);
    if (got!=(size_t)end || again!=end) { munmap(copy,(size_t)end); return; } /* changing while read: look again at the next poll */
    struct inject next={ .base=copy, .size=(size_t)end };
    if (!inject_parse(&next,copy,(size_t)end)) {
        munmap(copy,(size_t)end); inject_rejected=(size_t)end;
        fprintf(stderr,"[sp16-ui-stub] input injection: %s is not a usable WAV (16/24/32-bit PCM or float, mono or stereo)\n",path);
        return; }
    inject_rejected=0;
    struct inject old=inject_swap(&next);
    if (old.base) munmap(old.base,old.size);
    fprintf(stderr,"[sp16-ui-stub] input injection: %s channels=%u bits=%u rate=%.0f frames=%lu\n",path,next.channels,next.bits,
            next.rate,(unsigned long)next.frames);
}
/* Called from pace() on every PCM thread: at most one thread at a time looks at the file, and a thread that finds another
 * one at it simply goes on. */
static void inject_refresh(uint64_t now) {
    uint64_t poll=__atomic_load_n(&inject_poll_ns,__ATOMIC_RELAXED);
    if (!poll) { /* SP16_INPUT_WAV_POLL_MS (tests): how often the file is looked at, default 1000 */
        unsigned ms=1000; const char *e=getenv("SP16_INPUT_WAV_POLL_MS"); if (e) sscanf(e,"%u",&ms);
        poll=(uint64_t)(ms?ms:1)*1000000ULL; __atomic_store_n(&inject_poll_ns,poll,__ATOMIC_RELAXED);
    }
    uint64_t last=__atomic_load_n(&inject_checked_ns,__ATOMIC_RELAXED);
    if (last && (now<last || now-last<poll)) return;
    if (!original_open || !original_read || __atomic_exchange_n(&inject_refreshing,1,__ATOMIC_ACQUIRE)) return;
    __atomic_store_n(&inject_checked_ns,now,__ATOMIC_RELAXED);
    inject_reload();
    __atomic_store_n(&inject_refreshing,0,__ATOMIC_RELEASE);
}

/* Input limiter (GPIO5_IO06, the AUDIO TEST LIMIT button). Level above the threshold is soft-saturated towards full
 * scale. The threshold and curve are assumptions (grade U: the unit's limiter was not measured); high = on (grade I,
 * from the line name LMT_xOFF/ON). Below the threshold the signal passes unchanged. */
#define LIMITER_THRESHOLD 0.5f
static int limiter_on(void) {
    return __atomic_load_n(&gpio_written[4*32+6],__ATOMIC_ACQUIRE) && __atomic_load_n(&gpio_level[4*32+6],__ATOMIC_ACQUIRE)==1;
}
static float limit_soft(float x) {
    float a=x<0?-x:x;
    if (a<=LIMITER_THRESHOLD) return x;
    float over=(a-LIMITER_THRESHOLD)/(1-LIMITER_THRESHOLD), y=LIMITER_THRESHOLD+(1-LIMITER_THRESHOLD)*over/(1+over);
    return x<0?-y:y;
}
static void meter_report(uint64_t now,int force) {
    int lim=limiter_on();
    if (!force && lim==meter_limiter_logged && now-meter_report_ns<2000000000ULL) return;
    if (!force && lim==meter_limiter_logged && meter_in_peak<=0 && meter_out_peak<=0) { meter_report_ns=now; return; }
    struct analog a=analog_now;
    static const char *const names[]={"off","tone","wav"};
    float in=meter_in_peak, out=meter_out_peak;
    meter_report_ns=now; meter_limiter_logged=lim;
    fprintf(stderr,"[sp16-ui-stub] analog levels limiter=%d in_peak=%.4f in_dbu_peak=%.1f out_peak=%.4f out_dbu_peak=%.1f limited=%lu "
            "main_out_gain=%.4f input_gain=%.4f source=%s\n",lim,(double)in,(double)to_dbu(in),(double)out,(double)to_dbu(out),
            meter_limited,(double)a.main_gain,(double)a.input_gain,names[meter_source]);
    meter_in_peak=meter_out_peak=0; meter_limited=0;
}
static void input_fill_locked(struct pcm_state *s,void *buffer,snd_pcm_sframes_t frames);
static void input_fill(struct pcm_state *s,void *buffer,snd_pcm_sframes_t frames) {
    inject_lock();                       /* a refresh cannot swap or unmap the source while this call reads it */
    input_fill_locked(s,buffer,frames);
    inject_unlock();
}
static void input_fill_locked(struct pcm_state *s,void *buffer,snd_pcm_sframes_t frames) {
    struct analog a=analog_now;
    unsigned w=bm_width(s->format);
    if (!w || !s->channels || frames<=0 || !buffer) return;
    struct inject *wav=&inject_now;
    int use_wav=wav->data!=NULL, limiter=limiter_on();
    meter_source=use_wav?2:a.tone_amp>0?1:0;
    if (!use_wav && a.tone_amp<=0) {
        /* Research switch (file `capture-stale` in the developer share): reproduce what a silent capture did before the
         * buffer was written. "untouched" leaves the app's buffer as it is, "pattern" fills it with 0x5a bytes. */
        static int stale=-1;
        if (stale<0) {
            char mode[16]={0}; int fd=original_open?original_open("/opt/sp16/dev-share/capture-stale",O_RDONLY|O_CLOEXEC):-1;
            if (fd>=0) { ssize_t n=original_read?original_read(fd,mode,sizeof mode-1):0; (void)n; original_close(fd); }
            stale=!strncmp(mode,"untouched",9)?1:!strncmp(mode,"pattern",7)?2:0;
        }
        if (stale==1) return;
        memset(buffer,stale==2?0x5a:0,(size_t)frames*w*s->channels);
        return;
    }
    float rate=(float)(s->rate?s->rate:44100), step=2*BM_PI*a.tone_hz/rate, amp=a.tone_amp*a.input_gain;
    uint8_t *p=buffer; unsigned bpf=w*s->channels;
    double advance=use_wav?wav->rate/(double)rate:0;
    for (snd_pcm_sframes_t i=0;i<frames;i++,p+=bpf) {
        float y[2];
        if (use_wav) {
            uint64_t f0=(uint64_t)wav->pos, f1=f0+1<wav->frames?f0+1:0; float t=(float)(wav->pos-(double)f0);
            for (unsigned c=0;c<2;c++) y[c]=a.input_gain*(inject_sample(wav,f0,c)*(1-t)+inject_sample(wav,f1,c)*t);
            wav->pos+=advance; while (wav->pos>=(double)wav->frames) wav->pos-=(double)wav->frames; /* a short file at a high rate wraps more than once */
        } else {
            y[0]=y[1]=amp*bm_sin(s->phase);
            s->phase+=step; if (s->phase>BM_PI) s->phase-=2*BM_PI;
        }
        for (unsigned c=0;c<2;c++) {
            float m=y[c]<0?-y[c]:y[c]; if (m>meter_in_peak) meter_in_peak=m;
            if (limiter) { float l=limit_soft(y[c]); if (l!=y[c]) meter_limited++; y[c]=l; }
        }
        for (unsigned c=0;c<s->channels;c++) bm_put(p+c*w,s->format,y[c<2?c:1]);
    }
}

static const char *const io_names[4]={"writei","writen","readi","readn"};
static void pace(snd_pcm_t *handle,snd_pcm_sframes_t frames,unsigned method) {
    struct pcm_state *s=pcm(handle);
    if (!s) return;
    if (!(s->io_logged & (1u<<method))) {
        s->io_logged |= 1u<<method;
        fprintf(stderr,"[sp16-ui-stub] PCM %s io=%s first_call frames=%ld stream=%s\n",
                s->name,io_names[method],(long)frames,s->stream?"capture":"playback");
    }
    if (frames<=0) return;
    uint64_t step=(uint64_t)frames*1000000000ULL/s->rate, now=mono_ns();
    analog_refresh(now);
    inject_refresh(now);
    meter_report(now,0);
    dac_flush(now);
    if (!s->deadline_ns || now>s->deadline_ns+PCM_MAX_LAG_NS) s->deadline_ns=now;
    s->deadline_ns+=step;
    uint64_t slept=0;
    if (s->deadline_ns>now) {
        struct timespec until={ .tv_sec=(time_t)(s->deadline_ns/1000000000ULL), .tv_nsec=(long)(s->deadline_ns%1000000000ULL) };
        while (clock_nanosleep(CLOCK_MONOTONIC,TIMER_ABSTIME,&until,NULL)==EINTR) {}
        slept=mono_ns()-now;
    }
    uint64_t after=now+slept;
    s->calls++; s->frames+=(uint64_t)frames; s->slept_ns+=slept; s->tid=(int)syscall(SYS_gettid);
    if (!s->reported_ns) s->reported_ns=after;
    if (after-s->reported_ns>=PCM_STATS_NS) {
        double secs=(double)(after-s->reported_ns)/1e9;
        double rate=(double)s->frames/secs;
        fprintf(stderr,"[sp16-ui-stub] audio %s %s tid=%d rate=%u frames/s=%.0f speed=%.2fx calls/s=%.1f sleep_share=%.2f\n",
                s->name,s->stream?"capture":"playback",s->tid,s->rate,rate,rate/(double)s->rate,(double)s->calls/secs,
                (double)s->slept_ns/1e9/secs);
        if (s->ax) ax_report(s);
        s->calls=s->frames=s->slept_ns=0; s->reported_ns=after;
    }
}
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *handle,const void *buffer,snd_pcm_uframes_t frames) {
    snd_pcm_sframes_t (*real)(snd_pcm_t *,const void *,snd_pcm_uframes_t)=dlsym(RTLD_NEXT,"snd_pcm_writei");
    snd_pcm_sframes_t result=real(handle,buffer,frames);
    struct pcm_state *tapped=pcm(handle);
    if (tapped && tapped->tap) pcm_tap_feed(tapped,buffer,result);
    if (tapped && tapped->ax) ax_feed(tapped,buffer,result);
    if (tapped && is_board(tapped,"hw:ak4452audio,1",0)) loop_send(tapped,buffer,result);
    pace(handle,result,0); return result;
}
snd_pcm_sframes_t snd_pcm_writen(snd_pcm_t *handle,void **buffers,snd_pcm_uframes_t frames) {
    snd_pcm_sframes_t (*real)(snd_pcm_t *,void **,snd_pcm_uframes_t)=dlsym(RTLD_NEXT,"snd_pcm_writen");
    snd_pcm_sframes_t result=real(handle,buffers,frames); pace(handle,result,1); return result;
}
snd_pcm_sframes_t snd_pcm_readi(snd_pcm_t *handle,void *buffer,snd_pcm_uframes_t frames) {
    snd_pcm_sframes_t (*real)(snd_pcm_t *,void *,snd_pcm_uframes_t)=dlsym(RTLD_NEXT,"snd_pcm_readi");
    snd_pcm_sframes_t result=real(handle,buffer,frames);
    struct pcm_state *s=pcm(handle);
    if (s && is_board(s,"hw:ak4452audio,1",1)) loop_return(s,buffer,result);
    else if (s && is_board(s,"hw:ak4452audio,0",1)) input_fill(s,buffer,result);
    pace(handle,result,2); return result;
}
snd_pcm_sframes_t snd_pcm_readn(snd_pcm_t *handle,void **buffers,snd_pcm_uframes_t frames) {
    snd_pcm_sframes_t (*real)(snd_pcm_t *,void **,snd_pcm_uframes_t)=dlsym(RTLD_NEXT,"snd_pcm_readn");
    snd_pcm_sframes_t result=real(handle,buffers,frames); pace(handle,result,3); return result;
}

/* The UI harness has no init/service stack. Refuse guest shell helpers,
 * including the application's crash-handler format/mount/recovery commands.
 */
/* Log the refused path and at most 8 argv entries (each cut to 40 bytes, the line to ~200 bytes). Logging only. */
static void log_blocked_exec(const char *path,char *const argv[]) {
    char args[192]="";
    size_t used=0;
    unsigned count=0;
    for (unsigned i=0; argv && argv[i] && i<8; i++, count++) {
        int n=snprintf(args+used,sizeof args-used,"%s%.40s",i?" ":"",argv[i]);
        if (n<0 || (size_t)n>=sizeof args-used) { used=sizeof args-1; break; }
        used+=(size_t)n;
    }
    while (argv && argv[count]) count++;
    fprintf(stderr,"[sp16-ui-stub] blocked guest helper exec: %.120s argc=%u argv=[%s]\n",path,count,args);
}

/* Exec policy. Exactly one command is allowed: sync, with an optional -n, either executed directly
 * (path /bin/sync or /usr/bin/sync, argv[0] "sync" or the path) or as the whole command string of
 * `sh -c` (argv = {sh, -c, "sync"} and the same spellings with -n or a full path). Anything else,
 * including format, mount, mkfs, reboot, crash-handler and recovery scripts, stays refused with ENOTSUP.
 * The OEM shell runner forks before exec (it waits on the child), so an allowed sync runs only in a
 * forked child: it calls sync(2) and _exit(0), which the parent observes as a successful child. In the
 * application process itself the call is refused, because exiting there would end the app. */
static int is_sync_path(const char *s) {
    return s && (!strcmp(s,"sync") || !strcmp(s,"/bin/sync") || !strcmp(s,"/usr/bin/sync"));
}
static int sync_allowed(const char *path,char *const argv[]) {
    if (!path || !argv || !argv[0]) return 0;
    if (is_sync_path(path) && (!strcmp(argv[0],"sync") || !strcmp(argv[0],path))) {
        return !argv[1] || (!strcmp(argv[1],"-n") && !argv[2]);
    }
    if ((!strcmp(path,"/bin/sh") || !strcmp(path,"sh")) && argv[1] && argv[2] && !argv[3] && !strcmp(argv[1],"-c")) {
        static const char *const forms[] = {"sync","sync -n","/bin/sync","/bin/sync -n","/usr/bin/sync","/usr/bin/sync -n"};
        for (unsigned i=0; i<sizeof forms/sizeof forms[0]; i++) if (!strcmp(argv[2],forms[i])) return 1;
    }
    return 0;
}
static int exec_policy(const char *path,char *const argv[],int complete) {
    if (complete && getpid()!=owner_pid && sync_allowed(path,argv)) {
        fprintf(stderr,"[sp16-ui-stub] allowed sync no-op exec: %.120s\n",path);
        sync();
        _exit(0);
    }
    log_blocked_exec(path,argv);
    errno=ENOTSUP; return -1;
}
int execl(const char *path,const char *arg,...) {
    /* Gather the NULL-terminated variadic list. complete=0 when it exceeds the 8 slots, which the policy refuses. */
    char *argv[9]; unsigned n=0;
    va_list args; va_start(args,arg);
    const char *a=arg;
    for (; a && n<8; a=va_arg(args,const char *)) argv[n++]=(char *)a;
    va_end(args);
    argv[n]=NULL;
    return exec_policy(path,argv,a==NULL);
}
int execv(const char *path,char *const argv[]) {
    return exec_policy(path,argv,1);
}
int execve(const char *path,char *const argv[],char *const envp[]) {
    (void)envp; return execv(path,argv);
}

/* Media nodes are only observable here through poll(); epoll/ppoll are not interposed. */
int poll(struct pollfd *fds,nfds_t count,int timeout) {
    int (*real)(struct pollfd *,nfds_t,int)=dlsym(RTLD_NEXT,"poll");
    for (nfds_t i=0; fds && i<count; i++) {
        struct state *s=device(fds[i].fd);
        if (s && s->kind==MEDIA) media_note(s,"poll");
    }
    return real(fds,count,timeout);
}

int socket(int domain,int type,int protocol) {
    int (*real)(int,int,int)=dlsym(RTLD_NEXT,"socket");
    if (domain==AF_PACKET && (type&0xf)==SOCK_DGRAM && protocol==1544 && !network_arp_enabled()) {
        int fd=idle_socket(PACKET);
        fprintf(stderr,"[sp16-ui-stub] ARP socket -> isolated idle fd=%d\n",fd);
        return fd;
    }
    return real(domain,type,protocol);
}
int bind(int fd,const struct sockaddr *address,socklen_t length) {
    int (*real)(int,const struct sockaddr *,socklen_t)=dlsym(RTLD_NEXT,"bind");
    struct state *s=device(fd);
    if (s && s->kind==PACKET && address && address->sa_family==AF_PACKET) return 0;
    return real(fd,address,length);
}
ssize_t sendto(int fd,const void *buffer,size_t length,int flags,const struct sockaddr *address,socklen_t address_length) {
    ssize_t (*real)(int,const void *,size_t,int,const struct sockaddr *,socklen_t)=dlsym(RTLD_NEXT,"sendto");
    struct state *s=device(fd);
    if (s && s->kind==PACKET) return (ssize_t)length; /* discarded; never sent on a network */
    return real(fd,buffer,length,flags,address,address_length);
}
