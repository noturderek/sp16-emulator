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
    puts("UART slots 320/640/960/1280 us; realtime priority; bounded queue: PASS");
    return 0;
}
