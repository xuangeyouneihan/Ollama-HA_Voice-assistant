"""
Audio streaming utilities for HumbleVoice
Handles audio format conversion and streaming protocols
"""
import struct
import logging

logger = logging.getLogger(__name__)

def convert_audio_format(audio_data, target_format='int16', target_rate=16000):
    """
    Convert audio data to target format
    
    Args:
        audio_data (bytes): Input audio data
        target_format (str): Target format ('int16', 'int8', etc.)
        target_rate (int): Target sample rate
    
    Returns:
        bytes: Converted audio data
    """
    # This is a placeholder - actual implementation would handle format conversion
    # For now, just validate the input format
    if target_format == 'int16':
        # Validate that input is 16-bit integer format
        if len(audio_data) % 2 != 0:
            logger.warning("Audio data length not divisible by 2 (not 16-bit)")
            # Pad with zeros if necessary
            audio_data += b'\x00'
    
    return audio_data

def validate_audio_stream(audio_data, expected_rate=16000):
    """
    Validate that audio stream is in correct format
    
    Args:
        audio_data (bytes): Audio data to validate
        expected_rate (int): Expected sample rate
    
    Returns:
        bool: True if valid, False otherwise
    """
    # Basic validation - check if data length is reasonable
    if len(audio_data) == 0:
        logger.warning("Empty audio data")
        return False
    
    # Check if data length is even (16-bit samples)
    if len(audio_data) % 2 != 0:
        logger.warning("Audio data length not divisible by 2")
        return False
    
    return True

def calculate_audio_duration(audio_data, sample_rate=16000, bits_per_sample=16):
    """
    Calculate duration of audio data
    
    Args:
        audio_data (bytes): Audio data
        sample_rate (int): Sample rate in Hz
        bits_per_sample (int): Bits per sample
    
    Returns:
        float: Duration in seconds
    """
    bytes_per_sample = bits_per_sample // 8
    total_samples = len(audio_data) // bytes_per_sample
    duration = total_samples / sample_rate
    return duration

def create_audio_header(sample_rate=16000, channels=1, bits_per_sample=16):
    """
    Create WAV file header
    
    Args:
        sample_rate (int): Sample rate
        channels (int): Number of channels
        bits_per_sample (int): Bits per sample
    
    Returns:
        bytes: WAV file header
    """
    # WAV header format
    header = b'RIFF'
    header += struct.pack('<I', 0)  # Chunk size (to be filled later)
    header += b'WAVE'
    
    # Format chunk
    header += b'fmt '
    header += struct.pack('<I', 16)  # Subchunk1Size
    header += struct.pack('<H', 1)   # AudioFormat (PCM)
    header += struct.pack('<H', channels)  # NumChannels
    header += struct.pack('<I', sample_rate)  # SampleRate
    header += struct.pack('<I', sample_rate * channels * bits_per_sample // 8)  # ByteRate
    header += struct.pack('<H', channels * bits_per_sample // 8)  # BlockAlign
    header += struct.pack('<H', bits_per_sample)  # BitsPerSample
    
    # Data chunk header
    header += b'data'
    header += struct.pack('<I', 0)  # Subchunk2Size (to be filled later)
    
    return header
