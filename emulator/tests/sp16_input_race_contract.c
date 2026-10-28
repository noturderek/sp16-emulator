/* Race contract for the injected input WAV. The file is replaced and removed over and over while the capture thread
 * reads it and a second PCM thread paces (which is where the shim looks at the file). Every frame the capture returns
 * must be silence or a whole frame of one of the two files: a torn swap (new data with the old length) or a mapping
 * pulled away from under the reader shows up as a foreign value or a crash.
 * Build twice, as sp16_input_chain_contract.c does (-DSP16_FAKE_ALSA -shared -fPIC -> libsp16-fake-alsa.so; normal -> test,
 * with -pthread) and run with the ARM UI preload, SP16_ANALOG=<file>, SP16_INPUT_WAV=<file> and SP16_INPUT_WAV_POLL_MS=2
 * (the shim looks at the file every 2 ms instead of every second).
 */
#define _GNU_SOURCE
#include <assert.h>
#include <fcntl.h>
#include <pthread.h>
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
    (void)stream; (void)mode; assert(!strcmp(name,"null")); *out=(snd_pcm_t *)&sentinels[__atomic_fetch_add(&opened,1,__ATOMIC_RELAXED) % 8]; return 0; }
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
/* File A: 3000 stereo frames of (1000, 2000); file B: 5000 frames of (3000, 4000). The sizes differ, so each replacement is
 * noticed. At INPUT LEVEL 0.5 (gain 1) the S16 capture carries the same numbers, give or take one count of rounding. */
static int near(int v,int want) { return v>=want-2 && v<=want+2; }
static int frame_ok(int l,int r) {
    return (near(l,1000) && near(r,2000)) || (near(l,3000) && near(r,4000)) || (l==0 && r==0);
}
static void write_wav(const char *path,unsigned frames,int16_t l,int16_t r) {
    char tmp[256]; snprintf(tmp,sizeof tmp,"%s.tmp",path);
    uint32_t data=frames*4, riff=36+data, fmt=16, rate=44100, br=44100*4; uint16_t tag=1, ch=2, ba=4, bits=16;
    FILE *f=fopen(tmp,"wb"); assert(f);
    fwrite("RIFF",1,4,f); fwrite(&riff,4,1,f); fwrite("WAVEfmt ",1,8,f); fwrite(&fmt,4,1,f); fwrite(&tag,2,1,f); fwrite(&ch,2,1,f);
    fwrite(&rate,4,1,f); fwrite(&br,4,1,f); fwrite(&ba,2,1,f); fwrite(&bits,2,1,f); fwrite("data",1,4,f); fwrite(&data,4,1,f);
    for (unsigned i=0;i<frames;i++) { fwrite(&l,2,1,f); fwrite(&r,2,1,f); }
    fclose(f);
    assert(rename(tmp,path)==0);                       /* atomic: a reader sees the old file or the new one */
}
static void analog(const char *path) {                 /* INPUT LEVEL at 0.5 (unity), no tone */
    uint8_t b[64]={0}; uint32_t seq=2; uint16_t v[6]={0xffff,500,0xffff,0xffff,0,1000};
    memcpy(b,"SP16ANA1",8); memcpy(b+8,&seq,4); memcpy(b+12,v,sizeof v);
    int fd=open(path,O_RDWR|O_CREAT,0600); assert(fd>=0);
    assert(pwrite(fd,b,64,0)==64); close(fd);
}
static snd_pcm_t *pcm(const char *name,int stream) {
    snd_pcm_t *h; unsigned rate=44100; int dir=0;
    char params[64]; snd_pcm_hw_params_t *p=(snd_pcm_hw_params_t *)params;
    assert(snd_pcm_open(&h,name,stream,0)==0);
    assert(snd_pcm_hw_params_set_format(h,p,2)==0);
    assert(snd_pcm_hw_params_set_channels(h,p,2)==0);
    assert(snd_pcm_hw_params_set_rate_near(h,p,&rate,&dir)==0);
    assert(snd_pcm_hw_params(h,p)==0);
    return h;
}
static volatile int stop_threads;
static unsigned long bad, reads, saw_a, saw_b, saw_silence;
static void *reader(void *arg) {
    snd_pcm_t *in=arg; int16_t b[N*2];
    while (!stop_threads) {
        assert(snd_pcm_readi(in,b,N)==N);
        __atomic_add_fetch(&reads,1,__ATOMIC_RELAXED);
        for (int i=0;i<N;i++) {
            int l=b[2*i], r=b[2*i+1];
            if (!frame_ok(l,r)) __atomic_add_fetch(&bad,1,__ATOMIC_RELAXED);
            else if (near(l,1000)) __atomic_add_fetch(&saw_a,1,__ATOMIC_RELAXED);
            else if (near(l,3000)) __atomic_add_fetch(&saw_b,1,__ATOMIC_RELAXED);
            else __atomic_add_fetch(&saw_silence,1,__ATOMIC_RELAXED);
        }
    }
    return NULL;
}
static void *pacer(void *arg) {                         /* another PCM thread: pace() runs the shim's file refresh here too */
    snd_pcm_t *out=arg; int16_t b[N*2]={0};
    while (!stop_threads) assert(snd_pcm_writei(out,b,N)==N);
    return NULL;
}
int main(void) {
    const char *state=getenv("SP16_ANALOG"), *path=getenv("SP16_INPUT_WAV");
    assert(state && path && getenv("SP16_INPUT_WAV_POLL_MS"));
    unlink(path);
    analog(state);
    snd_pcm_t *in=pcm("hw:ak4452audio,0",1), *out=pcm("hw:ak4452audio,1",0);
    pthread_t t_read, t_pace;
    assert(pthread_create(&t_read,NULL,reader,in)==0 && pthread_create(&t_pace,NULL,pacer,out)==0);
    struct timespec nap={ .tv_nsec=8000000 };
    for (int i=0;i<500;i++) {
        if (i%50==49) { unlink(path); nanosleep(&nap,NULL); nanosleep(&nap,NULL); }   /* removed for a while: silence */
        if (i%2) write_wav(path,5000,3000,4000); else write_wav(path,3000,1000,2000);
        nanosleep(&nap,NULL);
    }
    stop_threads=1;
    pthread_join(t_read,NULL); pthread_join(t_pace,NULL);
    fprintf(stderr,"race: reads=%lu bad=%lu a=%lu b=%lu silence=%lu\n",reads,bad,saw_a,saw_b,saw_silence);
    assert(bad==0);                                     /* no torn or foreign frame */
    assert(reads>500);                                  /* the reader kept running */
    assert(saw_a>0 && saw_b>0 && saw_silence>0);        /* both files, and the removal, really went through the shim */
    assert(snd_pcm_close(in)==0 && snd_pcm_close(out)==0);
    return 0;
}
#endif
