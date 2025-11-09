"""
Home Assistant integration module for HumbleVoice
Handles HA commands and entity control
"""
import requests
import json
import logging

logger = logging.getLogger(__name__)

# Configuration - these should be loaded from config file
HA_URL = "http://homeassistant.local:8123"
HA_TOKEN = "YOUR_HA_TOKEN"  # Should be loaded from environment/config

headers = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json"
}

def is_ha_command(text):
    """
    Determine if the text is a Home Assistant command
    
    Args:
        text (str): Text to analyze
    
    Returns:
        bool: True if it's an HA command, False otherwise
    """
    ha_keywords = ['turn on', 'turn off', 'light', 'switch', 'lamp', 'fan', 'ac', 'heater', 'thermostat']
    text_lower = text.lower()
    
    return any(keyword in text_lower for keyword in ha_keywords)

def process_command(text):
    """
    Process Home Assistant commands
    
    Args:
        text (str): Command text to process
    
    Returns:
        str: Response message
    """
    if not is_ha_command(text):
        return "This is not a Home Assistant command"
    
    try:
        # Simple command parsing - this should be improved with proper NLP
        text_lower = text.lower()
        
        if 'turn on' in text_lower:
            if 'light' in text_lower:
                entity_id = 'light.bedroom'  # This should be dynamic
                return call_ha_service('light.turn_on', {'entity_id': entity_id})
            elif 'switch' in text_lower:
                entity_id = 'switch.outlet'  # This should be dynamic
                return call_ha_service('switch.turn_on', {'entity_id': entity_id})
            else:
                return "I don't know how to turn that on"
                
        elif 'turn off' in text_lower:
            if 'light' in text_lower:
                entity_id = 'light.bedroom'  # This should be dynamic
                return call_ha_service('light.turn_off', {'entity_id': entity_id})
            elif 'switch' in text_lower:
                entity_id = 'switch.outlet'  # This should be dynamic
                return call_ha_service('switch.turn_off', {'entity_id': entity_id})
            else:
                return "I don't know how to turn that off"
        else:
            return "I don't understand that command"
            
    except Exception as e:
        logger.error(f"HA command error: {e}")
        return f"Home Assistant Error: {str(e)}"

def call_ha_service(service, data):
    """
    Call a Home Assistant service
    
    Args:
        service (str): Service name (e.g., 'light.turn_on')
        data (dict): Service data
    
    Returns:
        str: Response message
    """
    url = f"{HA_URL}/api/services/{service}"
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=10)
        
        if response.status_code == 200:
            logger.info(f"HA service {service} called successfully")
            return "Okay, I've done that"
        else:
            logger.error(f"HA service error: {response.status_code} - {response.text}")
            return f"Home Assistant Error: {response.status_code}"
            
    except requests.exceptions.ConnectionError:
        logger.error("Cannot connect to Home Assistant")
        return "Home Assistant Error: Cannot connect to server"
    except Exception as e:
        logger.error(f"HA service call error: {e}")
        return f"Home Assistant Error: {str(e)}"

def discover_entities():
    """
    Discover available entities from Home Assistant
    
    Returns:
        list: List of available entities
    """
    url = f"{HA_URL}/api/states"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            entities = response.json()
            logger.info(f"Discovered {len(entities)} entities")
            return entities
        else:
            logger.error(f"Entity discovery error: {response.status_code}")
            return []
            
    except Exception as e:
        logger.error(f"Entity discovery error: {e}")
        return []