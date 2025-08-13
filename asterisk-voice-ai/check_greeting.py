#!/usr/bin/env python3
# Test ob die greeting.raw wirklich Audio enthält
with open('/var/lib/asterisk/sounds/custom/greeting.raw', 'rb') as f:
    data = f.read(1000)
    
# Check for actual audio (non-zero values)
non_zero = sum(1 for b in data if b != 0)
print(f'Non-zero bytes in first 1000: {non_zero}/{len(data)}')

# Check if it's PCM16LE
import struct
samples = []
for i in range(0, min(20, len(data)), 2):
    sample = struct.unpack('<h', data[i:i+2])[0]
    samples.append(sample)
print(f'First 10 PCM16 samples: {samples[:10]}')
print(f'Sample range: {min(samples)} to {max(samples)}')