/* Compile natively in the guest image; deterministic timestamps, synthetic MIDI only. */
#define SP16_QUEUE_CONTRACT
#include "/opt/sp16/seq-bridge.c"
#include <assert.h>
int main(void) {
    int fds[2]; assert(pipe(fds)==0);
    struct wire wire={0};
    const uint8_t note[]={0x90,60,100},clock[]={0xf8};
    enqueue(&wire,note,3);
    assert(transmit(&wire,fds[1],0)==0);
    assert(transmit(&wire,fds[1],319999)==0);
    assert(transmit(&wire,fds[1],320000)==1);
    enqueue(&wire,clock,1);
    assert(transmit(&wire,fds[1],639999)==0);
    assert(transmit(&wire,fds[1],640000)==1);
    assert(transmit(&wire,fds[1],960000)==1);
    assert(transmit(&wire,fds[1],1280000)==1);
    assert(transmit(&wire,fds[1],1600000)==0);
    uint8_t got[4]; assert(read(fds[0],got,4)==4);
    const uint8_t expected[]={0x90,0xf8,60,100};
    assert(!memcmp(got,expected,4));
    uint8_t full[CAPACITY]; memset(full,1,sizeof full);
    enqueue(&wire,full,CAPACITY); enqueue(&wire,note,3);
    assert(wire.dropped==3 && wire.normal.size==CAPACITY);
    assert(din_request(&wire,DIN_CAPACITY_REQUEST)==0);
    assert(din_request(&wire,0)==-ENOSPC);
    assert(transmit(&wire,fds[1],2000000)==0);
    assert(transmit(&wire,fds[1],2320000)==1);
    assert(din_request(&wire,DIN_CAPACITY_REQUEST)==1);
    assert(din_request(&wire,0)==0);
    assert(din_request(&wire,DIN_CAPACITY_REQUEST)==0);
    assert(din_request(&wire,6)==-EINVAL);
    assert(din_request(&wire,0xf20102ff)==-ENOSPC);
    assert(din_request(&wire,0xf28102ff)==-EINVAL);
    close(fds[0]); close(fds[1]);
    /* Proxy packets are 'E' then a 28-byte event; the guard must read the flags byte (packet[2]) and refuse only a
     * variable-length event. */
    uint8_t packet[1+ARM_EVENT_SIZE]={0};
    packet[0]='E';
    assert(proxy_packet_is_fixed_event(packet,sizeof packet));
    packet[2]=SND_SEQ_EVENT_LENGTH_VARIABLE;
    assert(!proxy_packet_is_fixed_event(packet,sizeof packet));
    packet[2]=0;
    assert(!proxy_packet_is_fixed_event(packet,sizeof packet-1));
    assert(!proxy_packet_is_fixed_event(packet,ARM_EVENT_SIZE));
    packet[0]='X';
    assert(!proxy_packet_is_fixed_event(packet,sizeof packet));
    /* One port number subscribed on both routes goes out on both, never only on DIN. */
    struct proxy_client shared={.fd=0,.guest_client=130};
    shared.output[0]=shared.output[1]=1;
    shared.output_port[0]=shared.output_port[1]=3;
    assert(output_routes(&shared,3)==(ROUTE_DIN|ROUTE_USB));
    assert(output_routes(&shared,4)==0);
    shared.output[1]=0;
    assert(output_routes(&shared,3)==ROUTE_DIN);
    shared.output[0]=0;
    assert(output_routes(&shared,3)==0);
    /* A leftover endpoint is dropped, a live one is not, and a path that is not a socket is left alone. */
    const char *stale="/tmp/sp16-seq-stale.sock";
    unlink(stale);
    assert(unlink_stale_socket(stale)==0);
    int bound=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0);
    assert(bound>=0);
    struct sockaddr_un stale_addr={.sun_family=AF_UNIX};
    strcpy(stale_addr.sun_path,stale);
    assert(bind(bound,(void *)&stale_addr,sizeof stale_addr)==0);
    assert(listen(bound,1)==0);
    assert(unlink_stale_socket(stale)<0 && errno==EADDRINUSE);
    close(bound);
    assert(unlink_stale_socket(stale)==0 && access(stale,F_OK)<0);
    FILE *plain=fopen(stale,"w");
    assert(plain); fclose(plain);
    assert(unlink_stale_socket(stale)<0 && errno==EEXIST && access(stale,F_OK)==0);
    unlink(stale);
    puts("UART slots 320/640/960/1280 us; realtime priority; bounded queue: PASS");
    return 0;
}
