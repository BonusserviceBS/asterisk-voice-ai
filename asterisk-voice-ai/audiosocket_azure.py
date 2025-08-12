#!/usr/bin/env python3
"""
Asterisk 22 AudioSocket with AZURE SPEECH
Real-time Voice AI with proper telephony audio
"""

import asyncio
import struct
import uuid
import logging
import time
import os
from dotenv import load_dotenv
import azure.cognitiveservices.speech as speechsdk

# Load environment
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class AzureAudioSocketServer:
    """AudioSocket server with Azure Speech Services"""
    
    def __init__(self, host='127.0.0.1', port=9092):
        self.host = host
        self.port = port
        self.clients = {}
        
        # Azure Speech setup
        speech_key = os.getenv('AZURE_SPEECH_KEY', 'YOUR_AZURE_KEY_HERE')
        speech_region = os.getenv('AZURE_SPEECH_REGION', 'germanywestcentral')
        
        if speech_key == 'YOUR_AZURE_KEY_HERE':
            logger.error("🔑 AZURE_SPEECH_KEY not set in .env file!")
            
        self.speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        
        # Use German neural voice optimized for telephony
        self.speech_config.speech_synthesis_voice_name = "de-DE-ConradNeural"
        
        # Set output format for telephony (16-bit PCM, 8kHz)
        self.speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        
    async def handle_client(self, reader, writer):
        """Handle AudioSocket connection with Azure Speech"""
        client_id = str(uuid.uuid4())[:8]
        addr = writer.get_extra_info('peername')
        logger.info(f"🔌 [{client_id}] AudioSocket connected from {addr}")
        
        # Store client info
        call_uuid = None
        self.clients[client_id] = {
            'reader': reader,
            'writer': writer,
            'start_time': time.time(),
            'audio_chunks': 0,
            'call_uuid': None,
            'greeting_sent': False
        }
        
        try:
            # Process AudioSocket protocol messages
            while True:
                # Read message header: type(1) + length(2)
                header = await reader.readexactly(3)
                if not header:
                    break
                    
                msg_type = header[0]
                payload_length = struct.unpack('>H', header[1:3])[0]
                
                # Read payload if any
                payload = b''
                if payload_length > 0:
                    payload = await reader.readexactly(payload_length)
                
                # Process message based on type
                if msg_type == 0x01:  # UUID message
                    call_uuid = payload.hex() if len(payload) == 16 else payload.decode('utf-8', errors='ignore')
                    self.clients[client_id]['call_uuid'] = call_uuid
                    logger.info(f"📞 [{client_id}] Call UUID: {call_uuid}")
                    
                    # Send Azure TTS greeting
                    if not self.clients[client_id]['greeting_sent']:
                        await self.send_azure_greeting(client_id)
                        self.clients[client_id]['greeting_sent'] = True
                    
                elif msg_type == 0x10:  # Audio payload
                    await self.process_audio(client_id, payload)
                    
                elif msg_type == 0x03:  # DTMF digit
                    dtmf = payload.decode('ascii', errors='ignore')
                    logger.info(f"🔢 [{client_id}] DTMF: {dtmf}")
                    await self.process_dtmf(client_id, dtmf)
                    
                elif msg_type == 0x00:  # Hangup
                    logger.info(f"📱 [{client_id}] Hangup received")
                    break
                    
                else:
                    logger.warning(f"❓ [{client_id}] Unknown message type: 0x{msg_type:02x}")
                
        except asyncio.IncompleteReadError:
            logger.info(f"📡 [{client_id}] Connection closed by peer")
        except Exception as e:
            logger.error(f"💥 [{client_id}] Protocol error: {e}")
        finally:
            # Cleanup
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
            if client_id in self.clients:
                del self.clients[client_id]
            logger.info(f"👋 [{client_id}] Disconnected")
            
    async def send_azure_greeting(self, client_id):
        """Send greeting using Azure Speech (German)"""
        greeting = "Hallo! Ich bin Ihr persönlicher KI-Assistent. Wie kann ich Ihnen heute helfen?"
        await self.send_azure_tts(client_id, greeting)
        
    async def process_audio(self, client_id, audio_data):
        """Process incoming audio from Asterisk"""
        if client_id not in self.clients:
            return
            
        client = self.clients[client_id]
        client['audio_chunks'] += 1
        
        # Simple energy-based VAD for 16-bit PCM
        energy = self.calculate_pcm_energy(audio_data)
        
        # Log every second of audio
        if client['audio_chunks'] % 50 == 0:  # 50 * 20ms = 1 second
            elapsed = time.time() - client['start_time']
            logger.info(f"🎧 [{client_id}] Audio: {client['audio_chunks']} chunks, energy: {energy}")
            
            # Send response every 3 seconds to keep conversation alive
            if client['audio_chunks'] % 150 == 0:  # Every 3 seconds
                await self.send_auto_response(client_id)
    
    async def process_dtmf(self, client_id, dtmf_digit):
        """Process DTMF input with Azure TTS"""
        responses = {
            '1': "Sie haben die Eins gedrückt. Kann ich Ihnen mit allgemeinen Fragen helfen?",
            '2': "Sie haben die Zwei gedrückt. Möchten Sie technischen Support?",
            '*': "Stern-Taste erkannt. Drücken Sie die Null für einen Mitarbeiter.",
            '#': "Raute-Taste erkannt. Vielen Dank für Ihren Anruf.",
        }
        
        response = responses.get(dtmf_digit, f"DTMF-Taste {dtmf_digit} wurde erkannt.")
        await self.send_azure_tts(client_id, response)
    
    async def send_auto_response(self, client_id):
        """Send automatic response with Azure TTS"""
        responses = [
            "Ich höre Sie sehr gut! Bitte sprechen Sie weiter.",
            "Das System funktioniert einwandfrei. Wie kann ich helfen?",
            "Sprechen Sie ruhig weiter, ich bin für Sie da.",
            "Ihr Audio kommt klar und deutlich an."
        ]
        
        import random
        response = random.choice(responses)
        await self.send_azure_tts(client_id, response)
    
    async def send_azure_tts(self, client_id, text):
        """Generate and send TTS using Azure Speech"""
        start = time.time()
        
        try:
            # Create synthesizer
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=self.speech_config, 
                audio_config=None  # No audio output, we get raw PCM
            )
            
            # Synthesize speech
            result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                # Get raw PCM data (8kHz, 16-bit, mono)
                audio_data = result.audio_data
                
                # Send via AudioSocket
                await self.send_audio_message(client_id, audio_data)
                
                latency = (time.time() - start) * 1000
                logger.info(f"🗣️  [{client_id}] Azure TTS '{text[:30]}...' sent in {latency:.0f}ms")
                
            else:
                logger.error(f"❌ [{client_id}] Azure TTS failed: {result.reason}")
                
        except Exception as e:
            logger.error(f"💥 [{client_id}] Azure TTS error: {e}")
    
    async def send_audio_message(self, client_id, audio_data):
        """Send audio using AudioSocket protocol (8kHz 16-bit PCM)"""
        if client_id not in self.clients:
            return
            
        writer = self.clients[client_id]['writer']
        
        # AudioSocket expects 8kHz 16-bit PCM
        # Azure gives us exactly that format!
        chunk_size = 320  # 20ms at 8kHz = 160 samples = 320 bytes
        
        try:
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i+chunk_size]
                
                # AudioSocket message: 0x10 (audio) + length + data
                length = len(chunk)
                message = struct.pack('>BH', 0x10, length) + chunk
                
                writer.write(message)
                await writer.drain()
                
                # Real-time streaming delay
                await asyncio.sleep(0.02)  # 20ms
                
        except Exception as e:
            logger.error(f"📡 [{client_id}] Send error: {e}")
    
    def calculate_pcm_energy(self, pcm_data):
        """Calculate energy for 16-bit PCM audio"""
        if len(pcm_data) < 2:
            return 0
            
        # Convert to 16-bit samples
        samples = []
        for i in range(0, len(pcm_data) - 1, 2):
            try:
                # AudioSocket uses little-endian 16-bit PCM
                sample = struct.unpack('<h', pcm_data[i:i+2])[0]
                samples.append(sample)
            except:
                continue
        
        if not samples:
            return 0
            
        # RMS energy
        rms = sum(s*s for s in samples) / len(samples)
        return int(rms ** 0.5)
        
    async def start(self):
        """Start Azure AudioSocket server"""
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        addr = server.sockets[0].getsockname()
        logger.info(f"🚀 AZURE AudioSocket server listening on {addr[0]}:{addr[1]}")
        logger.info(f"🎤 Voice: {self.speech_config.speech_synthesis_voice_name}")
        logger.info(f"🔊 Format: 8kHz 16-bit PCM (telephony optimized)")
        logger.info(f"🎯 Target latency: <200ms")
        
        async with server:
            await server.serve_forever()

async def main():
    server = AzureAudioSocketServer(port=9092)
    await server.start()

if __name__ == "__main__":
    asyncio.run(main())