#!/usr/bin/env python3
"""
MINIMAL TEST - Just answer and play greeting
"""

import asyncio
import struct
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def handle_client(reader, writer):
    logger.info("📞 New connection")
    
    try:
        while True:
            # Read header
            header = await reader.readexactly(3)
            if not header:
                break
                
            msg_type = header[0]
            length = struct.unpack('>H', header[1:3])[0]
            
            payload = b''
            if length > 0:
                payload = await reader.readexactly(length)
            
            if msg_type == 0x01:  # UUID
                logger.info("UUID received - sending test audio")
                
                # Send 1 second of silence as test
                for _ in range(50):  # 50 x 20ms = 1 second
                    silence = b'\x00\x00' * 160  # 20ms at 8kHz
                    msg = struct.pack('>BH', 0x10, len(silence)) + silence
                    writer.write(msg)
                    await writer.drain()
                    await asyncio.sleep(0.02)
                
                logger.info("Test audio sent")
                
            elif msg_type == 0x00:  # Hangup
                logger.info("Hangup")
                break
                
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        writer.close()
        await writer.wait_closed()
        logger.info("Connection closed")

async def main():
    server = await asyncio.start_server(handle_client, '127.0.0.1', 9092)
    logger.info("🚀 Minimal test server on port 9092")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main()