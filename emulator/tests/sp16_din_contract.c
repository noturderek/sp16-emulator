/* The snddinmidi ALSA rawmidi card is a full-duplex 31,250 baud UART. Synthetic PTY bytes only. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
typedef struct _snd_rawmidi snd_rawmidi_t;
int snd_rawmidi_open(snd_rawmidi_t **,snd_rawmidi_t **,const char *,int);
int snd_rawmidi_close(snd_rawmidi_t *);
int snd_rawmidi_nonblock(snd_rawmidi_t *,int);
int snd_rawmidi_poll_descriptors(snd_rawmidi_t *,struct pollfd *,unsigned int);
ssize_t snd_rawmidi_read(snd_rawmidi_t *,void *,size_t);
ssize_t snd_rawmidi_write(snd_rawmidi_t *,const void *,size_t);
static uint64_t ns(void) {
    struct timespec t;
    assert(clock_gettime(CLOCK_MONOTONIC,&t)==0);
    return (uint64_t)t.tv_sec*1000000000ULL+t.tv_nsec;
}

/* A nonblocking write to a port that cannot take the byte returns EAGAIN without spending the 320 us wire slot: the
 * shim used to sleep the slot first and only then discover that nothing could be written. A rawmidi port is opened
 * once per process, so this runs in a child before the parent opens the PTY port. */
static void nonblock_full_port_case(void) {
    pid_t child=fork();
    assert(child>=0);
    if (!child) {
        const char *full="/tmp/sp16-din-full.fifo";
        unlink(full);
        assert(mkfifo(full,0600)==0);
        int filler=open(full,O_RDWR|O_NONBLOCK);
        assert(filler>=0);
        uint8_t fill[4096]={0};
        for (;;) {
            ssize_t n=write(filler,fill,sizeof fill);
            if (n<0) { assert(errno==EAGAIN); break; }
            assert(n>0);
        }
        assert(setenv("SP16_DIN_PORT",full,1)==0);
        snd_rawmidi_t *out=NULL;
        assert(snd_rawmidi_open(NULL,&out,"hw:snddinmidi,0",1)==0 && out);
        assert(snd_rawmidi_nonblock(out,1)==0);
        uint8_t byte=0x90;
        uint64_t begin=ns();
        for (int i=0;i<20;i++) assert(snd_rawmidi_write(out,&byte,1)==-EAGAIN);
        /* 20 rejected bytes must not cost 20 wire slots (6.4 ms); a handful of syscalls is well under 2 ms. */
        assert(ns()-begin<2000000ULL);
        assert(snd_rawmidi_close(out)==0);
        assert(close(filler)==0);
        unlink(full);
        _exit(0);
    }
    int status;
    assert(waitpid(child,&status,0)==child && WIFEXITED(status) && WEXITSTATUS(status)==0);
}

int main(void) {
    nonblock_full_port_case();
    int master=posix_openpt(O_RDWR|O_NOCTTY);
    assert(master>=0 && grantpt(master)==0 && unlockpt(master)==0);
    char *slave=ptsname(master);
    assert(slave && setenv("SP16_DIN_PORT",slave,1)==0);
    int control=open(slave,O_RDWR|O_NOCTTY);
    assert(control>=0);
    struct termios terminal;
    assert(tcgetattr(control,&terminal)==0);
    cfmakeraw(&terminal);
    assert(tcsetattr(control,TCSANOW,&terminal)==0);
    snd_rawmidi_t *in=NULL,*out=NULL;
    assert(snd_rawmidi_open(&in,&out,"hw:snddinmidi,0",0)==0);
    assert(in && out);
    struct pollfd pfd;
    assert(snd_rawmidi_poll_descriptors(in,&pfd,1)==1);
    assert(pfd.events==POLLIN);
    assert(snd_rawmidi_poll_descriptors(out,&pfd,1)==1);
    assert(pfd.events==POLLOUT);
    uint8_t received[3];
    assert(snd_rawmidi_nonblock(in,1)==0);
    assert(snd_rawmidi_read(in,received,1)==-EAGAIN);
    const uint8_t note[]={0x90,60,100};
    uint64_t begin=ns();
    assert(snd_rawmidi_write(out,note,sizeof(note))==3);
    uint64_t elapsed=ns()-begin;
    assert(elapsed>=900000ULL); /* 3 UART bytes at 320 us each, with scheduling tolerance */
    assert(read(master,received,sizeof(received))==3);
    assert(!memcmp(received,note,3));
    assert(snd_rawmidi_nonblock(in,0)==0);
    const uint8_t inbound[]={0x80,60,0};
    assert(write(master,inbound,sizeof(inbound))==3);
    assert(snd_rawmidi_read(in,received,sizeof(received))==3);
    assert(!memcmp(received,inbound,3));
    int gpio=open("/dev/gpiodrv",O_RDWR);
    assert(gpio>=0 && lseek(gpio,5,SEEK_SET)==5);
    uint8_t thru=1;
    assert(write(gpio,&thru,1)==1);
    assert(write(master,inbound,sizeof(inbound))==3);
    begin=ns();
    assert(snd_rawmidi_read(in,received,sizeof(received))==3);
    assert(ns()-begin>=900000ULL); /* THRU bytes use the same 320 us wire slots. */
    assert(read(master,received,sizeof(received))==3 && !memcmp(received,inbound,3));
    assert(close(gpio)==0);
    assert(snd_rawmidi_close(in)==0 && snd_rawmidi_close(out)==0);
    assert(close(control)==0 && close(master)==0);
    return 0;
}
