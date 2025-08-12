#!/usr/bin/env python3
"""
FAST Voice AI - Optimized for low latency
Target: <300ms response time
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
from concurrent.futures import ThreadPoolExecutor

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MISTRAL_API_KEY = "rbBHVYVpt7Oe2HdUVKNoZLQ7cTilajyb"

class FastVoiceAI:
    def __init__(self, host='127.0.0.1', port=9092):
        self.host = host
        self.port = port
        self.clients = {}
        self.db_pool = None
        self.executor = ThreadPoolExecutor(max_workers=4)
        
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
            logger.info("✅ Database pool ready")
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
    
    async def handle_client(self, reader, writer):
        client_id = str(int(time.time() * 1000))[-8:]
        start_time = time.time()
        logger.info(f"📞 Call started [{client_id}]")
        
        client = {
            'reader': reader,
            'writer': writer,
            'buffer': bytearray(),
            'silence_count': 0,
            'processing': False,
            'last_activity': time.time(),
            'tts_playing': False,
            'keep_alive_task': None,
            'conversation_history': [],
            'greeting_sent': False
        }
        self.clients[client_id] = client
        
        # Start keep-alive to prevent disconnection
        client['keep_alive_task'] = asyncio.create_task(self.keep_alive(client_id))
        
        try:
            while True:
                # Fast read with timeout
                try:
                    header = await asyncio.wait_for(
                        reader.readexactly(3), 
                        timeout=60  # Longer timeout for long greetings
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
                    await self.handle_uuid_fast(client_id, payload)
                elif msg_type == 0x10:  # Audio
                    await self.handle_audio_fast(client_id, payload)
                elif msg_type == 0x00:  # Hangup
                    break
                    
        except Exception as e:
            logger.error(f"Error [{client_id}]: {e}")
        finally:
            duration = time.time() - start_time
            logger.info(f"📴 Call ended [{client_id}] Duration: {duration:.1f}s")
            
            # Cancel keep-alive
            if client.get('keep_alive_task'):
                client['keep_alive_task'].cancel()
            
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
                
            if client_id in self.clients:
                del self.clients[client_id]
    
    async def keep_alive(self, client_id):
        """Send silence periodically to keep connection alive"""
        try:
            while client_id in self.clients:
                await asyncio.sleep(3)  # Every 3 seconds (more frequent)
                if client_id in self.clients and not self.clients[client_id].get('tts_playing'):
                    # Send tiny silence packet only when not playing TTS
                    silence = b'\x00\x00' * 160  # 20ms of silence
                    msg = struct.pack('>BH', 0x10, len(silence)) + silence
                    try:
                        self.clients[client_id]['writer'].write(msg)
                        await self.clients[client_id]['writer'].drain()
                    except:
                        break
        except asyncio.CancelledError:
            pass
    
    async def handle_uuid_fast(self, client_id, payload):
        """Fast UUID handler with pre-cached configs"""
        logger.info(f"Setting up [{client_id}]")
        
        # Get assistant data (can be cached)
        assistant = await self.get_assistant("4920189098723")
        
        # Setup TTS config
        tts_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
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
        
        # Setup STT config
        stt_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        stt_config.speech_recognition_language = "de-DE"
        # Enable partial results for faster response
        stt_config.output_format = speechsdk.OutputFormat.Simple
        
        # Store configs
        self.clients[client_id]['tts_config'] = tts_config
        self.clients[client_id]['stt_config'] = stt_config
        self.clients[client_id]['assistant'] = assistant
        
        # Send greeting FAST
        greeting = assistant['greeting'] if assistant else "Hallo, wie kann ich helfen?"
        
        # Mark greeting as sent and save to history
        self.clients[client_id]['greeting_sent'] = True
        self.clients[client_id]['conversation_history'].append({
            "user": "[CALL_START]",
            "assistant": greeting
        })
        
        # Start TTS immediately in background
        asyncio.create_task(self.send_tts_fast(client_id, greeting))
    
    async def handle_audio_fast(self, client_id, payload):
        """Fast audio processing with reduced VAD delay"""
        client = self.clients.get(client_id)
        if not client or client['processing']:
            return
            
        # Don't process if TTS is playing (barge-in detection)
        if client.get('tts_playing'):
            return
        
        # Add to buffer
        client['buffer'].extend(payload)
        
        # Fast VAD with lower threshold
        if len(payload) >= 320:  # Need enough data
            audio_array = np.frombuffer(payload, dtype=np.int16)
            energy = np.sqrt(np.mean(audio_array.astype(float) ** 2))
            
            if energy > 200:  # Lower threshold for faster detection
                client['silence_count'] = 0
                client['last_activity'] = time.time()
            else:
                client['silence_count'] += 1
        
        # Process faster - only 400ms silence (20 frames)
        MIN_AUDIO = 4800  # 300ms minimum
        SILENCE_FRAMES = 20  # 400ms silence
        
        if (len(client['buffer']) >= MIN_AUDIO and 
            client['silence_count'] >= SILENCE_FRAMES):
            
            # Mark as processing
            client['processing'] = True
            client['silence_count'] = 0
            
            # Get audio and clear buffer
            audio = bytes(client['buffer'])
            client['buffer'] = bytearray()
            
            # Process immediately
            asyncio.create_task(self.process_fast(client_id, audio))
        
        # Prevent overflow - keep last 3 seconds
        if len(client['buffer']) > 48000:
            client['buffer'] = client['buffer'][-24000:]
    
    async def process_fast(self, client_id, audio):
        """Ultra-fast processing pipeline"""
        client = self.clients.get(client_id)
        if not client:
            return
        
        try:
            start = time.time()
            
            # Fast STT
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
            
            # Fast recognition
            result = recognizer.recognize_once()
            
            if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                text = result.text
                if text and len(text) > 2:
                    stt_time = (time.time() - start) * 1000
                    logger.info(f"🎤 STT {stt_time:.0f}ms: {text}")
                    
                    # Get AI response with proper prompt
                    assistant = client.get('assistant', {})
                    system = assistant.get('system_prompt') if assistant else None
                    
                    # If prompt is too short or missing, use a better one
                    if not system or len(system) < 20:
                        system = "Du bist ein freundlicher KI-Assistent. Du hilfst Menschen bei ihren Fragen und Anliegen."
                    
                    # Add conversation history to context
                    history = client.get('conversation_history', [])
                    
                    ai_start = time.time()
                    response = await self.get_ai_fast_with_context(text, system, history)
                    ai_time = (time.time() - ai_start) * 1000
                    logger.info(f"🤖 AI {ai_time:.0f}ms")
                    
                    # Save to history
                    client['conversation_history'].append({"user": text, "assistant": response})
                    
                    # Send TTS immediately
                    await self.send_tts_fast(client_id, response)
                    
                    total_time = (time.time() - start) * 1000
                    logger.info(f"⏱️ Total: {total_time:.0f}ms")
                    
        except Exception as e:
            logger.error(f"Process error: {e}")
        finally:
            client['processing'] = False
    
    async def get_ai_fast_with_context(self, text, system, history):
        """AI with conversation context"""
        try:
            headers = {
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            }
            
            # Build messages with history
            messages = [{"role": "system", "content": system + " Antworte freundlich und hilfreich in 1-2 kurzen Sätzen."}]
            
            # Add conversation history (last 3 exchanges)
            for h in history[-3:]:
                messages.append({"role": "user", "content": h["user"]})
                messages.append({"role": "assistant", "content": h["assistant"]})
            
            # Add current user message
            messages.append({"role": "user", "content": text})
            
            # Log what we're sending
            logger.info(f"📝 Sending to LLM: {len(messages)} messages, last user: '{text}'")
            
            payload = {
                "model": "mistral-small-latest",
                "messages": messages,
                "temperature": 0.5,
                "max_tokens": 60,
                "top_p": 0.9
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
                        
        except asyncio.TimeoutError:
            return "Einen Moment bitte."
        except Exception as e:
            logger.error(f"AI error: {e}")
            
        return "Ja, gerne!"
    
    async def get_ai_fast(self, text, system):
        """Fast AI with timeout and caching"""
        try:
            headers = {
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            }
            
            # Use reasonable settings for natural conversation
            payload = {
                "model": "mistral-small-latest",  # Better quality than tiny
                "messages": [
                    {"role": "system", "content": system + " Antworte freundlich und hilfreich in 1-2 kurzen Sätzen."},
                    {"role": "user", "content": text}
                ],
                "temperature": 0.5,  # More natural
                "max_tokens": 60,  # Reasonable length
                "top_p": 0.9
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=3)  # 3 second timeout
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content']
                        
        except asyncio.TimeoutError:
            return "Einen Moment bitte."
        except Exception as e:
            logger.error(f"AI error: {e}")
            
        return "Ja, gerne!"
    
    async def send_tts_fast(self, client_id, text):
        """Fast TTS with streaming"""
        client = self.clients.get(client_id)
        if not client:
            return
        
        try:
            start = time.time()
            client['tts_playing'] = True
            
            # Fast TTS synthesis
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=client['tts_config'],
                audio_config=None
            )
            
            # Direct synthesis for simplicity and reliability
            result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                # Stream immediately
                await self.stream_fast(client_id, result.audio_data)
                
                tts_time = (time.time() - start) * 1000
                logger.info(f"🗣️ TTS {tts_time:.0f}ms: {text[:30]}...")
            
        except Exception as e:
            logger.error(f"TTS error: {e}")
        finally:
            client['tts_playing'] = False
    
    async def stream_fast(self, client_id, audio_data):
        """Reliable streaming with proper pacing"""
        client = self.clients.get(client_id)
        if not client:
            return
        
        writer = client['writer']
        chunk_size = 320  # 20ms chunks at 8kHz
        
        try:
            # Stream with proper timing to avoid skipping
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i+chunk_size]
                
                # Pad last chunk if needed
                if len(chunk) < chunk_size:
                    chunk = chunk + b'\x00' * (chunk_size - len(chunk))
                
                # Send chunk
                msg = struct.pack('>BH', 0x10, len(chunk)) + chunk
                writer.write(msg)
                
                # Smooth streaming - send faster initially then pace
                if i == 0:
                    # No delay for first chunk
                    pass
                elif i < 10 * chunk_size:
                    # Slightly faster for first 200ms
                    await asyncio.sleep(0.015)
                else:
                    # Normal pacing after buffer built up
                    await asyncio.sleep(0.018)
            
            # Ensure all data is sent
            await writer.drain()
                
        except Exception as e:
            logger.error(f"Stream error: {e}")
    
    async def start(self):
        await self.init_db()
        
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        logger.info("=" * 50)
        logger.info("⚡ FAST Voice AI Server")
        logger.info(f"📍 Port: {self.port}")
        logger.info("🎯 Target: <300ms latency")
        logger.info("✅ Optimizations:")
        logger.info("  • 400ms VAD (vs 800ms)")
        logger.info("  • Mistral Small model")
        logger.info("  • Faster streaming")
        logger.info("  • Normal speech rate")
        logger.info("=" * 50)
        
        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    server = FastVoiceAI()
    asyncio.run(server.start())