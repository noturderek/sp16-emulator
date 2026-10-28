/* Analog board model contract: the filter send/return loop, the DAC7811 high-pass word, the host analog state
 * (low-pass, input tone) and the DIN MIDI loopback cable.
 * Build twice, as sp16_hwparams_contract.c does:
 * -DSP16_FAKE_ALSA -shared -fPIC -> libsp16-fake-alsa.so (fake libasound whose reads leave the buffer alone)
 * normal -> test, linked to that library; run with the ARM UI preload, SP16_ANALOG=<file> and SP16_MIDI_LOOPBACK=1 and SP16_FAKE_ETH0=1.
 */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <net/if_arp.h>
#include <netinet/in.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
typedef struct _snd_pcm snd_pcm_t;
typedef struct _snd_pcm_hw_params snd_pcm_hw_params_t;
typedef unsigned long snd_pcm_uframes_t;
typedef long snd_pcm_sframes_t;

#ifdef SP16_FAKE_ALSA
static uintptr_t sentinels[8];
static unsigned opened;
static int stored_format;
static unsigned stored_channels, stored_rate;
int snd_pcm_open(snd_pcm_t **out,const char *name,int stream,int mode) {
    (void)stream; (void)mode;
    assert(!strcmp(name,"null"));
    *out=(snd_pcm_t *)&sentinels[opened++ % 8]; return 0;
}
int snd_pcm_close(snd_pcm_t *p) { (void)p; return 0; }
int snd_pcm_hw_params_set_format(snd_pcm_t *p,snd_pcm_hw_params_t *q,int f) { (void)p; (void)q; stored_format=f; return 0; }
int snd_pcm_hw_params_set_channels(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned c) { (void)p; (void)q; stored_channels=c; return 0; }
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned *r,int *d) {
    (void)p; (void)q; (void)d; stored_rate=*r; return 0; }
int snd_pcm_hw_params(snd_pcm_t *p,snd_pcm_hw_params_t *q) { (void)p; (void)q; return 0; }
int snd_pcm_hw_params_get_format(const snd_pcm_hw_params_t *q,int *f) { (void)q; *f=stored_format; return 0; }
int snd_pcm_hw_params_get_channels(const snd_pcm_hw_params_t *q,unsigned *c) { (void)q; *c=stored_channels; return 0; }
int snd_pcm_hw_params_get_rate(const snd_pcm_hw_params_t *q,unsigned *r,int *d) { (void)q; *r=stored_rate; *d=0; return 0; }
#define IO(name,type) snd_pcm_sframes_t name(snd_pcm_t *p,type buffer,snd_pcm_uframes_t frames) { (void)p; (void)buffer; return frames; }
IO(snd_pcm_readi,void *)
IO(snd_pcm_writei,const void *)
#else
int snd_pcm_open(snd_pcm_t **,const char *,int,int);
int snd_pcm_close(snd_pcm_t *);
int snd_pcm_hw_params_set_format(snd_pcm_t *,snd_pcm_hw_params_t *,int);
int snd_pcm_hw_params_set_channels(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned);
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned *,int *);
int snd_pcm_hw_params(snd_pcm_t *,snd_pcm_hw_params_t *);
snd_pcm_sframes_t snd_pcm_readi(snd_pcm_t *,void *,snd_pcm_uframes_t);
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *,const void *,snd_pcm_uframes_t);

