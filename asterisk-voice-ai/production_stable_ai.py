#!/usr/bin/env python3
"""
PRODUCTION STABLE Voice AI - Fixes audio quality and greeting issues
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
import re

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MISTRAL_API_KEY = "rbBHVYVpt7Oe2HdUVKNoZLQ7cTilajyb"

class ProductionStableAI:
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
                min_size=2,
                max_size=10,
                command_timeout=10
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
                    LIMIT 1
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
    
    def split_greeting(self, text, max_length=300):
        """Split long greeting into smaller chunks for TTS"""
        if len(text) <= max_length:
            return [text]
        
        # Split by sentences
        sentences = re.split(r'(?<=[.!?])\s+', text)
        
        chunks = []
        current = ""
        
        for sentence in sentences:
            if len(sentence) > max_length:
                # Split long sentence by commas
                parts = sentence.split(', ')
                for part in parts:
                    if len(current) + len(part) <= max_length:
                        current += part + ", "
                    else:
                        if current:
                            chunks.append(current.rstrip(", "))
                        current = part + ", "
            elif len(current) + len(sentence) <= max_length:
                current += sentence + " "
            else:
                if current:
                    chunks.append(current.rstrip())
                current = sentence + " "
        
        if current:
            chunks.append(current.rstrip())
        
        return chunks
    
    async def handle_client(self, reader, writer):
        client_id = str(int(time.time() * 1000))[-8:]
        logger.info(f"📞 New call [{client_id}]")
        
        client = {
            'reader': reader,
            'writer': writer,
            'buffer': bytearray(),
            'silence_count': 0,
            'processing': False,
            'tts_active': False,
            'conversation': [],
            'last_keep_alive': time.time()
        }
        self.clients[client_id] = client
        
        # Start keep-alive task
        keep_alive_task = asyncio.create_task(self.keep_alive(client_id))
        
        try:
            while True:
                try:
                    header = await asyncio.wait_for(
                        reader.readexactly(3), 
                        timeout=60
                    )
                except asyncio.TimeoutError:
                    logger.info(f"Timeout [{client_id}]")
                    break
                    
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
                    break
                    
        except Exception as e:
            logger.error(f"Error [{client_id}]: {e}")
        finally:
            keep_alive_task.cancel()
            
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
                
            if client_id in self.clients:
                del self.clients[client_id]
            logger.info(f"📴 Call ended [{client_id}]")
    
    async def keep_alive(self, client_id):
        """Send minimal keep-alive only when needed"""
        try:
            while client_id in self.clients:
                await asyncio.sleep(5)  # Every 5 seconds
                
                client = self.clients.get(client_id)
                if not client:
                    break
                
                # Only send if not actively sending TTS and last activity > 4s ago
                if not client['tts_active'] and (time.time() - client['last_keep_alive']) > 4:
                    try:
                        # Send minimal silence (5ms)
                        silence = b'\x00\x00' * 40  
                        msg = struct.pack('>BH', 0x10, len(silence)) + silence
                        client['writer'].write(msg)
                        await client['writer'].drain()
                        client['last_keep_alive'] = time.time()
                    except:
                        break
                        
        except asyncio.CancelledError:
            pass
    
    async def handle_uuid(self, client_id, payload):
        """Handle UUID and send chunked greeting"""
        logger.info(f"Initializing [{client_id}]")
        
        # Get assistant
        assistant = await self.get_assistant("4920189098723")
        
        # Setup TTS config with lower quality for better streaming
        tts_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        # Use lower quality for faster generation
        tts_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        
        # Get voice from settings
        voice = "de-DE-SeraphinaMultilingualNeural"
        if assistant and isinstance(assistant.get('settings'), dict):
            v = assistant['settings'].get('providers', {}).get('tts', {}).get('voice')
            if v and isinstance(v, str):
                voice = v.strip('"')
        
        tts_config.speech_synthesis_voice_name = voice
        
        # Setup STT
        stt_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        stt_config.speech_recognition_language = "de-DE"
        
        # Store configs
        self.clients[client_id]['tts_config'] = tts_config
        self.clients[client_id]['stt_config'] = stt_config
        self.clients[client_id]['assistant'] = assistant
        
        # Send greeting in chunks to avoid timeout
        greeting = assistant['greeting'] if assistant else "Hallo, wie kann ich helfen?"
        
        # Save to conversation history
        self.clients[client_id]['conversation'].append({
            "user": "[START]",
            "assistant": greeting
        })
        
        # Split and send greeting
        chunks = self.split_greeting(greeting)
        logger.info(f"Greeting has {len(chunks)} chunks")
        
        for i, chunk in enumerate(chunks):
            logger.info(f"Sending chunk {i+1}/{len(chunks)}: {len(chunk)} chars")
            await self.send_tts_chunk(client_id, chunk, is_greeting=True)
            # Small pause between chunks for natural flow
            if i < len(chunks) - 1:
                await asyncio.sleep(0.1)
    
    async def send_tts_chunk(self, client_id, text, is_greeting=False):
        """Send TTS with proper audio quality"""
        client = self.clients.get(client_id)
        if not client:
            return
        
        try:
            client['tts_active'] = True
            client['last_keep_alive'] = time.time()
            
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=client['tts_config'],
                audio_config=None
            )
            
            # For greetings, use slightly faster speech
            ssml = None
            if is_greeting and len(text) > 200:
                ssml = f'<speak><prosody rate="1.1">{text}</prosody></speak>'
                result = synthesizer.speak_ssml_async(ssml).get()
            else:
                result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                # Stream with proper pacing
                await self.stream_audio_quality(client_id, result.audio_data)
                logger.info(f"✅ TTS sent: {text[:40]}...")
                
        except Exception as e:
            logger.error(f"TTS error: {e}")
        finally:
            client['tts_active'] = False
            client['last_keep_alive'] = time.time()
    
    async def stream_audio_quality(self, client_id, audio_data):
        """Stream audio with proper quality and pacing"""
        client = self.clients.get(client_id)
        if not client:
            return
        
        writer = client['writer']
        chunk_size = 320  # 20ms at 8kHz
        
        try:
            total_chunks = len(audio_data) // chunk_size
            
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i+chunk_size]
                
                # Pad last chunk
                if len(chunk) < chunk_size:
                    chunk = chunk + b'\x00' * (chunk_size - len(chunk))
                
                # Send chunk
                msg = struct.pack('>BH', 0x10, len(chunk)) + chunk
                writer.write(msg)
                
                # Update keep-alive timer
                client['last_keep_alive'] = time.time()
                
                # Proper pacing for audio quality
                # 20ms chunks need 20ms spacing for real-time
                # But we go slightly faster to build buffer
                if i == 0:
                    # No delay for first chunk
                    await writer.drain()
                elif i < 10 * chunk_size:
                    # First 200ms: slightly faster to build buffer
                    await asyncio.sleep(0.018)
                else:
                    # Normal pacing: exactly 20ms
                    await asyncio.sleep(0.020)
                
                # Drain periodically to avoid buffer overflow
                if i % (50 * chunk_size) == 0:
                    await writer.drain()
            
            # Final drain
            await writer.drain()
            
        except Exception as e:
            logger.error(f"Stream error: {e}")
    
    async def handle_audio(self, client_id, payload):
        """Handle incoming audio with VAD"""
        client = self.clients.get(client_id)
        if not client or client['processing']:
            return
        
        # Don't process during TTS
        if client['tts_active']:
            return
        
        # Add to buffer
        client['buffer'].extend(payload)
        
        # Simple VAD
        if len(payload) >= 320:
            audio_array = np.frombuffer(payload, dtype=np.int16)
            energy = np.sqrt(np.mean(audio_array.astype(float) ** 2))
            
            if energy > 250:  # Adjusted threshold
                client['silence_count'] = 0
            else:
                client['silence_count'] += 1
        
        # Process after silence
        MIN_AUDIO = 4800  # 300ms
        SILENCE_FRAMES = 25  # 500ms silence
        
        if (len(client['buffer']) >= MIN_AUDIO and 
            client['silence_count'] >= SILENCE_FRAMES):
            
            client['processing'] = True
            client['silence_count'] = 0
            
            audio = bytes(client['buffer'])
            client['buffer'] = bytearray()
            
            asyncio.create_task(self.process_speech(client_id, audio))
        
        # Prevent overflow
        if len(client['buffer']) > 80000:
            client['buffer'] = client['buffer'][-40000:]
    
    async def process_speech(self, client_id, audio):
        """Process speech with context"""
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
                    logger.info(f"🎤 User: {text}")
                    
                    # Get AI response
                    assistant = client.get('assistant', {})
                    system = assistant.get('system_prompt', "Du bist ein freundlicher Assistent.")
                    
                    response = await self.get_ai_response(text, system, client['conversation'])
                    
                    # Save to history
                    client['conversation'].append({"user": text, "assistant": response})
                    
                    # Send response
                    await self.send_tts_chunk(client_id, response)
                    
        except Exception as e:
            logger.error(f"Process error: {e}")
        finally:
            client['processing'] = False
    
    async def get_ai_response(self, text, system, history):
        """Get AI response with context"""
        try:
            headers = {
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            }
            
            messages = [{
                "role": "system",
                "content": system + " Antworte kurz und präzise."
            }]
            
            # Add last 2 exchanges
            for h in history[-2:]:
                if h["user"] != "[START]":
                    messages.append({"role": "user", "content": h["user"]})
                    messages.append({"role": "assistant", "content": h["assistant"]})
            
            messages.append({"role": "user", "content": text})
            
            payload = {
                "model": "mistral-small-latest",
                "messages": messages,
                "temperature": 0.5,
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
            
        return "Entschuldigung, können Sie das wiederholen?"
    
    async def start(self):
        await self.init_db()
        
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        logger.info("=" * 50)
        logger.info("🎯 PRODUCTION STABLE Voice AI")
        logger.info(f"📍 Port: {self.port}")
        logger.info("✅ Fixed Issues:")
        logger.info("  • Audio quality with proper pacing")
        logger.info("  • Chunked greeting for long texts")
        logger.info("  • Smart keep-alive")
        logger.info("  • Better VAD thresholds")
        logger.info("=" * 50)
        
        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    server = ProductionStableAI()
    asyncio.run(server.start())