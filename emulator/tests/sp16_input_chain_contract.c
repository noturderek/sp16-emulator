/* Input chain contract: INPUT LEVEL gain, the input limiter (GPIO5_IO06), WAV injection and the level metrics line.
 * Build twice, as sp16_board_contract.c does (-DSP16_FAKE_ALSA -shared -fPIC -> libsp16-fake-alsa.so; normal -> test)
 * and run with the ARM UI preload, SP16_ANALOG=<file> and SP16_INPUT_WAV=<file>.
 */
#define _GNU_SOURCE
#include <assert.h>
#include <fcntl.h>
#include <math.h>
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

#define N 441
static char params[64];
static snd_pcm_t *capture(void) {
    snd_pcm_t *h; unsigned rate=44100; int dir=0;
    snd_pcm_hw_params_t *p=(snd_pcm_hw_params_t *)params;
    assert(snd_pcm_open(&h,"hw:ak4452audio,0",1,0)==0);
    assert(snd_pcm_hw_params_set_format(h,p,2)==0);
    assert(snd_pcm_hw_params_set_channels(h,p,2)==0);
    assert(snd_pcm_hw_params_set_rate_near(h,p,&rate,&dir)==0);
    assert(snd_pcm_hw_params(h,p)==0);
    return h;
}
/* analog state: main_out, input_level, lp_cutoff, lp_resonance, tone level, tone Hz (0xffff = never moved). */
static void analog(const char *path,uint16_t input_level,uint16_t tone) {
    static uint32_t seq=2;
    uint8_t b[64]={0}; uint16_t v[6]={0xffff,input_level,0xffff,0xffff,tone,1000};
    memcpy(b,"SP16ANA1",8); seq+=2; memcpy(b+8,&seq,4); memcpy(b+12,v,sizeof v);
    int fd=open(path,O_RDWR|O_CREAT,0600); assert(fd>=0);
    assert(pwrite(fd,b,64,0)==64); close(fd);
}
static void settle(void) { struct timespec t={ .tv_nsec=30000000 }; nanosleep(&t,NULL); } /* the shim re-reads the state every 20 ms */
static int peak16(snd_pcm_t *h,int *last_l,int *last_r) { /* peak magnitude over a few reads of the S16 capture */
    int16_t b[N*2]; int top=0;
    settle();
    assert(snd_pcm_readi(h,b,N)==N); /* the state is re-read after a call: this one still runs on the old state */
    for (int k=0;k<6;k++) {
        memset(b,0x5a,sizeof b);
        assert(snd_pcm_readi(h,b,N)==N);
        for (int i=0;i<N*2;i++) { int m=b[i]<0?-b[i]:b[i]; if (m>top) top=m; }
        if (last_l) *last_l=b[N*2-2];
        if (last_r) *last_r=b[N*2-1];
    }
    return top;
}
static void gpio_limiter(int level) {
    int g=open("/dev/gpiodrv",O_RDWR); assert(g>=0);
    uint8_t v=(uint8_t)level;
    assert(lseek(g,4*32+6,SEEK_SET)==4*32+6 && write(g,&v,1)==1); close(g);
}
static void wav(const char *path,unsigned channels,unsigned rate,const int16_t *frame,unsigned frames) {
    uint32_t data=frames*channels*2, riff=36+data, fmt=16, br=rate*channels*2; uint16_t tag=1, ch=(uint16_t)channels, ba=(uint16_t)(channels*2), bits=16;
    FILE *f=fopen(path,"wb"); assert(f);
    fwrite("RIFF",1,4,f); fwrite(&riff,4,1,f); fwrite("WAVEfmt ",1,8,f); fwrite(&fmt,4,1,f); fwrite(&tag,2,1,f); fwrite(&ch,2,1,f);
    fwrite(&rate,4,1,f); fwrite(&br,4,1,f); fwrite(&ba,2,1,f); fwrite(&bits,2,1,f); fwrite("data",1,4,f); fwrite(&data,4,1,f);
    for (unsigned i=0;i<frames;i++) fwrite(frame,2,channels,f);
    fclose(f);
}
int main(void) {
    const char *state=getenv("SP16_ANALOG"), *wavpath=getenv("SP16_INPUT_WAV");
    assert(state && wavpath);
    unlink(wavpath);
    snd_pcm_t *in=capture();
    /* 1. INPUT LEVEL law: gain = 4 * pot^2 (unity at pot 0.5). A 0 dBFS tone at pot 0.5 peaks near full scale, at
     * pot 0.25 near a quarter, and a tone at -30 dBFS at pot 0.5 near 0.0316 full scale. */
    analog(state,500,1000);
    int top=peak16(in,NULL,NULL); assert(top>32000);
    analog(state,250,1000);
    top=peak16(in,NULL,NULL); assert(top>8100 && top<8300);
    analog(state,500,500);
    top=peak16(in,NULL,NULL); assert(top>1000 && top<1070);
    /* 2. Limiter off (the line was never written): a tone at 0 dBFS passes unchanged. Limiter on: the peak is pulled
     * below full scale but stays above the threshold (0.5 full scale); a tone below the threshold passes unchanged.
     * The line going low switches it off again. */
    analog(state,500,1000);
    top=peak16(in,NULL,NULL); assert(top>32000);
    gpio_limiter(1);
    top=peak16(in,NULL,NULL); assert(top>16384+4000 && top<26000); /* 0.5 + 0.5 * 1/2 = 0.75 at the crest */
    analog(state,500,500);
    top=peak16(in,NULL,NULL); assert(top>1000 && top<1070);       /* -30 dBFS is below the threshold */
    analog(state,500,1000);
    gpio_limiter(0);
    top=peak16(in,NULL,NULL); assert(top>32000);
    /* 3. WAV injection: a stereo file replaces the tone, INPUT LEVEL scales it, the limiter acts on it. */
    analog(state,500,0);
    top=peak16(in,NULL,NULL); assert(top==0);                    /* no tone, no file: silence */
    int16_t lr[2]={ 16384, -8192 };
    wav(wavpath,2,44100,lr,2000);
    int l=0, r=0;
    sleep(1); settle();
    top=peak16(in,&l,&r); assert(l>16380 && l<16388 && r<-8188 && r>-8196);
    analog(state,250,0);
    top=peak16(in,&l,&r); assert(l>4090 && l<4098 && r<-2045 && r>-2051);
    analog(state,500,0);
    int16_t hot[2]={ 32000, 32000 };
    wav(wavpath,2,44100,hot,2001);                               /* a different size: the shim reloads it */
    sleep(2);
    gpio_limiter(1);
    top=peak16(in,&l,&r); assert(l>22000 && l<25000 && l==r);    /* 0.977 -> 0.5 + 0.5 * 0.954/1.954 = 0.744 */
    gpio_limiter(0);
    /* A mono file feeds both channels; a 22.05 kHz file plays at the codec rate (constant here, so unchanged). */
    int16_t mono[1]={ 8192 };
    wav(wavpath,1,22050,mono,3000);
    sleep(2);
    top=peak16(in,&l,&r); assert(l>8188 && l<8196 && l==r);
    /* Removing the file returns to the tone source (silence with the tone off). */
    unlink(wavpath);
    sleep(2);
    top=peak16(in,&l,&r); assert(top==0);
    assert(snd_pcm_close(in)==0);
    return 0;
}
#endif
