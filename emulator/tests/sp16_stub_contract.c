/* Synthetic interface checks. No OEM firmware or device access. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <stdint.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

struct spi_message { uint8_t multi_id,reserved,part_id,part_num; uint32_t bytes; uint64_t tx_buf; };
int main(void) {
    int panel=open("/dev/subucom_spi0.1",O_RDWR);
    int cdc=open("/dev/cdc_spi2.0",O_RDWR);
    int dac=open("/dev/dac_spi4.0",O_WRONLY);
    assert(panel>=0 && cdc>=0 && dac>=0);
    int ep=epoll_create1(EPOLL_CLOEXEC);
    struct epoll_event event={.events=EPOLLIN,.data.fd=panel};
    assert(ep>=0 && epoll_ctl(ep,EPOLL_CTL_ADD,panel,&event)==0);
    assert(epoll_wait(ep,&event,1,0)==0);
    uint32_t bytes=0; uint16_t bits=0; uint8_t timer=0;
    assert(ioctl(panel,0x80047004,&bytes)==0 && bytes==32);
    assert(ioctl(dac,0x80027003,&bits)==0 && bits==16);
    bytes=129; assert(ioctl(panel,0x40047004,&bytes)==-1 && errno==EINVAL);
    timer=1; assert(ioctl(panel,0x40017001,&timer)==0);
    bits=16; assert(ioctl(panel,0x40027003,&bits)==-1 && errno==EBUSY);
    assert(write(panel,"fixture",7)==-1 && errno==EFAULT);
    timer=0; assert(ioctl(panel,0x40017001,&timer)==0);
    assert(write(dac,"fixture",7)==0);
    uint8_t neutral[4]={1,2,3,4};
    assert(read(cdc,neutral,sizeof(neutral))==4);
    assert(neutral[0]==0 && neutral[1]==0 && neutral[2]==0 && neutral[3]==0);
    struct spi_message message={.bytes=4,.tx_buf=(uintptr_t)neutral};
    assert(sizeof(message)==16 && ioctl(panel,0x40107000,&message)==0);
    message.bytes=129;
    assert(ioctl(panel,0x40107000,&message)==-1 && errno==EINVAL);
    assert(ioctl(panel,0xdeadbeef,&bytes)==-1 && errno==ENOTTY);
    int backlight=open("/dev/backlight",O_RDWR);
    assert(backlight>=0);
    uint32_t level=0;
    assert(ioctl(backlight,0x40047000,&level)==0);
    level=255;
    assert(ioctl(backlight,0x40047000,&level)==0);
    assert(ioctl(backlight,0x40047000,NULL)==-1 && errno==EFAULT);
    assert(close(backlight)==0);
    int media=open("/proc/udev_usb1",O_RDONLY);
    assert(media>=0); event=(struct epoll_event){.events=EPOLLIN,.data.fd=media};
    assert(epoll_ctl(ep,EPOLL_CTL_ADD,media,&event)==0 && epoll_wait(ep,&event,1,0)==0);
    assert(execl("/bin/sh","sh","-c","echo prohibited",(char *)0)==-1 && errno==ENOTSUP);
    /* Without SP16_FAKE_ETH0 the guest has no eth0 and the shim must not invent one. */
    int net=socket(AF_INET,SOCK_DGRAM,0);
    struct ifreq ifr;
    assert(net>=0);
    memset(&ifr,0,sizeof ifr); strcpy(ifr.ifr_name,"eth0");
    assert(ioctl(net,0x8927,&ifr)==-1 && ioctl(net,0x8915,&ifr)==-1);
    assert(close(net)==0);
    assert(close(media)==0 && close(panel)==0 && close(cdc)==0 && close(dac)==0 && close(ep)==0);
    return 0;
}
