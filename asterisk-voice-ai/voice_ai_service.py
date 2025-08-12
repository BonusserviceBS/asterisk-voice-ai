#!/usr/bin/env python3
"""
Voice AI Service - Main Production Service
Real-time audio processing for Asterisk
"""

import asyncio
import os
import logging
from dotenv import load_dotenv
import time
from datetime import datetime

# Load environment
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class VoiceAIService:
    """Main Voice AI Service"""
    
    def __init__(self):
        self.config = {
            'azure_key': os.getenv('AZURE_SPEECH_KEY', ''),
            'azure_region': os.getenv('AZURE_SPEECH_REGION', 'germanywestcentral'),
            'mistral_key': os.getenv('MISTRAL_API_KEY', ''),
            'asterisk_host': 'localhost',
            'service_port': 8765  # WebSocket port for audio
        }
        
        # Service status
        self.active_calls = {}
        self.total_calls = 0
        self.start_time = datetime.now()
        
        # Check capabilities
        self.has_azure = bool(self.config['azure_key'])
        self.has_mistral = bool(self.config['mistral_key'])
        
        logger.info(f"Voice AI Service starting...")
        logger.info(f"Capabilities: Azure STT={self.has_azure}, Mistral AI={self.has_mistral}")
        
    async def handle_call(self, call_id: str, channel_info: dict):
        """Handle incoming call"""
        self.total_calls += 1
        self.active_calls[call_id] = {
            'start_time': time.time(),
            'channel': channel_info,
            'transcripts': [],
            'responses': []
        }
        
        logger.info(f"New call {call_id} from {channel_info.get('caller_id', 'unknown')}")
        
        try:
            # Process call
            await self.process_call_audio(call_id)
        except Exception as e:
            logger.error(f"Call {call_id} error: {e}")
        finally:
            # Cleanup
            if call_id in self.active_calls:
                duration = time.time() - self.active_calls[call_id]['start_time']
                logger.info(f"Call {call_id} ended. Duration: {duration:.1f}s")
                del self.active_calls[call_id]
    
    async def process_call_audio(self, call_id: str):
        """Process audio for a call"""
        call_data = self.active_calls[call_id]
        
        # Simulate processing
        # In production: 
        # 1. Receive RTP audio stream
        # 2. Process with STT
        # 3. Generate AI response
        # 4. Synthesize with TTS
        # 5. Send back via RTP
        
        # For now, just log
        logger.info(f"Processing audio for call {call_id}")
        
        # Simulate some work
        await asyncio.sleep(2)
        
        # Example response
        response = {
            'text': 'Hallo, ich bin Ihr Voice Assistant.',
            'latency_ms': 250,
            'confidence': 0.95
        }
        
        call_data['responses'].append(response)
        logger.info(f"Response for {call_id}: {response['text']} (latency: {response['latency_ms']}ms)")
    
    def get_status(self):
        """Get service status"""
        uptime = (datetime.now() - self.start_time).total_seconds()
        return {
            'status': 'running',
            'uptime_seconds': uptime,
            'active_calls': len(self.active_calls),
            'total_calls': self.total_calls,
            'capabilities': {
                'stt': 'azure' if self.has_azure else 'none',
                'tts': 'edge-tts',
                'ai': 'mistral' if self.has_mistral else 'basic'
            }
        }
    
    async def health_check(self):
        """Periodic health check"""
        while True:
            status = self.get_status()
            logger.info(f"Health: {status['active_calls']} active, {status['total_calls']} total")
            await asyncio.sleep(30)  # Every 30 seconds
    
    async def start(self):
        """Start the service"""
        logger.info("Voice AI Service started successfully")
        
        # Start health monitoring
        asyncio.create_task(self.health_check())
        
        # Main loop
        while True:
            # In production: Listen for WebSocket/ARI connections
            await asyncio.sleep(1)
            
            # Simulate incoming call for testing
            if self.total_calls == 0:
                await asyncio.sleep(5)
                logger.info("Simulating test call...")
                await self.handle_call("test-001", {
                    'caller_id': '+4912345678',
                    'called_number': '777z5laknf'
                })

async def main():
    """Main entry point"""
    service = VoiceAIService()
    
    try:
        await service.start()
    except KeyboardInterrupt:
        logger.info("Shutting down Voice AI Service...")
    except Exception as e:
        logger.error(f"Service error: {e}")
        raise

if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════════╗
    ║     Teli24 Voice AI Service v1.0    ║
    ║     Real-time Voice Assistant       ║
    ╚══════════════════════════════════════╝
    """)
    
    asyncio.run(main())