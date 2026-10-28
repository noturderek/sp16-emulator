/* The snddinmidi ALSA rawmidi card is a full-duplex 31,250 baud UART. Synthetic PTY bytes only. */
#define _GNU_SOURCE
#include <assert.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
typedef struct _snd_rawmidi snd_rawmidi_t;
int snd_rawmidi_open(snd_rawmidi_t **,snd_rawmidi_t **,const char *,int);
int snd_rawmidi_close(snd_rawmidi_t *);
int snd_rawmidi_poll_descriptors(snd_rawmidi_t *,struct pollfd *,unsigned int);
ssize_t snd_rawmidi_read(snd_rawmidi_t *,void *,size_t);
ssize_t snd_rawmidi_write(snd_rawmidi_t *,const void *,size_t);
static uint64_t ns(void) {
    struct timespec t;
    assert(clock_gettime(CLOCK_MONOTONIC,&t)==0);
    return (uint64_t)t.tv_sec*1000000000ULL+t.tv_nsec;
}
int main(void) {
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
    const uint8_t note[]={0x90,60,100};
    uint64_t begin=ns();
    assert(snd_rawmidi_write(out,note,sizeof(note))==3);
    uint64_t elapsed=ns()-begin;
    assert(elapsed>=900000ULL); /* 3 UART bytes at 320 us each, with scheduling tolerance */
    uint8_t received[3];
    assert(read(master,received,sizeof(received))==3);
    assert(!memcmp(received,note,3));
    const uint8_t inbound[]={0x80,60,0};
    assert(write(master,inbound,sizeof(inbound))==3);
    assert(snd_rawmidi_read(in,received,sizeof(received))==3);
    assert(!memcmp(received,inbound,3));
    assert(snd_rawmidi_close(in)==0 && snd_rawmidi_close(out)==0);
    assert(close(control)==0 && close(master)==0);
    return 0;
}
