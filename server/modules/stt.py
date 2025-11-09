"""
Speech-to-Text module for HumbleVoice
Uses whisper.cpp for local speech recognition
"""
import subprocess
import tempfile
import os
import logging

logger = logging.getLogger(__name__)

def transcribe(audio_data):
    """
    Transcribe audio data to text using whisper.cpp
    
    Args:
        audio_data (bytes): Raw audio data in 16kHz format
    
    Returns:
        str: Transcribed text
    """
    # Create temporary raw audio file
    with tempfile.NamedTemporaryFile(suffix='.raw', delete=False) as temp_file:
        temp_file.write(audio_data)
        temp_file_path = temp_file.name
    
    try:
        # Path to whisper.cpp executable and model
        whisper_path = "./whisper.cpp/main"
        model_path = "./models/ggml-tiny.en.bin"
        
        # Verify files exist
        if not os.path.exists(whisper_path):
            logger.error(f"Whisper.cpp executable not found at {whisper_path}")
            return "STT Error: Whisper.cpp not found"
        
        if not os.path.exists(model_path):
            logger.error(f"Whisper model not found at {model_path}")
            return "STT Error: Model not found"
        
        # Run whisper.cpp command
        cmd = [
            whisper_path,
            '-m', model_path,
            '-f', temp_file_path,
            '--language', 'en',
            '--output-txt'
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            logger.error(f"Whisper.cpp error: {result.stderr}")
            return f"STT Error: {result.stderr}"
        
        # Read the output text file
        txt_path = temp_file_path.replace('.raw', '.txt')
        if os.path.exists(txt_path):
            with open(txt_path, 'r', encoding='utf-8') as f:
                text = f.read().strip()
            
            # Clean up temporary files
            os.unlink(txt_path)
            os.unlink(temp_file_path)
            
            logger.info(f"Successfully transcribed: {text}")
            return text
        else:
            logger.error("Whisper.cpp output file not found")
            return "Could not transcribe audio"
            
    except subprocess.TimeoutExpired:
        logger.error("Whisper.cpp transcription timed out")
        return "STT Error: Transcription timed out"
    except Exception as e:
        logger.error(f"STT error: {e}")
        return f"STT Error: {str(e)}"
    finally:
        # Ensure cleanup even if error occurs
        if os.path.exists(temp_file_path):
            os.unlink(temp_file_path)