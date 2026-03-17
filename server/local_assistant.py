"""
本地语音助手入口：使用本机麦克风录音 -> Wyoming STT -> HA/LLM -> Wyoming TTS -> 本机扬声器播放。
用法：
    python server/local_assistant.py
在终端按回车开始录音，再按回车结束录音。
"""
import logging
import os
import wave
import asyncio
import numpy as np
import sounddevice as sd

from config_loader import get_config
from modules import stt, tts, ha_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("local_assistant")

cfg = get_config()



audio_cfg = cfg.get("audio", {}) if cfg else {}
tts_cfg = cfg.get("tts", {}) if cfg else {}
ha_cfg = cfg.get("home_assistant", {}) if cfg else {}

SAMPLE_RATE = int(audio_cfg.get("sample_rate", 16000))
INPUT_CHANNELS = int(audio_cfg.get("input_channels", audio_cfg.get("channels", 1)))
OUTPUT_CHANNELS = int(audio_cfg.get("output_channels", audio_cfg.get("channels", 1)))
INPUT_DEVICE = audio_cfg.get("input_device") if audio_cfg.get("input_device") is not None else None
OUTPUT_DEVICE = audio_cfg.get("output_device") if audio_cfg.get("output_device") is not None else None
BUFFER_SIZE = int(audio_cfg.get("buffer_size", 2048))
SAMPLE_WIDTH = int(audio_cfg.get("sample_width", 2))
START_DROP_MS = int(audio_cfg.get("start_drop_ms", 0))
INPUT_GAIN = float(audio_cfg.get("input_gain", 1.8))
TRIM_SILENCE = bool(audio_cfg.get("trim_silence", True))
SILENCE_THRESHOLD = int(audio_cfg.get("silence_threshold", 700))
SILENCE_KEEP_MS = int(audio_cfg.get("silence_keep_ms", 120))
SILENCE_THRESHOLD_MIN = int(audio_cfg.get("silence_threshold_min", 120))
SILENCE_THRESHOLD_RATIO = float(audio_cfg.get("silence_threshold_ratio", 0.22))
SILENCE_SMOOTH_MS = int(audio_cfg.get("silence_smooth_ms", 20))
SILENCE_MAX_HEAD_TRIM_MS = int(audio_cfg.get("silence_max_head_trim_ms", 300))
DEBUG_SAVE_WAV = bool(audio_cfg.get("debug_save_wav", False))
DEBUG_WAV_PATH = str(audio_cfg.get("debug_wav_path", "server/debug_last_record.wav"))
MONITOR_INPUT = bool(audio_cfg.get("monitor_input", False))
ASSIST_AUDIO_MODE = bool(ha_cfg.get("assist_audio_mode", False))


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


