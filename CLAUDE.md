# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## System Overview

This is a multi-component Voice AI telephony system that enables AI-powered phone conversations. The system uses Asterisk PBX for telephony, integrates with Azure and Mistral AI for speech and conversation services, and provides real-time voice interactions through RTP streaming.

## Architecture Components

### Core Services
- **Asterisk PBX** (Server 6): Handles SIP telephony with Placetel trunk
- **ARI-Bridge** (Server 6): Manages External Media channels via Asterisk REST Interface
- **Voice AI Core** (Server 7): Processes audio streams, STT/TTS, and AI conversations
- **Teli24 Middleware** (Server 7): FastAPI service bridging frontend with backend services
- **PostgreSQL Database** (Server 5): Stores assistant configs, call records, and transcripts

### Key Technologies
- **Language**: Python 3.12+
- **Telephony**: Asterisk 22 LTS with ARI (Asterisk REST Interface)
- **Audio**: RTP/RTCP for real-time audio streaming
- **AI Services**: Azure Speech Services, Mistral AI, OpenAI GPT
- **Framework**: FastAPI for middleware, asyncio for concurrent operations
- **Database**: PostgreSQL with asyncpg

## Common Development Commands

### Starting Services

```bash
# Start Voice AI Core (Production)
cd /home/admini/asterisk-voice-ai
DB_PASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' ./venv/bin/python ari_snoop_final.py

# Start Teli24 Middleware
cd /home/admini/archivalles/teli24-middleware
source venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080

# Monitor Asterisk
asterisk -rvvv

# Check SIP registration
asterisk -rx "pjsip show registrations"

# Check active channels
asterisk -rx "core show channels"
```

### Testing & Debugging

```bash
# Test call to Voice AI
asterisk -rx "channel originate PJSIP/4920189098723@fpbx application Playback hello-world"

# Monitor RTP traffic
sudo tcpdump -i any -n udp portrange 15000-16000 -v

# Check Voice AI logs
tail -f /home/admini/asterisk-voice-ai/ari_final.log

# Database queries
PGPASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' psql -U teli24_user -h 10.0.0.5 -d teli24_development

# Check call records
PGPASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' psql -U teli24_user -h 10.0.0.5 -d teli24_development -c "SELECT * FROM \"CallRecord\" ORDER BY \"startTime\" DESC LIMIT 5;"
```

### Process Management

```bash
# Find and kill processes
pgrep -f "ari_snoop_final.py"
pkill -f "ari_snoop_final.py"

# Check port usage
ss -tlnp | grep -E "8080|8088|5060"
lsof -i :8080

# Service management
sudo systemctl status asterisk
sudo systemctl restart asterisk
```

## High-Level Architecture

### Call Flow
1. **Inbound Call**: Placetel SIP → Asterisk → Stasis(teli24-voice-ai)
2. **ARI Processing**: StasisStart → Create SnoopChannel + External Media
3. **Audio Pipeline**: 
   - Caller → SnoopChannel → RTP → STT → AI → TTS → RTP → External Media → Caller
4. **Recording**: Real-time transcription + MP3 recording saved to database

### Port Allocation
- **5060**: SIP signaling
- **8088**: Asterisk HTTP/WebSocket (ARI)
- **8080**: Teli24 Middleware API
- **15000-16000**: RTP audio streams (500 port pairs)
- **5432**: PostgreSQL database

### Audio Processing
- **Inbound**: G.711 μ-law/A-law → 16kHz PCM for STT
- **Outbound**: 24kHz synthesis → 8kHz G.711 for telephony
- **VAD**: Voice Activity Detection with 400-800ms silence threshold
- **Jitter Buffer**: Handles ±40ms network variation

## Critical Implementation Notes

### ✅ Current Architecture: SnoopChannel + External Media
The system uses a hybrid approach:
- **SnoopChannel**: Captures caller audio (unidirectional, no echo)
- **External Media**: Plays AI responses back to caller
- **Benefits**: No feedback loops, industry-standard approach

### ❌ Avoid These Patterns
- **AudioSocket**: Experimental, unstable, TCP-based (never use)
- **Bidirectional External Media**: Causes feedback and echo
- **Direct RTP binding on Server 7**: Causes port conflicts

### Database Schema
Key tables:
- `Assistant`: AI assistant configurations
- `PhoneNumber`: Phone number to assistant mapping
- `CallRecord`: Call metadata and recordings
- `CallTranscript`: Real-time conversation transcripts
- `CallRecording`: MP3 audio files

### Environment Variables
```bash
# Database
DB_HOST=10.0.0.5
DB_PASSWORD=BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW

# Azure Speech
AZURE_SPEECH_KEY=<key>
AZURE_SPEECH_REGION=germanywestcentral

# Mistral AI
MISTRAL_API_KEY=rbBHVYVpt7Oe2HdUVKNoZLQ7cTilajyb
```