#define N 64
static char params[64];
static snd_pcm_t *pcm(const char *name,int stream,int format) {
    snd_pcm_t *h; unsigned rate=44100; int dir=0;
    snd_pcm_hw_params_t *p=(snd_pcm_hw_params_t *)params;
    assert(snd_pcm_open(&h,name,stream,0)==0);
    assert(snd_pcm_hw_params_set_format(h,p,format)==0);
    assert(snd_pcm_hw_params_set_channels(h,p,2)==0);
    assert(snd_pcm_hw_params_set_rate_near(h,p,&rate,&dir)==0);
    assert(snd_pcm_hw_params(h,p)==0);
    return h;
}
/* 64-byte analog state: magic, sequence, then u16 main_out, input_level, lp_cutoff, lp_resonance, tone, tone_hz. */
static void analog(const char *path,uint16_t lp_cutoff,uint16_t tone) {
    uint8_t b[64]={0}; uint32_t seq=2; uint16_t v[6]={0xffff,0xffff,lp_cutoff,0xffff,tone,1000};
    memcpy(b,"SP16ANA1",8); memcpy(b+8,&seq,4); memcpy(b+12,v,sizeof v);
    int fd=open(path,O_RDWR|O_CREAT,0600); assert(fd>=0);
    assert(pwrite(fd,b,64,0)==64); close(fd);
}
static void settle(snd_pcm_t *send) { /* the shim re-reads the analog state at most every 20 ms, from the audio clock */
    float silence[N*2]={0};
    struct timespec t={ .tv_nsec=30000000 }; nanosleep(&t,NULL);
    assert(snd_pcm_writei(send,silence,N)==N);
}
int main(void) {
    const char *state=getenv("SP16_ANALOG");
    assert(state);
    analog(state,0xffff,0);
    snd_pcm_t *send=pcm("hw:ak4452audio,1",0,14), *ret=pcm("hw:ak4452audio,1",1,14), *in=pcm("hw:ak4452audio,0",1,2);
    settle(send);
    float out[N*2], back[N*2];
    /* 1. Untouched controls and no DAC word: the loop returns the send frames unchanged, one call later. */
    for (int i=0;i<N*2;i++) out[i]=(float)(i%17)/20.0f-0.4f;
    for (int i=0;i<N*2;i++) back[i]=9;
    assert(snd_pcm_writei(send,out,N)==N);
    assert(snd_pcm_readi(ret,back,N)==N);
    for (int i=0;i<N*2;i++) assert(back[i]==0); /* the settle period's silence comes back first */
    assert(snd_pcm_readi(ret,back,N)==N);
    for (int i=0;i<N*2;i++) assert(back[i]==out[i]);
    /* An empty queue returns silence, never stale or uninitialised samples. */
    for (int i=0;i<N*2;i++) back[i]=9;
    assert(snd_pcm_readi(ret,back,N)==N);
    for (int i=0;i<N*2;i++) assert(back[i]==0);
    /* 1b. S24_LE (the format the app negotiates): send and return carry 24-bit samples low in a 32-bit word,
     * round-tripped through the float queue to within one LSB. */
    snd_pcm_t *send24=pcm("hw:ak4452audio,1",0,6), *ret24=pcm("hw:ak4452audio,1",1,6);
    settle(send24);
    int32_t s24[N*2], b24[N*2];
    for (int i=0;i<N*2;i++) s24[i]=(int32_t)((uint32_t)(i*100003u)%16777216u)-8388608;   /* full signed 24-bit range */
    assert(snd_pcm_writei(send24,s24,N)==N);
    assert(snd_pcm_readi(ret24,b24,N)==N);
    for (int i=0;i<N*2;i++) assert(b24[i]==0);                              /* the settle silence comes back first */
    assert(snd_pcm_readi(ret24,b24,N)==N);
    for (int i=0;i<N*2;i++) { int32_t d=s24[i]-b24[i]; assert(d>=-1 && d<=1); }
    for (int i=0;i<N*2;i++) b24[i]=9;
    assert(snd_pcm_readi(ret24,b24,N)==N);
    for (int i=0;i<N*2;i++) assert(b24[i]==0);                              /* empty queue returns silence */
    assert(snd_pcm_close(send24)==0 && snd_pcm_close(ret24)==0);
    /* 2. A DAC7811 word turns the high-pass on: DC through the loop decays to nothing. */
    int dac=open("/dev/dac_spi4.0",O_WRONLY);
    assert(dac>=0);
    uint8_t word[2]={0xff,0x1f};
    assert(write(dac,word,2)==0);
    float dc[N*2];
    for (int i=0;i<N*2;i++) dc[i]=0.5f;
    for (int k=0;k<200;k++) { assert(snd_pcm_writei(send,dc,N)==N); assert(snd_pcm_readi(ret,back,N)==N); }
    assert(back[N*2-1]<0.02f && back[N*2-1]>-0.02f);
    /* 3. The host low-pass at its lowest cutoff (20 Hz) and the HPF at its lowest code: a 5 kHz-ish square is
     * attenuated by more than 20 dB. */
    word[0]=0x00; word[1]=0x10;
    assert(write(dac,word,2)==0);
    analog(state,0,0);
    settle(send);
    for (int k=0;k<4;k++) assert(snd_pcm_readi(ret,back,N)==N); /* drain */
    float square[N*2], peak=0;
    for (int i=0;i<N*2;i++) square[i]=(i/8)%2?0.8f:-0.8f;
    for (int k=0;k<100;k++) {
        assert(snd_pcm_writei(send,square,N)==N); assert(snd_pcm_readi(ret,back,N)==N);
        if (k>50) for (int i=0;i<N*2;i++) { float m=back[i]<0?-back[i]:back[i]; if (m>peak) peak=m; }
    }
    assert(peak<0.08f);
    /* 4. The input test tone at 0 dBFS reaches the input capture (S16 here), on both channels. */
    analog(state,0xffff,1000);
    settle(send);
    int16_t tone[N*2]; int top=0;
    for (int k=0;k<4;k++) {
        memset(tone,0,sizeof tone);
        assert(snd_pcm_readi(in,tone,N)==N);
        for (int i=0;i<N*2;i++) { int m=tone[i]<0?-tone[i]:tone[i]; if (m>top) top=m; }
        for (int i=0;i<N;i++) assert(tone[2*i]==tone[2*i+1]);
    }
    assert(top>30000);
    /* 4b. The same tone reaches an S24_LE input capture, sign-extended 24-bit low in a 32-bit word. */
    snd_pcm_t *in24=pcm("hw:ak4452audio,0",1,6);
    int32_t tone24[N*2]; int top24=0;
    for (int k=0;k<4;k++) {
        memset(tone24,0,sizeof tone24);
        assert(snd_pcm_readi(in24,tone24,N)==N);
        for (int i=0;i<N*2;i++) { int m=tone24[i]<0?-tone24[i]:tone24[i]; if (m>top24) top24=m; }
        for (int i=0;i<N;i++) assert(tone24[2*i]==tone24[2*i+1]);
    }
    assert(top24>7000000);
    assert(snd_pcm_close(in24)==0);
    /* Tone off: the input capture is written with silence; the fake library (like the null plugin) leaves the
     * buffer as given, so it starts as the last tone samples would leave it, non-zero. */
    analog(state,0xffff,0);
    settle(send);
    memset(tone,0x5a,sizeof tone);
    assert(snd_pcm_readi(in,tone,N)==N);
    for (int i=0;i<N*2;i++) assert(tone[i]==0);
    assert(close(dac)==0 && snd_pcm_close(send)==0 && snd_pcm_close(ret)==0 && snd_pcm_close(in)==0);
    /* 5. DIN MIDI loopback: two opens of the UART share one cable; termios calls are accepted. */
    int a=open("/dev/ttymxc1",O_RDWR), b=open("/dev/ttymxc1",O_RDWR);
    assert(a>=0 && b>=0);
    uint8_t termios[64];
    assert(ioctl(a,0x5401,termios)==0 && ioctl(a,0x5402,termios)==0);
    assert(write(a,"\xf0\x7e\x01\xf7",4)==4);
    struct pollfd p={ .fd=b, .events=POLLIN };
    assert(poll(&p,1,1000)==1);
    uint8_t got[4];
    for (int i=0;i<4;i++) assert(read(b,got+i,1)==1);
    assert(!memcmp(got,"\xf0\x7e\x01\xf7",4));
    assert(close(a)==0 && close(b)==0);
    int c=open("/dev/ttymxc1",O_RDWR);
    p.fd=c;
    assert(c>=0 && poll(&p,1,0)==0); /* nothing left on the cable */
    assert(close(c)==0);
    /* 6. /dev/gpiodrv: one byte per line at (bank - 1) * 32 + number; lines read 1 until written; state is shared
     * by every open. */
    int g=open("/dev/gpiodrv",O_RDWR), h=open("/dev/gpiodrv",O_RDWR);
    assert(g>=0 && h>=0);
    uint8_t level=9;
    assert(lseek(g,3*32+14,SEEK_SET)==3*32+14 && read(g,&level,1)==1 && level==1);
    level=0;
    assert(lseek(g,4*32+5,SEEK_SET)==4*32+5 && write(g,&level,1)==1);
    level=9;
    assert(lseek(h,4*32+5,SEEK_SET)==4*32+5 && read(h,&level,1)==1 && level==0);
    assert(lseek(h,7*32+1,SEEK_SET)==-1);
    assert(close(g)==0 && close(h)==0);
    /* 7. Fake eth0 (SP16_FAKE_ETH0=1): MAC with bytes 3..5 non-zero, 192.0.2.16/24, up; no ethtool data; other
     * interfaces are not faked. */
    int net=socket(AF_INET,SOCK_DGRAM,0);
    assert(net>=0);
    struct ifreq ifr;
    memset(&ifr,0,sizeof ifr); strcpy(ifr.ifr_name,"eth0");
    assert(ioctl(net,0x8927,&ifr)==0 && ifr.ifr_hwaddr.sa_family==ARPHRD_ETHER);
    assert(!memcmp(ifr.ifr_hwaddr.sa_data,"\x02\x00\x5e\x10\x00\x01",6));
    memset(&ifr,0,sizeof ifr); strcpy(ifr.ifr_name,"eth0");
    assert(ioctl(net,0x8915,&ifr)==0 && ifr.ifr_addr.sa_family==AF_INET);
    assert(((struct sockaddr_in *)&ifr.ifr_addr)->sin_addr.s_addr==htonl(0xc0000210u));
    memset(&ifr,0,sizeof ifr); strcpy(ifr.ifr_name,"eth0");
    assert(ioctl(net,0x891b,&ifr)==0);
    assert(((struct sockaddr_in *)&ifr.ifr_addr)->sin_addr.s_addr==htonl(0xffffff00u));
    memset(&ifr,0,sizeof ifr); strcpy(ifr.ifr_name,"eth0");
    assert(ioctl(net,0x8913,&ifr)==0 && ifr.ifr_flags==(IFF_UP|IFF_RUNNING|IFF_BROADCAST));
    memset(&ifr,0,sizeof ifr); strcpy(ifr.ifr_name,"eth0");
    assert(ioctl(net,0x8946,&ifr)==-1 && errno==EOPNOTSUPP);
    memset(&ifr,0,sizeof ifr); strcpy(ifr.ifr_name,"eth1");
    assert(ioctl(net,0x8927,&ifr)==-1);
    assert(close(net)==0);
    return 0;
}
#endif
