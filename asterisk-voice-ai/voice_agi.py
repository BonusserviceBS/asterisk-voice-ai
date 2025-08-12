#!/usr/bin/env python3
"""
Asterisk AGI Voice Assistant
Simple integration without ARI/Stasis
"""

import sys
import os
import asyncio
import tempfile
from pathlib import Path

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simple_voice_agent import SimpleVoiceAgent

class AGI:
    """Simple AGI interface for Asterisk"""
    
    def __init__(self):
        self.env = {}
        self.parse_agi_environment()
    
    def parse_agi_environment(self):
        """Parse AGI environment variables"""
        while True:
            line = sys.stdin.readline().strip()
            if not line:
                break
            if ':' in line:
                key, value = line.split(':', 1)
                self.env[key.strip()] = value.strip()
    
    def send(self, command):
        """Send command to Asterisk"""
        sys.stdout.write(command + '\n')
        sys.stdout.flush()
        return self.read_response()
    
    def read_response(self):
        """Read response from Asterisk"""
        response = sys.stdin.readline().strip()
        # Parse response code
        if response.startswith('200'):
            return True
        return response
    
    def answer(self):
        """Answer the call"""
        return self.send("ANSWER")
    
    def stream_file(self, filename):
        """Play audio file"""
        # Remove extension for Asterisk
        if filename.endswith('.wav'):
            filename = filename[:-4]
        return self.send(f'STREAM FILE {filename} ""')
    
    def hangup(self):
        """Hangup the call"""
        return self.send("HANGUP")
    
    def say_text(self, text):
        """Say text using Festival or similar"""
        # For now, just log it
        self.send(f'VERBOSE "AI says: {text}" 1')
        return True

async def handle_call(agi):
    """Handle incoming call with Voice AI"""
    try:
        # Answer the call
        agi.answer()
        agi.send('VERBOSE "Voice AI: Call answered" 1')
        
        # Initialize voice agent
        agent = SimpleVoiceAgent()
        
        # Welcome message  
        welcome_text = "Guten Tag! Willkommen bei der Teli24 Voice AI. Ich bin Ihr intelligenter Assistent."
        
        agi.send('VERBOSE "Generating welcome message..." 1')
        
        # Generate and play welcome audio
        try:
            audio_file = await agent.text_to_speech(welcome_text)
            if audio_file and os.path.exists(audio_file):
                # Convert to GSM for better Asterisk compatibility
                gsm_file = audio_file.replace('.wav', '.gsm')
                os.system(f"ffmpeg -i {audio_file} -ar 8000 -ac 1 -f gsm {gsm_file} -y > /dev/null 2>&1")
                
                if os.path.exists(gsm_file):
                    # Move to Asterisk sounds
                    asterisk_file = f"/usr/share/asterisk/sounds/tts_welcome_{os.getpid()}.gsm"
                    os.system(f"sudo mv {gsm_file} {asterisk_file}")
                    
                    # Play without extension
                    basename = os.path.basename(asterisk_file).replace('.gsm', '')
                    agi.send(f'VERBOSE "Playing: {basename}" 1')
                    agi.stream_file(basename)
                    
                    # Cleanup
                    os.system(f"sudo rm -f {asterisk_file}")
                
                os.unlink(audio_file) if os.path.exists(audio_file) else None
        except Exception as e:
            agi.send(f'VERBOSE "TTS Error: {str(e)}" 1')
            # Fallback to built-in sounds
            agi.stream_file("hello-world")
        
        # For now, just play a simple response
        # In production, you would:
        # 1. Record user input
        # 2. Convert to text (STT)
        # 3. Process with AI
        # 4. Convert response to speech (TTS)
        # 5. Play response
        
        # Interactive response
        response_text = "Ich kann Ihnen bei verschiedenen Aufgaben helfen. Zum Beispiel Termine vereinbaren, Informationen suchen oder Fragen beantworten."
        
        agi.send('VERBOSE "Generating response..." 1')
        try:
            audio_file = await agent.text_to_speech(response_text)
            if audio_file and os.path.exists(audio_file):
                gsm_file = audio_file.replace('.wav', '.gsm')
                os.system(f"ffmpeg -i {audio_file} -ar 8000 -ac 1 -f gsm {gsm_file} -y > /dev/null 2>&1")
                
                if os.path.exists(gsm_file):
                    asterisk_file = f"/usr/share/asterisk/sounds/tts_response_{os.getpid()}.gsm"
                    os.system(f"sudo mv {gsm_file} {asterisk_file}")
                    agi.stream_file(os.path.basename(asterisk_file).replace('.gsm', ''))
                    os.system(f"sudo rm -f {asterisk_file}")
                
                os.unlink(audio_file) if os.path.exists(audio_file) else None
        except Exception as e:
            agi.send(f'VERBOSE "Response Error: {str(e)}" 1')
        
        # Wait a moment
        agi.send("WAIT 1")
        
        # Goodbye message
        goodbye_text = "Dies war eine Demonstration der Teli24 Voice AI. Vielen Dank für Ihren Testanruf. Auf Wiederhören!"
        
        agi.send('VERBOSE "Generating goodbye..." 1')
        try:
            audio_file = await agent.text_to_speech(goodbye_text)
            if audio_file and os.path.exists(audio_file):
                gsm_file = audio_file.replace('.wav', '.gsm')
                os.system(f"ffmpeg -i {audio_file} -ar 8000 -ac 1 -f gsm {gsm_file} -y > /dev/null 2>&1")
                
                if os.path.exists(gsm_file):
                    asterisk_file = f"/usr/share/asterisk/sounds/tts_goodbye_{os.getpid()}.gsm"
                    os.system(f"sudo mv {gsm_file} {asterisk_file}")
                    agi.stream_file(os.path.basename(asterisk_file).replace('.gsm', ''))
                    os.system(f"sudo rm -f {asterisk_file}")
                
                os.unlink(audio_file) if os.path.exists(audio_file) else None
        except Exception as e:
            agi.send(f'VERBOSE "Goodbye Error: {str(e)}" 1')
        
    except Exception as e:
        agi.send(f'VERBOSE "Error: {str(e)}" 1')
    finally:
        agi.hangup()

def main():
    """Main entry point"""
    agi = AGI()
    
    # Run async handler
    asyncio.run(handle_call(agi))

if __name__ == "__main__":
    main()