#!/usr/bin/env python3
"""
Test script to analyze RTP packet construction and identify potential audio issues.
"""

import struct
import socket
import asyncio
import os

def analyze_rtp_packet(packet_data):
    """Analyze RTP packet structure"""
    if len(packet_data) < 12:
        return None
    
    # Parse RTP header
    v_p_x_cc, m_pt, seq, ts, ssrc = struct.unpack("!BBHII", packet_data[:12])
    
    version = (v_p_x_cc >> 6) & 0x3
    padding = (v_p_x_cc >> 5) & 0x1
    extension = (v_p_x_cc >> 4) & 0x1
    cc = v_p_x_cc & 0xF
    
    marker = (m_pt >> 7) & 0x1
    payload_type = m_pt & 0x7F
    
    header_len = 12 + (cc * 4)
    payload = packet_data[header_len:]
    
    return {
        'version': version,
        'padding': padding,
        'extension': extension,
        'cc': cc,
        'marker': marker,
        'payload_type': payload_type,
        'sequence': seq,
        'timestamp': ts,
        'ssrc': ssrc,
        'header_length': header_len,
        'payload_length': len(payload),
        'total_length': len(packet_data)
    }

def build_test_rtp_packet(payload_type=118, seq=1, ts=320, ssrc=0x12345678, payload_size=640):
    """Build a test RTP packet for slin16"""
    # RTP Header: V=2, P=0, X=0, CC=0, M=0, PT=payload_type
    v_p_x_cc = 0x80  # Version 2, no padding, no extension, no CSRC
    m_pt = payload_type & 0x7F  # No marker bit, PT as specified
    
    # Pack RTP header
    header = struct.pack("!BBHII", v_p_x_cc, m_pt, seq, ts, ssrc)
    
    # Generate test payload (silence for slin16)
    payload = b'\x00' * payload_size
    
    return header + payload

def compare_packet_formats():
    """Compare different RTP packet formats"""
    print("=== RTP Packet Format Analysis ===\n")
    
    # Test different payload types
    test_cases = [
        {'name': 'slin16 (PT 118)', 'pt': 118, 'payload_size': 640},
        {'name': 'slin16 (PT 97)', 'pt': 97, 'payload_size': 640},
        {'name': 'ulaw (PT 0)', 'pt': 0, 'payload_size': 160},
        {'name': 'alaw (PT 8)', 'pt': 8, 'payload_size': 160},
    ]
    
    for case in test_cases:
        print(f"--- {case['name']} ---")
        packet = build_test_rtp_packet(
            payload_type=case['pt'],
            payload_size=case['payload_size']
        )
        
        analysis = analyze_rtp_packet(packet)
        if analysis:
            print(f"  Version: {analysis['version']}")
            print(f"  Payload Type: {analysis['payload_type']}")
            print(f"  Sequence: {analysis['sequence']}")
            print(f"  Timestamp: {analysis['timestamp']}")
            print(f"  SSRC: 0x{analysis['ssrc']:08X}")
            print(f"  Header Length: {analysis['header_length']} bytes")
            print(f"  Payload Length: {analysis['payload_length']} bytes")
            print(f"  Total Length: {analysis['total_length']} bytes")
            print(f"  Raw Header: {packet[:12].hex()}")
        print()

def check_audio_format_compatibility():
    """Check audio format compatibility between Azure TTS and Asterisk"""
    print("=== Audio Format Compatibility Analysis ===\n")
    
    print("Azure TTS Output Format:")
    print("  - Raw16Khz16BitMonoPcm")
    print("  - Sample Rate: 16000 Hz")
    print("  - Bit Depth: 16 bits")
    print("  - Channels: 1 (mono)")
    print("  - Byte Order: Little Endian")
    print("  - Samples per 20ms: 320")
    print("  - Bytes per 20ms: 640")
    print()
    
    print("Asterisk slin16 Format:")
    print("  - Sample Rate: 16000 Hz")
    print("  - Bit Depth: 16 bits")
    print("  - Channels: 1 (mono)")
    print("  - Byte Order: Host order (typically Little Endian)")
    print("  - Samples per 20ms: 320")
    print("  - Bytes per 20ms: 640")
    print()
    
    print("Expected Compatibility: ✅ GOOD - Formats should match")
    print()

def identify_potential_issues():
    """Identify potential issues in the current implementation"""
    print("=== Potential Issues Analysis ===\n")
    
    issues = [
        {
            'category': 'Payload Type (PT) Handling',
            'issue': 'Hard-coded PT 118 vs dynamic PT',
            'description': 'Code uses PT 118 but also has fallback to PT 97. Asterisk might expect different PT.',
            'severity': 'HIGH',
            'fix': 'Always use the PT learned from incoming RTP packets'
        },
        {
            'category': 'RTP Timing',
            'issue': 'Timestamp calculation',
            'description': 'Timestamp increments by 320 per packet, which is correct for 16kHz @ 20ms',
            'severity': 'LOW',
            'fix': 'Current implementation appears correct'
        },
        {
            'category': 'Socket Binding',
            'issue': 'Port conflicts',
            'description': 'Log shows "Address already in use" errors',
            'severity': 'HIGH',
            'fix': 'Use SO_REUSEADDR socket option or dynamic port allocation'
        },
        {
            'category': 'Audio Pacing',
            'issue': 'Packet timing',
            'description': 'Using asyncio.sleep(0.02) for 20ms pacing might not be precise',
            'severity': 'MEDIUM',
            'fix': 'Use more precise timing or let Asterisk handle jitter buffering'
        },
        {
            'category': 'External Media Direction',
            'issue': 'Direction setting',
            'description': 'ExternalMedia created with direction="both" but might need explicit binding',
            'severity': 'MEDIUM',
            'fix': 'Verify ExternalMedia channel is properly bound to bridge'
        }
    ]
    
    for i, issue in enumerate(issues, 1):
        print(f"{i}. {issue['category']} [{issue['severity']}]")
        print(f"   Issue: {issue['issue']}")
        print(f"   Description: {issue['description']}")
        print(f"   Suggested Fix: {issue['fix']}")
        print()

def main():
    print("Voice AI RTP Packet Analysis Tool")
    print("=" * 50)
    print()
    
    compare_packet_formats()
    check_audio_format_compatibility()
    identify_potential_issues()
    
    print("=== Recommendations ===")
    print("1. Ensure PT consistency between incoming and outgoing RTP")
    print("2. Add socket option SO_REUSEADDR to prevent port conflicts") 
    print("3. Verify ExternalMedia channels are properly bridged")
    print("4. Check Asterisk logs for any RTP-related warnings")
    print("5. Test with a simple tone generator to isolate audio path issues")

if __name__ == "__main__":
    main()