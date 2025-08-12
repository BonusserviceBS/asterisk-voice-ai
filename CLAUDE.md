# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## System Architecture Overview

### PROFESSIONAL ARCHITECTURE: External Media with RTP (Production Ready)

```
┌─────────────┐     SIP (Signaling)     ┌──────────────┐     HTTP API      ┌─────────────┐
│  Placetel   │ ←──────────────────────→ │  Asterisk 22 │ ←───────────────→ │  Voice AI   │
│   Trunk     │                          │    (PJSIP)   │                   │  HTTP:8080  │
└─────────────┘                          └──────────────┘                   └─────────────┘
      ↓                                        ↓                                   ↓
   RTP Audio                            ExternalMedia()                      RTP Server
      ↓                                        ↓                                   ↓
┌─────────────┐     RTP (Audio Stream)  ┌──────────────┐                   ┌─────────────┐
│  Placetel   │ ←──────────────────────→│  RTP Server  │                   │  Voice AI   │
│  RTP:10000  │     Bidirectional        │  UDP:10000   │                   │  Processing │
└─────────────┘     G.711 μ-law/A-law   └──────────────┘                   └─────────────┘
```

**Why External Media RTP is Professional:**
- Used by Twilio, Vonage, Amazon Connect, Google Cloud
- Direct RTP stream - no TCP overhead
- Full control over jitter buffer and timing
- Supports DTMF, comfort noise, packet loss concealment
- Scales to thousands of concurrent calls
- True bidirectional real-time audio

### OLD ARCHITECTURE (Deprecated): AudioSocket
```
Problems with AudioSocket:
- Experimental/unstable
- TCP-based (adds latency)
- Connection drops after 2-3 responses
- No production deployments
- Not suitable for bidirectional communication
```

## Core Implementation

### Current: External Media RTP Architecture

The system uses Asterisk's ExternalMedia application to establish direct RTP streams between the caller and our Voice AI server. This is the same architecture used by major cloud providers.

**Components:**
1. **RTP Server** (`rtp_voice_ai.py`): Handles bidirectional RTP audio streams
2. **HTTP API**: Receives External Media requests from Asterisk
3. **Codec Support**: G.711 μ-law/A-law (standard telephony)
4. **Port Range**: UDP 10000-20000 for RTP

### Key Components

1. **Asterisk Configuration** (`/etc/asterisk/`)
   - `pjsip.conf`: SIP trunk registration with Placetel (777z5laknf@fpbx.de)
   - `extensions_voice_ai.conf`: Dialplan routing calls to AudioSocket
   - Phone numbers: 4920189098720-723 routed to AudioSocket on port 9092

2. **Voice AI Implementations** (`/home/admini/asterisk-voice-ai/`)
   - **`rtp_voice_ai.py`**: PRODUCTION - External Media RTP server (recommended)
   - **`ari_voice_ai.py`**: Alternative using Asterisk REST Interface
   - Legacy AudioSocket attempts (deprecated):
     - `fast_voice_ai.py`: Had connection stability issues
     - `stable_voice_ai.py`: Workarounds for AudioSocket problems
     - Various other attempts to fix AudioSocket (not production ready)

3. **Database Integration**
   - PostgreSQL on 10.0.0.5 (teli24_development)
   - Assistant configurations with greetings and system prompts
   - Phone number to assistant mapping

## Common Development Commands

### Starting the RTP Voice AI Server (PRODUCTION)
```bash
# Kill any existing processes
pkill -f "voice_ai.py" || true

# Start RTP server
cd /home/admini/asterisk-voice-ai
DB_PASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' ./venv/bin/python rtp_voice_ai.py

# Or run in background
DB_PASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' nohup ./venv/bin/python rtp_voice_ai.py > rtp_voice.log 2>&1 &

# Monitor RTP traffic
sudo tcpdump -i any -n udp port 10000 -v
```

### Configuring Asterisk for External Media
```ini
; In extensions.conf or extensions_rtp.conf
exten => 4920189098723,1,NoOp(RTP Voice AI)
 same => n,Answer()
 same => n,ExternalMedia(channel,${CHANNEL},http://127.0.0.1:8080/externalmedia)
 same => n,Hangup()
```

### Testing Calls
```bash
# Check SIP registration
asterisk -rx "pjsip show registrations"

# Make test call
asterisk -rx "channel originate PJSIP/4920189098723@fpbx application Playback hello-world"

# Monitor Asterisk console
asterisk -rvvv

# Watch SIP traffic
sudo tcpdump -i any -n port 5060 -v
```

### Database Queries
```bash
# Check assistant configuration
PGPASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' psql -U teli24_user -h 10.0.0.5 -d teli24_development -c "
  SELECT a.name, a.greeting, p.number 
  FROM \"Assistant\" a 
  JOIN \"PhoneNumber\" p ON a.id = p.\"assignedToAssistantId\" 
  WHERE p.number LIKE '%4920189098723%'
"
```

### Debugging
```bash
# Check Voice AI logs
tail -f /home/admini/asterisk-voice-ai/voice_ai.log

# Asterisk logs
sudo tail -f /var/log/asterisk/messages.log

# Check if AudioSocket is listening
ss -tlnp | grep 9092
```

