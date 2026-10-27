/* Knob-law contract: MAIN OUT scales the master copy the host receives and nothing else, and the low-pass of the
 * Send & Return loop follows CUTOFF and RESONANCE. Expected gains are computed by analog_laws.py and passed on the
 * command line: argv[1] the MAIN OUT gain at slot 500, argv[2..3] the loop gain of a 1 kHz tone at CUTOFF 500 with
 * RESONANCE 0 and 500. Build twice, as sp16_board_contract.c does; run with the ARM UI preload, SP16_ANALOG=<file>
 * and SP16_PCM_PORT=<file>.
 */
#define _GNU_SOURCE
#include <assert.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
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
    (void)stream; (void)mode; assert(!strcmp(name,"null")); *out=(snd_pcm_t *)&sentinels[opened++ % 8]; return 0; }
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
static snd_pcm_t *pcm(const char *name,int stream,int format,unsigned channels) {
    snd_pcm_t *h; unsigned rate=44100; int dir=0;
    snd_pcm_hw_params_t *p=(snd_pcm_hw_params_t *)params;
    assert(snd_pcm_open(&h,name,stream,0)==0);
    assert(snd_pcm_hw_params_set_format(h,p,format)==0);
    assert(snd_pcm_hw_params_set_channels(h,p,channels)==0);
    assert(snd_pcm_hw_params_set_rate_near(h,p,&rate,&dir)==0);
    assert(snd_pcm_hw_params(h,p)==0);
    return h;
}
/* analog state: main_out, input_level, lp_cutoff, lp_resonance (0xffff = never moved). */
static void analog(const char *path,uint16_t main_out,uint16_t cutoff,uint16_t resonance) {
    static uint32_t seq=2;
    uint8_t b[64]={0}; uint16_t v[6]={main_out,0xffff,cutoff,resonance,0,1000};
    memcpy(b,"SP16ANA1",8); seq+=2; memcpy(b+8,&seq,4); memcpy(b+12,v,sizeof v);
    int fd=open(path,O_RDWR|O_CREAT,0600); assert(fd>=0);
    assert(pwrite(fd,b,64,0)==64); close(fd);
}
static void settle(snd_pcm_t *h,const void *silence) { /* the shim re-reads the state every 20 ms, from the audio clock */
    struct timespec t={ .tv_nsec=30000000 }; nanosleep(&t,NULL);
    assert(snd_pcm_writei(h,silence,N)==N);
}
/* Steady-state peak of the loop return for a 1 kHz tone of amplitude 0.05 (S24_LE, the codec's format). */
static float loop_peak(const char *state,uint16_t cutoff,uint16_t resonance,snd_pcm_t *send,snd_pcm_t *ret) {
    int32_t out[N*2], back[N*2], silence[N*2]={0}; float peak=0, phase=0;
    analog(state,0xffff,cutoff,resonance);
    settle(send,silence);
    for (int k=0;k<300;k++) {
        for (int i=0;i<N;i++) {
            out[2*i]=out[2*i+1]=(int32_t)(0.05f*8388607.0f*(float)__builtin_sin(phase)); phase+=6.2831853f*1000/44100;
        }
        assert(snd_pcm_writei(send,out,N)==N);
        assert(snd_pcm_readi(ret,back,N)==N);
        if (k>150) for (int i=0;i<N*2;i++) {
            int32_t v=(int32_t)((uint32_t)back[i]<<8)>>8; float m=(float)v/8388608.0f; if (m<0) m=-m; if (m>peak) peak=m; }
    }
    return peak;
}
static uint32_t rd32(const uint8_t *p) { uint32_t v; memcpy(&v,p,4); return v; }
int main(int argc,char **argv) {
    assert(argc==4);
    double main_gain=atof(argv[1]), g0=atof(argv[2]), g500=atof(argv[3]);
    const char *state=getenv("SP16_ANALOG");
    assert(state);
    /* 1. MAIN OUT: the master copy that reaches the host is scaled, the app's own buffer is not. */
    snd_pcm_t *m=pcm("hw:ak4458audio,0",0,2,16);
    int16_t buf[N*16], keep[N*16]; for (int i=0;i<N*16;i++) keep[i]=buf[i]=16384;
    analog(state,0xffff,0xffff,0xffff);
    settle(m,buf);
    for (int k=0;k<8;k++) { assert(snd_pcm_writei(m,buf,N)==N); }
    analog(state,500,0xffff,0xffff);
    settle(m,buf);
    for (int k=0;k<8;k++) { assert(snd_pcm_writei(m,buf,N)==N); }
    assert(!memcmp(buf,keep,sizeof buf));
    struct timespec nap={ .tv_nsec=150000000 }; nanosleep(&nap,NULL);
    int fd=open(getenv("SP16_PCM_PORT"),O_RDONLY); assert(fd>=0);
    static uint8_t data[2*1024*1024]; ssize_t size=read(fd,data,sizeof data); assert(size>0);
    int first=-1, last=-1, pad=-1, right=-1; size_t at=0;
    while (at+48<=(size_t)size) {
        uint32_t payload=rd32(data+at+44);
        if (!memcmp(data+at,"SPCM",4) && data[at+4]==2 && at+48+payload<=(size_t)size && payload>=8) {
            int16_t v; memcpy(&v,data+at+48,2);
            if (first<0) first=v;
            memcpy(&v,data+at+48+payload-8,2); /* the newest frame: a packet may coalesce older periods */
            last=v;
            memcpy(&v,data+at+48+payload-6,2); pad=v;   /* channel 13: padding, not behind the knob */
            memcpy(&v,data+at+48+payload-4,2); right=v; /* channel 14: the other channel of OUT1-2 */
        }
        at+=48+rd32(data+at+44);
    }
    assert(first==16384);                                   /* untouched control: unity */
    assert(last>=(int)(16384*main_gain)-2 && last<=(int)(16384*main_gain)+2);
    assert(right>=(int)(16384*main_gain)-2 && right<=(int)(16384*main_gain)+2);
    assert(pad==16384);                                     /* only OUT1-2 (channels 12 and 14) follows MAIN OUT */
    assert(snd_pcm_close(m)==0);
    /* 2. The loop low-pass: a 1 kHz tone at CUTOFF 500 comes back at the law's gain, and RESONANCE 500 lifts it. */
    snd_pcm_t *send=pcm("hw:ak4452audio,1",0,6,2), *ret=pcm("hw:ak4452audio,1",1,6,2);
    float p0=loop_peak(state,500,0,send,ret)/0.05f, p500=loop_peak(state,500,500,send,ret)/0.05f;
    assert(p0>g0*0.92 && p0<g0*1.08);
    assert(p500>g500*0.92 && p500<g500*1.08);
    assert(p500>p0*1.5);
    assert(snd_pcm_close(send)==0 && snd_pcm_close(ret)==0);
    return 0;
}
#endif
