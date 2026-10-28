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
_Static_assert(SNDRV_SEQ_IOCTL_DELETE_PORT==0x40a45321,"ARM sequencer delete port ioctl");
_Static_assert(SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT==0x40505330,"ARM sequencer subscribe ioctl");

/* A full listen backlog makes the guest's nonblocking connect fail (EAGAIN; an in-flight attempt reports EINPROGRESS)
 * before the proxy accepts. Treating either as a connected socket hands the app an fd whose first write fails and drops
 * the proxy client, so the open waits for the connection instead (pt-i44.81). Old code failed the open here. */
static void connect_retry_case(void) {
    const char *path="/tmp/sp16-seq-backlog.sock";
    unlink(path);
    int listener=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,0);
    CHECK(listener>=0);
    struct sockaddr_un address={.sun_family=AF_UNIX};
    strcpy(address.sun_path,path);
    CHECK(bind(listener,(void *)&address,sizeof address)==0);
    CHECK(listen(listener,1)==0);
    int fillers[4], filled=0, full=0;
    while (filled<4 && !full) { /* fill the backlog, and prove it is full rather than assuming it */
        int fd=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,0);
        CHECK(fd>=0);
        if (connect(fd,(void *)&address,sizeof address)==0) fillers[filled++]=fd;
        else { CHECK(errno==EAGAIN); close(fd); full=1; }
    }
    CHECK(full);
    int started[2];
    CHECK(pipe(started)==0);
    pid_t child=fork();
    CHECK(child>=0);
    if (!child) { /* stands in for the app: open the sequencer and send one event as the first thing it does */
        CHECK(setenv("SP16_SEQ_PROXY",path,1)==0);
        CHECK(write(started[1],"x",1)==1); /* the parent must not free the backlog before this connect */
        int fd=open("/dev/snd/seq",O_RDWR|O_NONBLOCK);
        if (fd<0) _exit(3);
        struct snd_seq_event event={.type=SNDRV_SEQ_EVENT_NOTEOFF};
        if (write(fd,&event,sizeof event)!=sizeof event) _exit(4);
        _exit(0);
    }
    close(started[1]);
    char ready_byte;
    CHECK(read(started[0],&ready_byte,1)==1);
    close(started[0]);
    usleep(150000); /* the guest is inside its connect by now, and its first attempt sees the full backlog */
    /* Accept after the guest is inside its wait: each accepted filler frees one backlog slot until its own connect
     * completes. The accepted sockets stay open so the guest's write still has a live peer. */
    int peers[8], count=0;
    for (int round=0; round<400 && count<filled+1; round++) {
        int peer=accept4(listener,NULL,NULL,SOCK_CLOEXEC|SOCK_NONBLOCK);
        if (peer>=0) { if (count<8) peers[count++]=peer; else close(peer); }
        else usleep(5000);
    }
    int status;
    CHECK(waitpid(child,&status,0)==child && WIFEXITED(status) && WEXITSTATUS(status)==0);
    for (int i=0;i<count;i++) close(peers[i]);
    for (int i=0;i<filled;i++) close(fillers[i]);
    close(listener);
    unlink(path);
}