## RTP Protocol Implementation

### RTP Packet Structure
```python
# RTP Header (12 bytes)
0                   1                   2                   3
0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|V=2|P|X|  CC   |M|     PT      |       sequence number         |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                           timestamp                           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|           synchronization source (SSRC) identifier            |
+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+
```

### Codec Support
- **G.711 μ-law** (PT=0): North America, Japan
- **G.711 A-law** (PT=8): Europe
- **Packet size**: 160 bytes (20ms at 8kHz)
- **Sample rate**: 8000 Hz
- **Bit depth**: 8-bit (compressed from 16-bit)

### Port Configuration
```bash
# RTP uses even ports, RTCP uses odd ports
RTP_PORT=10000   # Audio stream
RTCP_PORT=10001  # Control (optional)
HTTP_PORT=8080   # External Media API
```

## Critical Implementation Notes

### RTP Stream Management

#### Connection Establishment
1. Asterisk calls ExternalMedia() in dialplan
2. HTTP POST to our server with channel info
3. We respond with RTP endpoint details
4. Asterisk establishes bidirectional RTP stream

#### Stream Characteristics
- **Continuous flow**: 50 packets/second (20ms each)
- **Jitter buffer**: Handle ±40ms variation
- **Packet loss**: Tolerate up to 3% loss
- **DTMF**: RFC 4733 events or inband
- **Comfort noise**: During silence periods

### Voice Activity Detection (VAD)
- Energy threshold: 200-300 for 8kHz audio
- Minimum speech: 300-500ms
- End-of-speech silence: 400-800ms
- Buffer management: Keep max 10 seconds, process incrementally

