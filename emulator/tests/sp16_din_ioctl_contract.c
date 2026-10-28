/* Exercise the normal /dev/din_midi ioctl and proc-capacity shim against a local control peer. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void) {
    const char *path="/tmp/run/din-control.sock";
    int listener=socket(AF_UNIX,SOCK_SEQPACKET,0);
    assert(listener>=0);
    struct sockaddr_un address={.sun_family=AF_UNIX};
    strcpy(address.sun_path,path);
    assert(bind(listener,(struct sockaddr *)&address,sizeof address)==0);
    assert(listen(listener,4)==0);
    assert(setenv("SP16_DIN_CONTROL",path,1)==0);
    pid_t child=fork();
    assert(child>=0);
    if (!child) {
        const uint32_t expected[]={UINT32_MAX,1,0xf20102ff,7,UINT32_MAX};
        for (unsigned i=0;i<sizeof expected/sizeof expected[0];i++) {
            int fd=accept(listener,NULL,NULL);
            assert(fd>=0);
            uint32_t request=0;
            assert(recv(fd,&request,sizeof request,0)==sizeof request);
            assert(request==expected[i]);
            int32_t result=request==UINT32_MAX ? 4096 : request==7 ? -EINVAL : 0;
            assert(send(fd,&result,sizeof result,0)==sizeof result);
            close(fd);
        }
        _exit(0);
    }
    int fd=open("/dev/din_midi",O_RDWR);
    assert(fd>=0);
    uint32_t request=1;
    assert(ioctl(fd,0x40047000UL,&request)==0);
    request=0xf20102ff;
    assert(ioctl(fd,0x40047000UL,&request)==0);
    request=7;
    assert(ioctl(fd,0x40047000UL,&request)==-1 && errno==EINVAL);
    assert(ioctl(fd,0x40047000UL,NULL)==-1 && errno==EFAULT);
    assert(ioctl(fd,0x1234UL,&request)==-1 && errno==ENOTTY);
    assert(close(fd)==0);
    fd=open("/proc/asound/snddinmidi/midi0",O_RDONLY);
    assert(fd>=0);
    char report[64]={0};
    assert(read(fd,report,sizeof report-1)>0);
    assert(!strcmp(report,"Avail        : 4096\n"));
    assert(close(fd)==0);
    int status=0;
    assert(waitpid(child,&status,0)==child && WIFEXITED(status) && WEXITSTATUS(status)==0);
    close(listener);
    unlink(path);
    puts("DIN ioctl forwarding and live capacity: PASS");
    return 0;
}
