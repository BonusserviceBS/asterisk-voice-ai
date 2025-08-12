#!/usr/bin/python3
"""
Simple Asterisk AGI Script for Voice AI Testing
"""
import sys
import os

class AGI:
    def __init__(self):
        self.env = {}
        # Read AGI environment
        while True:
            line = sys.stdin.readline().strip()
            if not line:
                break
            if ':' in line:
                key, value = line.split(':', 1)
                self.env[key.strip()] = value.strip()
    
    def send(self, command):
        """Send AGI command"""
        sys.stdout.write(command + '\n')
        sys.stdout.flush()
        # Read response
        response = sys.stdin.readline().strip()
        return response
    
    def answer(self):
        return self.send("ANSWER")
    
    def hangup(self):
        return self.send("HANGUP")
    
    def stream_file(self, filename):
        return self.send(f'STREAM FILE {filename} ""')
    
    def wait(self, seconds):
        return self.send(f"WAIT {seconds}")
    
    def verbose(self, msg):
        return self.send(f'VERBOSE "{msg}" 1')

def main():
    agi = AGI()
    
    try:
        # Log call
        agi.verbose("Voice AI: Starting...")
        
        # Answer the call
        agi.answer()
        agi.verbose("Voice AI: Call answered")
        
        # Wait a moment
        agi.wait(1)
        
        # Play greeting
        agi.verbose("Voice AI: Playing greeting")
        agi.stream_file("demo-congrats")
        
        # Wait
        agi.wait(2)
        
        # Play more
        agi.stream_file("demo-thanks")
        
        # Wait before hangup
        agi.wait(1)
        
        agi.verbose("Voice AI: Done")
        
    except Exception as e:
        agi.verbose(f"Error: {str(e)}")
    
    finally:
        agi.hangup()

if __name__ == "__main__":
    main()