#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sound/asequencer.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr,"line %d: %s errno=%d\n",__LINE__,#x,errno); exit(1); } } while (0)
_Static_assert(sizeof(struct snd_seq_event)==28,"ARM sequencer event ABI");
_Static_assert(offsetof(struct snd_seq_event,data)==16,"ARM sequencer event data offset");
_Static_assert(sizeof(struct snd_seq_client_info)==188,"ARM sequencer client info ABI");
_Static_assert(sizeof(struct snd_seq_port_info)==164,"ARM sequencer port info ABI");
_Static_assert(sizeof(struct snd_seq_port_subscribe)==80,"ARM sequencer subscription ABI");
_Static_assert(sizeof(struct snd_seq_running_info)==16,"ARM sequencer running mode ABI");
_Static_assert(sizeof(struct snd_seq_system_info)==48,"ARM sequencer system info ABI");
_Static_assert(SNDRV_SEQ_IOCTL_PVERSION==0x80045300,"ARM sequencer version ioctl");
_Static_assert(SNDRV_SEQ_IOCTL_CREATE_PORT==0xc0a45320,"ARM sequencer create port ioctl");
_Static_assert(SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT==0x40505330,"ARM sequencer subscribe ioctl");

int main(void) {
    const char *path="/tmp/sp16-seq-contract.sock";
    unlink(path);
    int listener=socket(AF_UNIX,SOCK_SEQPACKET,0);
    CHECK(listener>=0);
    struct sockaddr_un address={.sun_family=AF_UNIX};
    strcpy(address.sun_path,path);
    CHECK(bind(listener,(void *)&address,sizeof address)==0);
    CHECK(listen(listener,2)==0);
    CHECK(setenv("SP16_SEQ_PROXY",path,1)==0);
    pid_t child=fork();
    CHECK(child>=0);
    if (!child) {
        int peer=accept(listener,NULL,NULL);
        CHECK(peer>=0);
        uint8_t command[3];
        CHECK(recv(peer,command,sizeof command,0)==sizeof command && command[0]=='I' && command[1]==129 && command[2]==0);
        struct snd_seq_event incoming={.type=SNDRV_SEQ_EVENT_NOTEON};
        incoming.source.client=128; incoming.source.port=0;
        incoming.dest.client=129; incoming.dest.port=0;
        incoming.data.note.note=0; incoming.data.note.velocity=100;
        CHECK(send(peer,&incoming,sizeof incoming,0)==sizeof incoming);
        CHECK(recv(peer,command,sizeof command,0)==sizeof command && command[0]=='i');
        CHECK(recv(peer,command,sizeof command,0)==sizeof command && command[0]=='I');
        CHECK(recv(peer,command,sizeof command,0)==sizeof command && command[0]=='O');
        uint8_t packet[1+sizeof(struct snd_seq_event)];
        CHECK(recv(peer,packet,sizeof packet,0)==sizeof packet);
        CHECK(packet[0]=='E');
        struct snd_seq_event outgoing;
        memcpy(&outgoing,packet+1,sizeof outgoing);
        CHECK(outgoing.type==SNDRV_SEQ_EVENT_NOTEOFF && outgoing.data.note.note==0);
        close(peer); close(listener); _exit(0);
    }
    int fd=open("/dev/snd/seq",O_RDWR|O_NONBLOCK);
    CHECK(fd>=0);
    int version=0,client=0;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_PVERSION,&version)==0 && version==SNDRV_SEQ_VERSION);
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_CLIENT_ID,&client)==0 && client==129);
    struct snd_seq_running_info running={0};
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_RUNNING_MODE,&running)==0 && running.cpu_mode==4);
    struct snd_seq_system_info system={0};
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SYSTEM_INFO,&system)==0 && system.clients==256);
    struct snd_seq_client_info info={.client=-1};
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_QUERY_NEXT_CLIENT,&info)==0 && info.client==128 && !strcmp(info.name,"snd-din-midi"));
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_QUERY_NEXT_CLIENT,&info)<0 && errno==ENOENT);
    info.client=128;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_GET_CLIENT_INFO,&info)==0 && info.num_ports==1);
    info.client=client;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SET_CLIENT_INFO,&info)==0);
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_GET_CLIENT_INFO,&info)==0 && info.client==client);
    struct snd_seq_port_info port={0};
    port.addr.client=128; port.addr.port=255;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_QUERY_NEXT_PORT,&port)==0 && port.addr.port==0);
    CHECK(port.capability&(SNDRV_SEQ_PORT_CAP_READ|SNDRV_SEQ_PORT_CAP_WRITE));
    port.addr.client=0; /* ALSA's simple-port creation leaves this unset. */
    strcpy(port.name,"JuceMidiInput");
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_CREATE_PORT,&port)==0 && port.addr.client==client && port.addr.port==0);
    struct snd_seq_port_subscribe sub={0};
    sub.sender.client=128; sub.dest.client=client;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT,&sub)==0);
    struct pollfd pfd={.fd=fd,.events=POLLIN};
    CHECK(poll(&pfd,1,1000)==1 && (pfd.revents&POLLIN));
    struct snd_seq_event event={0};
    CHECK(read(fd,&event,sizeof event)==sizeof event && event.type==SNDRV_SEQ_EVENT_NOTEON);
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_UNSUBSCRIBE_PORT,&sub)==0);
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT,&sub)==0);
    sub.sender.client=client; sub.dest.client=128;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT,&sub)==0);
    event.type=SNDRV_SEQ_EVENT_NOTEOFF; event.data.note.note=0;
    CHECK(write(fd,&event,sizeof event)==sizeof event);
    CHECK(ioctl(fd,0x1234,&info)<0 && errno==ENOTTY);
    CHECK(close(fd)==0);
    int status;
    CHECK(waitpid(child,&status,0)==child && WIFEXITED(status) && WEXITSTATUS(status)==0);
    close(listener); unlink(path);
    puts("sequencer ARM ABI and event socket passed");
    return 0;
}
