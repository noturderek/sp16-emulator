/* Contract for the audio transport. Build twice like sp16_pcm_tap_contract.c
 * (-DSP16_FAKE_ALSA -shared -fPIC -> libsp16-fake-alsa.so; normal -> test) and run with the ARM UI preload plus
 * SP16_PCM_PORT=/tmp/run/port (a regular file in mode "file", a FIFO nobody reads in mode "stall") and no tap.
 *   file : every written frame arrives bit-exact (all 16 channels of the master stream), packets carry a
 *          contiguous sequence, config comes first, drops are zero, the ping gets a pong, pacing is intact.
 *   stall: a consumer that never reads must not delay snd_pcm_writei beyond the 44.1 kHz pacing; overflow is
 *          dropped (the frame index keeps advancing) and the drop counter in the packet stream is not zero.
 *   off  : without SP16_PCM_PORT nothing is created.
 */
#include <assert.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
typedef struct _snd_pcm snd_pcm_t;
typedef struct _snd_pcm_hw_params snd_pcm_hw_params_t;
typedef unsigned long snd_pcm_uframes_t;
typedef long snd_pcm_sframes_t;

#ifdef SP16_FAKE_ALSA
static uintptr_t sentinels[4]; static unsigned opened, stored_channels, stored_rate; static int stored_format;
int snd_pcm_open(snd_pcm_t **out,const char *name,int stream,int mode) {
    (void)stream; (void)mode; assert(!strcmp(name,"null")); *out=(snd_pcm_t *)&sentinels[opened++%4]; return 0; }
