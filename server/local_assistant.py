"""
本地语音助手入口：使用本机麦克风录音 -> Wyoming STT -> HA/LLM -> Wyoming TTS -> 本机扬声器播放。
用法：
    python server/local_assistant.py
在终端按回车开始录音，再按回车结束录音。
"""
import logging
import numpy as np
import sounddevice as sd

from config_loader import get_config
from modules import stt, tts, llm, ha_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("local_assistant")

cfg = get_config()
audio_cfg = cfg.get("audio", {}) if cfg else {}
tts_cfg = cfg.get("tts", {}) if cfg else {}

SAMPLE_RATE = int(audio_cfg.get("sample_rate", 16000))
CHANNELS = int(audio_cfg.get("channels", 1))
INPUT_DEVICE = audio_cfg.get("input_device") if audio_cfg.get("input_device") is not None else None
OUTPUT_DEVICE = audio_cfg.get("output_device") if audio_cfg.get("output_device") is not None else None
TTS_RATE = int(tts_cfg.get("sample_rate", SAMPLE_RATE))


def _describe_device(device, kind: str):
    """辅助调试：打印输入/输出设备实际解析结果。"""
    try:
        info = sd.query_devices(device, kind=kind)
        logger.info("%s设备: idx=%s name=%s sr_max=%s in_ch=%s out_ch=%s",
                    "输入" if kind == "input" else "输出",
                    info.get("index"), info.get("name"), info.get("default_samplerate"),
                    info.get("max_input_channels"), info.get("max_output_channels"))
    except Exception as exc:  # 查询失败时仅告警
        logger.warning("无法查询%s设备 %s: %s", "输入" if kind == "input" else "输出", device, exc)


def record_once() -> bytes:
    """按回车开始录音，再次按回车结束，返回原始PCM字节。"""
    frames = []

    def callback(indata, frames_count, time_info, status):
        if status:
            logger.warning(f"录音状态: {status}")
        frames.append(indata.copy())

    input("按回车开始录音...")
    _describe_device(INPUT_DEVICE, "input")
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        device=INPUT_DEVICE,
        callback=callback,
    ):
        input("录音中，完成后按回车结束...")

    if not frames:
        return b""
    audio = np.concatenate(frames, axis=0)
    return audio.tobytes()


def play_audio(raw_audio: bytes):
    if not raw_audio:
        return
    _describe_device(OUTPUT_DEVICE, "output")
    audio = np.frombuffer(raw_audio, dtype=np.int16)
    sd.play(audio, samplerate=TTS_RATE, device=OUTPUT_DEVICE)
    sd.wait()


def handle_once():
    audio_bytes = record_once()
    if not audio_bytes:
        print("未录到音频，重试")
        return

    text = stt.transcribe(audio_bytes)
    print(f"识别: {text}")

    if ha_client.is_ha_command(text):
        reply = ha_client.process_command(text)
    else:
        reply = llm.generate_response(text)

    print(f"回复: {reply}")

    audio_reply = tts.synthesize(reply)
    if not audio_reply:
        print("TTS 失败，未播放音频")
        return
    play_audio(audio_reply)


def main():
    print("本地语音助手已启动。输入 q 后回车退出。")
    while True:
        cmd = input("按回车开始一次对话，或输入 q 后回车退出: ")
        if cmd.strip().lower() == "q":
            break
        handle_once()


if __name__ == "__main__":
    main()
