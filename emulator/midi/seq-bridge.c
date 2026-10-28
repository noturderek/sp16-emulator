/* Guest ALSA sequencer to a dedicated virtio MIDI stream. No firmware dependencies. */
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#define BYTE_NS 320000ULL
#define CAPACITY 8192
#define DIN_CONTROL_PATH "/tmp/sp16-root/dev/sp16-din-control"
#define SEQ_PROXY_PATH "/tmp/sp16-root/dev/sp16-seq-proxy"
#define DIN_CAPACITY_REQUEST UINT32_MAX
#define ARM_EVENT_SIZE 28
#define MAX_PROXY_CLIENTS 8
#define MAX_CONTROL_CLIENTS 16
struct bytes { uint8_t data[CAPACITY]; unsigned head, size; };
struct wire { struct bytes normal, realtime; uint64_t next; unsigned dropped; };
static int enqueue(struct wire *wire, const uint8_t *data, unsigned size) {
    if (size > CAPACITY - wire->normal.size - wire->realtime.size) {
        wire->dropped += size;
        return -ENOSPC; /* Drop a whole message, never leave its data bytes orphaned. */
    }
    for (unsigned i=0; i<size; i++) {
        struct bytes *q = data[i]>=0xf8 ? &wire->realtime : &wire->normal;
        q->data[(q->head+q->size++)%CAPACITY]=data[i];
    }
    return 0;
}
static int din_request(struct wire *wire, uint32_t request) {
    static const uint8_t realtime[]={0xf8,0xfa,0xfb,0xfc,0xfe,0xff};
    if (request==DIN_CAPACITY_REQUEST)
        return CAPACITY-wire->normal.size-wire->realtime.size;
    if (request<sizeof realtime) return enqueue(wire,&realtime[request],1);
    if ((request>>24)==0xf2 && (request&0xff)==0xff &&
        !((request>>16)&0x80) && !((request>>8)&0x80)) {
        uint8_t position[]={0xf2,(uint8_t)(request>>16),(uint8_t)(request>>8)};
        return enqueue(wire,position,3);
    }
    return -EINVAL;
}
/* One slot per completed UART byte; realtime takes precedence at the next slot. */
static int transmit(struct wire *wire, int fd, uint64_t now) {
    struct bytes *q = wire->realtime.size ? &wire->realtime : &wire->normal;
    if (!q->size) { wire->next=0; return 0; }
    if (!wire->next) wire->next=now+BYTE_NS;
    if (now<wire->next) return 0;
    if (write(fd, q->data+q->head, 1)!=1) {
        if (errno==EINTR || errno==EAGAIN) return 0;
        return -1;
    }
    q->head=(q->head+1)%CAPACITY; q->size--;
    wire->next=now+BYTE_NS; /* Never burst overdue bytes after a scheduler stall. */
    return 1;
}
#ifndef SP16_QUEUE_CONTRACT
struct proxy_client {
    int fd;
    uint8_t guest_client, input_port[2], output_port[2];
    unsigned input[2], output[2];
};
static int usb_enqueue(struct bytes *queue,const uint8_t *data,unsigned size) {
    if (size>CAPACITY-queue->size) return -ENOSPC;
    for (unsigned i=0;i<size;i++) queue->data[(queue->head+queue->size++)%CAPACITY]=data[i];
    return 0;
}
static int usb_flush(struct bytes *queue,int fd) {
    while (queue->size) {
        unsigned span=CAPACITY-queue->head;
        if (span>queue->size) span=queue->size;
        ssize_t n=write(fd,queue->data+queue->head,span);
        if (n<0 && (errno==EAGAIN || errno==EINTR)) return 0;
        if (n<=0) return -1;
        queue->head=(queue->head+(unsigned)n)%CAPACITY;
        queue->size-=(unsigned)n;
    }
    return 0;
}
static int proxy_listener(void) {
    int fd=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
    if (fd<0) return -1;
    struct sockaddr_un addr={.sun_family=AF_UNIX};
    strcpy(addr.sun_path,SEQ_PROXY_PATH);
    if (bind(fd,(struct sockaddr *)&addr,sizeof addr)<0 || listen(fd,MAX_PROXY_CLIENTS)<0) {
        close(fd); return -1;
    }
    return fd;
}
static void accept_proxies(int listener,struct proxy_client clients[MAX_PROXY_CLIENTS]) {
    for (int i=0;i<MAX_PROXY_CLIENTS;i++) {
        int fd=accept4(listener,NULL,NULL,SOCK_CLOEXEC|SOCK_NONBLOCK);
        if (fd<0) break;
        int slot=-1;
        for (int j=0;j<MAX_PROXY_CLIENTS;j++) if (clients[j].fd<0) { slot=j; break; }
        if (slot<0) { close(fd); continue; }
        clients[slot]=(struct proxy_client){.fd=fd};
    }
}
static void proxy_input(struct proxy_client *client,struct wire *wire,struct bytes *usb_queue,
                        snd_midi_event_t *decoder,int usb_fd) {
    uint8_t packet[1+ARM_EVENT_SIZE];
    for (int i=0;i<64;i++) {
        ssize_t n=recv(client->fd,packet,sizeof packet,0);
        if (n<0 && (errno==EAGAIN || errno==EINTR)) break;
        if (n<=0) { close(client->fd); client->fd=-1; break; }
        if (n==3) {
            switch (packet[0]) {
            case 'I': case 'U': {
                int route=packet[0]=='I'?0:1;
                client->input[route]=1; client->guest_client=packet[1];
                client->input_port[route]=packet[2]; break;
            }
            case 'i': case 'u': client->input[packet[0]=='i'?0:1]=0; break;
            case 'O': case 'V': {
                int route=packet[0]=='O'?0:1;
                client->output[route]=1; client->guest_client=packet[1];
                client->output_port[route]=packet[2]; break;
            }
            case 'o': case 'v': client->output[packet[0]=='o'?0:1]=0; break;
            case 'D':
                if (packet[1]==client->guest_client) {
                    for (int route=0;route<2;route++) {
                        if (client->input_port[route]==packet[2]) client->input[route]=0;
                        if (client->output_port[route]==packet[2]) client->output[route]=0;
                    }
                }
                break;
            }
        } else if (n==(ssize_t)sizeof packet && packet[0]=='E' &&
                   !(packet[2]&SND_SEQ_EVENT_LENGTH_MASK)) {
            snd_seq_event_t event;
            memset(&event,0,sizeof event);
            memcpy(&event,packet+1,ARM_EVENT_SIZE);
            uint8_t bytes[4096];
            long count=snd_midi_event_decode(decoder,bytes,sizeof bytes,&event);
            if (count>0) {
                if (client->output[0] && event.source.port==client->output_port[0])
                    (void)enqueue(wire,bytes,(unsigned)count);
                else if (client->output[1] && event.source.port==client->output_port[1] && usb_fd>=0)
                    (void)usb_enqueue(usb_queue,bytes,(unsigned)count);
            }
        }
    }
}
static void send_proxy_event(struct proxy_client clients[MAX_PROXY_CLIENTS],const snd_seq_event_t *event,
                             int source_client,int source_port,int route) {
    if (event->flags&SND_SEQ_EVENT_LENGTH_MASK) return;
    /* The fixed 32-bit ABI has a 16-byte header and a 12-byte data union. */
    for (int i=0;i<MAX_PROXY_CLIENTS;i++) if (clients[i].fd>=0 && clients[i].input[route-1]) {
        snd_seq_event_t addressed=*event;
        addressed.source.client=(uint8_t)source_client;
        addressed.source.port=(uint8_t)source_port;
        addressed.dest.client=clients[i].guest_client;
        addressed.dest.port=clients[i].input_port[route-1];
        uint8_t packet[ARM_EVENT_SIZE];
        memcpy(packet,&addressed,sizeof packet);
        if (send(clients[i].fd,packet,sizeof packet,MSG_NOSIGNAL|MSG_DONTWAIT)<0 &&
            errno!=EAGAIN) { close(clients[i].fd); clients[i].fd=-1; }
    }
}
static int control_listener(void) {
    int fd=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
    if (fd<0) return -1;
    struct sockaddr_un addr={.sun_family=AF_UNIX};
    strcpy(addr.sun_path,DIN_CONTROL_PATH);
    /* The guest root is recreated on boot; refuse a stale/live endpoint. */
    if (bind(fd,(struct sockaddr *)&addr,sizeof addr)<0 || listen(fd,16)<0) {
        close(fd); return -1;
    }
    return fd;
}
static void accept_controls(int listener,int clients[MAX_CONTROL_CLIENTS]) {
    for (int i=0;i<32;i++) {
        int fd=accept4(listener,NULL,NULL,SOCK_CLOEXEC|SOCK_NONBLOCK);
        if (fd<0) break;
        int slot=-1;
        for (int j=0;j<MAX_CONTROL_CLIENTS;j++) if (clients[j]<0) { slot=j; break; }
        if (slot<0) { close(fd); continue; }
        clients[slot]=fd;
    }
}
static void receive_control(int *fd,struct wire *wire) {
    uint32_t request;
    ssize_t n=recv(*fd,&request,sizeof request,MSG_DONTWAIT);
    if (n<0 && (errno==EAGAIN || errno==EINTR)) return;
    if (n>0) {
        int32_t result=n==(ssize_t)sizeof request ? din_request(wire,request) : -EINVAL;
        (void)send(*fd,&result,sizeof result,MSG_NOSIGNAL|MSG_DONTWAIT);
    }
    close(*fd);
    *fd=-1;
}
static uint64_t mono_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec*1000000000ULL+(uint64_t)t.tv_nsec;
}
int main(void) {
    snd_seq_t *seq=NULL;
    if (snd_seq_open(&seq,"default",SND_SEQ_OPEN_DUPLEX,SND_SEQ_NONBLOCK)<0) {
        fprintf(stderr,"SP16_DIN sequencer=open-failed\n"); return 1;
    }
    snd_seq_set_client_name(seq,"snd-din-midi");
    int port=snd_seq_create_simple_port(seq,"DIN MIDI",
        SND_SEQ_PORT_CAP_READ|SND_SEQ_PORT_CAP_WRITE|SND_SEQ_PORT_CAP_SUBS_READ|SND_SEQ_PORT_CAP_SUBS_WRITE,
        SND_SEQ_PORT_TYPE_MIDI_GENERIC|SND_SEQ_PORT_TYPE_HARDWARE);
    if (port<0) return 1;
    snd_midi_event_t *encoder=NULL,*decoder=NULL;
    if (snd_midi_event_new(4096,&encoder)<0 || snd_midi_event_new(4096,&decoder)<0) return 1;
    snd_midi_event_no_status(decoder,1);
    int serial=open("/dev/sp16-din",O_RDWR|O_NONBLOCK|O_CLOEXEC);
    if (serial<0) { perror("SP16_DIN virtio"); return 1; }
    int usb=open("/dev/sp16-usb-midi",O_RDWR|O_NONBLOCK|O_CLOEXEC);
    if (usb<0) { perror("SP16_USB_MIDI virtio"); return 1; }
    snd_midi_event_t *usb_encoder=NULL;
    if (snd_midi_event_new(4096,&usb_encoder)<0) return 1;
    int control=control_listener();
    if (control<0) { perror("SP16_DIN control"); return 1; }
    int proxy=proxy_listener();
    if (proxy<0) { perror("SP16_DIN sequencer proxy"); return 1; }
    int count=snd_seq_poll_descriptors_count(seq,POLLIN);
    if (count<1 || count>15) return 1;
    struct pollfd pollfds[20+MAX_PROXY_CLIENTS+MAX_CONTROL_CLIENTS];
    snd_seq_poll_descriptors(seq,pollfds,count,POLLIN);
    pollfds[count]=(struct pollfd){.fd=serial,.events=POLLIN};
    pollfds[count+1]=(struct pollfd){.fd=control,.events=POLLIN};
    pollfds[count+2]=(struct pollfd){.fd=proxy,.events=POLLIN};
    pollfds[count+3]=(struct pollfd){.fd=usb,.events=POLLIN};
    struct proxy_client clients[MAX_PROXY_CLIENTS];
    for (int i=0;i<MAX_PROXY_CLIENTS;i++) clients[i]=(struct proxy_client){.fd=-1};
    int controls[MAX_CONTROL_CLIENTS];
    for (int i=0;i<MAX_CONTROL_CLIENTS;i++) controls[i]=-1;
    struct wire wire={0};
    struct bytes usb_output={0};
    unsigned long received=0,sent=0;
    fprintf(stderr,"SP16_DIN forwarder=ready client=%d port=%d\n",snd_seq_client_id(seq),port);
    for (;;) {
        uint64_t now=mono_ns();
        if ((wire.normal.size || wire.realtime.size) && !wire.next) wire.next=now+BYTE_NS;
        uint64_t wait=wire.next ? (wire.next>now ? wire.next-now : 0) : 100000000ULL;
        struct timespec timeout={.tv_sec=0,.tv_nsec=(long)wait};
        pollfds[count+3].events=POLLIN|(usb_output.size?POLLOUT:0);
        for (int i=0;i<MAX_PROXY_CLIENTS;i++)
            pollfds[count+4+i]=(struct pollfd){.fd=clients[i].fd,.events=POLLIN};
        for (int i=0;i<MAX_CONTROL_CLIENTS;i++)
            pollfds[count+4+MAX_PROXY_CLIENTS+i]=(struct pollfd){.fd=controls[i],.events=POLLIN};
        int ready=ppoll(pollfds,count+4+MAX_PROXY_CLIENTS+MAX_CONTROL_CLIENTS,&timeout,NULL);
        if (ready<0) { if (errno==EINTR) continue; break; }
        if (pollfds[count+1].revents&POLLIN) accept_controls(control,controls);
        for (int i=0;i<MAX_CONTROL_CLIENTS;i++)
            if (controls[i]>=0 && (pollfds[count+4+MAX_PROXY_CLIENTS+i].revents&(POLLIN|POLLHUP|POLLERR)))
                receive_control(&controls[i],&wire);
        if (pollfds[count+2].revents&POLLIN) accept_proxies(proxy,clients);
        for (int i=0;i<MAX_PROXY_CLIENTS;i++)
            if (clients[i].fd>=0 && (pollfds[count+4+i].revents&(POLLIN|POLLHUP|POLLERR)))
                proxy_input(&clients[i],&wire,&usb_output,decoder,usb);
        if (pollfds[count+3].revents&POLLIN) {
            uint8_t buffer[4096];
            ssize_t n=read(usb,buffer,sizeof buffer);
            for (ssize_t i=0;i<n;i++) {
                snd_seq_event_t event;
                snd_seq_ev_clear(&event);
                if (snd_midi_event_encode_byte(usb_encoder,buffer[i],&event)==1)
                    send_proxy_event(clients,&event,129,0,2);
            }
        }
        if (pollfds[count+3].revents&(POLLERR|POLLNVAL)) break;
        if (usb_flush(&usb_output,usb)<0) break;
        if (pollfds[count].revents&POLLIN) {
            uint8_t buffer[4096];
            ssize_t n=read(serial,buffer,sizeof buffer);
            if (n>0 && !received) fprintf(stderr,"SP16_DIN input=active\n");
            if (n>0) received+=(unsigned long)n;
            for (ssize_t i=0;i<n;i++) {
                snd_seq_event_t event;
                snd_seq_ev_clear(&event);
                if (snd_midi_event_encode_byte(encoder,buffer[i],&event)==1) {
                    snd_seq_ev_set_source(&event,port);
                    snd_seq_ev_set_subs(&event);
                    snd_seq_ev_set_direct(&event);
                    snd_seq_event_output_direct(seq,&event);
                    send_proxy_event(clients,&event,snd_seq_client_id(seq),port,1);
                }
            }
        }
        if (pollfds[count].revents&(POLLERR|POLLNVAL)) break;
        /* Bound batches so event floods cannot starve UART input or output. */
        for (int i=0;i<64;i++) {
            snd_seq_event_t *event=NULL;
            if (snd_seq_event_input(seq,&event)<0) break;
            uint8_t buffer[4096];
            long n=snd_midi_event_decode(decoder,buffer,sizeof buffer,event);
            if (n>0) enqueue(&wire,buffer,(unsigned)n);
            snd_seq_free_event(event);
        }
        int result=transmit(&wire,serial,mono_ns());
        if (result<0) break;
        if (result>0 && !sent++) fprintf(stderr,"SP16_DIN output=active\n");
    }
    fprintf(stderr,"SP16_DIN closed input=%lu output=%lu dropped=%u\n",received,sent,wire.dropped);
    close(serial);
    close(usb);
    close(control);
    close(proxy);
    unlink(DIN_CONTROL_PATH);
    unlink(SEQ_PROXY_PATH);
    for (int i=0;i<MAX_PROXY_CLIENTS;i++) if (clients[i].fd>=0) close(clients[i].fd);
    for (int i=0;i<MAX_CONTROL_CLIENTS;i++) if (controls[i]>=0) close(controls[i]);
    snd_midi_event_free(encoder); snd_midi_event_free(decoder);
    snd_midi_event_free(usb_encoder); snd_seq_close(seq);
    return 1;
}
#endif
