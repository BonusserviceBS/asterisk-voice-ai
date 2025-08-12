#!/usr/bin/env python3
"""
Production AudioSocket with REAL Assistant Data from Database
Loads greeting, voice, and system prompt from teli24 database
"""

import asyncio
import struct
import uuid
import logging
import time
import os
import json
from dotenv import load_dotenv
import azure.cognitiveservices.speech as speechsdk
import asyncpg

# Load environment
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ProductionAudioSocketServer:
    """Production AudioSocket server with database integration"""
    
    def __init__(self, host='127.0.0.1', port=9092):
        self.host = host
        self.port = port
        self.clients = {}
        self.db_pool = None
        
        # Azure Speech setup
        speech_key = os.getenv('AZURE_SPEECH_KEY')
        speech_region = os.getenv('AZURE_SPEECH_REGION', 'germanywestcentral')
        
        if not speech_key:
            logger.error("🔑 AZURE_SPEECH_KEY not set!")
            
        self.speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        
        # Will be set per assistant
        self.speech_config.speech_synthesis_voice_name = "de-DE-ConradNeural"
        
        # Set output format for telephony (16-bit PCM, 8kHz)
        self.speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        
    async def init_db(self):
        """Initialize database connection pool"""
        try:
            self.db_pool = await asyncpg.create_pool(
                host=os.getenv('DB_HOST', '10.0.0.5'),
                port=int(os.getenv('DB_PORT', '5432')),
                database=os.getenv('DB_NAME', 'teli24_development'),
                user=os.getenv('DB_USER', 'teli24_user'),
                password=os.getenv('DB_PASSWORD'),
                min_size=1,
                max_size=3
            )
            logger.info("📊 Database connection pool initialized")
        except Exception as e:
            logger.error(f"💥 Database connection failed: {e}")
            
    async def get_assistant_by_phone(self, phone_number):
        """Get assistant data by phone number"""
        if not self.db_pool:
            return None
            
        try:
            # Try different phone number formats
            formats = [
                phone_number,
                f"+{phone_number}" if not phone_number.startswith('+') else phone_number,
                phone_number.replace('+49', '49') if phone_number.startswith('+49') else f"49{phone_number}"
            ]
            
            async with self.db_pool.acquire() as conn:
                for fmt in formats:
                    query = """
                    SELECT a.name, a.greeting, a."systemPrompt", a.settings 
                    FROM "Assistant" a 
                    JOIN "PhoneNumber" p ON a.id = p."assignedToAssistantId" 
                    WHERE p.number = $1
                    """
                    result = await conn.fetchrow(query, fmt)
                    if result:
                        return {
                            'name': result['name'],
                            'greeting': result['greeting'],
                            'system_prompt': result['systemPrompt'],
                            'settings': result['settings']
                        }
                        
            logger.warning(f"📱 No assistant found for phone number: {phone_number}")
            return None
            
        except Exception as e:
            logger.error(f"💥 Database query failed: {e}")
            return None
        
    async def handle_client(self, reader, writer):
        """Handle AudioSocket connection with real assistant data"""
        client_id = str(uuid.uuid4())[:8]
        addr = writer.get_extra_info('peername')
        logger.info(f"🔌 [{client_id}] AudioSocket connected from {addr}")
        
        # Store client info
        self.clients[client_id] = {
            'reader': reader,
            'writer': writer,
            'start_time': time.time(),
            'audio_chunks': 0,
            'call_uuid': None,
            'greeting_sent': False,
            'assistant_data': None,
            'phone_number': None
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
                    
                    # Try to determine phone number from Asterisk context
                    # For now, use default - could be enhanced with SIP headers
                    phone_number = "4920189098723"  # Default for testing
                    self.clients[client_id]['phone_number'] = phone_number
                    
                    # Load assistant data
                    assistant_data = await self.get_assistant_by_phone(phone_number)
                    self.clients[client_id]['assistant_data'] = assistant_data
                    
                    if assistant_data:
                        logger.info(f"🤖 [{client_id}] Assistant: {assistant_data['name']}")
                        
                        # Configure voice from assistant settings
                        settings = assistant_data.get('settings', {})
                        if isinstance(settings, dict):  # Fix: Check if settings is dict
                            voice = settings.get('providers', {}).get('tts', {}).get('voice')
                            if voice:
                                self.speech_config.speech_synthesis_voice_name = voice
                                logger.info(f"🎤 [{client_id}] Voice: {voice}")
                        else:
                            logger.warning(f"⚠️ [{client_id}] Settings is not a dict: {type(settings)}")
                    
                    # Send real assistant greeting
                    if not self.clients[client_id]['greeting_sent']:
                        await self.send_assistant_greeting(client_id)
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
            
    async def send_assistant_greeting(self, client_id):
        """Send REAL assistant greeting from database"""
        client = self.clients[client_id]
        assistant_data = client.get('assistant_data')
        
        if assistant_data and isinstance(assistant_data, dict) and assistant_data.get('greeting'):
            # Use real assistant greeting
            greeting = assistant_data['greeting']
            logger.info(f"💬 [{client_id}] Using assistant greeting: '{greeting}'")
        else:
            # Fallback greeting
            greeting = "Hallo! Wie kann ich Ihnen helfen?"
            logger.info(f"💬 [{client_id}] Using fallback greeting")
            
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
            
            # Send response every 5 seconds
            if client['audio_chunks'] % 250 == 0:  # Every 5 seconds
                await self.send_status_response(client_id)
    
    async def process_dtmf(self, client_id, dtmf_digit):
        """Process DTMF input with personalized responses"""
        client = self.clients[client_id]
        assistant_data = client.get('assistant_data')
        assistant_name = assistant_data['name'] if assistant_data else "Assistant"
        
        responses = {
            '1': f"Gerne! Ich bin {assistant_name} und helfe Ihnen mit allgemeinen Fragen.",
            '2': f"Perfekt! {assistant_name} kann Ihnen mit technischen Fragen helfen.",
            '*': f"Ich bin {assistant_name}. Drücken Sie 0 für einen menschlichen Mitarbeiter.",
            '#': f"Vielen Dank für Ihren Anruf! {assistant_name} wünscht Ihnen einen schönen Tag.",
        }
        
        response = responses.get(dtmf_digit, f"Sie haben {dtmf_digit} gedrückt. Wie kann ich helfen?")
        await self.send_azure_tts(client_id, response)
    
    async def send_status_response(self, client_id):
        """Send status response with assistant personality"""
        client = self.clients[client_id]
        assistant_data = client.get('assistant_data')
        
        if assistant_data and isinstance(assistant_data, dict):
            name = assistant_data.get('name', 'Assistant')
            system_prompt = assistant_data.get('system_prompt', '')
            
            # Generate response based on system prompt personality
            if system_prompt and 'hilfreich' in system_prompt.lower():
                responses = [
                    f"Ich höre Sie sehr gut! {name} ist bereit zu helfen.",
                    f"Sprechen Sie ruhig weiter. {name} hört aufmerksam zu.",
                    f"Das System funktioniert perfekt. {name} wartet auf Ihre Frage."
                ]
            else:
                responses = [
                    f"Audio kommt klar an. {name} ist für Sie da.",
                    f"Verbindung ist stabil. Wie kann {name} helfen?",
                    f"System läuft optimal. {name} wartet auf Ihre Nachricht."
                ]
        else:
            responses = [
                "System funktioniert einwandfrei.",
                "Audio kommt klar und deutlich an.",
                "Sprechen Sie weiter, ich bin für Sie da."
            ]
        
        import random
        response = random.choice(responses)
        await self.send_azure_tts(client_id, response)
    
    async def send_azure_tts(self, client_id, text):
        """Generate and send TTS using Azure Speech"""
        start = time.time()
        
        try:
            # Create synthesizer with current voice settings
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
                logger.info(f"🗣️  [{client_id}] Azure TTS '{text[:50]}...' sent in {latency:.0f}ms")
                
            else:
                logger.error(f"❌ [{client_id}] Azure TTS failed: {result.reason}")
                
        except Exception as e:
            logger.error(f"💥 [{client_id}] Azure TTS error: {e}")
    
    async def send_audio_message(self, client_id, audio_data):
        """Send audio using AudioSocket protocol (8kHz 16-bit PCM)"""
        if client_id not in self.clients:
            return
            
        writer = self.clients[client_id]['writer']
        
        # AudioSocket expects 8kHz 16-bit PCM - Azure gives us exactly that!
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
        """Start Production AudioSocket server"""
        # Initialize database
        await self.init_db()
        
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        addr = server.sockets[0].getsockname()
        logger.info(f"🚀 PRODUCTION AudioSocket server listening on {addr[0]}:{addr[1]}")
        logger.info(f"📊 Database integration: ENABLED")
        logger.info(f"🎤 Dynamic voice selection: ENABLED")
        logger.info(f"💬 Real assistant greetings: ENABLED")
        logger.info(f"🎯 Target latency: <200ms")
        
        async with server:
            await server.serve_forever()

async def main():
    server = ProductionAudioSocketServer(port=9092)
    await server.start()

if __name__ == "__main__":
    asyncio.run(main())