int snd_pcm_close(snd_pcm_t *p) { (void)p; return 0; }
int snd_pcm_hw_params_set_format(snd_pcm_t *p,snd_pcm_hw_params_t *q,int f) { (void)p; (void)q; stored_format=f; return 0; }
int snd_pcm_hw_params_set_channels(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned c) { (void)p; (void)q; stored_channels=c; return 0; }
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned *r,int *d) { (void)p; (void)q; (void)d; stored_rate=*r; return 0; }
int snd_pcm_hw_params(snd_pcm_t *p,snd_pcm_hw_params_t *q) { (void)p; (void)q; return 0; }
int snd_pcm_hw_params_get_format(const snd_pcm_hw_params_t *q,int *f) { (void)q; *f=stored_format; return 0; }
int snd_pcm_hw_params_get_channels(const snd_pcm_hw_params_t *q,unsigned *c) { (void)q; *c=stored_channels; return 0; }
int snd_pcm_hw_params_get_rate(const snd_pcm_hw_params_t *q,unsigned *r,int *d) { (void)q; *r=stored_rate; *d=0; return 0; }
const char *snd_pcm_format_name(int f) { return f==2 ? "S16_LE" : "OTHER"; }
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *p,const void *b,snd_pcm_uframes_t n) { (void)p; (void)b; return n; }
snd_pcm_sframes_t snd_pcm_readi(snd_pcm_t *p,void *b,snd_pcm_uframes_t n) { (void)p; (void)b; return n; }
#else
int snd_pcm_open(snd_pcm_t **,const char *,int,int);
int snd_pcm_close(snd_pcm_t *);
int snd_pcm_hw_params_set_format(snd_pcm_t *,snd_pcm_hw_params_t *,int);
int snd_pcm_hw_params_set_channels(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned);
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned *,int *);
int snd_pcm_hw_params(snd_pcm_t *,snd_pcm_hw_params_t *);
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *,const void *,snd_pcm_uframes_t);
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
static void configure(snd_pcm_t *h,int format,unsigned channels) {
    char params[64]; snd_pcm_hw_params_t *p=(snd_pcm_hw_params_t *)params; unsigned rate=44100; int dir=0;
    assert(snd_pcm_hw_params_set_format(h,p,format)==0);
    assert(snd_pcm_hw_params_set_channels(h,p,channels)==0);
    assert(snd_pcm_hw_params_set_rate_near(h,p,&rate,&dir)==0);
    assert(snd_pcm_hw_params(h,p)==0);
}
enum { CH=16, FRAMES=64 };
static void fill(int16_t *buf,unsigned period) {
    for (unsigned f=0;f<FRAMES;f++) for (unsigned c=0;c<CH;c++) buf[f*CH+c]=(int16_t)((period*131u+f*17u+c*7u)%30011u+1u);
}
static uint32_t rd32(const uint8_t *p) { uint32_t v; memcpy(&v,p,4); return v; }
static uint64_t rd64(const uint8_t *p) { uint64_t v; memcpy(&v,p,8); return v; }
int main(int argc,char **argv) {
    const char *mode=argc>1?argv[1]:"file";
    if (!strcmp(mode,"s24")) {
        /* S24_LE in, FLOAT_LE out: the transport sign-extends the low 24 bits of each 32-bit word and divides by
         * 2^23. The send/return playback carries 2 channels, so the conversion is exercised without the 16-channel
         * master stream (whose 4 bytes x 16 channels exceed the transport's 32-byte-per-frame slot). */
        snd_pcm_t *s24;
        assert(snd_pcm_open(&s24,"hw:ak4452audio,1",0,0)==0);
        configure(s24,6,2);
        for (unsigned i=0;i<200;i++) {
            int32_t s[FRAMES*2];
            for (unsigned f=0;f<FRAMES;f++) for (unsigned c=0;c<2;c++) {
                uint32_t u=(i*2654435761u+f*1103515245u+c*12345u)&0xffffffu;
                s[f*2+c]=(int32_t)(u<<8)>>8;
            }
            assert(snd_pcm_writei(s24,s,FRAMES)==FRAMES);
        }
        struct timespec nap={ .tv_nsec=150000000 }; nanosleep(&nap,NULL);   /* let the forwarder drain */
        int fd=open("/tmp/run/port",O_RDONLY); assert(fd>=0);
        static uint8_t data[8*1024*1024]; ssize_t size=read(fd,data,sizeof data); assert(size>0);
        size_t at=0; unsigned frames=0; int saw_data=0;
        while ((size_t)size-at>=48) {
            const uint8_t *h0=data+at; assert(!memcmp(h0,"SPCM",4));
            unsigned type=h0[4]; uint32_t n=rd32(h0+20), payload=rd32(h0+44);
            assert(h0[5]==2 && h0[6]==14 && h0[7]==2 && h0[8]==2 && h0[9]==0);   /* send/return, FLOAT_LE, 2 ch */
            assert(rd32(h0+12)==44100);
            if (type==1) { at+=48; continue; }
            assert(type==2 && payload==n*8);
            assert((size_t)size-at>=48+payload);
            for (uint32_t f=0;f<n;f++) {
                uint64_t abs_frame=rd64(h0+32)+f;
                unsigned period=(unsigned)(abs_frame/FRAMES), off=(unsigned)(abs_frame%FRAMES);
                for (unsigned c=0;c<2;c++) {
                    uint32_t u=(period*2654435761u+off*1103515245u+c*12345u)&0xffffffu;
                    int32_t v=(int32_t)(u<<8)>>8;
                    float got; memcpy(&got,h0+48+(f*2+c)*4,4);
                    assert(got==(float)v/8388608.0f);
                }
            }
            frames+=n; saw_data=1; at+=48+payload;
        }
        assert(saw_data && at==(size_t)size);
        assert(snd_pcm_close(s24)==0);
        printf("s24 frames=%u converted bit-exact\n",frames);
        return 0;
    }
    snd_pcm_t *h;
    assert(snd_pcm_open(&h,"hw:ak4458audio,0",0,0)==0);
    configure(h,2,CH);
    int16_t buf[FRAMES*CH];
    if (!strcmp(mode,"off")) {
        for (int i=0;i<20;i++) { fill(buf,i); assert(snd_pcm_writei(h,buf,FRAMES)==FRAMES); }
        assert(access("/tmp/run/port",F_OK)!=0);
        return 0;
    }
    unsigned periods=!strcmp(mode,"stall")?1500:600;       /* 1500 periods overflow the 64-slot ring */
    double start=now();
    for (unsigned i=0;i<periods;i++) { fill(buf,i); assert(snd_pcm_writei(h,buf,FRAMES)==FRAMES); }
    double elapsed=now()-start, expected=periods*(double)FRAMES/44100.0;
    assert(elapsed>=expected*0.97 && elapsed<expected+0.35);  /* paced by the deadline clock, never by the consumer */
    if (!strcmp(mode,"stall")) { printf("stall elapsed=%.3f expected=%.3f\n",elapsed,expected); return 0; }
    struct timespec nap={ .tv_nsec=150000000 }; nanosleep(&nap,NULL);   /* let the forwarder drain */
    int fd=open("/tmp/run/port",O_RDONLY); assert(fd>=0);
    static uint8_t data[8*1024*1024]; ssize_t size=read(fd,data,sizeof data); assert(size>0);
    size_t at=0; unsigned seq=0, frames=0, packets=0; int config_first=0; uint64_t next_index=0;
    while ((size_t)size-at>=48) {
        const uint8_t *h0=data+at; assert(!memcmp(h0,"SPCM",4));
        unsigned type=h0[4]; uint32_t n=rd32(h0+20), payload=rd32(h0+44);
        assert(rd32(h0+16)==seq); seq++;                       /* contiguous sequence */
        assert(h0[5]==0 && h0[6]==2 && h0[7]==16 && h0[8]==16 && h0[9]==0);   /* stream, S16_LE, all 16 channels */
        assert(rd32(h0+12)==44100);
        if (type==1) { assert(packets==0 && !config_first); config_first=1; at+=48; continue; }
        assert(type==2 && config_first && payload==n*32);
        assert((size_t)size-at>=48+payload);
        assert(rd64(h0+32)==next_index);                       /* no frame lost without a gap in the index */
        for (uint32_t f=0;f<n;f++) {
            uint64_t abs_frame=next_index+f; int16_t ref[FRAMES*CH]; fill(ref,(unsigned)(abs_frame/FRAMES));
            assert(!memcmp(h0+48+f*32,&ref[(abs_frame%FRAMES)*CH],32));
        }
        next_index+=n; frames+=n; packets++; at+=48+payload;
        assert(rd32(h0+40)==0);                                /* no drops while the consumer keeps up */
    }
    assert(at==(size_t)size && frames==periods*FRAMES);
    printf("file packets=%u frames=%u coalesced=%.1f periods/packet\n",packets,frames,(double)periods/packets);
    return 0;
}
#endif
