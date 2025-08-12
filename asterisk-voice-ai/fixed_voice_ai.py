#!/usr/bin/env python3
"""
FIXED Voice AI - Handles consent properly and prevents greeting loops
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

class FixedVoiceAI:
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
    
    async def handle_client(self, reader, writer):
        client_id = str(int(time.time() * 1000))[-8:]
        logger.info(f"📞 Call started [{client_id}]")
        
        client = {
            'reader': reader,
            'writer': writer,
            'buffer': bytearray(),
            'silence_count': 0,
            'processing': False,
            'conversation': [],
            'connected': True,
            'greeting_sent': False,
            'consent_given': False  # Track consent status
        }
        self.clients[client_id] = client
        
        try:
            while client['connected']:
                try:
                    header = await asyncio.wait_for(
                        reader.readexactly(3), 
                        timeout=30
                    )
                except asyncio.TimeoutError:
                    logger.info(f"Timeout [{client_id}]")
                    break
                except asyncio.IncompleteReadError:
                    logger.info(f"Connection lost [{client_id}]")
                    break
                    
                if not header:
                    break
                    
                msg_type = header[0]
                length = struct.unpack('>H', header[1:3])[0]
                
                payload = b''
                if length > 0:
                    try:
                        payload = await reader.readexactly(length)
                    except asyncio.IncompleteReadError:
                        break
                
                if msg_type == 0x01:  # UUID
                    await self.handle_uuid(client_id, payload)
                elif msg_type == 0x10:  # Audio
                    await self.handle_audio(client_id, payload)
                elif msg_type == 0x00:  # Hangup
                    break
                    
        except Exception as e:
            logger.error(f"Error [{client_id}]: {e}")
        finally:
            client['connected'] = False
            
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
                
            if client_id in self.clients:
                del self.clients[client_id]
            logger.info(f"📴 Call ended [{client_id}]")
    
    async def handle_uuid(self, client_id, payload):
        """Initialize and send greeting"""
        logger.info(f"Setting up [{client_id}]")
        
        # Get assistant
        assistant = await self.get_assistant("4920189098723")
        
        # Setup TTS
        tts_config = speechsdk.SpeechConfig(
            subscription=self.azure_key,
            region=self.azure_region
        )
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
        
        # Mark greeting as sent
        self.clients[client_id]['greeting_sent'] = True
        self.clients[client_id]['conversation'].append({
            "role": "assistant",
            "content": greeting
        })
        
        await self.send_tts(client_id, greeting)
    
    async def handle_audio(self, client_id, payload):
        """Handle incoming audio"""
        client = self.clients.get(client_id)
        if not client or not client['connected'] or client['processing']:
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
        MIN_AUDIO = 4800  # 300ms
        SILENCE_FRAMES = 20  # 400ms
        
        if (len(client['buffer']) >= MIN_AUDIO and 
            client['silence_count'] >= SILENCE_FRAMES):
            
            client['processing'] = True
            client['silence_count'] = 0
            
            audio = bytes(client['buffer'])
            client['buffer'] = bytearray()
            
            # Process immediately
            await self.process_speech(client_id, audio)
        
        # Prevent overflow
        if len(client['buffer']) > 48000:
            client['buffer'] = client['buffer'][-24000:]
    
    async def process_speech(self, client_id, audio):
        """Process speech and respond"""
        client = self.clients.get(client_id)
        if not client or not client['connected']:
            client['processing'] = False
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
                    
                    # Check for consent response
                    if not client['consent_given'] and client['greeting_sent']:
                        # Check if user gave consent
                        text_lower = text.lower()
                        if any(word in text_lower for word in ['ja', 'okay', 'ok', 'einverstanden', 'klar', 'sicher', 'natürlich', 'gut']):
                            client['consent_given'] = True
                            response = "Vielen Dank! Wie kann ich Ihnen heute helfen?"
                        elif any(word in text_lower for word in ['nein', 'nicht', 'nö', 'ne']):
                            response = "Verstanden. Kann ich Ihnen trotzdem bei etwas helfen?"
                            client['consent_given'] = True  # Mark as handled
                        else:
                            # Unclear response, ask again
                            response = "Entschuldigung, ich habe das nicht verstanden. Sind Sie mit der Aufnahme einverstanden?"
                    else:
                        # Normal conversation
                        assistant = client.get('assistant', {})
                        system = assistant.get('system_prompt', "Du bist Sabine, eine freundliche KI-Assistentin.")
                        
                        # Get AI response with context
                        response = await self.get_ai_response(text, system, client['conversation'])
                    
                    # Save assistant response
                    client['conversation'].append({
                        "role": "assistant",
                        "content": response
                    })
                    
                    # Send response
                    if client['connected']:
                        await self.send_tts(client_id, response)
                    
        except Exception as e:
            logger.error(f"Process error [{client_id}]: {e}")
        finally:
            client['processing'] = False
    
    async def get_ai_response(self, text, system, conversation):
        """Get AI response with conversation context"""
        try:
            headers = {
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            }
            
            # Build messages with context
            messages = [{
                "role": "system",
                "content": system + "\nDu bist bereits vorgestellt. Wiederhole NICHT die Begrüßung. Beantworte direkt die Frage des Nutzers."
            }]
            
            # Add last 3 exchanges for context (skip greeting)
            for msg in conversation[-6:]:
                if msg['role'] == 'user' and msg['content'] not in ['[START]', '[CALL_START]']:
                    messages.append({"role": "user", "content": msg['content']})
                elif msg['role'] == 'assistant':
                    # Don't repeat the greeting
                    if not msg['content'].startswith("Guten Tag ich bin"):
                        messages.append({"role": "assistant", "content": msg['content']})
            
            # Add current message
            messages.append({"role": "user", "content": text})
            
            logger.info(f"Sending {len(messages)} messages to AI")
            
            payload = {
                "model": "mistral-small-latest",
                "messages": messages,
                "temperature": 0.5,
                "max_tokens": 100,
                "top_p": 0.9
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=2)  # Faster timeout
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content']
                        
        except asyncio.TimeoutError:
            return "Ja, gerne! Wie kann ich helfen?"
        except Exception as e:
            logger.error(f"AI error: {e}")
            
        return "Natürlich, wie kann ich Ihnen weiterhelfen?"
    
    async def send_tts(self, client_id, text):
        """Send TTS audio"""
        client = self.clients.get(client_id)
        if not client or not client['connected']:
            return
        
        try:
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=client['tts_config'],
                audio_config=None
            )
            
            result = synthesizer.speak_text_async(text).get()
            
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                # Stream audio
                await self.stream_audio(client_id, result.audio_data)
                logger.info(f"🔊 [{client_id}] Sent: {text[:50]}...")
                
        except Exception as e:
            logger.error(f"TTS error [{client_id}]: {e}")
    
    async def stream_audio(self, client_id, audio_data):
        """Stream audio with proper pacing"""
        client = self.clients.get(client_id)
        if not client or not client['connected']:
            return
        
        writer = client['writer']
        chunk_size = 320  # 20ms at 8kHz
        
        try:
            for i in range(0, len(audio_data), chunk_size):
                if not client['connected']:
                    break
                    
                chunk = audio_data[i:i+chunk_size]
                
                # Pad last chunk
                if len(chunk) < chunk_size:
                    chunk = chunk + b'\x00' * (chunk_size - len(chunk))
                
                # Send chunk
                msg = struct.pack('>BH', 0x10, len(chunk)) + chunk
                writer.write(msg)
                
                # Proper pacing
                if i == 0:
                    await writer.drain()
                else:
                    await asyncio.sleep(0.019)  # Slightly faster than real-time
            
            # Final drain
            await writer.drain()
            
        except (ConnectionResetError, BrokenPipeError):
            logger.warning(f"Connection lost during streaming [{client_id}]")
            client['connected'] = False
        except Exception as e:
            logger.error(f"Stream error [{client_id}]: {e}")
    
    async def start(self):
        await self.init_db()
        
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        logger.info("=" * 50)
        logger.info("🔧 FIXED Voice AI Server")
        logger.info(f"📍 Port: {self.port}")
        logger.info("✅ Fixes:")
        logger.info("  • Consent handling without loops")
        logger.info("  • No greeting repetition")
        logger.info("  • Faster AI response (2s timeout)")
        logger.info("  • Better connection handling")
        logger.info("=" * 50)
        
        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    server = FixedVoiceAI()
    asyncio.run(server.start())