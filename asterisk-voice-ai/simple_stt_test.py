#!/usr/bin/env python3
"""
SIMPLE STT Test - Process incoming audio and transcribe it
Focus on getting Speech-to-Text working first
"""

import asyncio
import struct
import uuid
import logging
import time
import os
from dotenv import load_dotenv
import azure.cognitiveservices.speech as speechsdk
import asyncpg
import io
import wave

# Load environment
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SimpleSTTServer:
    """Simple AudioSocket server with Azure STT only"""
    
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
            
        # TTS Config for responses
        self.tts_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        # Default to female voice - will be overridden by assistant settings
        self.tts_config.speech_synthesis_voice_name = "de-DE-SeraphinaMultilingualNeural"
        self.tts_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        
        # STT Config
        self.stt_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        self.stt_config.speech_recognition_language = "de-DE"
        
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
                        
            return None
            
        except Exception as e:
            logger.error(f"💥 Database query failed: {e}")
            return None
    
    async def handle_client(self, reader, writer):
        """Handle AudioSocket connection with STT focus"""
        client_id = str(uuid.uuid4())[:8]
        addr = writer.get_extra_info('peername')
        logger.info(f"🔌 [{client_id}] AudioSocket connected from {addr}")
        
        # Initialize client state
        self.clients[client_id] = {
            'reader': reader,
            'writer': writer,
            'start_time': time.time(),
            'audio_chunks': 0,
            'call_uuid': None,
            'greeting_sent': False,
            'assistant_data': None,
            'audio_buffer': bytearray(),  # Buffer for STT
            'recognized_count': 0,
            'last_recognition': time.time()
        }
        
        try:
            while True:
                # Read message header: type(1) + length(2)
                header = await reader.readexactly(3)
                if not header:
                    break
                    
                msg_type = header[0]
                payload_length = struct.unpack('>H', header[1:3])[0]
                
                # Read payload
                payload = b''
                if payload_length > 0:
                    payload = await reader.readexactly(payload_length)
                
                # Process message based on type
                if msg_type == 0x01:  # UUID message
                    call_uuid = payload.hex() if len(payload) == 16 else payload.decode('utf-8', errors='ignore')
                    self.clients[client_id]['call_uuid'] = call_uuid
                    logger.info(f"📞 [{client_id}] Call UUID: {call_uuid}")
                    
                    # Load assistant data
                    phone_number = "4920189098723"
                    assistant_data = await self.get_assistant_by_phone(phone_number)
                    self.clients[client_id]['assistant_data'] = assistant_data
                    
                    if assistant_data:
                        logger.info(f"🤖 [{client_id}] Assistant: {assistant_data['name']}")
                        settings = assistant_data.get('settings', {})
                        if isinstance(settings, dict):
                            voice = settings.get('providers', {}).get('tts', {}).get('voice')
                            if voice:
                                # Remove quotes if present in JSON
                                if isinstance(voice, str):
                                    voice = voice.strip('"')
                                self.tts_config.speech_synthesis_voice_name = voice
                                logger.info(f"🎤 [{client_id}] Using voice: {voice}")
                    
                    # Send greeting
                    if not self.clients[client_id]['greeting_sent']:
                        await self.send_greeting(client_id)
                        self.clients[client_id]['greeting_sent'] = True
                    
                elif msg_type == 0x10:  # Audio payload
                    await self.handle_audio(client_id, payload)
                    
                elif msg_type == 0x00:  # Hangup
                    logger.info(f"📱 [{client_id}] Hangup received")
                    break
                    
        except asyncio.IncompleteReadError:
            logger.info(f"📡 [{client_id}] Connection closed")
        except Exception as e:
            logger.error(f"💥 [{client_id}] Error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
            if client_id in self.clients:
                del self.clients[client_id]
            logger.info(f"👋 [{client_id}] Disconnected")
    
    async def send_greeting(self, client_id):
        """Send greeting with instructions"""
        client = self.clients[client_id]
        assistant_data = client.get('assistant_data')
        
        # Use ONLY the greeting from database, nothing else!
        if assistant_data and isinstance(assistant_data, dict) and assistant_data.get('greeting'):
            greeting = assistant_data['greeting']
            logger.info(f"💬 [{client_id}] Using DB greeting: '{greeting}'")
        else:
            greeting = "Hallo! Wie kann ich helfen?"
            logger.info(f"💬 [{client_id}] Using fallback greeting")
            
        await self.send_tts(client_id, greeting)
    
    async def handle_audio(self, client_id, audio_data):
        """Handle incoming audio and process with STT"""
        if client_id not in self.clients:
            return
            
        client = self.clients[client_id]
        client['audio_chunks'] += 1
        
        # Add to buffer
        client['audio_buffer'].extend(audio_data)
        
        # Process every 1.6 seconds of audio (80 chunks * 20ms)
        if len(client['audio_buffer']) >= 25600:  # 1.6s at 8kHz
            logger.info(f"🎤 [{client_id}] Processing {len(client['audio_buffer'])} bytes of audio...")
            
            # Create WAV file in memory for Azure STT
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)  # Mono
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(8000)  # 8kHz
                wav_file.writeframes(bytes(client['audio_buffer']))
            
            wav_buffer.seek(0)
            wav_data = wav_buffer.read()
            
            # Process with Azure STT
            try:
                # Create push stream
                stream = speechsdk.audio.PushAudioInputStream(
                    stream_format=speechsdk.audio.AudioStreamFormat(
                        samples_per_second=8000,
                        bits_per_sample=16,
                        channels=1,
                        wave_stream_format=speechsdk.audio.AudioStreamWaveFormat.PCM
                    )
                )
                
                # Write audio data
                stream.write(wav_data)
                stream.close()
                
                # Configure recognizer
                audio_config = speechsdk.audio.AudioConfig(stream=stream)
                recognizer = speechsdk.SpeechRecognizer(
                    speech_config=self.stt_config,
                    audio_config=audio_config
                )
                
                # Recognize speech
                result = recognizer.recognize_once()
                
                if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                    text = result.text
                    if text and len(text) > 2:
                        logger.info(f"✅ [{client_id}] RECOGNIZED: '{text}'")
                        client['recognized_count'] += 1
                        
                        # Echo back what we heard
                        response = f"Ich habe verstanden: {text}"
                        await self.send_tts(client_id, response)
                        
                elif result.reason == speechsdk.ResultReason.NoMatch:
                    logger.info(f"❓ [{client_id}] No speech detected in audio chunk")
                else:
                    logger.warning(f"⚠️ [{client_id}] STT failed: {result.reason}")
                    
            except Exception as e:
                logger.error(f"💥 [{client_id}] STT processing error: {e}")
            
            # Clear buffer
            client['audio_buffer'] = bytearray()
            
        # Log progress every second
        elif client['audio_chunks'] % 50 == 0:
            buffer_seconds = len(client['audio_buffer']) / 16000  # 8kHz * 2 bytes
            logger.info(f"📊 [{client_id}] Buffer: {buffer_seconds:.1f}s, Recognized: {client['recognized_count']} times")
    
    async def send_tts(self, client_id, text):
        """Send TTS response"""
        if client_id not in self.clients:
            return
            
        start = time.time()
        
        try:
            # Create synthesizer
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=self.tts_config, 
                audio_config=None
            )
            
            # Synthesize speech
            result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                audio_data = result.audio_data
                await self.send_audio_chunks(client_id, audio_data)
                
                latency = (time.time() - start) * 1000
                logger.info(f"🗣️ [{client_id}] TTS sent in {latency:.0f}ms")
            else:
                logger.error(f"❌ [{client_id}] TTS failed: {result.reason}")
                
        except Exception as e:
            logger.error(f"💥 [{client_id}] TTS error: {e}")
    
    async def send_audio_chunks(self, client_id, audio_data):
        """Send audio via AudioSocket protocol"""
        if client_id not in self.clients:
            return
            
        writer = self.clients[client_id]['writer']
        chunk_size = 320  # 20ms at 8kHz
        
        try:
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i+chunk_size]
                
                # Pad if needed
                if len(chunk) < chunk_size:
                    chunk = chunk + b'\x00' * (chunk_size - len(chunk))
                
                # AudioSocket message: 0x10 (audio) + length + data
                message = struct.pack('>BH', 0x10, len(chunk)) + chunk
                
                writer.write(message)
                await writer.drain()
                await asyncio.sleep(0.02)  # 20ms
                
        except Exception as e:
            logger.error(f"📡 [{client_id}] Send error: {e}")
    
    async def start(self):
        """Start Simple STT server"""
        await self.init_db()
        
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        addr = server.sockets[0].getsockname()
        logger.info(f"🚀 SIMPLE STT TEST server on {addr[0]}:{addr[1]}")
        logger.info(f"📝 Will echo back recognized speech")
        logger.info(f"⏱️ Processing audio every 1.6 seconds")
        
        async with server:
            await server.serve_forever()

async def main():
    server = SimpleSTTServer(port=9092)
    await server.start()

if __name__ == "__main__":
    asyncio.run(main())