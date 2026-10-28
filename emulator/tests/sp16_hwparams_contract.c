/* Observability contract for the ALSA hw_params wrappers, media-node logging and blocked-exec argv logging.
 * Build twice, as sp16_pcm_contract.c does:
 * -DSP16_FAKE_ALSA -shared -fPIC -> libsp16-fake-alsa.so (unversioned fake libasound)
 * normal -> test, linked to that library; run with the ARM UI preload.
 * arm-contracts.sh greps the stub's log for the lines this test must produce.
 * Every wrapper must still return the real result unchanged.
 */
#include <assert.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
typedef struct _snd_pcm snd_pcm_t;
typedef struct _snd_pcm_hw_params snd_pcm_hw_params_t;
typedef unsigned long snd_pcm_uframes_t;
typedef long snd_pcm_sframes_t;

#ifdef SP16_FAKE_ALSA
/* Fake libasound: records the values it was given and returns them through the getters. */
static uintptr_t sentinels[4];
static int stored_access, stored_format, stored_dir;
static unsigned stored_channels, stored_rate, stored_periods;
static snd_pcm_uframes_t stored_period, stored_buffer;
int snd_pcm_open(snd_pcm_t **out,const char *name,int stream,int mode) {
    (void)stream; (void)mode;
    assert(!strcmp(name,"null") || !strcmp(name,"unmodeled"));
    *out=(snd_pcm_t *)&sentinels[0]; return 0;
}
int snd_pcm_close(snd_pcm_t *p) { (void)p; return 0; }
int snd_pcm_hw_params_set_access(snd_pcm_t *p,snd_pcm_hw_params_t *q,int a) { (void)p; (void)q; stored_access=a; return 0; }
int snd_pcm_hw_params_set_format(snd_pcm_t *p,snd_pcm_hw_params_t *q,int f) { (void)p; (void)q; stored_format=f; return 0; }
int snd_pcm_hw_params_set_channels(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned c) { (void)p; (void)q; stored_channels=c; return 0; }
int snd_pcm_hw_params_set_rate(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned r,int d) { (void)p; (void)q; stored_rate=r; stored_dir=d; return 0; }
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned *r,int *d) {
    (void)p; (void)q; (void)d; stored_rate=*r; return 0; }
int snd_pcm_hw_params_set_period_size_near(snd_pcm_t *p,snd_pcm_hw_params_t *q,snd_pcm_uframes_t *f,int *d) {
    (void)p; (void)q; (void)d; stored_period=*f; return 0; }
int snd_pcm_hw_params_set_buffer_size_near(snd_pcm_t *p,snd_pcm_hw_params_t *q,snd_pcm_uframes_t *f) {
    (void)p; (void)q; stored_buffer=*f; return 0; }
int snd_pcm_hw_params_set_periods_near(snd_pcm_t *p,snd_pcm_hw_params_t *q,unsigned *n,int *d) {
    (void)p; (void)q; (void)d; stored_periods=*n; return 0; }
