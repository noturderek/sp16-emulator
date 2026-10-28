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
#define DIN_CAPACITY_REQUEST UINT32_MAX
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
static void receive_control(int listener,struct wire *wire) {
    for (int i=0;i<32;i++) {
        int fd=accept4(listener,NULL,NULL,SOCK_CLOEXEC);
        if (fd<0) break;
        struct timeval timeout={.tv_sec=0,.tv_usec=100000};
        (void)setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof timeout);
        uint32_t request;
        ssize_t n=recv(fd,&request,sizeof request,0);
        int32_t result=n==(ssize_t)sizeof request ? din_request(wire,request) : -EINVAL;
        (void)send(fd,&result,sizeof result,MSG_NOSIGNAL);
        close(fd);
    }
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
    int control=control_listener();
    if (control<0) { perror("SP16_DIN control"); return 1; }
    int count=snd_seq_poll_descriptors_count(seq,POLLIN);
    if (count<1 || count>15) return 1;
    struct pollfd pollfds[17];
    snd_seq_poll_descriptors(seq,pollfds,count,POLLIN);
    pollfds[count]=(struct pollfd){.fd=serial,.events=POLLIN};
    pollfds[count+1]=(struct pollfd){.fd=control,.events=POLLIN};
    struct wire wire={0};
    unsigned long received=0,sent=0;
    fprintf(stderr,"SP16_DIN forwarder=ready client=%d port=%d\n",snd_seq_client_id(seq),port);
    for (;;) {
        uint64_t now=mono_ns();
        if ((wire.normal.size || wire.realtime.size) && !wire.next) wire.next=now+BYTE_NS;
        uint64_t wait=wire.next ? (wire.next>now ? wire.next-now : 0) : 100000000ULL;
        struct timespec timeout={.tv_sec=0,.tv_nsec=(long)wait};
        int ready=ppoll(pollfds,count+2,&timeout,NULL);
        if (ready<0) { if (errno==EINTR) continue; break; }
        if (pollfds[count+1].revents&POLLIN) receive_control(control,&wire);
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
    close(control);
    unlink(DIN_CONTROL_PATH);
    snd_midi_event_free(encoder); snd_midi_event_free(decoder); snd_seq_close(seq);
    return 1;
}
#endif
