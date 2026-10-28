/* USB faults must not stop the independent DIN and sequencer proxy routes. */
#define main bridge_main
#include "/opt/sp16/seq-bridge.c"
#undef main
#include <assert.h>
#include <stdlib.h>

static void check_din_and_proxy(struct proxy_client clients[MAX_PROXY_CLIENTS],int peer) {
    int din[2];
    assert(pipe(din)==0);
    struct wire wire={0};
    assert(din_request(&wire,0)==0); /* MIDI clock, on the paced DIN route. */
    assert(transmit(&wire,din[1],0)==0);
    assert(transmit(&wire,din[1],BYTE_NS)==1);
    uint8_t byte=0;
    assert(read(din[0],&byte,1)==1 && byte==0xf8);
    close(din[0]); close(din[1]);

    snd_seq_event_t event;
    snd_seq_ev_clear(&event);
    event.type=SND_SEQ_EVENT_CLOCK;
    send_proxy_event(clients,&event,42,1,1);
    snd_seq_event_t delivered;
    assert(recv(peer,&delivered,ARM_EVENT_SIZE,0)==ARM_EVENT_SIZE);
    assert(delivered.type==SND_SEQ_EVENT_CLOCK);
    assert(delivered.source.client==42 && delivered.source.port==1);
    assert(delivered.dest.client==7 && delivered.dest.port==3);
}

int main(void) {
    /* publish_usb_peer: the g_midi marker appears once the USB transport is open, never for an absent one (pt-i44.35). */
    setenv("SP16_USB_MIDI_MODE_PATH","/tmp/run/usb-midi-enabled",1);
    unlink("/tmp/run/usb-midi-enabled");
    publish_usb_peer(-1);
    assert(access("/tmp/run/usb-midi-enabled",F_OK)<0 && errno==ENOENT);
    publish_usb_peer(0);
    assert(access("/tmp/run/usb-midi-enabled",F_OK)==0);
    unlink("/tmp/run/usb-midi-enabled");
    unsetenv("SP16_USB_MIDI_MODE_PATH");

    int peers[2];
    assert(socketpair(AF_UNIX,SOCK_SEQPACKET,0,peers)==0);
    struct proxy_client clients[MAX_PROXY_CLIENTS];
    for (int i=0;i<MAX_PROXY_CLIENTS;i++) clients[i]=(struct proxy_client){.fd=-1};
    clients[0]=(struct proxy_client){.fd=peers[0],.guest_client=7,.input_port={3,0},.input={1,0}};
    struct bytes queue={0};
    const uint8_t note[]={0x90,60,100};

    int usb=open_usb("/tmp/run/absent-usb-midi");
    assert(usb==-1);
    service_usb(&usb,&queue,NULL,clients,POLLIN);
    check_din_and_proxy(clients,peers[1]);

    const short faults[]={POLLERR,POLLNVAL};
    for (unsigned i=0;i<sizeof faults/sizeof faults[0];i++) {
        int fds[2]; assert(pipe(fds)==0);
        usb=fds[1];
        assert(usb_enqueue(&queue,note,sizeof note)==0);
        service_usb(&usb,&queue,NULL,clients,faults[i]);
        assert(usb==-1 && queue.size==0);
        assert(fcntl(fds[1],F_GETFD)==-1 && errno==EBADF);
        close(fds[0]);
        check_din_and_proxy(clients,peers[1]);
    }

    int fds[2]; assert(pipe(fds)==0);
    usb=fds[0]; /* A read-only route forces usb_flush to fail. */
    assert(usb_enqueue(&queue,note,sizeof note)==0);
    service_usb(&usb,&queue,NULL,clients,0);
    assert(usb==-1 && queue.size==0);
    assert(fcntl(fds[0],F_GETFD)==-1 && errno==EBADF);
    close(fds[1]);
    check_din_and_proxy(clients,peers[1]);

    close(peers[0]); close(peers[1]);
    puts("USB absent, poll faults and flush failure leave DIN and proxy active: PASS");
    return 0;
}
