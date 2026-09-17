import sys
import micropython
from machine import Pin, PWM, ADC
import time
import gc

# Stop Ctrl+C from breaking the binary data stream
micropython.kbd_intr(-1)

# ── Hardware Setup ───────────────────────────────────────────────────────────
# Constants and pins from the original source[cite: 2]
out_signal = PWM(Pin(10), freq=500)
analog_in  = ADC(Pin(26))
led        = Pin(25, Pin.OUT) 

# ADC calibration values[cite: 2]
ADC_MIN   = 400
ADC_MAX   = 65100
ADC_RANGE = ADC_MAX - ADC_MIN
SETTLE_US = 1500

def settle():
    """Wait for the RC filter to stabilize[cite: 2]"""
    end = time.ticks_add(time.ticks_us(), SETTLE_US)
    while time.ticks_diff(end, time.ticks_us()) > 0:
        pass

# ── Main Processing Loop ─────────────────────────────────────────────────────
# We use a 1-byte buffer to avoid all chunk-alignment and remainder issues.
single_byte_out = bytearray(1)

while True:
    # Read exactly one byte from the PC. 
    # This is blocking, so the Pico waits until data actually arrives.[cite: 2]
    raw_byte = sys.stdin.buffer.read(1)
    
    if raw_byte:
        led.on()
        
        # 1. Set the PWM duty cycle based on the pixel value[cite: 2]
        # raw_byte[0] is the 0-255 value
        out_signal.duty_u16(raw_byte[0] << 8)
        
        # 2. Wait for the analog signal to settle[cite: 2]
        settle()
        
        # 3. Read the resulting analog voltage[cite: 2]
        raw_val = analog_in.read_u16()
        
        # 4. Clamp and map the value back to 0-255[cite: 2]
        clamped = max(ADC_MIN, min(ADC_MAX, raw_val))
        single_byte_out[0] = (clamped - ADC_MIN) * 255 // ADC_RANGE
        
        # 5. Send the processed pixel back to the PC immediately[cite: 2]
        sys.stdout.buffer.write(single_byte_out)
        
        led.off()
    
    # Periodic garbage collection to maintain stability[cite: 2]
    # We do this every few pixels to ensure the heap never fragments.
    gc.collect()