int snd_pcm_hw_params(snd_pcm_t *p,snd_pcm_hw_params_t *q) { (void)p; (void)q; return 0; }
int snd_pcm_hw_params_get_access(const snd_pcm_hw_params_t *q,int *a) { (void)q; *a=stored_access; return 0; }
int snd_pcm_hw_params_get_format(const snd_pcm_hw_params_t *q,int *f) { (void)q; *f=stored_format; return 0; }
int snd_pcm_hw_params_get_channels(const snd_pcm_hw_params_t *q,unsigned *c) { (void)q; *c=stored_channels; return 0; }
int snd_pcm_hw_params_get_rate(const snd_pcm_hw_params_t *q,unsigned *r,int *d) { (void)q; *r=stored_rate; *d=stored_dir; return 0; }
int snd_pcm_hw_params_get_period_size(const snd_pcm_hw_params_t *q,snd_pcm_uframes_t *f,int *d) { (void)q; *f=stored_period; *d=0; return 0; }
int snd_pcm_hw_params_get_buffer_size(const snd_pcm_hw_params_t *q,snd_pcm_uframes_t *f) { (void)q; *f=stored_buffer; return 0; }
int snd_pcm_hw_params_get_periods(const snd_pcm_hw_params_t *q,unsigned *n,int *d) { (void)q; *n=stored_periods; *d=0; return 0; }
const char *snd_pcm_access_name(int a) { return a==3 ? "RW_INTERLEAVED" : "OTHER"; }
const char *snd_pcm_format_name(int f) { return f==2 ? "S16_LE" : "OTHER"; }
#define IO(name,type) snd_pcm_sframes_t name(snd_pcm_t *p,type buffer,snd_pcm_uframes_t frames) { (void)p; (void)buffer; return frames; }
IO(snd_pcm_readi,void *)
IO(snd_pcm_readn,void **)
IO(snd_pcm_writei,const void *)
IO(snd_pcm_writen,void **)
#else
int snd_pcm_open(snd_pcm_t **,const char *,int,int);
int snd_pcm_close(snd_pcm_t *);
int snd_pcm_hw_params_set_access(snd_pcm_t *,snd_pcm_hw_params_t *,int);
int snd_pcm_hw_params_set_format(snd_pcm_t *,snd_pcm_hw_params_t *,int);
int snd_pcm_hw_params_set_channels(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned);
int snd_pcm_hw_params_set_rate(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned,int);
int snd_pcm_hw_params_set_rate_near(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned *,int *);
int snd_pcm_hw_params_set_period_size_near(snd_pcm_t *,snd_pcm_hw_params_t *,snd_pcm_uframes_t *,int *);
int snd_pcm_hw_params_set_buffer_size_near(snd_pcm_t *,snd_pcm_hw_params_t *,snd_pcm_uframes_t *);
int snd_pcm_hw_params_set_periods_near(snd_pcm_t *,snd_pcm_hw_params_t *,unsigned *,int *);
int snd_pcm_hw_params(snd_pcm_t *,snd_pcm_hw_params_t *);
snd_pcm_sframes_t snd_pcm_readi(snd_pcm_t *,void *,snd_pcm_uframes_t);
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *,const void *,snd_pcm_uframes_t);
int main(void) {
    snd_pcm_t *handle; char buffer[4]; char params[64]; /* opaque, the fake library ignores it */
    snd_pcm_hw_params_t *p=(snd_pcm_hw_params_t *)params;
    unsigned rate=44100, periods=4; int dir=0;
    snd_pcm_uframes_t period=512, size=2048;
    assert(snd_pcm_open(&handle,"hw:ak4452audio,1",0,0)==0);
    /* Every setter must return the real result (0) and keep the handle usable. */
    assert(snd_pcm_hw_params_set_access(handle,p,3)==0);
    assert(snd_pcm_hw_params_set_format(handle,p,2)==0);
    assert(snd_pcm_hw_params_set_channels(handle,p,2)==0);
    assert(snd_pcm_hw_params_set_rate_near(handle,p,&rate,&dir)==0);
    assert(snd_pcm_hw_params_set_period_size_near(handle,p,&period,&dir)==0);
    assert(snd_pcm_hw_params_set_buffer_size_near(handle,p,&size)==0);
    assert(snd_pcm_hw_params_set_periods_near(handle,p,&periods,&dir)==0);
    assert(snd_pcm_hw_params(handle,p)==0);
    /* Repeated calls are forwarded but logged only once; I/O keeps working. */
    assert(snd_pcm_hw_params_set_access(handle,p,3)==0);
    assert(snd_pcm_writei(handle,buffer,960)==960);
    assert(snd_pcm_writei(handle,buffer,960)==960);
    assert(snd_pcm_readi(handle,buffer,0)==0);
    assert(snd_pcm_close(handle)==0);
    /* By default: the AK4452 codec lists S16_LE, S24_LE and S20_3LE, so FLOAT_LE
     * and S32_LE are refused as the unit's driver would and the app's format search settles on S24_LE; S20_3LE is
     * refused too because the board model's sample path carries only 2- and 4-byte layouts; the master
     * codec stream is not filtered. */
    assert(snd_pcm_open(&handle,"hw:ak4452audio,0",1,0)==0);
    assert(snd_pcm_hw_params_set_format(handle,p,14)<0);
    assert(snd_pcm_hw_params_set_format(handle,p,10)<0);
    assert(snd_pcm_hw_params_set_format(handle,p,36)<0);
    assert(snd_pcm_hw_params_set_format(handle,p,6)==0);
    assert(snd_pcm_close(handle)==0);
    assert(snd_pcm_open(&handle,"hw:ak4458audio,0",0,0)==0);
    assert(snd_pcm_hw_params_set_format(handle,p,14)==0);
    assert(snd_pcm_close(handle)==0);
    /* Media node: an empty queue polls idle (a read would block like the kernel's); logged rate-limited. */
    int fd=open("/proc/udev_usb1",O_RDONLY);
    assert(fd>=0);
    struct pollfd pfd={ .fd=fd, .events=POLLIN };
    assert(poll(&pfd,1,0)==0);
    assert(close(fd)==0);
    /* Blocked exec: the refusal is kept (returns -1) and argv is logged. */
    assert(execl("/bin/sync","sync","-n",(char *)0)==-1);
    return 0;
}
#endif