def _drop_head_pcm16(audio_bytes: bytes, sample_rate: int, channels: int, drop_ms: int) -> bytes:
    if drop_ms <= 0 or not audio_bytes:
        return audio_bytes

    audio = np.frombuffer(audio_bytes, dtype=np.int16)
    if channels > 1:
        usable = (len(audio) // channels) * channels
        audio = audio[:usable].reshape(-1, channels)
        drop_frames = int(sample_rate * drop_ms / 1000)
        return audio[drop_frames:].tobytes()

    drop_samples = int(sample_rate * drop_ms / 1000)
    return audio[drop_samples:].tobytes()


def _trim_silence_pcm16(
    audio_bytes: bytes,
    sample_rate: int,
    channels: int,
    threshold: int,
    keep_ms: int,
) -> bytes:
    if not audio_bytes:
        return audio_bytes

    audio = np.frombuffer(audio_bytes, dtype=np.int16)
    keep_frames = int(sample_rate * keep_ms / 1000)
    max_head_trim_frames = int(sample_rate * SILENCE_MAX_HEAD_TRIM_MS / 1000)
    smooth_frames = max(int(sample_rate * SILENCE_SMOOTH_MS / 1000), 1)

    def _moving_average(arr: np.ndarray, window: int) -> np.ndarray:
        if window <= 1 or arr.size == 0:
            return arr
        kernel = np.ones(window, dtype=np.float32) / float(window)
        return np.convolve(arr.astype(np.float32), kernel, mode="same")

    def _pick_threshold(energy: np.ndarray, configured_threshold: int) -> int:
        if energy.size == 0:
            return configured_threshold

        peak = float(np.max(energy))
        p95 = float(np.percentile(energy, 95))
        dynamic = max(SILENCE_THRESHOLD_MIN, int(max(peak, p95) * SILENCE_THRESHOLD_RATIO))
        # Prefer lower (more permissive) threshold when speech volume is low.
        picked = min(configured_threshold, dynamic)
        return max(SILENCE_THRESHOLD_MIN, picked)

    if channels > 1:
        usable = (len(audio) // channels) * channels
        audio = audio[:usable].reshape(-1, channels)
        energy = np.max(np.abs(audio), axis=1)
        energy_smooth = _moving_average(energy, smooth_frames)
        adaptive_threshold = _pick_threshold(energy_smooth, threshold)
        active = np.where(energy_smooth > adaptive_threshold)[0]
        if len(active) == 0:
            return audio_bytes
        start_candidate = max(int(active[0]) - keep_frames, 0)
        start = min(start_candidate, max_head_trim_frames)
        end = min(int(active[-1]) + keep_frames + 1, len(audio))
        logger.info(
            "静音裁剪参数(多声道): threshold=%s adaptive=%s peak=%s p95=%s start=%sms end=%sms",
            threshold,
            adaptive_threshold,
            int(np.max(energy_smooth)) if energy_smooth.size else 0,
            int(np.percentile(energy_smooth, 95)) if energy_smooth.size else 0,
            int(start * 1000 / sample_rate),
            int(end * 1000 / sample_rate),
        )
        return audio[start:end].tobytes()

    energy = np.abs(audio)
    energy_smooth = _moving_average(energy, smooth_frames)
    adaptive_threshold = _pick_threshold(energy_smooth, threshold)
    active = np.where(energy_smooth > adaptive_threshold)[0]
    if len(active) == 0:
        return audio_bytes
    start_candidate = max(int(active[0]) - keep_frames, 0)
    start = min(start_candidate, max_head_trim_frames)
    end = min(int(active[-1]) + keep_frames + 1, len(audio))
    logger.info(
        "静音裁剪参数: threshold=%s adaptive=%s peak=%s p95=%s start=%sms end=%sms",
        threshold,
        adaptive_threshold,
        int(np.max(energy_smooth)) if energy_smooth.size else 0,
        int(np.percentile(energy_smooth, 95)) if energy_smooth.size else 0,
        int(start * 1000 / sample_rate),
        int(end * 1000 / sample_rate),
    )
    return audio[start:end].tobytes()


def _save_debug_wav(audio_bytes: bytes, sample_rate: int, channels: int, sample_width: int, wav_path: str):
    if not audio_bytes:
        return
    os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_bytes)
    logger.info("已写入调试录音: %s", wav_path)


def _apply_gain_pcm16(audio_bytes: bytes, gain: float) -> bytes:
    if not audio_bytes or gain <= 0:
        return audio_bytes
    if abs(gain - 1.0) < 1e-6:
        return audio_bytes

    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
    audio *= gain
    np.clip(audio, -32768, 32767, out=audio)
    return audio.astype(np.int16).tobytes()


def record_once() -> bytes:
    """按回车开始录音，再次按回车结束，返回原始PCM字节。"""
    frames = []
    overflow_count = 0

    def callback(indata, frames_count, time_info, status):
        nonlocal overflow_count
        if status:
            logger.warning(f"录音状态: {status}")
            if getattr(status, "input_overflow", False):
                overflow_count += 1
        frames.append(indata.copy())

    input("按回车开始录音...")
    _describe_device(INPUT_DEVICE, "input")
    logger.info(
        "录音参数: device=%s samplerate=%s channels=%s blocksize=%s latency=high",
        INPUT_DEVICE,
        SAMPLE_RATE,
        INPUT_CHANNELS,
        BUFFER_SIZE,
    )
    sd.check_input_settings(
        device=INPUT_DEVICE,
        samplerate=SAMPLE_RATE,
        channels=INPUT_CHANNELS,
        dtype="int16",
    )
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=INPUT_CHANNELS,
        dtype="int16",
        device=INPUT_DEVICE,
        blocksize=BUFFER_SIZE,
        latency="high",
        callback=callback,
    ):
        input("录音中，完成后按回车结束...")

    if overflow_count > 0:
        logger.warning("本次录音发生 input overflow 次数: %s（建议继续增大 audio.buffer_size）", overflow_count)

    if not frames:
        return b""
    audio = np.concatenate(frames, axis=0)
    audio_bytes = audio.tobytes()

    if INPUT_GAIN > 0 and abs(INPUT_GAIN - 1.0) > 1e-6:
        audio_bytes = _apply_gain_pcm16(audio_bytes, INPUT_GAIN)
        logger.info("已应用输入增益: x%s", INPUT_GAIN)

    if START_DROP_MS > 0:
        audio_bytes = _drop_head_pcm16(audio_bytes, SAMPLE_RATE, INPUT_CHANNELS, START_DROP_MS)
        logger.info("已丢弃录音前 %sms 音频", START_DROP_MS)

    if TRIM_SILENCE:
        before_len = len(audio_bytes)
        audio_bytes = _trim_silence_pcm16(
            audio_bytes,
            SAMPLE_RATE,
            INPUT_CHANNELS,
            SILENCE_THRESHOLD,
            SILENCE_KEEP_MS,
        )
        logger.info("静音裁剪: %s -> %s 字节", before_len, len(audio_bytes))

    if DEBUG_SAVE_WAV:
        _save_debug_wav(audio_bytes, SAMPLE_RATE, INPUT_CHANNELS, SAMPLE_WIDTH, DEBUG_WAV_PATH)

    return audio_bytes


