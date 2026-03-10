"""
Large Language Model module for HumbleVoice
Uses Ollama for local LLM inference
"""
import requests
import json
import logging
from config_loader import get_config

logger = logging.getLogger(__name__)

cfg = get_config()
llm_cfg = cfg.get("llm", {}) if cfg else {}
OLLAMA_HOST = llm_cfg.get("host", "http://localhost:11434").rstrip("/")
OLLAMA_URL = f"{OLLAMA_HOST}/api/generate"
MODEL_NAME = llm_cfg.get("model", "phi3:mini")
TIMEOUT = llm_cfg.get("timeout", 60)
SYSTEM_PROMPT = llm_cfg.get(
    "system_prompt",
    "You are a voice assistant for Home Assistant.\n"
    "Answer questions about the world truthfully.\n"
    "Answer in plain text. Keep it simple and to the point.",
)
TEMPERATURE = llm_cfg.get("temperature", 0.7)
MAX_TOKENS = llm_cfg.get("max_tokens", 500)

def generate_response(prompt, temperature=None, max_tokens=None, retry_on_empty=True):
    """
    Generate response using local LLM via Ollama
    
    Args:
        prompt (str): Input text to generate response for
    
    Returns:
        str: Generated response text
    """
    if not prompt or prompt.strip() == "":
        logger.warning("Empty prompt provided to LLM")
        return "I didn't understand that."
    
    use_temperature = TEMPERATURE if temperature is None else float(temperature)
    use_max_tokens = MAX_TOKENS if max_tokens is None else int(max_tokens)

    try:
        max_attempts = 2 if retry_on_empty else 1
        for attempt in range(max_attempts):
            current_prompt = prompt
            current_temperature = use_temperature
            if attempt > 0:
                # Retry with a stricter, short instruction to avoid blank generations.
                current_prompt = (
                    f"{prompt}\n\n"
                    "IMPORTANT: Return a non-empty plain text answer. Do not return ellipsis."
                )
                current_temperature = min(use_temperature, 0.1)

            data = {
                "model": MODEL_NAME,
                "system": SYSTEM_PROMPT,
                "prompt": current_prompt,
                "stream": False,
                "options": {
                    "temperature": current_temperature,
                    "num_predict": use_max_tokens,
                    "top_p": 0.9,
                    "top_k": 40,
                },
            }

            response = requests.post(OLLAMA_URL, json=data, timeout=TIMEOUT)

            if response.status_code != 200:
                logger.error(f"LLM API error: {response.status_code} - {response.text}")
                return "Sorry, I'm having trouble thinking right now."

            result = response.json()
            response_text = (result.get("response") or "").strip()
            logger.info(f"LLM generated response: {response_text[:100]}...")

            if response_text and response_text not in {"...", "…"}:
                return response_text

            if attempt < max_attempts - 1:
                logger.warning("LLM returned empty response, retrying once with stricter instruction")

        return ""
            
    except requests.exceptions.ConnectionError:
        logger.error("Cannot connect to Ollama server")
        return "LLM Error: Cannot connect to local model server"
    except requests.exceptions.Timeout:
        logger.error("LLM request timed out")
        return "LLM Error: Request timed out"
    except Exception as e:
        logger.error(f"LLM error: {e}")
        return f"LLM Error: {str(e)}"