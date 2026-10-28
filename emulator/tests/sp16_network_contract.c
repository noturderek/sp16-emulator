/* Opt-in GLINK and ARP contract for the ARM preload, with synthetic requests only. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <net/if.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

static int query(int fd, const char *name, uint32_t command, uint32_t *answer) {
    struct ifreq ifr={0};
    uint32_t data[2]={command,0xfeedbeefU};
    strncpy(ifr.ifr_name,name,IFNAMSIZ-1);
    ifr.ifr_data=(void *)data;
    int result=ioctl(fd,0x8946UL,&ifr);
    *answer=data[1];
    return result;
}

int main(int argc, char **argv) {
    assert(argc==2);
    int fd=socket(AF_INET,SOCK_DGRAM,0);
    assert(fd>=0);
    uint32_t answer=0;
    if (!strcmp(argv[1],"up") || !strcmp(argv[1],"down")) {
        assert(query(fd,"eth0",10,&answer)==0);
        assert(answer==(!strcmp(argv[1],"up") ? 1U : 0U));
        assert(query(fd,"eth1",10,&answer)==-1 && answer==0xfeedbeefU);
        assert(query(fd,"eth0",11,&answer)==-1 && answer==0xfeedbeefU);
        struct ifreq ifr={0};
        strcpy(ifr.ifr_name,"eth0");
        assert(ioctl(fd,0x8946UL,&ifr)==-1 && errno==EFAULT);
        /* A different interface's real address still comes from the kernel. */
        strcpy(ifr.ifr_name,"lo");
        assert(ioctl(fd,0x8915UL,&ifr)==0);
        assert(((struct sockaddr_in *)&ifr.ifr_addr)->sin_addr.s_addr==htonl(0x7f000001U));
    } else if (!strcmp(argv[1],"off")) {
        assert(query(fd,"eth0",10,&answer)==-1 && answer==0xfeedbeefU);
    } else if (!strcmp(argv[1],"arp")) {
        int packet=socket(AF_PACKET,SOCK_DGRAM,1544);
        assert(packet>=0);
        assert(close(packet)==0);
    } else abort();
    assert(close(fd)==0);
    puts("network contract passed");
    return 0;
}