### Latency Optimization
- Use `mistral-small-latest` for <200ms AI responses
- Stream TTS chunks as they generate (don't wait for full synthesis)
- Process STT immediately on silence detection
- Target total latency: <600ms user-to-response

## CURRENT PROBLEM STATUS

### Problem: "legt sofort auf!" (Hangs up immediately)
**Status**: UNRESOLVED - Call drops immediately when dialing 4920189098723

**Root Cause Analysis**:
1. AudioSocket is fundamentally unstable for production use
2. ExternalMedia in Asterisk 22 works via ARI REST API, not as dialplan application
3. ARI WebSocket connection established but Stasis events not triggering properly
4. Dialplan routes to Stasis(voice-ai) but connection drops before processing

**Attempted Solutions**:
1. ❌ AudioSocket with various keep-alive mechanisms - unstable, drops after 2-3 responses
2. ❌ Direct ExternalMedia() in dialplan - not supported in Asterisk 22
3. ⚠️ ARI with Stasis app - partially working, WebSocket connects but calls drop
4. ⚠️ RTP External Media via ARI - implemented but not fully tested

## TODO LIST (Priority Order)

### IMMEDIATE (Fix "legt sofort auf!")
- [ ] Debug why Stasis(voice-ai) causes immediate hangup
- [ ] Check ARI user permissions in ari.conf
- [ ] Verify http.conf allows WebSocket connections
- [ ] Test with simple ARI echo application first
- [ ] Monitor SIP/RTP traffic during failed calls

### Phase 1: Get Basic Call Working
- [ ] Establish stable bidirectional audio (any method)
- [ ] Fix immediate hangup issue
- [ ] Get greeting to play without dropping
- [ ] Handle at least 3 conversation turns without dropping

### Phase 2: Stabilization
- [x] Female voice from database
- [x] Conversation history/context
- [ ] Proper error recovery
- [ ] Connection monitoring
- [ ] Graceful degradation

### Phase 3: Alternative Solutions
- [ ] Consider FreeSWITCH instead of Asterisk
- [ ] Evaluate LiveKit SIP Gateway
- [ ] Test Jambonz for voice AI
- [ ] Try Twilio Elastic SIP Trunking with Media Streams

## CRITICAL INSIGHTS

### Why Current Setup Fails
1. **AudioSocket is not production-ready** - It's experimental and unstable
2. **Asterisk 22 ExternalMedia** requires full ARI implementation, not simple dialplan
3. **TCP-based audio (AudioSocket)** has inherent timing issues vs UDP (RTP)
4. **No proper jitter buffer** in our implementation
5. **Missing RTCP** for quality control

### What Works in Production (Industry Standard)
- **Twilio**: Uses Media Streams (WebSocket) with mulaw/alaw
- **Amazon Connect**: Uses Kinesis Video Streams
- **Google Cloud**: Uses bidirectional streaming gRPC
- **Vonage**: Uses WebSocket with RTP
- **LiveKit**: Uses WebRTC with proper signaling

### Recommended Solution
**STOP trying to fix AudioSocket** - it will never be stable enough for production.
**IMPLEMENT** proper ARI with ExternalMedia or switch to a modern platform.

## Testing the RTP Implementation

```bash
# 1. Start RTP server
DB_PASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' ./venv/bin/python rtp_voice_ai.py

# 2. Configure Asterisk to use External Media
asterisk -rx "dialplan reload"

# 3. Make test call
asterisk -rx "channel originate PJSIP/4920189098723@fpbx application Playback hello-world"

# 4. Monitor RTP packets
sudo tcpdump -i lo -n udp port 10000 -X
```

## Environment Configuration

### Required Environment Variables
```bash
# Azure Speech Services (Required for production)
AZURE_SPEECH_KEY=<key>
AZURE_SPEECH_REGION=germanywestcentral

# Database (PostgreSQL)
DB_HOST=10.0.0.5
DB_PASSWORD=BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW

# Mistral AI (Hardcoded in files, should be moved to env)
MISTRAL_API_KEY=rbBHVYVpt7Oe2HdUVKNoZLQ7cTilajyb
```

### File Locations
- Python implementations: `/home/admini/asterisk-voice-ai/`
- Asterisk config: `/etc/asterisk/`
- Logs: `/var/log/asterisk/messages.log`
- Virtual environment: `/home/admini/asterisk-voice-ai/venv/`

### Dependencies
```bash
# Python packages (in venv)
azure-cognitiveservices-speech
asyncpg
aiohttp
numpy
python-dotenv

# System requirements
Asterisk 22 LTS (with AudioSocket support)
Python 3.12+
PostgreSQL client libraries
```

## Specialized Sub-Agents Architecture

### Available Agents for Voice AI System

The following specialized agents have been created to handle different aspects of the Voice AI system:

#### 1. **asterisk-config-agent** & **asterisk-config-manager**
- **Purpose**: Manage Asterisk 22 LTS configuration
- **Responsibilities**: 
  - PJSIP/SIP configuration
  - Dialplan management (extensions.conf)
  - Module loading and codec negotiation
  - Registration monitoring
- **Key Files**: `/etc/asterisk/pjsip.conf`, `/etc/asterisk/extensions*.conf`

#### 2. **ari-bridge-agent**
- **Purpose**: Handle ARI (Asterisk REST Interface) operations
- **Responsibilities**:
  - WebSocket connection management
  - Stasis application registration
  - Channel bridging and control
  - Event subscription handling
- **Key Commands**: `ari show apps`, ARI REST API calls

#### 3. **external-media-rtp**
- **Purpose**: Implement RTP-based External Media streaming
- **Responsibilities**:
  - RTP socket creation and management
  - Media format negotiation (G.711 μ-law/a-law)
  - Packet timing (20ms frames)
  - SSRC and sequence number management
- **Key Files**: `ari_external_media.py`, `rtp_voice_ai.py`

#### 4. **audio-quality-optimizer** & **audio-quality-diagnostics**
- **Purpose**: Optimize audio pipeline and diagnose quality issues
- **Responsibilities**:
  - Audio resampling (8kHz ↔ 16kHz ↔ 24kHz)
  - Echo cancellation configuration
  - Jitter buffer optimization
  - Voice Activity Detection (VAD)
  - Analyze choppy audio problems
  - Debug codec mismatches
- **Key Tools**: sox, ffmpeg, webrtcvad

#### 5. **stt-tts-integration**
- **Purpose**: Integrate speech services
- **Responsibilities**:
  - Azure Speech Services configuration
  - Edge-TTS implementation
  - Streaming synthesis and recognition
  - Voice selection from database
- **Key APIs**: Azure Cognitive Services, Edge-TTS

#### 6. **database-assistant-manager**
- **Purpose**: Manage assistant configurations from database
- **Responsibilities**:
  - Query PostgreSQL for assistant settings
  - Parse voice configurations
  - Manage conversation history
  - Handle system prompts
- **Database**: `teli24_development` on 10.0.0.5

#### 7. **performance-monitor**
- **Purpose**: Monitor system performance and metrics
- **Responsibilities**:
  - Track call setup time
  - Measure STT/TTS/LLM latency
  - Monitor concurrent calls
  - Export Prometheus metrics
- **Target Metrics**: <600ms E2E latency

#### 8. **debug-troubleshoot-agent**
- **Purpose**: Debug and troubleshoot issues
- **Responsibilities**:
  - Analyze "sofort aufgelegt" (immediate hangup) issues
  - Debug RTP stream problems
  - Trace SIP messages
  - Identify performance bottlenecks
- **Key Tools**: tcpdump, asterisk CLI, log analysis

### Agent Usage Examples

```bash
# When audio quality is poor
# Use: audio-quality-optimizer or audio-quality-diagnostics

# When calls hang up immediately  
# Use: debug-troubleshoot-agent

# When implementing External Media
# Use: external-media-rtp + ari-bridge-agent

# When configuring Asterisk
# Use: asterisk-config-agent or asterisk-config-manager

# When integrating STT/TTS
# Use: stt-tts-integration

# When fetching assistant data
# Use: database-assistant-manager

# When monitoring performance
# Use: performance-monitor
```

### Current Agent Tasks

- **asterisk-config-agent**: Configure PJSIP for stable registration
- **ari-bridge-agent**: Fix Stasis application immediate hangup
- **external-media-rtp**: Implement bidirectional RTP streaming
- **audio-quality-optimizer**: Reduce audio latency to <200ms
- **debug-troubleshoot-agent**: Solve "wird immer noch sofort aufgelegt" issue