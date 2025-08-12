#!/usr/bin/env python3
"""
Outbound AudioSocket Real-time Call Script
Make outbound calls with <200ms latency Voice AI
"""

import asyncio
import subprocess
import sys
import time

async def make_outbound_call(phone_number, message=""):
    """
    Make an outbound call with AudioSocket real-time Voice AI
    
    Args:
        phone_number (str): Phone number to call (with +49 prefix)
        message (str): Optional initial message to speak
    """
    
    print(f"🚀 Making outbound AudioSocket call to {phone_number}")
    print(f"📞 Using real-time Voice AI with <200ms latency")
    
    if message:
        print(f"💬 Initial message: {message}")
    
    # Format the number for Asterisk
    if not phone_number.startswith('+'):
        phone_number = f"+49{phone_number}"
    
    # Asterisk CLI command to originate call
    # This will:
    # 1. Call the number via PJSIP/fpbx trunk
    # 2. Connect to AudioSocket real-time server
    # 3. Start Voice AI conversation immediately
    
    cmd = [
        'asterisk', '-rx',
        f'channel originate PJSIP/{phone_number}@fpbx '
        f'extension test@outbound-test'
    ]
    
    print(f"📡 Executing: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0:
            print(f"✅ Call initiated successfully!")
            print(f"📋 Asterisk response: {result.stdout.strip()}")
            
            # Monitor call progress
            print(f"🔄 Monitoring call progress...")
            await monitor_call_progress()
            
        else:
            print(f"❌ Call failed!")
            print(f"Error: {result.stderr.strip()}")
            
    except subprocess.TimeoutExpired:
        print(f"⏰ Command timed out - call may still be in progress")
    except Exception as e:
        print(f"💥 Error making call: {e}")

async def monitor_call_progress():
    """Monitor the call progress for a few seconds"""
    
    for i in range(10):
        # Check active channels
        cmd = ['asterisk', '-rx', 'core show channels concise']
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            lines = result.stdout.strip().split('\n')
            active_calls = [line for line in lines if 'PJSIP' in line and 'Up' in line]
            
            if active_calls:
                print(f"📞 Active calls: {len(active_calls)}")
                for call in active_calls[:2]:  # Show first 2
                    parts = call.split('!')
                    if len(parts) > 0:
                        channel = parts[0]
                        print(f"   🔗 {channel}")
            else:
                print(f"📵 No active calls")
                
            await asyncio.sleep(2)
            
        except Exception as e:
            print(f"⚠️  Monitoring error: {e}")
            break

async def test_system():
    """Test the outbound call system"""
    print("🧪 Testing AudioSocket outbound call system...")
    
    # Check if AudioSocket server is running
    cmd = ['netstat', '-tlnp']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if ':9092' in result.stdout:
            print("✅ AudioSocket server is running on port 9092")
        else:
            print("❌ AudioSocket server not found on port 9092")
            print("   Please start: ./venv/bin/python audiosocket_realtime.py")
            return False
    except:
        print("⚠️  Could not check AudioSocket server")
    
    # Check Asterisk status
    cmd = ['asterisk', '-rx', 'core show version']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if 'Asterisk 22' in result.stdout:
            print("✅ Asterisk 22 is running")
        else:
            print("❌ Asterisk 22 not found")
            return False
    except:
        print("❌ Could not connect to Asterisk")
        return False
        
    # Check SIP registration
    cmd = ['asterisk', '-rx', 'pjsip show registrations']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if 'Registered' in result.stdout:
            print("✅ SIP trunk is registered")
        else:
            print("⚠️  SIP trunk registration issue")
            print(result.stdout)
    except:
        print("⚠️  Could not check SIP registration")
    
    print("🎯 System ready for outbound calls!")
    return True

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python make_outbound_call.py <phone_number> [message]")
        print("")
        print("Examples:")
        print("  python make_outbound_call.py +4917012345678")
        print("  python make_outbound_call.py 17012345678")
        print("  python make_outbound_call.py +4917012345678 'Hallo, hier ist der Teli24 Voice AI'")
        print("")
        print("Test system:")
        print("  python make_outbound_call.py test")
        sys.exit(1)
    
    phone_number = sys.argv[1]
    message = sys.argv[2] if len(sys.argv) > 2 else ""
    
    if phone_number == "test":
        asyncio.run(test_system())
    else:
        asyncio.run(make_outbound_call(phone_number, message))