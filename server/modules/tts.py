"""
Text-to-Speech module for HumbleVoice
Uses Piper TTS for local speech synthesis
"""
import subprocess
import tempfile
import os
import logging

logger = logging.getLogger(__name__)

def synthesize(text):
    """
    Convert text to audio using Piper TTS
    
    Args:
        text (str): Text to convert to speech
    
    Returns:
        bytes: Audio data in WAV format
    """
    if not text or text.strip() == "":
        logger.warning("Empty text provided to TTS")
        return b""
    
    # Create temporary WAV file
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
        temp_file_path = temp_file.name
    
    try:
        # Path to Piper executable and model
        piper_path = "./piper/piper"
        model_path = "./models/en_US-lessac-medium.onnx"
        
        # Verify files exist
        if not os.path.exists(piper_path):
            logger.error(f"Piper executable not found at {piper_path}")
            return b""
        
        if not os.path.exists(model_path):
            logger.error(f"Piper model not found at {model_path}")
            return b""
        
        # Run Piper command
        cmd = [
            piper_path,
            '--model', model_path,
            '--output_file', temp_file_path
        ]
        
        process = subprocess.Popen(
            cmd, 
            stdin=subprocess.PIPE, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE
        )
        
        # Send text to Piper via stdin
        stdout, stderr = process.communicate(input=text.encode('utf-8'), timeout=30)
        
        if process.returncode != 0:
            logger.error(f"Piper error: {stderr}")
            return b""
        
        # Read the generated WAV file
        with open(temp_file_path, 'rb') as f:
            audio_data = f.read()
        
        logger.info(f"Successfully synthesized audio for text: {text[:50]}...")
        return audio_data
        
    except subprocess.TimeoutExpired:
        logger.error("Piper synthesis timed out")
        return b""
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return b""
    finally:
        # Clean up temporary file
        if os.path.exists(temp_file_path):
            os.unlink(temp_file_path)