#!/usr/bin/env python3
"""Quick test to verify RTP sending works"""
import socket
import struct
import time

# Create socket and bind to 10001 (where we receive)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 10001))

# Target (where we should send - Asterisk's port)
target = ('127.0.0.1', 10642)

# Build simple RTP header (PT=118 for slin16)
seq = 1000
ts = 10000
ssrc = 0x12345678

for i in range(10):
    # RTP header
    v_p_x_cc = 0x80  # V=2, no padding, no extension, no CSRC
    m_pt = 118  # PT=118 (slin16)
    header = struct.pack('!BBHII', v_p_x_cc, m_pt, seq, ts, ssrc)
    
    # Dummy payload (640 bytes = 20ms at 16kHz)
    payload = b'\x00' * 640
    
    # Send packet
    sock.sendto(header + payload, target)
    print(f"Sent RTP packet {i} to {target}")
    
    seq += 1
    ts += 320  # 20ms worth of samples at 16kHz
    time.sleep(0.02)

sock.close()
print("Done - check tcpdump if packets were sent")