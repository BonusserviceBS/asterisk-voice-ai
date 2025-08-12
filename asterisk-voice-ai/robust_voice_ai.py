#!/usr/bin/env python3
"""
ROBUST Voice AI - Maximum stability with error recovery
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

logging.basicConfig(level=logging.DEBUG)  # More detailed logging
logger = logging.getLogger(__name__)

MISTRAL_API_KEY = "rbBHVYVpt7Oe2HdUVKNoZLQ7cTilajyb"

class RobustVoiceAI:
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
            logger.info("✅ Database ready")
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
    
    async def send_keep_alive(self, client_id):
        """Send periodic keep-alive to maintain connection"""
        client = self.clients.get(client_id)
        if not client:
            return
            
        try:
            while client['connected']:
                await asyncio.sleep(3)  # Every 3 seconds
                
                if not client.get('streaming'):  # Don't send during streaming
                    try:
                        # Send minimal silence packet
                        silence = b'\x00\x00' * 80  # 10ms silence
                        msg = struct.pack('>BH', 0x10, len(silence)) + silence
                        
                        client['writer'].write(msg)
                        await client['writer'].drain()
                        logger.debug(f"Keep-alive sent [{client_id}]")
                    except:
                        break
        except asyncio.CancelledError:
            pass
    
    async def handle_client(self, reader, writer):
        client_id = str(int(time.time() * 1000))[-8:]
        logger.info(f"📞 Call started [{client_id}]")
        
        client = {
            'reader': reader,
            'writer': writer,
            'buffer': bytearray(),
            'silence_count': 0,
            'processing': False,
            'streaming': False,
            'conversation': [],
            'connected': True,
            'greeting_sent': False,
            'consent_given': False,
            'response_count': 0  # Track number of responses
        }
        self.clients[client_id] = client
        
        # Start keep-alive
        keep_alive_task = asyncio.create_task(self.send_keep_alive(client_id))
        
        try:
            while client['connected']:
                try:
                    # Read with longer timeout
                    header = await asyncio.wait_for(
                        reader.readexactly(3), 
                        timeout=60  # 60 second timeout
                    )
                except asyncio.TimeoutError:
                    logger.info(f"Timeout after 60s [{client_id}]")
                    break
                except asyncio.IncompleteReadError as e:
                    logger.warning(f"Incomplete read: {e} [{client_id}]")
                    break
                except Exception as e:
                    logger.error(f"Read error: {e} [{client_id}]")
                    break
                    
                if not header:
                    logger.info(f"Empty header [{client_id}]")
                    break
                    
                msg_type = header[0]
                length = struct.unpack('>H', header[1:3])[0]
                
                logger.debug(f"Message type: {msg_type:#04x}, length: {length}")
                
                payload = b''
                if length > 0:
                    try:
                        payload = await reader.readexactly(length)
                    except asyncio.IncompleteReadError as e:
                        logger.warning(f"Incomplete payload: {e}")
                        break
                
                if msg_type == 0x01:  # UUID
                    await self.handle_uuid(client_id, payload)
                elif msg_type == 0x10:  # Audio
                    await self.handle_audio(client_id, payload)
                elif msg_type == 0x00:  # Hangup
                    logger.info(f"Hangup received [{client_id}]")
                    break
                else:
                    logger.warning(f"Unknown message type: {msg_type:#04x}")
                    
        except Exception as e:
            logger.error(f"Main loop error [{client_id}]: {e}", exc_info=True)
        finally:
            client['connected'] = False
            keep_alive_task.cancel()
            
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
                
            if client_id in self.clients:
                del self.clients[client_id]
            logger.info(f"📴 Call ended [{client_id}] - {client['response_count']} responses sent")
    
    async def handle_uuid(self, client_id, payload):
        """Initialize and send greeting"""
        logger.info(f"Setting up [{client_id}]")
        
        # Get assistant
        assistant = await self.get_assistant("4920189098723")
        
        # Setup TTS with lower quality for stability
        tts_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
        # Use 8kHz directly to avoid resampling issues
        tts_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw8Khz16BitMonoPcm
        )
        
        # Get voice
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
        
        # Send greeting
        greeting = assistant['greeting'] if assistant else "Hallo, wie kann ich helfen?"
        
        self.clients[client_id]['greeting_sent'] = True
        self.clients[client_id]['conversation'].append({
            "role": "assistant",
            "content": greeting
        })
        
        await self.send_tts_safe(client_id, greeting)
    
    async def handle_audio(self, client_id, payload):
        """Handle incoming audio"""
        client = self.clients.get(client_id)
        if not client or not client['connected']:
            return
            
        # Don't process if already processing or streaming
        if client['processing'] or client['streaming']:
            logger.debug(f"Skipping audio - busy [{client_id}]")
            return
        
        # Add to buffer
        client['buffer'].extend(payload)
        
        # Simple VAD
        if len(payload) >= 320:
            audio_array = np.frombuffer(payload, dtype=np.int16)
            energy = np.sqrt(np.mean(audio_array.astype(float) ** 2))
            
            if energy > 200:
                client['silence_count'] = 0
            else:
                client['silence_count'] += 1
        
        # Process after silence
        MIN_AUDIO = 4800  # 300ms minimum
        SILENCE_FRAMES = 30  # 600ms silence (more conservative)
        
        if (len(client['buffer']) >= MIN_AUDIO and 
            client['silence_count'] >= SILENCE_FRAMES):
            
            client['processing'] = True
            client['silence_count'] = 0
            
            # Copy and clear buffer
            audio = bytes(client['buffer'])
            client['buffer'] = bytearray()
            
            # Process asynchronously
            asyncio.create_task(self.process_speech_safe(client_id, audio))
        
        # Prevent overflow
        if len(client['buffer']) > 80000:  # 5 seconds max
            client['buffer'] = client['buffer'][-40000:]
    
    async def process_speech_safe(self, client_id, audio):
        """Process speech with error handling"""
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
                text = result.text.strip()
                if text and len(text) > 1:
                    logger.info(f"🎤 [{client_id}] User: {text}")
                    
                    # Save user input
                    client['conversation'].append({
                        "role": "user",
                        "content": text
                    })
                    
                    # Generate response
                    response = await self.get_response(client_id, text)
                    
                    # Save and send response
                    if response and client['connected']:
                        client['conversation'].append({
                            "role": "assistant",
                            "content": response
                        })
                        
                        await self.send_tts_safe(client_id, response)
                        client['response_count'] += 1
                    
        except Exception as e:
            logger.error(f"Process error [{client_id}]: {e}", exc_info=True)
        finally:
            client['processing'] = False
    
    async def get_response(self, client_id, text):
        """Get appropriate response"""
        client = self.clients.get(client_id)
        if not client:
            return None
            
        # Handle consent if needed
        if not client['consent_given'] and client['greeting_sent']:
            text_lower = text.lower()
            if any(word in text_lower for word in ['ja', 'okay', 'ok', 'einverstanden', 'klar']):
                client['consent_given'] = True
                return "Vielen Dank! Wie kann ich Ihnen heute helfen?"
            elif any(word in text_lower for word in ['nein', 'nicht']):
                client['consent_given'] = True
                return "Verstanden. Kann ich Ihnen trotzdem bei etwas helfen?"
            else:
                return "Entschuldigung, sind Sie mit der Aufnahme einverstanden? Bitte antworten Sie mit Ja oder Nein."
        
        # Normal AI response
        try:
            assistant = client.get('assistant', {})
            system = assistant.get('system_prompt', "Du bist Sabine, eine freundliche KI-Assistentin.")
            
            headers = {
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            }
            
            messages = [{
                "role": "system",
                "content": system + "\nWichtig: Wiederhole NIEMALS die Begrüßung. Beantworte direkt die Frage."
            }]
            
            # Add context (last 2 exchanges)
            for msg in client['conversation'][-4:]:
                if msg['content'] and not msg['content'].startswith("Guten Tag"):
                    messages.append(msg)
            
            messages.append({"role": "user", "content": text})
            
            payload = {
                "model": "mistral-small-latest",
                "messages": messages,
                "temperature": 0.5,
                "max_tokens": 100
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content']
                        
        except Exception as e:
            logger.error(f"AI error: {e}")
            
        return "Ja, gerne! Wie kann ich Ihnen weiterhelfen?"
    
    async def send_tts_safe(self, client_id, text):
        """Send TTS with maximum safety"""
        client = self.clients.get(client_id)
        if not client or not client['connected']:
            return
        
        client['streaming'] = True
        
        try:
            # Generate TTS
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=client['tts_config'],
                audio_config=None
            )
            
            result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                # Stream with error handling
                await self.stream_audio_safe(client_id, result.audio_data)
                logger.info(f"🔊 [{client_id}] Sent: {text[:50]}...")
            else:
                logger.error(f"TTS synthesis failed: {result.reason}")
                
        except Exception as e:
            logger.error(f"TTS error [{client_id}]: {e}", exc_info=True)
        finally:
            client['streaming'] = False
    
    async def stream_audio_safe(self, client_id, audio_data):
        """Stream audio with maximum safety"""
        client = self.clients.get(client_id)
        if not client or not client['connected']:
            return
        
        writer = client['writer']
        chunk_size = 320  # 20ms at 8kHz
        
        total_chunks = len(audio_data) // chunk_size
        sent_chunks = 0
        
        try:
            for i in range(0, len(audio_data), chunk_size):
                # Check connection before each chunk
                if not client['connected']:
                    logger.warning(f"Connection lost during streaming [{client_id}]")
                    break
                
                chunk = audio_data[i:i+chunk_size]
                
                # Pad last chunk
                if len(chunk) < chunk_size:
                    chunk = chunk + b'\x00' * (chunk_size - len(chunk))
                
                # Send chunk
                msg = struct.pack('>BH', 0x10, len(chunk)) + chunk
                
                try:
                    writer.write(msg)
                    
                    # Drain periodically, not every chunk
                    if sent_chunks % 10 == 0:
                        await writer.drain()
                    
                    sent_chunks += 1
                    
                    # Proper pacing - exactly 20ms
                    await asyncio.sleep(0.020)
                    
                except (ConnectionResetError, BrokenPipeError, OSError) as e:
                    logger.warning(f"Connection error during streaming: {e}")
                    client['connected'] = False
                    break
            
            # Final drain if still connected
            if client['connected']:
                await writer.drain()
                
            logger.debug(f"Streamed {sent_chunks}/{total_chunks} chunks [{client_id}]")
            
        except Exception as e:
            logger.error(f"Stream error [{client_id}]: {e}", exc_info=True)
            client['connected'] = False
    
    async def start(self):
        await self.init_db()
        
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        logger.info("=" * 50)
        logger.info("🛡️ ROBUST Voice AI Server")
        logger.info(f"📍 Port: {self.port}")
        logger.info("✅ Features:")
        logger.info("  • Maximum error handling")
        logger.info("  • Connection monitoring")
        logger.info("  • Safe streaming")
        logger.info("  • Response counting")
        logger.info("  • Detailed debug logging")
        logger.info("=" * 50)
        
        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    server = RobustVoiceAI()
    asyncio.run(server.start())