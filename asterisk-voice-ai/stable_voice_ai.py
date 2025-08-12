#!/usr/bin/env python3
"""
STABLE Voice AI - Keeps connection alive
"""

import asyncio
import struct
import logging
import time
import os
from dotenv import load_dotenv
import azure.cognitiveservices.speech as speechsdk
import asyncpg
import aiohttp
import numpy as np

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MISTRAL_API_KEY = "rbBHVYVpt7Oe2HdUVKNoZLQ7cTilajyb"

class StableVoiceAI:
    def __init__(self, host='127.0.0.1', port=9092):
        self.host = host
        self.port = port
        self.clients = {}
        self.db_pool = None
        
        self.azure_key = os.getenv('AZURE_SPEECH_KEY')
        self.azure_region = os.getenv('AZURE_SPEECH_REGION', 'germanywestcentral')
        
    async def init_db(self):
        try:
            self.db_pool = await asyncpg.create_pool(
                host='10.0.0.5',
                port=5432,
                database='teli24_development',
                user='teli24_user',
                password=os.getenv('DB_PASSWORD'),
                min_size=1,
                max_size=5
            )
            logger.info("✅ Database connected")
        except Exception as e:
            logger.error(f"DB error: {e}")
            
    async def get_assistant(self, phone):
        if not self.db_pool:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.fetchrow("""
                    SELECT a.name, a.greeting, a."systemPrompt", a.settings 
                    FROM "Assistant" a 
                    JOIN "PhoneNumber" p ON a.id = p."assignedToAssistantId" 
                    WHERE p.number LIKE $1
                """, f"%{phone[-10:]}%")
                
                if result:
                    return {
                        'name': result['name'],
                        'greeting': result['greeting'],
                        'system_prompt': result['systemPrompt'],
                        'settings': result['settings']
                    }
        except Exception as e:
            logger.error(f"Query error: {e}")
        return None
    
    async def handle_client(self, reader, writer):
        client_id = str(int(time.time()))[-6:]
        logger.info(f"📞 New call [{client_id}]")
        
        client = {
            'reader': reader,
            'writer': writer,
            'buffer': bytearray(),
            'silence_count': 0,
            'processing': False,
            'keep_alive_task': None
        }
        self.clients[client_id] = client
        
        # Start keep-alive task
        client['keep_alive_task'] = asyncio.create_task(
            self.keep_alive(client_id)
        )
        
        try:
            while True:
                # Read header
                header = await asyncio.wait_for(reader.readexactly(3), timeout=60)
                if not header:
                    break
                    
                msg_type = header[0]
                length = struct.unpack('>H', header[1:3])[0]
                
                payload = b''
                if length > 0:
                    payload = await reader.readexactly(length)
                
                if msg_type == 0x01:  # UUID
                    await self.handle_uuid(client_id, payload)
                elif msg_type == 0x10:  # Audio
                    await self.handle_audio(client_id, payload)
                elif msg_type == 0x00:  # Hangup
                    logger.info(f"📴 Hangup [{client_id}]")
                    break
                    
        except asyncio.TimeoutError:
            logger.info(f"Timeout [{client_id}]")
        except Exception as e:
            logger.error(f"Error [{client_id}]: {e}")
        finally:
            # Cancel keep-alive
            if client['keep_alive_task']:
                client['keep_alive_task'].cancel()
                
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
            
            del self.clients[client_id]
            logger.info(f"Disconnected [{client_id}]")
    
    async def keep_alive(self, client_id):
        """Send silence periodically to keep connection alive"""
        try:
            while client_id in self.clients:
                await asyncio.sleep(5)  # Every 5 seconds
                
                # Send 100ms of silence
                silence = b'\x00\x00' * 800  # 100ms at 8kHz
                await self.send_audio(client_id, silence)
                logger.debug(f"Keep-alive sent [{client_id}]")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Keep-alive error: {e}")
    
    async def handle_uuid(self, client_id, payload):
        logger.info(f"UUID received [{client_id}]")
        
        # Get assistant
        assistant = await self.get_assistant("4920189098723")
        
        # Configure TTS
        tts_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        tts_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        
        # Set voice
        voice = "de-DE-SeraphinaMultilingualNeural"
        if assistant and isinstance(assistant.get('settings'), dict):
            v = assistant['settings'].get('providers', {}).get('tts', {}).get('voice')
            if v and isinstance(v, str):
                voice = v.strip('"')
        
        tts_config.speech_synthesis_voice_name = voice
        self.clients[client_id]['tts_config'] = tts_config
        
        logger.info(f"Voice: {voice}")
        
        # Send greeting
        greeting = "Hi, wie soll ich helfen?"
        if assistant and assistant.get('greeting'):
            greeting = assistant['greeting']
        
        logger.info(f"Greeting: {greeting}")
        await self.send_tts(client_id, greeting)
        
        # Configure STT
        stt_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        stt_config.speech_recognition_language = "de-DE"
        self.clients[client_id]['stt_config'] = stt_config
        self.clients[client_id]['assistant'] = assistant
    
    async def handle_audio(self, client_id, audio_data):
        client = self.clients.get(client_id)
        if not client or client['processing']:
            return
        
        # Add to buffer
        client['buffer'].extend(audio_data)
        
        # Simple VAD
        energy = np.sqrt(np.mean(np.frombuffer(audio_data, dtype=np.int16).astype(float) ** 2))
        
        if energy > 300:
            client['silence_count'] = 0
        else:
            client['silence_count'] += 1
        
        # Process after 800ms silence
        if len(client['buffer']) > 8000 and client['silence_count'] > 40:
            client['processing'] = True
            
            audio = bytes(client['buffer'])
            client['buffer'] = bytearray()
            client['silence_count'] = 0
            
            # Process async
            asyncio.create_task(self.process_speech(client_id, audio))
        
        # Prevent overflow
        if len(client['buffer']) > 80000:
            client['buffer'] = client['buffer'][-40000:]
    
    async def process_speech(self, client_id, audio):
        client = self.clients.get(client_id)
        if not client:
            return
        
        try:
            # STT
            stream = speechsdk.audio.PushAudioInputStream(
                stream_format=speechsdk.audio.AudioStreamFormat(
                    samples_per_second=8000,
                    bits_per_sample=16,
                    channels=1,
                    wave_stream_format=speechsdk.audio.AudioStreamWaveFormat.PCM
                )
            )
            stream.write(audio)
            stream.close()
            
            audio_config = speechsdk.audio.AudioConfig(stream=stream)
            recognizer = speechsdk.SpeechRecognizer(
                speech_config=client['stt_config'],
                audio_config=audio_config
            )
            
            result = recognizer.recognize_once()
            
            if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                text = result.text
                if text and len(text) > 2:
                    logger.info(f"User: {text}")
                    
                    # Get AI response
                    system = "Du bist ein hilfreicher Assistent. Antworte kurz auf Deutsch."
                    if client.get('assistant'):
                        system = client['assistant'].get('system_prompt', system)
                    
                    response = await self.get_ai_response(text, system)
                    await self.send_tts(client_id, response)
                    
        except Exception as e:
            logger.error(f"Process error: {e}")
        finally:
            client['processing'] = False
    
    async def get_ai_response(self, text, system):
        try:
            headers = {
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "mistral-small-latest",  # From database config
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text}
                ],
                "temperature": 0.7,
                "max_tokens": 80
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=3)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content']
                        
        except Exception as e:
            logger.error(f"AI error: {e}")
        
        return "Entschuldigung, ich verstehe das nicht."
    
    async def send_tts(self, client_id, text):
        client = self.clients.get(client_id)
        if not client:
            return
        
        try:
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=client['tts_config'],
                audio_config=None
            )
            
            result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                await self.send_audio(client_id, result.audio_data)
                logger.info(f"TTS: {text[:40]}...")
                
        except Exception as e:
            logger.error(f"TTS error: {e}")
    
    async def send_audio(self, client_id, audio_data):
        client = self.clients.get(client_id)
        if not client:
            return
        
        writer = client['writer']
        chunk_size = 320
        
        try:
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i+chunk_size]
                if len(chunk) < chunk_size:
                    chunk = chunk + b'\x00' * (chunk_size - len(chunk))
                
                msg = struct.pack('>BH', 0x10, len(chunk)) + chunk
                writer.write(msg)
                await writer.drain()
                await asyncio.sleep(0.02)
                
        except Exception as e:
            logger.error(f"Send error: {e}")
    
    async def start(self):
        await self.init_db()
        
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        logger.info(f"🚀 STABLE Voice AI on port {self.port}")
        logger.info("✅ Keep-alive enabled")
        logger.info("✅ Female voice from DB")
        logger.info("✅ STT + AI + TTS")
        
        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    server = StableVoiceAI()
    asyncio.run(server.start())