## Project Structure

```
/home/admini/
├── asterisk-voice-ai/        # Voice AI implementations
│   ├── ari_snoop_final.py    # Production ARI bridge
│   ├── requirements.txt      # Python dependencies
│   └── venv/                 # Virtual environment
├── archivalles/
│   └── teli24-middleware/    # FastAPI middleware service
│       ├── app/              # Application code
│       ├── requirements.txt  # Dependencies
│       └── venv/            # Virtual environment
└── config/
    └── asterisk/            # Asterisk configurations
        ├── pjsip.conf       # SIP settings
        └── extensions.conf  # Dialplan

/etc/asterisk/               # System Asterisk config
├── ari.conf                 # ARI settings
├── http.conf               # HTTP/WebSocket config
└── extensions_voice_ai.conf # Voice AI dialplan
```

## Troubleshooting Guide

### Common Issues

**"Anruf wird sofort aufgelegt" (Call drops immediately)**
- Check ARI WebSocket connection
- Verify Stasis application is running
- Check `ari.conf` for correct user/password
- Monitor with `asterisk -rvvv`

**No audio/Silent calls**
- Check RTP port allocation (15000-16000)
- Verify codec negotiation (G.711)
- Monitor with `tcpdump -i any udp portrange 15000-16000`

**STT not recognizing speech**
- Verify audio format (16kHz, 16-bit PCM)
- Check Azure credentials and region
- Test VAD sensitivity settings

**High latency**
- Use Mistral small model for faster responses
- Enable TTS streaming
- Check network between servers

## Testing Procedures

### Basic Health Check
```bash
# 1. Check all services
ps aux | grep -E "asterisk|ari_snoop|uvicorn"

# 2. Verify SIP registration
asterisk -rx "pjsip show registrations" | grep Registered

# 3. Test database connection
PGPASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' psql -U teli24_user -h 10.0.0.5 -d teli24_development -c "SELECT 1;"

# 4. Check middleware health
curl http://localhost:8080/api/health
```

### End-to-End Call Test
```bash
# 1. Start monitoring
asterisk -rvvv  # In terminal 1
tail -f /home/admini/asterisk-voice-ai/ari_final.log  # In terminal 2

# 2. Make test call
asterisk -rx "channel originate PJSIP/4920189098723@fpbx application Playback hello-world"

# 3. Verify in database
PGPASSWORD='BlLNl1jMWpZC9_td5bFVUKgAvtI3LQkW' psql -U teli24_user -h 10.0.0.5 -d teli24_development \
  -c "SELECT * FROM \"CallRecord\" WHERE \"startTime\" > NOW() - INTERVAL '5 minutes';"
```

## Important File Locations

- **Production Voice AI**: `/home/admini/asterisk-voice-ai/ari_snoop_final.py`
- **Middleware Service**: `/home/admini/archivalles/teli24-middleware/app/main.py`
- **Asterisk Dialplan**: `/etc/asterisk/extensions_voice_ai.conf`
- **ARI Config**: `/etc/asterisk/ari.conf`
- **Systemd Services**: `/etc/systemd/system/teli24-*.service`
- **Logs**: `/var/log/asterisk/`, `/home/admini/asterisk-voice-ai/*.log`

## Current Status & Known Issues

### ✅ Working (Production Ready!)
- **Complete Call Flow**: Greeting → Conversation → AI Responses → TTS Playback
- **STT Recognition**: Optimized for complete sentences (2-second audio chunks)
- **AI Conversations**: Clean system prompt prevents prompt injection issues
- **TTS Audio Output**: Bidirectional RTP with μ-law conversion works
- **SnoopChannel + External Media**: Clean audio capture without feedback
- **Database Integration**: Call recording, transcripts, assistant configs
- **Multi-assistant Support**: Dynamic assistant selection by phone number

### 🔧 Recent Critical Fixes (August 2025)
1. **STT Word Fragmentation** - Fixed audio chunks being too small (1s→2s)
2. **Prompt Injection Attack** - Replaced massive DB system prompt with clean version
3. **Audio Timeout Issues** - Optimized STT timeouts for natural speech patterns
4. **Conversation Flow** - Fixed AttributeError in caller identification

### ⚠️ Monitoring Points
- **STT Quality**: Monitor for word fragmentation in complex sentences
- **Response Latency**: Target <2s from speech to TTS playback
- **Audio Quality**: Check for stuttering or interruptions in TTS output

### 🎯 Optimization Opportunities
- **Latency Reduction**: Pre-cache common TTS responses
- **Error Handling**: Better fallbacks for Azure service outages
- **DTMF Support**: Handle keypad input during conversations
- **Call Analytics**: Enhanced metrics and conversation insights