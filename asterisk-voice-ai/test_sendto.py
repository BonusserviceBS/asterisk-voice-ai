#!/usr/bin/env python3
import socket
import struct

# Socket ohne connect()
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 10001))

# RTP header
header = struct.pack('!BBHII', 0x80, 118, 1000, 10000, 0x12345678)
payload = b'\x00' * 640

# Send mit sendto()
target = ('127.0.0.1', 19464)
sent = sock.sendto(header + payload, target)
print(f"Sent {sent} bytes from port 10001 to {target}")

sock.close()