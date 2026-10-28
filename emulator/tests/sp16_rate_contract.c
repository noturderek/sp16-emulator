/* Rate contract for the board PCMs: the codec clock is fixed at 44.1 kHz, so all three streams (AK4452 playback and
 * capture, AK4458 master) open at 44100 whatever the app asks for. Build twice, as sp16_hwparams_contract.c does:
 * -DSP16_FAKE_ALSA -shared -fPIC -> libsp16-fake-alsa.so; normal -> test, run with the ARM UI preload.
 * Run with SP16_PCM_ANY_RATE=1 the third argument "any" expects the null plugin's pass-through instead.
 */
#include <assert.h>
#include <string.h>
typedef struct _snd_pcm snd_pcm_t;
typedef struct _snd_pcm_hw_params snd_pcm_hw_params_t;
typedef unsigned long snd_pcm_uframes_t;

#ifdef SP16_FAKE_ALSA
static unsigned stored_rate;
int snd_pcm_open(snd_pcm_t **out,const char *name,int stream,int mode) {
    static char sentinel; (void)stream; (void)mode;
    assert(!strcmp(name,"null") || !strcmp(name,"unmodeled")); *out=(snd_pcm_t *)&sentinel; return 0;
}
int snd_pcm_close(snd_pcm_t *p) { (void)p; return 0; }
int snd_pcm_hw_params_set_rate(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned r,int d) { (void)p; (void)q; (void)d; stored_rate=r; return 0; }
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned *r,int *d) { (void)p; (void)q; (void)d; stored_rate=*r; return 0; }
int snd_pcm_hw_params_get_rate(const snd_pcm_hw_params_t *q,unsigned *r,int *d) { (void)q; *r=stored_rate; *d=0; return 0; }
#else
int snd_pcm_open(snd_pcm_t **,const char *,int,int);
int snd_pcm_close(snd_pcm_t *);
int snd_pcm_hw_params_set_rate(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned,int);
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned *,int *);
int snd_pcm_hw_params_get_rate(const snd_pcm_hw_params_t *,unsigned *,int *);
int main(int argc,char **argv) {
    static const char *names[] = { "hw:ak4452audio,0", "hw:ak4452audio,1", "hw:ak4458audio,0" };
    int any=argc>1 && !strcmp(argv[1],"any");
    char params[64]; snd_pcm_hw_params_t *p=(snd_pcm_hw_params_t *)params;
    for (int i=0;i<3;i++) for (int stream=0;stream<2;stream++) {
        snd_pcm_t *handle; unsigned rate, got; int dir=0;
        assert(snd_pcm_open(&handle,names[i],stream,0)==0);
        assert(snd_pcm_hw_params_set_rate(handle,p,44100,0)==0);
        assert(snd_pcm_hw_params_set_rate(handle,p,48000,0)==(any?0:-22));
        assert(snd_pcm_hw_params_set_rate(handle,p,96000,0)==(any?0:-22));
        rate=48000; assert(snd_pcm_hw_params_set_rate_near(handle,p,&rate,&dir)==0);
        assert(rate==(any?48000u:44100u));
        rate=96000; assert(snd_pcm_hw_params_set_rate_near(handle,p,&rate,&dir)==0);
        assert(rate==(any?96000u:44100u));
        rate=44100; assert(snd_pcm_hw_params_set_rate_near(handle,p,&rate,&dir)==0);
        assert(rate==44100u);
        assert(snd_pcm_hw_params_get_rate(p,&got,&dir)==0 && got==44100u);
        assert(snd_pcm_close(handle)==0);
    }
    /* A PCM the shim does not model keeps the real answer. */
    { snd_pcm_t *handle; unsigned rate=48000; int dir=0;
      assert(snd_pcm_open(&handle,"unmodeled",0,0)==0);
      assert(snd_pcm_hw_params_set_rate(handle,p,48000,0)==0);
      assert(snd_pcm_hw_params_set_rate_near(handle,p,&rate,&dir)==0 && rate==48000u);
      assert(snd_pcm_close(handle)==0); }
    return 0;
}
#endif