int main(void) {
    connect_retry_case();
    const char *path="/tmp/sp16-seq-contract.sock";
    const char *mode_path="/tmp/sp16-seq-usb-enabled";
    unlink(path);
    unlink(mode_path);
    int mode=open(mode_path,O_CREAT|O_WRONLY,0600);
    CHECK(mode>=0 && close(mode)==0);
    CHECK(setenv("SP16_USB_MIDI_MODE_PATH",mode_path,1)==0);
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
        CHECK(recv(peer,command,sizeof command,0)==sizeof command && command[0]=='I' && command[1]==130 && command[2]==0);
        struct snd_seq_event incoming={.type=SNDRV_SEQ_EVENT_NOTEON};
        incoming.source.client=128; incoming.source.port=0;
        incoming.dest.client=130; incoming.dest.port=0;
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
        /* The event reaches the proxy addressed to the port the guest subscribed to (DIN 128:0), unchanged: a write
         * path that lost or moved the destination would still carry the right type (pt-i44.81). */
        CHECK(outgoing.dest.client==128 && outgoing.dest.port==0);
        CHECK(recv(peer,command,sizeof command,0)==sizeof command && command[0]=='U' && command[1]==130 && command[2]==1);
        incoming.source.client=129; incoming.dest.port=1;
        CHECK(send(peer,&incoming,sizeof incoming,0)==sizeof incoming);
        CHECK(recv(peer,command,sizeof command,0)==sizeof command && command[0]=='V' && command[2]==1);
        CHECK(recv(peer,packet,sizeof packet,0)==sizeof packet && packet[0]=='E');
        CHECK(recv(peer,command,sizeof command,0)==sizeof command && command[0]=='D' && command[2]==0);
        CHECK(recv(peer,command,sizeof command,0)==sizeof command && command[0]=='D' && command[2]==1);
        CHECK(recv(peer,command,sizeof command,0)==sizeof command && command[0]=='U' && command[2]==1);
        close(peer); close(listener); _exit(0);
    }
    int fd=open("/dev/snd/seq",O_RDWR|O_NONBLOCK);
    CHECK(fd>=0);
    int version=0,client=0;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_PVERSION,&version)==0 && version==SNDRV_SEQ_VERSION);
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_CLIENT_ID,&client)==0 && client==130);
    struct snd_seq_running_info running={0};
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_RUNNING_MODE,&running)==0 && running.cpu_mode==4);
    struct snd_seq_system_info system={0};
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SYSTEM_INFO,&system)==0 && system.clients==256);
    struct snd_seq_client_info info={.client=-1};
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_QUERY_NEXT_CLIENT,&info)==0 && info.client==128 && !strcmp(info.name,"snd-din-midi"));
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_QUERY_NEXT_CLIENT,&info)==0 && info.client==129 && !strcmp(info.name,"g_midi"));
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
    port.addr.client=129; port.addr.port=255;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_QUERY_NEXT_PORT,&port)==0 && port.addr.client==129 && !strcmp(port.name,"USB MIDI Gadget"));
    port.addr.client=0; /* ALSA's simple-port creation leaves this unset. */
    strcpy(port.name,"JuceMidiInput");
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_CREATE_PORT,&port)==0 && port.addr.client==client && port.addr.port==0);
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_CREATE_PORT,&port)==0 && port.addr.port==1);
    struct snd_seq_port_subscribe sub={0};
    sub.sender.client=128; sub.dest.client=client;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT,&sub)==0);
    struct pollfd pfd={.fd=fd,.events=POLLIN};
    CHECK(poll(&pfd,1,1000)==1 && (pfd.revents&POLLIN));
    struct snd_seq_event event={0};
    CHECK(read(fd,&event,sizeof event)==sizeof event && event.type==SNDRV_SEQ_EVENT_NOTEON);
    /* The DIN dest rewrite the proxy does: the event arrives addressed to this client and the input port it registered
     * ('I' 130 0 above) from the DIN source, not to the address the wire carried (pt-i44.81). */
    CHECK(event.dest.client==client && event.dest.port==0);
    CHECK(event.source.client==128 && event.source.port==0);
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_UNSUBSCRIBE_PORT,&sub)==0);
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT,&sub)==0);
    sub.sender.client=client; sub.dest.client=128;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT,&sub)==0);
    event.type=SNDRV_SEQ_EVENT_NOTEOFF; event.data.note.note=0;
    event.dest.client=128; event.dest.port=0; /* the DIN port this client subscribed to */
    CHECK(write(fd,&event,sizeof event)==sizeof event);
    sub.sender.client=129; sub.dest.client=client; sub.dest.port=1;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT,&sub)==0);
    CHECK(poll(&pfd,1,1000)==1 && (pfd.revents&POLLIN));
    CHECK(read(fd,&event,sizeof event)==sizeof event && event.source.client==129);
    sub.sender.client=client; sub.sender.port=1; sub.dest.client=129; sub.dest.port=0;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT,&sub)==0);
    event.source.client=client; event.source.port=1;
    CHECK(write(fd,&event,sizeof event)==sizeof event);
    port.addr.client=client; port.addr.port=0;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_DELETE_PORT,&port)==0);
    port.addr.port=1;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_DELETE_PORT,&port)==0);
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_CREATE_PORT,&port)==0 && port.addr.port==0);
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_CREATE_PORT,&port)==0 && port.addr.port==1);
    sub.sender.client=129; sub.sender.port=0; sub.dest.client=client; sub.dest.port=1;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_SUBSCRIBE_PORT,&sub)==0);
    pid_t mode_child=fork();
    CHECK(mode_child>=0);
    if (!mode_child) { execl("/bin/sh","sh","-c","/root/pdj/usb_gadget.sh mass_storage probe",(char *)NULL); _exit(2); }
    int mode_status;
    CHECK(waitpid(mode_child,&mode_status,0)==mode_child && WIFEXITED(mode_status) && WEXITSTATUS(mode_status)==0);
    CHECK(access(mode_path,F_OK)<0 && errno==ENOENT);
    info.client=128;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_QUERY_NEXT_CLIENT,&info)<0 && errno==ENOENT);
    mode_child=fork();
    CHECK(mode_child>=0);
    if (!mode_child) { execl("/bin/sh","sh","-c","/root/pdj/usb_gadget.sh midi probe",(char *)NULL); _exit(2); }
    CHECK(waitpid(mode_child,&mode_status,0)==mode_child && WIFEXITED(mode_status) && WEXITSTATUS(mode_status)==0);
    info.client=128;
    CHECK(ioctl(fd,SNDRV_SEQ_IOCTL_QUERY_NEXT_CLIENT,&info)==0 && info.client==129);
    CHECK(ioctl(fd,0x1234,&info)<0 && errno==ENOTTY);
    CHECK(close(fd)==0);
    int status;
    CHECK(waitpid(child,&status,0)==child && WIFEXITED(status) && WEXITSTATUS(status)==0);
    close(listener); unlink(path); unlink(mode_path);
    puts("sequencer ARM ABI and event socket passed");
    return 0;
}
