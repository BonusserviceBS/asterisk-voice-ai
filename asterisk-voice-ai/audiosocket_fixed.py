#!/usr/bin/env python3
"""
Asterisk 22 AudioSocket FIXED Implementation
Corrected AudioSocket protocol for Asterisk 22
"""

import asyncio
import struct
import uuid
import logging
import time
import edge_tts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class AudioSocketServer:
    """Fixed AudioSocket server for Asterisk 22"""
    
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
            'audio_chunks': 0
        }
        
        try:
            # Send immediate greeting using correct AudioSocket format
            await self.send_greeting(client_id)
            
            # Main audio processing loop - FIXED PROTOCOL
            await self.process_audio_stream_fixed(client_id)
            
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
        """Send immediate greeting with CORRECT AudioSocket format"""
        greeting = "Hallo! Das ist das neue AudioSocket Real-time System. Sprechen Sie!"
        
        start = time.time()
        communicate = edge_tts.Communicate(
            greeting,
            "de-DE-ConradNeural",
            rate="+15%"
        )
        
        # Generate TTS
        audio_data = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data.extend(chunk["data"])
        
        # Convert to proper format (16-bit PCM, 8kHz for telephony)
        # For now, send raw audio - Asterisk will handle conversion
        await self.send_audio_fixed(client_id, bytes(audio_data))
        
        latency = (time.time() - start) * 1000
        logger.info(f"[{client_id}] Greeting sent in {latency:.0f}ms")
        
    async def process_audio_stream_fixed(self, client_id):
        """FIXED AudioSocket protocol processing"""
        client = self.clients[client_id]
        reader = client['reader']
        
        # AudioSocket protocol for Asterisk 22:
        # Raw audio frames, no headers!
        # Each read gives us audio data directly
        
        silence_counter = 0
        speech_buffer = bytearray()
        
        logger.info(f"[{client_id}] Starting FIXED AudioSocket processing")
        
        while True:
            try:
                # Read raw audio data (no protocol headers in Asterisk 22!)
                # Default chunk size for AudioSocket is 320 bytes (20ms at 16kHz)
                data = await reader.read(320)
                
                if not data:
                    logger.info(f"[{client_id}] No more data - connection closed")
                    break
                    
                client['audio_chunks'] += 1
                
                # Simple energy-based VAD
                energy = self.calculate_energy(data)
                
                if energy > 500:  # Speech detected
                    speech_buffer.extend(data)
                    silence_counter = 0
                    logger.debug(f"[{client_id}] Speech detected, energy: {energy}")
                    
                else:  # Silence
                    if len(speech_buffer) > 0:
                        silence_counter += 1
                        
                        # 500ms silence = end of speech
                        if silence_counter > 25:  # 25 * 20ms = 500ms
                            logger.info(f"[{client_id}] Processing speech ({len(speech_buffer)} bytes)")
                            await self.process_speech_fixed(client_id, bytes(speech_buffer))
                            speech_buffer.clear()
                            silence_counter = 0
                
                # Log progress every 2 seconds
                if client['audio_chunks'] % 100 == 0:
                    elapsed = time.time() - client['start_time']
                    logger.info(f"[{client_id}] Processed {client['audio_chunks']} chunks in {elapsed:.1f}s")
                    
            except asyncio.IncompleteReadError:
                logger.info(f"[{client_id}] Stream ended")
                break
            except Exception as e:
                logger.error(f"[{client_id}] Stream error: {e}")
                break
                
    async def process_speech_fixed(self, client_id, audio_data):
        """Process detected speech and generate response"""
        logger.info(f"[{client_id}] Generating response for {len(audio_data)} bytes audio")
        
        # Simple responses for testing
        responses = [
            "Ja, ich höre Sie sehr gut!",
            "Das AudioSocket System funktioniert perfekt.",
            "Die Latenz ist jetzt unter 200 Millisekunden.",
            "Haben Sie noch weitere Fragen?",
            "Wunderbar, das neue System läuft optimal!"
        ]
        
        import random
        response = random.choice(responses)
        
        # Generate TTS response
        start = time.time()
        communicate = edge_tts.Communicate(
            response,
            "de-DE-KatjaNeural",
            rate="+10%"
        )
        
        audio_response = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_response.extend(chunk["data"])
        
        # Send response immediately
        await self.send_audio_fixed(client_id, bytes(audio_response))
        
        latency = (time.time() - start) * 1000
        logger.info(f"[{client_id}] Response sent in {latency:.0f}ms")
        
    async def send_audio_fixed(self, client_id, audio_data):
        """Send audio to Asterisk with FIXED protocol"""
        if client_id not in self.clients:
            return
            
        writer = self.clients[client_id]['writer']
        
        # For Asterisk 22 AudioSocket: send raw audio data
        # No protocol headers needed!
        
        # Send in 20ms chunks (320 bytes at 16kHz)
        chunk_size = 320
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i+chunk_size]
            
            try:
                writer.write(chunk)
                await writer.drain()
                
                # Small delay to simulate real-time
                await asyncio.sleep(0.02)  # 20ms
                
            except Exception as e:
                logger.error(f"[{client_id}] Send error: {e}")
                break
            
    def calculate_energy(self, audio_data):
        """Calculate audio energy for VAD"""
        if len(audio_data) < 2:
            return 0
            
        # Convert bytes to 16-bit samples
        samples = []
        for i in range(0, len(audio_data)-1, 2):
            try:
                sample = struct.unpack('<h', audio_data[i:i+2])[0]
                samples.append(sample)
            except:
                continue
        
        if not samples:
            return 0
            
        # RMS energy
        rms = sum(s*s for s in samples) / len(samples)
        return int(rms ** 0.5)
        
    async def start(self):
        """Start FIXED AudioSocket server"""
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        addr = server.sockets[0].getsockname()
        logger.info(f"🔧 FIXED AudioSocket server listening on {addr[0]}:{addr[1]}")
        logger.info("✅ Ready for Asterisk 22 connections!")
        logger.info("🎯 Target latency: <200ms")
        logger.info("🐛 FIXED AudioSocket protocol for immediate response!")
        
        async with server:
            await server.serve_forever()

async def main():
    server = AudioSocketServer(port=9092)
    await server.start()

if __name__ == "__main__":
    asyncio.run(main())