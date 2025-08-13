#!/usr/bin/env python3
"""
ARI Debug Test Application - Debug hangup issues for +4920189098723
This application will:
1. Connect to Asterisk ARI WebSocket
2. Register the voice-ai app 
3. Handle StasisStart events with detailed logging
4. Answer channels and keep them alive
5. Log all events and errors
"""

import asyncio
import aiohttp
import json
import logging
import os
import sys
from urllib.parse import urlencode
from datetime import datetime

# Logging setup
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler('ari_debug_test.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("ari-debug-test")

# Configuration
AST_HTTP = os.environ.get("AST_HTTP", "http://127.0.0.1:8088")
AST_USER = os.environ.get("AST_USER", "ari-user")
AST_PASS = os.environ.get("AST_PASS", "ari123")
APP_NAME = "voice-ai"

class AriDebugTest:
    def __init__(self):
        self.base_http = AST_HTTP.rstrip("/")
        self.user = AST_USER
        self.password = AST_PASS
        self.app = APP_NAME
        self.session = None
        self.ws = None
        self.active_channels = {}
        
    async def start(self):
        """Start the debug test application"""
        log.info("🚀 Starting ARI Debug Test Application")
        log.info(f"📡 Connecting to: {self.base_http}")
        log.info(f"👤 User: {self.user}")
        log.info(f"📱 App: {self.app}")
        
        # Create HTTP session
        self.session = aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(self.user, self.password),
            timeout=aiohttp.ClientTimeout(total=30)
        )
        
        # Test ARI connectivity first
        await self.test_ari_connectivity()
        
        # Connect WebSocket
        await self.connect_websocket()
        
        # Handle events
        await self.handle_events()
        
    async def test_ari_connectivity(self):
        """Test basic ARI connectivity"""
        log.info("🔍 Testing ARI connectivity...")
        
        try:
            # Test basic endpoint
            url = f"{self.base_http}/ari/asterisk/info"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    log.info(f"✅ ARI Connected - Asterisk {data.get('version', 'unknown')}")
                else:
                    log.error(f"❌ ARI connectivity failed: {resp.status}")
                    
        except Exception as e:
            log.error(f"❌ ARI connectivity test failed: {e}")
            
        try:
            # Test applications endpoint
            url = f"{self.base_http}/ari/applications"
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    apps = await resp.json()
                    log.info(f"✅ Available applications: {[app.get('name') for app in apps]}")
                else:
                    log.error(f"❌ Applications endpoint failed: {resp.status}")
                    
        except Exception as e:
            log.error(f"❌ Applications test failed: {e}")
            
    async def connect_websocket(self):
        """Connect to ARI WebSocket"""
        log.info("🔌 Connecting to ARI WebSocket...")
        
        ws_url = self.base_http.replace("https://", "wss://").replace("http://", "ws://")
        qs = urlencode({
            "app": self.app,
            "subscribeAll": "true",
            "api_key": f"{self.user}:{self.password}"
        })
        url = f"{ws_url}/ari/events?{qs}"
        
        log.info(f"📡 WebSocket URL: {url}")
        
        try:
            self.ws = await self.session.ws_connect(url, heartbeat=30)
            log.info("✅ WebSocket connected successfully")
            log.info(f"📱 Application '{self.app}' registered and listening for events")
        except Exception as e:
            log.error(f"❌ WebSocket connection failed: {e}")
            raise
            
    async def handle_events(self):
        """Handle ARI events with detailed logging"""
        log.info("👂 Starting event handler...")
        
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    event = json.loads(msg.data)
                    await self.process_event(event)
                except Exception as e:
                    log.error(f"❌ Event processing error: {e}")
                    log.error(f"📄 Raw message: {msg.data}")
                    
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.error(f"❌ WebSocket error: {self.ws.exception()}")
                break
                
            elif msg.type == aiohttp.WSMsgType.CLOSED:
                log.warning("⚠️ WebSocket closed")
                break
                
    async def process_event(self, event):
        """Process individual ARI events"""
        event_type = event.get("type")
        timestamp = datetime.now().isoformat()
        
        # Log all events
        log.info(f"📨 [{timestamp}] Event: {event_type}")
        
        if event_type == "StasisStart":
            await self.handle_stasis_start(event)
        elif event_type == "StasisEnd":
            await self.handle_stasis_end(event)
        elif event_type == "ChannelHangupRequest":
            await self.handle_hangup_request(event)
        elif event_type == "ChannelDestroyed":
            await self.handle_channel_destroyed(event)
        elif event_type == "ChannelStateChange":
            await self.handle_channel_state_change(event)
        else:
            # Log other events briefly
            channel = event.get("channel", {})
            channel_id = channel.get("id", "unknown")
            log.debug(f"📋 {event_type}: {channel_id}")
            
    async def handle_stasis_start(self, event):
        """Handle StasisStart event with detailed analysis"""
        log.info("🎯 STASIS START EVENT RECEIVED")
        log.info("="*60)
        
        # Extract channel information
        channel = event.get("channel", {})
        channel_id = channel.get("id", "unknown")
        channel_name = channel.get("name", "unknown")
        state = channel.get("state", "unknown")
        caller_id = channel.get("caller", {})
        caller_number = caller_id.get("number", "unknown")
        caller_name = caller_id.get("name", "unknown")
        
        # Extract dialplan information
        dialplan = channel.get("dialplan", {})
        context = dialplan.get("context", "unknown")
        exten = dialplan.get("exten", "unknown")
        priority = dialplan.get("priority", "unknown")
        
        # Extract application information  
        args = event.get("args", [])
        app_name = event.get("application", "unknown")
        
        log.info(f"📱 Channel ID: {channel_id}")
        log.info(f"📱 Channel Name: {channel_name}")
        log.info(f"📱 Channel State: {state}")
        log.info(f"📞 Caller Number: {caller_number}")
        log.info(f"📞 Caller Name: {caller_name}")
        log.info(f"🎯 Context: {context}")
        log.info(f"🎯 Extension: {exten}")
        log.info(f"🎯 Priority: {priority}")
        log.info(f"🎯 Application: {app_name}")
        log.info(f"🎯 Arguments: {args}")
        
        # Check if this is our target number
        if exten == "4920189098723":
            log.info("🎯 TARGET NUMBER DETECTED: +4920189098723")
            
        # Store channel info
        self.active_channels[channel_id] = {
            "name": channel_name,
            "state": state,
            "caller_number": caller_number,
            "exten": exten,
            "context": context,
            "start_time": datetime.now().isoformat()
        }
        
        log.info("="*60)
        
        # Answer the channel immediately
        try:
            await self.answer_channel(channel_id)
            log.info("✅ Channel answered successfully")
        except Exception as e:
            log.error(f"❌ Failed to answer channel: {e}")
            
        # Keep channel alive by playing continuous audio
        asyncio.create_task(self.keep_alive(channel_id))
        
    async def handle_stasis_end(self, event):
        """Handle StasisEnd event"""
        channel = event.get("channel", {})
        channel_id = channel.get("id", "unknown")
        
        log.info(f"🔚 STASIS END: {channel_id}")
        
        if channel_id in self.active_channels:
            channel_info = self.active_channels[channel_id]
            duration = datetime.now().isoformat()
            log.info(f"📊 Call duration from {channel_info['start_time']} to {duration}")
            
            # Check if this was our target number
            if channel_info.get("exten") == "4920189098723":
                log.error("🚨 TARGET NUMBER CALL ENDED - THIS IS THE PROBLEM!")
                
            del self.active_channels[channel_id]
            
    async def handle_hangup_request(self, event):
        """Handle ChannelHangupRequest event"""
        channel = event.get("channel", {})
        channel_id = channel.get("id", "unknown")
        cause = event.get("cause", "unknown")
        cause_txt = event.get("cause_txt", "unknown")
        
        log.warning(f"📴 HANGUP REQUEST: {channel_id}")
        log.warning(f"📴 Cause: {cause} - {cause_txt}")
        
        if channel_id in self.active_channels:
            channel_info = self.active_channels[channel_id]
            if channel_info.get("exten") == "4920189098723":
                log.error("🚨 TARGET NUMBER HANGUP REQUEST!")
                log.error(f"🚨 Cause: {cause} - {cause_txt}")
                
    async def handle_channel_destroyed(self, event):
        """Handle ChannelDestroyed event"""
        channel = event.get("channel", {})
        channel_id = channel.get("id", "unknown")
        cause = event.get("cause", "unknown")
        cause_txt = event.get("cause_txt", "unknown")
        
        log.warning(f"💀 CHANNEL DESTROYED: {channel_id}")
        log.warning(f"💀 Cause: {cause} - {cause_txt}")
        
    async def handle_channel_state_change(self, event):
        """Handle ChannelStateChange event"""
        channel = event.get("channel", {})
        channel_id = channel.get("id", "unknown")
        old_state = event.get("old_state", "unknown")
        new_state = event.get("new_state", "unknown")
        
        log.info(f"🔄 STATE CHANGE: {channel_id} - {old_state} → {new_state}")
        
        if channel_id in self.active_channels:
            self.active_channels[channel_id]["state"] = new_state
            
    async def answer_channel(self, channel_id):
        """Answer a channel"""
        url = f"{self.base_http}/ari/channels/{channel_id}/answer"
        async with self.session.post(url) as resp:
            if resp.status not in [200, 204]:
                body = await resp.text()
                raise RuntimeError(f"Answer failed: {resp.status} - {body}")
                
    async def play_audio(self, channel_id, media):
        """Play audio on a channel"""
        url = f"{self.base_http}/ari/channels/{channel_id}/play"
        params = {"media": f"sound:{media}"}
        async with self.session.post(url, params=params) as resp:
            if resp.status not in [200, 201]:
                body = await resp.text()
                log.error(f"Play audio failed: {resp.status} - {body}")
                return None
            return await resp.json()
            
    async def keep_alive(self, channel_id):
        """Keep channel alive by playing periodic audio"""
        log.info(f"🔄 Starting keep-alive for {channel_id}")
        
        try:
            # Wait a moment after answering
            await asyncio.sleep(1)
            
            # Play hello-world first
            await self.play_audio(channel_id, "hello-world")
            await asyncio.sleep(3)
            
            # Keep playing audio every 10 seconds to prevent timeout
            counter = 0
            while channel_id in self.active_channels:
                counter += 1
                log.info(f"🔄 Keep-alive {counter} for {channel_id}")
                
                # Alternate between different sounds
                sounds = ["demo-thanks", "demo-abouttotry", "digits/1", "digits/2"]
                sound = sounds[counter % len(sounds)]
                
                await self.play_audio(channel_id, sound)
                await asyncio.sleep(10)
                
        except Exception as e:
            log.error(f"❌ Keep-alive error for {channel_id}: {e}")
            
    async def stop(self):
        """Stop the application"""
        log.info("🛑 Stopping ARI Debug Test Application")
        
        if self.ws:
            await self.ws.close()
            
        if self.session:
            await self.session.close()

async def main():
    """Main entry point"""
    app = AriDebugTest()
    
    try:
        await app.start()
    except KeyboardInterrupt:
        log.info("⌨️ Keyboard interrupt received")
    except Exception as e:
        log.error(f"❌ Application error: {e}")
    finally:
        await app.stop()

if __name__ == "__main__":
    print("🚀 ARI Debug Test Application Starting...")
    print("📱 This will register the 'voice-ai' application and debug hangup issues")
    print("📞 Call +4920189098723 to test")
    print("🛑 Press Ctrl+C to stop")
    print("="*60)
    
    asyncio.run(main())