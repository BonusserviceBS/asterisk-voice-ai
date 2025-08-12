#!/usr/bin/env python3
"""
Simple Voice Agent for Asterisk
Works with AGI for direct integration
"""

import os
import sys
import asyncio
import logging
from pathlib import Path
import edge_tts
from mistralai import Mistral
import tempfile
import subprocess
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SimpleVoiceAgent:
    def __init__(self):
        # Configuration
        self.mistral_api_key = os.getenv('MISTRAL_API_KEY', '')
        self.voice = 'de-DE-ConradNeural'  # German male voice
        self.language = 'de-DE'
        
        # Initialize Mistral if key available
        if self.mistral_api_key:
            self.mistral_client = Mistral(api_key=self.mistral_api_key)
            self.model = "mistral-large-latest"
        else:
            self.mistral_client = None
            logger.warning("No Mistral API key found, using fallback responses")
        
        # Conversation history
        self.messages = [{
            "role": "system",
            "content": "Du bist ein freundlicher Assistent. Antworte kurz und präzise auf Deutsch."
        }]
    
    async def generate_response(self, user_input: str) -> str:
        """Generate AI response using Mistral or fallback"""
        if self.mistral_client:
            try:
                self.messages.append({"role": "user", "content": user_input})
                
                response = self.mistral_client.chat.complete(
                    model=self.model,
                    messages=self.messages
                )
                
                ai_response = response.choices[0].message.content
                self.messages.append({"role": "assistant", "content": ai_response})
                
                return ai_response
            except Exception as e:
                logger.error(f"Mistral API error: {e}")
                return "Entschuldigung, ich konnte Ihre Anfrage nicht verarbeiten."
        else:
            # Simple fallback responses
            if "hallo" in user_input.lower():
                return "Hallo! Wie kann ich Ihnen helfen?"
            elif "wie geht" in user_input.lower():
                return "Mir geht es gut, danke! Und Ihnen?"
            elif "tschüss" in user_input.lower() or "auf wiedersehen" in user_input.lower():
                return "Auf Wiedersehen! Schönen Tag noch!"
            else:
                return "Ich verstehe. Können Sie das bitte näher erläutern?"
    
    async def text_to_speech(self, text: str) -> str:
        """Convert text to speech using Edge-TTS"""
        try:
            # Create temp file for audio
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp_file:
                output_file = tmp_file.name
            
            # Generate speech
            communicate = edge_tts.Communicate(text, self.voice)
            await communicate.save(output_file)
            
            # Convert to 8kHz for Asterisk
            output_8k = output_file.replace('.wav', '_8k.wav')
            # Use ffmpeg instead of sox for better compatibility
            subprocess.run([
                'ffmpeg', '-i', output_file, '-ar', '8000', '-ac', '1', 
                '-y', output_8k
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Clean up original
            os.unlink(output_file)
            
            return output_8k
        except Exception as e:
            logger.error(f"TTS error: {e}")
            return None

async def main():
    """Test the voice agent"""
    agent = SimpleVoiceAgent()
    
    # Test conversation
    test_inputs = [
        "Hallo, wie geht es dir?",
        "Was ist das Wetter heute?",
        "Auf Wiedersehen!"
    ]
    
    for user_input in test_inputs:
        print(f"\nUser: {user_input}")
        
        # Generate response
        response = await agent.generate_response(user_input)
        print(f"AI: {response}")
        
        # Generate audio (optional)
        audio_file = await agent.text_to_speech(response)
        if audio_file:
            print(f"Audio saved to: {audio_file}")
            # Clean up
            os.unlink(audio_file)

if __name__ == "__main__":
    asyncio.run(main())