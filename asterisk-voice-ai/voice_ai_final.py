#!/usr/bin/env python3
"""
FINAL Production Voice AI - Simple and Working
Uses assistant data from database, responds intelligently
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
import aiohttp
import io
import wave

# Load environment
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Hardcoded Mistral API key for testing
MISTRAL_API_KEY = "eGKlq7RFBQLiohBetRmuL5PNZxuLkrpz"

class VoiceAIFinal:
    """Final production voice AI system"""
    
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
        
        # STT Config
        self.stt_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        self.stt_config.speech_recognition_language = "de-DE"
        
        # Store Azure credentials for per-client TTS config
        self.azure_key = speech_key
        self.azure_region = speech_region
        
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
    
    async def process_with_ai(self, text: str, system_prompt: str = None) -> str:
        """Process text with Mistral AI"""
        try:
            start = time.time()
            
            headers = {
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            }
            
            # Use system prompt from assistant or default
            if not system_prompt:
                system_prompt = "Du bist ein hilfreicher KI-Assistent für Teli24. Antworte kurz und präzise auf Deutsch."
            
            payload = {
                "model": "mistral-tiny",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}
                ],
                "temperature": 0.7,
                "max_tokens": 100  # Keep responses short for phone
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post("https://api.mistral.ai/v1/chat/completions", 
                                       headers=headers, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        reply = data['choices'][0]['message']['content']
                        latency = (time.time() - start) * 1000
                        logger.info(f"🤖 AI response in {latency:.0f}ms: '{reply[:50]}...'")
                        return reply
                    else:
                        logger.error(f"❌ Mistral API error: {response.status}")
                        return "Entschuldigung, ich konnte das nicht verarbeiten."
                        
        except Exception as e:
            logger.error(f"💥 AI error: {e}")
            return "Es tut mir leid, es gab einen technischen Fehler."
    
    async def handle_client(self, reader, writer):
        """Handle AudioSocket connection"""
        client_id = str(uuid.uuid4())[:8]
        addr = writer.get_extra_info('peername')
        logger.info(f"🔌 [{client_id}] AudioSocket connected from {addr}")
        
        # Initialize client state
        self.clients[client_id] = {
            'reader': reader,
            'writer': writer,
            'start_time': time.time(),
            'audio_chunks': 0,
            'audio_buffer': bytearray(),
            'assistant_data': None,
            'tts_config': None,
            'conversation': []
        }
        
        try:
            while True:
                # Read message header
                header = await reader.readexactly(3)
                if not header:
                    break
                    
                msg_type = header[0]
                payload_length = struct.unpack('>H', header[1:3])[0]
                
                # Read payload
                payload = b''
                if payload_length > 0:
                    payload = await reader.readexactly(payload_length)
                
                # Process message
                if msg_type == 0x01:  # UUID message
                    await self.handle_uuid(client_id, payload)
                    
                elif msg_type == 0x10:  # Audio payload
                    await self.handle_audio(client_id, payload)
                    
                elif msg_type == 0x00:  # Hangup
                    logger.info(f"📱 [{client_id}] Hangup")
                    break
                    
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
    
    async def handle_uuid(self, client_id, payload):
        """Handle UUID and send greeting"""
        call_uuid = payload.hex() if len(payload) == 16 else payload.decode('utf-8', errors='ignore')
        logger.info(f"📞 [{client_id}] Call UUID: {call_uuid}")
        
        # Load assistant data
        phone_number = "4920189098723"
        assistant_data = await self.get_assistant_by_phone(phone_number)
        self.clients[client_id]['assistant_data'] = assistant_data
        
        # Configure TTS voice
        tts_config = speechsdk.SpeechConfig(subscription=self.azure_key, region=self.azure_region)
        tts_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        
        if assistant_data:
            logger.info(f"🤖 [{client_id}] Assistant: {assistant_data['name']}")
            
            # Set voice from assistant settings
            settings = assistant_data.get('settings', {})
            if isinstance(settings, dict):
                voice = settings.get('providers', {}).get('tts', {}).get('voice')
                if voice and isinstance(voice, str):
                    voice = voice.strip('"')  # Remove quotes if present
                    tts_config.speech_synthesis_voice_name = voice
                    logger.info(f"🎤 [{client_id}] Voice: {voice}")
                else:
                    # Default female voice
                    tts_config.speech_synthesis_voice_name = "de-DE-SeraphinaMultilingualNeural"
            else:
                tts_config.speech_synthesis_voice_name = "de-DE-SeraphinaMultilingualNeural"
        else:
            # Default if no assistant found
            tts_config.speech_synthesis_voice_name = "de-DE-SeraphinaMultilingualNeural"
            
        self.clients[client_id]['tts_config'] = tts_config
        
        # Send greeting
        await self.send_greeting(client_id)
    
    async def send_greeting(self, client_id):
        """Send assistant greeting"""
        client = self.clients[client_id]
        assistant_data = client.get('assistant_data')
        
        if assistant_data and assistant_data.get('greeting'):
            greeting = assistant_data['greeting']
            logger.info(f"💬 [{client_id}] Greeting: '{greeting}'")
        else:
            greeting = "Hallo! Wie kann ich helfen?"
            
        await self.send_tts(client_id, greeting)
    
    async def handle_audio(self, client_id, audio_data):
        """Handle incoming audio"""
        if client_id not in self.clients:
            return
            
        client = self.clients[client_id]
        client['audio_chunks'] += 1
        client['audio_buffer'].extend(audio_data)
        
        # Process every 2 seconds of audio
        if len(client['audio_buffer']) >= 32000:  # 2s at 8kHz * 2 bytes
            logger.info(f"🎤 [{client_id}] Processing audio...")
            
            # Create WAV for STT
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(8000)
                wav_file.writeframes(bytes(client['audio_buffer']))
            
            wav_buffer.seek(0)
            
            # Process with STT
            try:
                stream = speechsdk.audio.PushAudioInputStream(
                    stream_format=speechsdk.audio.AudioStreamFormat(
                        samples_per_second=8000,
                        bits_per_sample=16,
                        channels=1,
                        wave_stream_format=speechsdk.audio.AudioStreamWaveFormat.PCM
                    )
                )
                
                stream.write(wav_buffer.read())
                stream.close()
                
                audio_config = speechsdk.audio.AudioConfig(stream=stream)
                recognizer = speechsdk.SpeechRecognizer(
                    speech_config=self.stt_config,
                    audio_config=audio_config
                )
                
                result = recognizer.recognize_once()
                
                if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                    text = result.text
                    if text and len(text) > 2:
                        logger.info(f"✅ [{client_id}] User said: '{text}'")
                        
                        # Get AI response
                        assistant_data = client.get('assistant_data')
                        system_prompt = None
                        if assistant_data:
                            system_prompt = assistant_data.get('system_prompt')
                        
                        response = await self.process_with_ai(text, system_prompt)
                        
                        # Send response
                        await self.send_tts(client_id, response)
                        
                        # Save conversation
                        client['conversation'].append({
                            'user': text,
                            'assistant': response
                        })
                        
            except Exception as e:
                logger.error(f"💥 [{client_id}] STT error: {e}")
            
            # Clear buffer
            client['audio_buffer'] = bytearray()
    
    async def send_tts(self, client_id, text):
        """Send TTS response with correct voice"""
        if client_id not in self.clients:
            return
            
        client = self.clients[client_id]
        tts_config = client.get('tts_config')
        
        if not tts_config:
            logger.error(f"❌ [{client_id}] No TTS config!")
            return
            
        start = time.time()
        
        try:
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=tts_config,
                audio_config=None
            )
            
            result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                audio_data = result.audio_data
                
                # Send audio chunks
                chunk_size = 320  # 20ms at 8kHz
                writer = client['writer']
                
                for i in range(0, len(audio_data), chunk_size):
                    chunk = audio_data[i:i+chunk_size]
                    if len(chunk) < chunk_size:
                        chunk = chunk + b'\x00' * (chunk_size - len(chunk))
                    
                    message = struct.pack('>BH', 0x10, len(chunk)) + chunk
                    writer.write(message)
                    await writer.drain()
                    await asyncio.sleep(0.02)
                
                latency = (time.time() - start) * 1000
                logger.info(f"🗣️ [{client_id}] TTS '{text[:40]}...' in {latency:.0f}ms")
            else:
                logger.error(f"❌ [{client_id}] TTS failed: {result.reason}")
                
        except Exception as e:
            logger.error(f"💥 [{client_id}] TTS error: {e}")
    
    async def start(self):
        """Start server"""
        await self.init_db()
        
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        addr = server.sockets[0].getsockname()
        logger.info(f"🚀 FINAL Voice AI on {addr[0]}:{addr[1]}")
        logger.info(f"✅ STT: Azure Speech Recognition")
        logger.info(f"✅ AI: Mistral AI Processing")
        logger.info(f"✅ TTS: Azure with assistant voice")
        logger.info(f"✅ Database: Assistant configuration")
        
        async with server:
            await server.serve_forever()

async def main():
    server = VoiceAIFinal(port=9092)
    await server.start()

if __name__ == "__main__":
    asyncio.run(main())