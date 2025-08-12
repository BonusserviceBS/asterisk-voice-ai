#!/usr/bin/env python3
"""
COMPLETE Real-Time Voice AI with AudioSocket
Integrates Azure STT, Mistral AI, and Azure TTS
Target latency: <200ms end-to-end
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
import aiohttp
from typing import Optional, Dict, Any
import wave
import io

# Load environment
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RealTimeVoiceAI:
    """Complete real-time voice AI system"""
    
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
            
        # TTS Config
        self.tts_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        self.tts_config.speech_synthesis_voice_name = "de-DE-ConradNeural"
        self.tts_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        
        # STT Config for streaming recognition
        self.stt_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        self.stt_config.speech_recognition_language = "de-DE"
        
        # Mistral AI setup
        self.mistral_api_key = None
        self.mistral_url = "https://api.mistral.ai/v1/chat/completions"
        
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
            
            # Get Mistral API key from database
            async with self.db_pool.acquire() as conn:
                result = await conn.fetchrow("""
                    SELECT value FROM "SystemSetting" 
                    WHERE key = 'mistral_api_key' OR key = 'openai_api_key'
                    LIMIT 1
                """)
                if result:
                    self.mistral_api_key = result['value']
                    logger.info("🤖 Mistral AI key loaded from database")
                    
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
                        
            logger.warning(f"📱 No assistant found for phone number: {phone_number}")
            return None
            
        except Exception as e:
            logger.error(f"💥 Database query failed: {e}")
            return None
    
    async def process_with_mistral(self, text: str, system_prompt: str) -> str:
        """Process text with Mistral AI and get response"""
        if not self.mistral_api_key:
            return "Entschuldigung, der KI-Dienst ist momentan nicht verfügbar."
            
        try:
            start = time.time()
            
            headers = {
                "Authorization": f"Bearer {self.mistral_api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "mistral-tiny",  # Fast model for low latency
                "messages": [
                    {"role": "system", "content": system_prompt or "Du bist ein hilfreicher KI-Assistent."},
                    {"role": "user", "content": text}
                ],
                "temperature": 0.7,
                "max_tokens": 150,  # Keep responses concise
                "stream": False
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(self.mistral_url, headers=headers, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        reply = data['choices'][0]['message']['content']
                        latency = (time.time() - start) * 1000
                        logger.info(f"🤖 Mistral response in {latency:.0f}ms")
                        return reply
                    else:
                        logger.error(f"❌ Mistral API error: {response.status}")
                        return "Entschuldigung, ich konnte das nicht verarbeiten."
                        
        except Exception as e:
            logger.error(f"💥 Mistral AI error: {e}")
            return "Es tut mir leid, es gab einen Fehler."
    
    async def handle_client(self, reader, writer):
        """Handle AudioSocket connection with full voice AI pipeline"""
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
            'phone_number': None,
            'audio_buffer': bytearray(),  # Buffer for STT
            'conversation_history': [],
            'is_speaking': False,
            'last_speech_time': time.time()
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
                    await self.handle_uuid_message(client_id, payload)
                    
                elif msg_type == 0x10:  # Audio payload
                    await self.handle_audio_message(client_id, payload)
                    
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
    
    async def handle_uuid_message(self, client_id, payload):
        """Handle UUID message and send greeting"""
        call_uuid = payload.hex() if len(payload) == 16 else payload.decode('utf-8', errors='ignore')
        self.clients[client_id]['call_uuid'] = call_uuid
        logger.info(f"📞 [{client_id}] Call UUID: {call_uuid}")
        
        # Default phone number for testing
        phone_number = "4920189098723"
        self.clients[client_id]['phone_number'] = phone_number
        
        # Load assistant data
        assistant_data = await self.get_assistant_by_phone(phone_number)
        self.clients[client_id]['assistant_data'] = assistant_data
        
        if assistant_data:
            logger.info(f"🤖 [{client_id}] Assistant: {assistant_data['name']}")
            
            # Configure voice from assistant settings
            settings = assistant_data.get('settings', {})
            if isinstance(settings, dict):
                voice = settings.get('providers', {}).get('tts', {}).get('voice')
                if voice:
                    self.tts_config.speech_synthesis_voice_name = voice
                    logger.info(f"🎤 [{client_id}] Voice: {voice}")
        
        # Send assistant greeting
        if not self.clients[client_id]['greeting_sent']:
            await self.send_assistant_greeting(client_id)
            self.clients[client_id]['greeting_sent'] = True
    
    async def handle_audio_message(self, client_id, audio_data):
        """Process incoming audio with STT"""
        if client_id not in self.clients:
            return
            
        client = self.clients[client_id]
        client['audio_chunks'] += 1
        
        # Add to audio buffer for STT processing
        client['audio_buffer'].extend(audio_data)
        
        # Process every 320ms of audio (16 chunks of 20ms each)
        if len(client['audio_buffer']) >= 5120:  # 320ms at 8kHz
            # Process with Azure STT
            await self.process_speech_to_text(client_id)
            
            # Clear processed audio
            client['audio_buffer'] = bytearray()
    
    async def process_speech_to_text(self, client_id):
        """Process audio buffer with Azure STT"""
        client = self.clients[client_id]
        audio_data = bytes(client['audio_buffer'])
        
        if len(audio_data) < 1600:  # Need at least 100ms of audio
            return
            
        try:
            # Create audio stream for Azure STT
            stream = speechsdk.audio.PushAudioInputStream(
                stream_format=speechsdk.audio.AudioStreamFormat.get_wav_format_pcm(
                    samples_per_second=8000,
                    bits_per_sample=16,
                    channels=1
                )
            )
            
            # Write audio data
            stream.write(audio_data)
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
                if text and len(text) > 2:  # Ignore very short utterances
                    logger.info(f"🎤 [{client_id}] Recognized: '{text}'")
                    
                    # Process with AI and respond
                    await self.process_user_input(client_id, text)
                    
            elif result.reason == speechsdk.ResultReason.NoMatch:
                # No speech detected in this chunk
                pass
            else:
                logger.warning(f"⚠️ [{client_id}] STT failed: {result.reason}")
                
        except Exception as e:
            logger.error(f"💥 [{client_id}] STT error: {e}")
    
    async def process_user_input(self, client_id, text):
        """Process recognized text with AI and respond"""
        client = self.clients[client_id]
        assistant_data = client.get('assistant_data')
        
        # Get system prompt
        system_prompt = "Du bist ein hilfreicher KI-Assistent."
        if assistant_data and isinstance(assistant_data, dict):
            system_prompt = assistant_data.get('system_prompt', system_prompt)
        
        # Get AI response
        response = await self.process_with_mistral(text, system_prompt)
        
        # Add to conversation history
        client['conversation_history'].append({
            'user': text,
            'assistant': response,
            'timestamp': time.time()
        })
        
        # Send TTS response
        await self.send_tts(client_id, response)
    
    async def send_assistant_greeting(self, client_id):
        """Send assistant greeting"""
        client = self.clients[client_id]
        assistant_data = client.get('assistant_data')
        
        if assistant_data and isinstance(assistant_data, dict) and assistant_data.get('greeting'):
            greeting = assistant_data['greeting']
            logger.info(f"💬 [{client_id}] Using assistant greeting: '{greeting}'")
        else:
            greeting = "Hallo! Wie kann ich Ihnen helfen?"
            logger.info(f"💬 [{client_id}] Using fallback greeting")
            
        await self.send_tts(client_id, greeting)
    
    async def process_dtmf(self, client_id, dtmf_digit):
        """Process DTMF input"""
        client = self.clients[client_id]
        assistant_data = client.get('assistant_data')
        assistant_name = assistant_data['name'] if assistant_data else "Assistant"
        
        responses = {
            '1': f"Gerne! Ich bin {assistant_name} und helfe Ihnen mit allgemeinen Fragen.",
            '2': f"Perfekt! {assistant_name} kann Ihnen mit technischen Fragen helfen.",
            '9': f"Einen Moment bitte, ich verbinde Sie mit einem Mitarbeiter.",
            '0': f"Auf Wiederhören! {assistant_name} wünscht Ihnen einen schönen Tag.",
        }
        
        response = responses.get(dtmf_digit, f"Sie haben {dtmf_digit} gedrückt. Wie kann ich helfen?")
        await self.send_tts(client_id, response)
    
    async def send_tts(self, client_id, text):
        """Generate and send TTS using Azure Speech"""
        start = time.time()
        
        try:
            # Mark as speaking to avoid interruptions
            self.clients[client_id]['is_speaking'] = True
            
            # Create synthesizer
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=self.tts_config, 
                audio_config=None
            )
            
            # Synthesize speech
            result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                # Get raw PCM data (8kHz, 16-bit, mono)
                audio_data = result.audio_data
                
                # Send via AudioSocket
                await self.send_audio_chunks(client_id, audio_data)
                
                latency = (time.time() - start) * 1000
                logger.info(f"🗣️ [{client_id}] TTS '{text[:50]}...' sent in {latency:.0f}ms")
                
            else:
                logger.error(f"❌ [{client_id}] Azure TTS failed: {result.reason}")
            
            # Mark as done speaking
            self.clients[client_id]['is_speaking'] = False
                
        except Exception as e:
            logger.error(f"💥 [{client_id}] Azure TTS error: {e}")
            self.clients[client_id]['is_speaking'] = False
    
    async def send_audio_chunks(self, client_id, audio_data):
        """Send audio using AudioSocket protocol"""
        if client_id not in self.clients:
            return
            
        writer = self.clients[client_id]['writer']
        
        # AudioSocket expects 8kHz 16-bit PCM in 20ms chunks
        chunk_size = 320  # 20ms at 8kHz = 160 samples = 320 bytes
        
        try:
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i+chunk_size]
                
                # Pad last chunk if needed
                if len(chunk) < chunk_size:
                    chunk = chunk + b'\x00' * (chunk_size - len(chunk))
                
                # AudioSocket message: 0x10 (audio) + length + data
                length = len(chunk)
                message = struct.pack('>BH', 0x10, length) + chunk
                
                writer.write(message)
                await writer.drain()
                
                # Real-time streaming delay
                await asyncio.sleep(0.02)  # 20ms
                
        except Exception as e:
            logger.error(f"📡 [{client_id}] Send error: {e}")
    
    async def start(self):
        """Start Real-Time Voice AI server"""
        # Initialize database
        await self.init_db()
        
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        addr = server.sockets[0].getsockname()
        logger.info(f"🚀 REAL-TIME Voice AI server listening on {addr[0]}:{addr[1]}")
        logger.info(f"🎯 Components:")
        logger.info(f"   ✅ Azure Speech-to-Text (STT)")
        logger.info(f"   ✅ Mistral AI Processing")
        logger.info(f"   ✅ Azure Text-to-Speech (TTS)")
        logger.info(f"   ✅ Database Integration")
        logger.info(f"🎯 Target latency: <200ms")
        
        async with server:
            await server.serve_forever()

async def main():
    server = RealTimeVoiceAI(port=9092)
    await server.start()

if __name__ == "__main__":
    asyncio.run(main())