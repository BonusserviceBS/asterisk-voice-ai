#!/usr/bin/env python3
"""
Asterisk 22 AudioSocket Real-time Voice AI
Using the NEW AudioSocket channel driver for true streaming!
Target: <200ms latency
"""

import asyncio
import struct
import uuid
import logging
import time
from collections import deque
import edge_tts
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class AudioSocketServer:
    """
    AudioSocket server for Asterisk 22
    Receives continuous audio stream - no waiting for silence!
    """
    
    def __init__(self, host='127.0.0.1', port=9092):
        self.host = host
        self.port = port
        self.clients = {}
        
    async def handle_client(self, reader, writer):
        """Handle AudioSocket connection from Asterisk"""
        client_id = str(uuid.uuid4())[:8]
        addr = writer.get_extra_info('peername')
        logger.info(f"[{client_id}] AudioSocket connected from {addr}")
        
        # Store client info
        self.clients[client_id] = {
            'reader': reader,
            'writer': writer,
            'start_time': time.time(),
            'audio_chunks': 0,
            'is_speaking': False,
            'buffer': bytearray()
        }
        
        try:
            # Send immediate greeting
            await self.send_greeting(client_id)
            
            # Main audio processing loop
            await self.process_audio_stream(client_id)
            
        except Exception as e:
            logger.error(f"[{client_id}] Error: {e}")
        finally:
            # Cleanup
            writer.close()
            await writer.wait_closed()
            if client_id in self.clients:
                del self.clients[client_id]
            logger.info(f"[{client_id}] Disconnected")
            
    async def send_greeting(self, client_id):
        """Send immediate greeting"""
        greeting = "Hallo, ich bin der Echtzeit Assistent. Wie kann ich helfen?"
        
        # Generate TTS immediately
        start = time.time()
        communicate = edge_tts.Communicate(
            greeting,
            "de-DE-ConradNeural",
            rate="+10%"
        )
        
        audio_data = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data.extend(chunk["data"])
        
        # Send audio to Asterisk via AudioSocket
        # AudioSocket expects: [type][length][data]
        # Type 0x01 = audio data
        await self.send_audio(client_id, bytes(audio_data))
        
        latency = (time.time() - start) * 1000
        logger.info(f"[{client_id}] Greeting sent in {latency:.0f}ms")
        
    async def process_audio_stream(self, client_id):
        """Process incoming audio stream"""
        client = self.clients[client_id]
        reader = client['reader']
        
        silence_counter = 0
        speech_buffer = bytearray()
        
        while True:
            try:
                # Read AudioSocket frame
                # Format: [type:1][length:2][data:length]
                type_byte = await reader.readexactly(1)
                if not type_byte:
                    break
                    
                frame_type = struct.unpack('!B', type_byte)[0]
                
                # Read length
                length_bytes = await reader.readexactly(2)
                length = struct.unpack('!H', length_bytes)[0]
                
                # Read data
                data = await reader.readexactly(length)
                
                if frame_type == 0x01:  # Audio frame
                    client['audio_chunks'] += 1
                    
                    # Simple VAD - check audio energy
                    energy = self.calculate_energy(data)
                    
                    if energy > 500:  # Speech detected
                        speech_buffer.extend(data)
                        silence_counter = 0
                        client['is_speaking'] = True
                        
                    else:  # Silence
                        if client['is_speaking']:
                            silence_counter += 1
                            
                            # 500ms silence = end of speech
                            if silence_counter > 25:  # 25 * 20ms = 500ms
                                # Process speech
                                await self.process_speech(client_id, bytes(speech_buffer))
                                speech_buffer.clear()
                                client['is_speaking'] = False
                                silence_counter = 0
                    
                    # Log every second
                    if client['audio_chunks'] % 50 == 0:
                        elapsed = time.time() - client['start_time']
                        logger.debug(f"[{client_id}] {client['audio_chunks']} chunks in {elapsed:.1f}s")
                        
            except asyncio.IncompleteReadError:
                break
            except Exception as e:
                logger.error(f"[{client_id}] Stream error: {e}")
                break
                
    async def process_speech(self, client_id, audio_data):
        """Process detected speech and generate response"""
        logger.info(f"[{client_id}] Processing speech ({len(audio_data)} bytes)")
        
        # For now, simple responses
        responses = [
            "Ja, das verstehe ich.",
            "Einen Moment bitte, ich prüfe das.",
            "Sehr gerne helfe ich Ihnen dabei.",
            "Ist sonst noch etwas?"
        ]
        
        import random
        response = random.choice(responses)
        
        # Generate TTS response
        start = time.time()
        communicate = edge_tts.Communicate(
            response,
            "de-DE-KatjaNeural",
            rate="+5%"
        )
        
        audio_response = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_response.extend(chunk["data"])
        
        # Send response
        await self.send_audio(client_id, bytes(audio_response))
        
        latency = (time.time() - start) * 1000
        logger.info(f"[{client_id}] Response sent in {latency:.0f}ms")
        
    async def send_audio(self, client_id, audio_data):
        """Send audio to Asterisk via AudioSocket"""
        if client_id not in self.clients:
            return
            
        writer = self.clients[client_id]['writer']
        
        # Send in chunks of 320 bytes (20ms at 16kHz)
        chunk_size = 320
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i+chunk_size]
            
            # AudioSocket frame: [type:1][length:2][data]
            frame = struct.pack('!BH', 0x01, len(chunk)) + chunk
            
            writer.write(frame)
            await writer.drain()
            
            # Small delay to simulate real-time
            await asyncio.sleep(0.02)  # 20ms
            
    def calculate_energy(self, audio_data):
        """Calculate audio energy for VAD"""
        if len(audio_data) < 2:
            return 0
            
        # Convert bytes to samples
        samples = []
        for i in range(0, len(audio_data)-1, 2):
            sample = struct.unpack('<h', audio_data[i:i+2])[0]
            samples.append(sample)
        
        if not samples:
            return 0
            
        # RMS energy
        rms = sum(s*s for s in samples) / len(samples)
        return int(rms ** 0.5)
        
    async def start(self):
        """Start AudioSocket server"""
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        addr = server.sockets[0].getsockname()
        logger.info(f"AudioSocket server listening on {addr[0]}:{addr[1]}")
        logger.info("Ready for Asterisk 22 connections!")
        logger.info("Target latency: <200ms")
        
        async with server:
            await server.serve_forever()


async def main():
    server = AudioSocketServer(port=9092)
    await server.start()


if __name__ == "__main__":
    asyncio.run(main())