def play_audio(raw_audio: bytes, sample_rate: int, sample_width: int, channels: int):
    if not raw_audio:
        return
    _describe_device(OUTPUT_DEVICE, "output")
    if sample_width != 2:
        logger.warning("当前仅支持16-bit PCM播放，收到 sample_width=%s，按16-bit尝试播放", sample_width)
    audio = np.frombuffer(raw_audio, dtype=np.int16)
    if channels > 1:
        try:
            audio = audio.reshape(-1, channels)
        except ValueError:
            logger.warning("TTS返回声道数=%s，但音频长度无法整除，按单声道播放", channels)
    logger.info("播放参数: device=%s samplerate=%s channels=%s", OUTPUT_DEVICE, sample_rate, channels)
    sd.play(audio, samplerate=sample_rate, device=OUTPUT_DEVICE)
    sd.wait()


def handle_once():
    audio_bytes = record_once()
    if not audio_bytes:
        print("未录到音频，重试")
        return

    if MONITOR_INPUT:
        logger.info("录音返听: 回放本次录音")
        play_audio(
            audio_bytes,
            sample_rate=SAMPLE_RATE,
            sample_width=SAMPLE_WIDTH,
            channels=INPUT_CHANNELS,
        )

    if ASSIST_AUDIO_MODE:
        result = asyncio.run(
            ha_client.process_audio_with_assist_pipeline(
                audio_bytes,
                sample_rate=SAMPLE_RATE,
            )
        )
        if not bool(result.get("ok", False)):
            message = str(result.get("message") or "assist pipeline request failed")
            print(f"HA Assist 失败: {message}")
            return

        transcript = str(result.get("transcript") or "").strip()
        if transcript:
            print(f"识别: {transcript}")

        reply = str(result.get("response_text") or "").strip()
        if reply:
            print(f"回复: {reply}")

        tts_audio = result.get("tts_audio") or b""
        tts_rate = int(result.get("tts_sample_rate") or 0)
        tts_width = int(result.get("tts_sample_width") or 0)
        tts_channels = int(result.get("tts_channels") or 0)
        if tts_audio and tts_rate > 0 and tts_width > 0 and tts_channels > 0:
            play_audio(tts_audio, tts_rate, tts_width, tts_channels)
            return

        print("HA Assist TTS 音频不可播放（可能不是 WAV/PCM），暂不播放")
        return

    text = stt.transcribe(
        audio_bytes,
        sample_rate=SAMPLE_RATE,
        sample_width=SAMPLE_WIDTH,
        channels=INPUT_CHANNELS,
    )
    print(f"识别: {text}")

    reply = ha_client.handle_user_text(text)

    print(f"回复: {reply}")

    audio_reply, tts_rate, tts_width, tts_channels = tts.synthesize_with_format(reply)
    if not audio_reply:
        print("TTS 失败，未播放音频")
        return
    play_audio(audio_reply, tts_rate, tts_width, tts_channels)


def main():
    print("本地语音助手已启动。输入 q 后回车退出。")
    try:
        while True:
            cmd = input("按回车开始一次对话，或输入 q 后回车退出: ")
            if cmd.strip().lower() == "q":
                break
            handle_once()
    except KeyboardInterrupt:
        print("\n已中断，正在安全退出...")
    finally:
        try:
            sd.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
