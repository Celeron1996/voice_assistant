#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""100ask i.MX6ULL 云 AI 语音对话最小原型。

流程: arecord + 能量 VAD 自动断句 -> 豆包大模型录音识别(极速版) -> 火山方舟 LLM -> 豆包 TTS -> mpg123/aplay 播放
纯 Python3 标准库实现，无需交叉编译，直接拷贝到板子运行。
"""

import argparse
import audioop
import base64
import configparser
import fcntl
import glob
import gzip
import io
import json
import os
import queue
import select
import signal
import socket
import statistics
import struct
import subprocess
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from collections import deque

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000
MIC_STARTUP_DISCARD_S = 1.2  # arecord 启动瞬态约 0.8s，丢弃后再开始标定
PLAYBACK_DISCARD_S = 0.6     # 播放结束后丢弃扬声器回声

DEFAULT_CONFIG = {
    "llm": {
        "ark_api_key": "",
        "model": "doubao-seed-1-6-flash-250828",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "system_prompt": "你是一个运行在嵌入式开发板上的语音助手。用中文口语化回答，控制在两句话以内，不要使用 Markdown。",
        "max_tokens": "300",
        "temperature": "0.7",
        "stream": "true",
        "thinking": "auto",
    },
    "asr": {
        "mode": "stream",
        "api_key": "",
        "appid": "",
        "access_token": "",
        "url": "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
        "resource_id": "volc.seedasr.auc",
        "stream_url": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
        "stream_resource_id": "volc.seedasr.sauc.duration",
    },
    "tts": {
        "mode": "seed",
        "api_key": "",
        "appid": "",
        "access_token": "",
        "cluster": "volcano_tts",
        "voice_type": "zh_female_shuangkuaisisi_uranus_bigtts",
        "encoding": "mp3",
        "speed_ratio": "1.0",
        "base_url": "https://openspeech.bytedance.com/api/v3/tts/unidirectional",
        "resource_id": "seed-tts-2.0",
        "app_key": "",
    },
    "audio": {
        "energy_threshold": "0",
        "silence_ms": "800",
        "min_speech_ms": "300",
        "max_record_ms": "15000",
        "mic_device": "default",
    },
    "network": {
        "ca_file": "",
        "insecure_skip_verify": "false",
    },
    "dialogue": {
        "mode": "realtime",
    },
    "realtime": {
        "url": "wss://openspeech.bytedance.com/api/v3/realtime/dialogue",
        "resource_id": "volc.speech.dialog",
        "app_id": "",
        "access_token": "",
        "api_key": "",
        "app_key": "PlgvMymc7f3tQnJ6",
        "model": "2.2.0.0",
        "speaker": "ICL_uranus_zh_female_wenrouwenya_tob",
        "bot_name": "小助手",
        "system_prompt": "你是一个运行在嵌入式开发板上的语音助手，回答简短、口语化。",
        "speaking_style": "自然、简洁、友好。",
        "say_hello": "",
        "barge_in": "true",
        "output_sample_rate": "24000",
        "end_smooth_window_ms": "1500",
    },
}

ENV_MAP = {
    ("llm", "ark_api_key"): "ARK_API_KEY",
    ("llm", "model"): "ARK_MODEL",
    ("llm", "base_url"): "ARK_BASE_URL",
    ("llm", "stream"): "ARK_STREAM",
    ("llm", "thinking"): "ARK_THINKING",
    ("asr", "api_key"): "VOLC_ASR_API_KEY",
    ("asr", "appid"): "VOLC_ASR_APPID",
    ("asr", "access_token"): "VOLC_ASR_ACCESS_TOKEN",
    ("asr", "resource_id"): "VOLC_ASR_RESOURCE_ID",
    ("asr", "mode"): "VOLC_ASR_MODE",
    ("asr", "stream_url"): "VOLC_ASR_STREAM_URL",
    ("asr", "stream_resource_id"): "VOLC_ASR_STREAM_RESOURCE_ID",
    ("tts", "mode"): "VOLC_TTS_MODE",
    ("tts", "api_key"): "VOLC_TTS_API_KEY",
    ("tts", "appid"): "VOLC_TTS_APPID",
    ("tts", "access_token"): "VOLC_TTS_ACCESS_TOKEN",
    ("tts", "voice_type"): "VOLC_TTS_VOICE",
    ("network", "ca_file"): "CA_FILE",
    ("dialogue", "mode"): "VOICE_DIALOGUE_MODE",
    ("realtime", "app_id"): "VOLC_RT_APP_ID",
    ("realtime", "access_token"): "VOLC_RT_ACCESS_TOKEN",
    ("realtime", "api_key"): "VOLC_RT_API_KEY",
    ("realtime", "speaker"): "VOLC_RT_SPEAKER",
    ("realtime", "model"): "VOLC_RT_MODEL",
}


def cfg_get(conf, section, option, fallback=None):
    env = ENV_MAP.get((section, option))
    if env and os.environ.get(env):
        return os.environ[env]
    if fallback is None:
        fallback = DEFAULT_CONFIG.get(section, {}).get(option, "")
    if conf is not None and conf.has_option(section, option):
        return conf.get(section, option)
    return fallback


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- HTTP / TLS

def make_ssl_context(conf):
    candidates = [
        cfg_get(conf, "network", "ca_file"),
        os.environ.get("SSL_CERT_FILE", ""),
        "/etc/ssl/certs/ca-certificates.crt",
        os.path.join(BASE_DIR, "cacert.pem"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return ssl.create_default_context(cafile=path)
    if cfg_get(conf, "network", "insecure_skip_verify", "false").lower() in ("1", "true", "yes"):
        log("警告: 未找到 CA 证书，已按配置跳过校验（仅限调试）")
        return ssl._create_unverified_context()
    sys.exit(
        "错误: 找不到 CA 证书，无法建立 HTTPS 连接。\n"
        "请在板子上执行: curl -k -L -o %s/cacert.pem https://curl.se/ca/cacert.pem\n"
        "(或在 config.ini 的 [network] 里设置 ca_file / insecure_skip_verify=true)"
        % BASE_DIR
    )


def http_post(url, payload, headers, timeout, ssl_ctx):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


# ------------------------------------------------------------------------ VAD

class VAD(object):
    """基于短时能量的自动断句：静音标定 -> 检测说话 -> 静音结束。"""

    def __init__(self, threshold=0, silence_ms=800, min_speech_ms=300, max_record_ms=15000):
        self.auto_threshold = int(threshold) <= 0
        self.threshold = 0 if self.auto_threshold else int(threshold)
        self.silence_frames = max(1, int(silence_ms) // FRAME_MS)
        self.min_speech_frames = max(1, int(min_speech_ms) // FRAME_MS)
        self.max_frames = max(self.min_speech_frames + 1, int(max_record_ms) // FRAME_MS)
        self.calib = []
        self.noise = None
        self.preroll = deque(maxlen=15)
        self.above = 0
        self.recording = False
        self.trailing_silence = 0
        self.frames = []

    @staticmethod
    def _auto_threshold(noise):
        # 4 倍噪声底，下限 300 防过低，上限 6000 防环境太吵时“耳聋”
        return min(max(int(noise * 4), 300), 6000)

    def reset_utterance(self):
        """开始新一轮聆听前清空状态（保留已标定的噪声门限）。"""
        self.preroll.clear()
        self.above = 0
        self.recording = False
        self.trailing_silence = 0
        self.frames = []

    def feed(self, frame):
        rms = audioop.rms(frame, 2)
        if self.noise is None:
            if self.auto_threshold:
                self.calib.append(rms)
                if len(self.calib) < 25:
                    return None
                self.noise = max(statistics.median(self.calib), 1)
                self.threshold = self._auto_threshold(self.noise)
                log("环境噪声 RMS=%d, 说话判定阈值=%d" % (self.noise, self.threshold))
            else:
                self.noise = 0
        if not self.recording:
            self.preroll.append(frame)
            if rms > self.threshold:
                self.above += 1
                if self.above >= 3:
                    self.recording = True
                    self.frames = list(self.preroll)
                    self.trailing_silence = 0
                    return "start"
            else:
                self.above = 0
                if self.auto_threshold:
                    self.noise = 0.98 * self.noise + 0.02 * rms
                    self.threshold = self._auto_threshold(self.noise)
            return None
        self.frames.append(frame)
        if rms < self.threshold:
            self.trailing_silence += 1
        else:
            self.trailing_silence = 0
        if len(self.frames) >= self.max_frames:
            self.recording = False
            return "timeout"
        if self.trailing_silence >= self.silence_frames and len(self.frames) >= self.min_speech_frames:
            self.recording = False
            return "end"
        return None


def frames_to_wav(frames):
    tail = [b"\x00" * FRAME_BYTES] * 10  # 尾部补 200ms 静音，提升识别率
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames + tail))
    return buf.getvalue()


def wav_to_frames(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        if (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError("只支持 16kHz 单声道 16bit WAV")
        return [wf.readframes(FRAME_MS * SAMPLE_RATE // 1000)
                for _ in range(wf.getnframes() // (FRAME_MS * SAMPLE_RATE // 1000))]


# ---------------------------------------------------------------- 录音 / 播放

class Recorder(object):
    """常开的 arecord 进程：避免每轮重启带来的启动瞬态。"""

    def __init__(self, conf):
        cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1"]
        device = cfg_get(conf, "audio", "mic_device")
        if device and device != "default":
            cmd += ["-D", device]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read_frame(self):
        frame = self.proc.stdout.read(FRAME_BYTES)
        if not frame or len(frame) < FRAME_BYTES:
            raise RuntimeError("arecord 异常退出，请检查麦克风 (arecord -d 3 /tmp/t.wav 测试)")
        return frame

    def discard(self, seconds):
        """实时读取并丢弃指定时长的音频（先快速消费管道里积压的旧数据）。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.read_frame()

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def record_turn(conf, ssl_ctx, vad, recorder):
    """阻塞直到听完一句话，返回 (wav bytes, 识别文本)。

    stream 模式：说话开始就建 WebSocket，边说边发 PCM，静音后立即取最终结果；
    flash 模式：整段录完后一次性上传识别。
    """
    vad.reset_utterance()
    log("正在聆听……")
    stream_mode = cfg_get(conf, "asr", "mode") == "stream"
    session = None
    sent = 0
    last_partial = ""
    while True:
        event = vad.feed(recorder.read_frame())
        if event == "start":
            if stream_mode:
                log("检测到说话，流式识别中……")
                try:
                    session = StreamingASR(conf, ssl_ctx)
                except Exception as err:
                    log("流式识别连接失败（%s），本轮降级为整段识别" % err)
                    session = None
            else:
                log("检测到说话，录音中……")
        if session is not None and vad.recording and len(vad.frames) - sent >= 5:
            session.send_audio(b"".join(vad.frames[sent:]))
            sent = len(vad.frames)
            partial = session.text
            if partial and partial != last_partial:
                last_partial = partial
                log("识别中… %s" % partial)
        if event in ("end", "timeout"):
            duration = len(vad.frames) * FRAME_MS / 1000.0
            if event == "timeout":
                log("达到最长录音时间 %.1fs，强制结束" % duration)
            else:
                log("录音结束，时长 %.1fs" % duration)
            wav_bytes = frames_to_wav(vad.frames)
            if session is not None:
                if len(vad.frames) > sent:
                    session.send_audio(b"".join(vad.frames[sent:]))
                try:
                    text = session.finish()
                except Exception as err:
                    log("流式识别失败（%s），本轮降级为整段识别" % err)
                    text = asr_transcribe(conf, ssl_ctx, wav_bytes)
            else:
                text = asr_transcribe(conf, ssl_ctx, wav_bytes)
            return wav_bytes, text


def find_pulse_server():
    """查找可用的 PulseAudio socket：直接连一下 native socket，连得上才算数。"""
    for socket_path in sorted(glob.glob("/tmp/pulse-*/native")):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(socket_path)
            return "unix:" + socket_path
        except OSError:
            continue
        finally:
            probe.close()
    return ""


def playback_encoding(conf):
    """seed 模式下 PulseAudio 可用则用 pcm（可流式喂 paplay），否则用配置编码。"""
    configured = cfg_get(conf, "tts", "encoding").lower()
    if cfg_get(conf, "tts", "mode") == "seed" and find_pulse_server():
        return "pcm"
    return configured


def _audio_ext(encoding):
    return {"pcm": "pcm", "wav": "wav"}.get(encoding, "mp3")


def _paplay_raw_cmd():
    return ["paplay", "--raw", "--format=s16le", "--rate=24000", "--channels=1"]


def _resume_sink(pulse):
    """播放前恢复 PulseAudio sink：唤醒 + 取消静音 + 音量 100%；并恢复 ALSA 硬件音量。"""
    env = dict(os.environ, PULSE_SERVER=pulse)
    try:
        subprocess.run(["alsactl", "restore"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for args in (["suspend-sink", "@DEFAULT_SINK@", "0"],
                 ["set-sink-mute", "@DEFAULT_SINK@", "0"],
                 ["set-sink-volume", "@DEFAULT_SINK@", "100%"]):
        try:
            subprocess.run(["pactl"] + args, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _write_all(player, chunk, timeout=15):
    """非阻塞写入播放器 stdin，超时抛 OSError（避免 PulseAudio 卡住时死等）。"""
    fd = player.stdin.fileno()
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)
    view = memoryview(chunk)
    deadline = time.monotonic() + timeout
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OSError("播放器写入超时")
        _, writable, _ = select.select([], [fd], [], remaining)
        if not writable:
            raise OSError("播放器写入超时")
        try:
            written = os.write(fd, view)
            view = view[written:]
        except BlockingIOError:
            continue


def _run_player(cmd, env=None):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              env=env, timeout=120)
        return proc.returncode, proc.stderr.decode("utf-8", "ignore").strip()
    except subprocess.TimeoutExpired:
        return -1, "播放超时(120s)，已终止"


def _run_paplay_mp3(path, env):
    """mp3 文件: ffmpeg 解码成 raw pcm 喂给 paplay。"""
    decoder = subprocess.Popen(
        ["ffmpeg", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "24000", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    player = subprocess.Popen(_paplay_raw_cmd(), stdin=decoder.stdout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
    decoder.stdout.close()
    _, player_err = player.communicate()
    _, decoder_err = decoder.communicate()
    err = (player_err or decoder_err).decode("utf-8", "ignore").strip()
    return player.returncode, err


def play_audio(conf, audio_bytes):
    encoding = playback_encoding(conf)
    path = "/tmp/va_reply." + _audio_ext(encoding)
    with open(path, "wb") as fh:
        fh.write(audio_bytes)
    pulse = find_pulse_server()
    if pulse:
        _resume_sink(pulse)
        env = dict(os.environ, PULSE_SERVER=pulse)
        if encoding == "pcm":
            rc, err = _run_player(_paplay_raw_cmd() + [path], env)
        elif encoding == "wav":
            rc, err = _run_player(["paplay", path], env)
        else:
            rc, err = _run_paplay_mp3(path, env)
    elif encoding == "mp3":
        rc, err = _run_player(["mpg123", "-q", path])
    else:
        rc, err = _run_player(["aplay", "-q", path])
    if rc != 0:
        log("播放失败 (%s): %s" % (path, err))


# ---------------------------------------------------------------- 云端三步

def asr_transcribe(conf, ssl_ctx, wav_bytes):
    api_key = cfg_get(conf, "asr", "api_key")
    appid = cfg_get(conf, "asr", "appid")
    token = cfg_get(conf, "asr", "access_token")
    headers = {
        "X-Api-Resource-Id": cfg_get(conf, "asr", "resource_id"),
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    if api_key:
        headers["X-Api-Key"] = api_key
    elif appid and token:
        headers["X-Api-App-Key"] = appid
        headers["X-Api-Access-Key"] = token
    else:
        raise RuntimeError("ASR 未配置凭证（[asr] api_key 或 appid+access_token）")
    payload = {
        "user": {"uid": "100ask-board"},
        "audio": {
            "data": base64.b64encode(wav_bytes).decode("ascii"),
            "format": "wav",
            "rate": SAMPLE_RATE,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_punc": True,
            "enable_itn": True,
            "show_utterances": False,
        },
    }
    _, resp_headers, body = http_post(cfg_get(conf, "asr", "url"), payload, headers, 30, ssl_ctx)
    code = resp_headers.get("X-Api-Status-Code", "")
    message = resp_headers.get("X-Api-Message", "")
    if code == "20000003":
        return ""
    if code != "20000000":
        raise RuntimeError("ASR 失败: code=%s message=%s body=%s"
                           % (code or "?", message, body[:200].decode("utf-8", "ignore")))
    result = json.loads(body.decode("utf-8"))
    return (result.get("result") or {}).get("text", "").strip()


# ------------------------------------------------------------ 流式 ASR (WS)

class WSClient(object):
    """最小 RFC6455 WebSocket 客户端（仅二进制消息），用于火山流式 ASR。"""

    def __init__(self, url, headers, ssl_ctx, timeout=15):
        if not url.startswith("wss://"):
            raise ValueError("只支持 wss:// 地址")
        host_path = url[len("wss://"):]
        host, _, path = host_path.partition("/")
        path = "/" + path
        raw = socket.create_connection((host, 443), timeout=timeout)
        self.sock = ssl_ctx.wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._frag = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            "GET %s HTTP/1.1" % path,
            "Host: %s" % host,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in headers.items():
            lines.append("%s: %s" % (name, value))
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手连接被关闭")
            self._buf += chunk
        head, _, self._buf = self._buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("utf-8", "ignore")
        if " 101 " not in status_line:
            detail = self._buf.decode("utf-8", "ignore").strip() or \
                head.decode("utf-8", "ignore").replace("\r\n", " | ")
            raise RuntimeError("WebSocket 握手失败: %s | %s" % (status_line, detail[:300]))
        self.sock.settimeout(1.0)

    def _read_exact(self, size):
        while len(self._buf) < size:
            chunk = self.sock.recv(size - len(self._buf))
            if not chunk:
                raise ConnectionError("WebSocket 连接已关闭")
            self._buf += chunk
        data, self._buf = self._buf[:size], self._buf[size:]
        return data

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_binary(self, payload):
        self._send_frame(0x02, payload)

    def recv_message(self):
        """返回一条二进制消息；超时返回 None；连接关闭抛 ConnectionError。"""
        while True:
            try:
                first, second = self._read_exact(2)
            except socket.timeout:
                return None
            fin = first & 0x80
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            masked = bool(second & 0x80)
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length) if length else b""
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x09:  # ping -> pong
                self._send_frame(0x0A, payload)
                continue
            if opcode == 0x0A:  # pong
                continue
            if opcode == 0x08:  # close
                raise ConnectionError("WebSocket 服务端关闭连接")
            if opcode in (0x00, 0x02):
                self._frag += payload
                if fin:
                    data, self._frag = self._frag, b""
                    return data
                continue

    def close(self):
        try:
            self._send_frame(0x08, b"")
        except (OSError, ValueError):
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _parse_asr_frame(data):
    """解析火山流式 ASR 服务端帧，返回 (kind, flags, payload)。"""
    if len(data) < 4:
        return None
    msg_type = data[1] >> 4
    flags = data[1] & 0x0F
    if msg_type == 0x0F:  # 错误响应: [4B header][4B code][4B len][payload]
        length = int.from_bytes(data[8:12], "big") if len(data) >= 12 else 0
        payload = data[12:12 + length] if length else data[12:]
        return ("error", flags, payload)
    if msg_type in (0x09, 0x0B):  # 服务端结果
        offset = 4 + (4 if flags & 0x01 else 0)
        length = int.from_bytes(data[offset:offset + 4], "big")
        offset += 4
        return ("result", flags, data[offset:offset + length])
    return None


def _asr_frame(message_type, flags, payload):
    header = bytes([0x11, (message_type << 4) | flags, 0x11, 0x00])
    body = gzip.compress(payload)
    return header + len(body).to_bytes(4, "big") + body


class StreamingASR(object):
    """Seed ASR 2.0 双向流式识别：边说边发，静音后立即出最终结果。"""

    def __init__(self, conf, ssl_ctx):
        self.text = ""
        self.error = None
        self.finished = threading.Event()
        self._closed = False
        self.ws = self._connect(conf, ssl_ctx)
        request = {
            "user": {"uid": "100ask-board"},
            "audio": {"format": "pcm", "rate": SAMPLE_RATE, "bits": 16, "channel": 1},
            "request": {
                "model_name": "bigmodel",
                "enable_punc": True,
                "enable_itn": True,
                "result_type": "full",
                "show_utterances": True,
            },
        }
        self.ws.send_binary(_asr_frame(0x01, 0x00, json.dumps(request).encode("utf-8")))
        self._reader = threading.Thread(target=self._read_loop)
        self._reader.daemon = True
        self._reader.start()

    def _connect(self, conf, ssl_ctx):
        api_key = cfg_get(conf, "asr", "api_key")
        appid = cfg_get(conf, "asr", "appid")
        token = cfg_get(conf, "asr", "access_token")
        if not api_key and not (appid and token):
            raise RuntimeError("流式 ASR 未配置凭证")
        endpoint = cfg_get(conf, "asr", "stream_url")
        alt_endpoint = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
        resources = [cfg_get(conf, "asr", "stream_resource_id"),
                     "volc.seedasr.sauc.duration", "volc.seedasr.sauc.concurrent"]
        candidates = []
        for url in (endpoint, alt_endpoint):
            for resource in resources:
                item = (url, resource)
                if item not in candidates:
                    candidates.append(item)
        last_error = None
        for url, resource in candidates:
            headers = {
                "X-Api-Resource-Id": resource,
                "X-Api-Request-Id": str(uuid.uuid4()),
                "X-Api-Connect-Id": str(uuid.uuid4()),
            }
            if api_key:
                headers["X-Api-Key"] = api_key
            else:
                headers["X-Api-App-Key"] = appid
                headers["X-Api-Access-Key"] = token
            try:
                return WSClient(url, headers, ssl_ctx)
            except Exception as err:
                last_error = err
        raise RuntimeError("流式 ASR 连接失败: %s" % last_error)

    def _read_loop(self):
        try:
            while not self._closed:
                data = self.ws.recv_message()
                if data is None:
                    continue
                parsed = _parse_asr_frame(data)
                if parsed is None:
                    continue
                kind, flags, payload = parsed
                if kind == "error":
                    message = payload.decode("utf-8", "ignore")
                    if "last packet" in message or "last package" in message:
                        self.finished.set()
                        return
                    self.error = message
                    self.finished.set()
                    return
                try:
                    result = json.loads(payload.decode("utf-8"))
                except ValueError:
                    continue
                text = ((result.get("result") or {}).get("text") or "").strip()
                if text:
                    self.text = text
                if flags & 0x02:  # 末包
                    self.finished.set()
                    return
        except Exception as err:
            if not self._closed:
                self.error = str(err)
                self.finished.set()

    def send_audio(self, pcm):
        if self._closed:
            return
        try:
            self.ws.send_binary(_asr_frame(0x02, 0x00, pcm))
        except (OSError, ConnectionError) as err:
            self.error = str(err)
            self.finished.set()

    def finish(self, timeout=8):
        if not self._closed:
            try:
                self.ws.send_binary(_asr_frame(0x02, 0x02, b""))
            except (OSError, ConnectionError):
                pass
        self.finished.wait(timeout)
        text = self.text
        error = self.error
        self.close()
        if not text and error:
            raise RuntimeError("流式 ASR 失败: %s" % error)
        return text

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.ws.close()


# ------------------------------------------------- 端到端实时语音（全双工）

RT_START_CONNECTION = 1
RT_FINISH_CONNECTION = 2
RT_CONNECTION_STARTED = 50
RT_CONNECTION_FAILED = 51
RT_CONNECTION_FINISHED = 52
RT_START_SESSION = 100
RT_FINISH_SESSION = 102
RT_SESSION_STARTED = 150
RT_SESSION_FINISHED = 152
RT_SESSION_FAILED = 153
RT_USAGE_RESPONSE = 154
RT_TASK_REQUEST = 200
RT_SAY_HELLO = 300
RT_TTS_SENTENCE_START = 350
RT_TTS_SENTENCE_END = 351
RT_TTS_RESPONSE = 352
RT_TTS_ENDED = 359
RT_ASR_INFO = 450
RT_ASR_RESPONSE = 451
RT_ASR_ENDED = 459
RT_CHAT_RESPONSE = 550
RT_CHAT_ENDED = 559

RT_MSG_FULL_CLIENT = 0x10
RT_MSG_AUDIO_ONLY_CLIENT = 0x20
RT_MSG_FULL_SERVER = 0x90
RT_MSG_AUDIO_ONLY_SERVER = 0xB0
RT_MSG_ERROR = 0xF0
RT_FLAG_WITH_EVENT = 0x04
RT_SERIAL_RAW = 0x00
RT_SERIAL_JSON = 0x10
RT_COMPRESS_GZIP = 0x01


def rt_encode_frame(event, session_id, payload, msg_type=RT_MSG_FULL_CLIENT,
                    serialization=RT_SERIAL_JSON, compress=True):
    frame = bytearray([0x11, msg_type | RT_FLAG_WITH_EVENT,
                       serialization | (RT_COMPRESS_GZIP if compress else 0), 0x00])
    frame += struct.pack(">i", event)
    if event not in (1, 2, 50, 51, 52):
        sid = (session_id or "").encode("utf-8")
        frame += struct.pack(">I", len(sid)) + sid
    body = gzip.compress(payload) if compress else payload
    frame += struct.pack(">I", len(body)) + body
    return bytes(frame)


def rt_decode_frame(data):
    if len(data) < 4:
        return None
    msg_type = data[1] & 0xF0
    flags = data[1] & 0x0F
    compression = data[2] & 0x0F
    offset = 4
    event = None
    session_id = None
    error_code = None
    if msg_type == RT_MSG_ERROR and len(data) >= 8:
        error_code = struct.unpack(">I", data[offset:offset + 4])[0]
        offset += 4
    if flags & RT_FLAG_WITH_EVENT:
        if len(data) < offset + 4:
            return None
        event = struct.unpack(">i", data[offset:offset + 4])[0]
        offset += 4
        if event not in (1, 2, 50, 51, 52):
            if len(data) < offset + 4:
                return None
            sid_len = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4
            session_id = data[offset:offset + sid_len].decode("utf-8", "ignore")
            offset += sid_len
        if event in (50, 51, 52):
            if len(data) < offset + 4:
                return None
            cid_len = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4 + cid_len
    if len(data) < offset + 4:
        return None
    payload_len = struct.unpack(">I", data[offset:offset + 4])[0]
    offset += 4
    payload = data[offset:offset + payload_len]
    if compression == RT_COMPRESS_GZIP and payload:
        try:
            payload = gzip.decompress(payload)
        except OSError:
            pass
    return {"msg_type": msg_type, "flags": flags, "event": event,
            "error_code": error_code, "session_id": session_id, "payload": payload}


class RealtimeDialogue(object):
    """豆包端到端实时语音（全双工）：语音进 -> 语音出，支持服务端 VAD 与打断。"""

    def __init__(self, conf, ssl_ctx):
        self.conf = conf
        self.session_id = str(uuid.uuid4())
        api_key = cfg_get(conf, "realtime", "api_key")
        app_id = cfg_get(conf, "realtime", "app_id")
        token = cfg_get(conf, "realtime", "access_token")
        if not api_key and not (app_id and token):
            raise RuntimeError("实时语音未配置凭证（[realtime] app_id + access_token）")
        headers = {
            "X-Api-Resource-Id": cfg_get(conf, "realtime", "resource_id"),
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        if api_key:
            headers["X-Api-Key"] = api_key
        else:
            headers["X-Api-App-ID"] = app_id
            headers["X-Api-Access-Key"] = token
            headers["X-Api-App-Key"] = cfg_get(conf, "realtime", "app_key")
        self.ws = WSClient(cfg_get(conf, "realtime", "url"), headers, ssl_ctx)
        self.events = queue.Queue()
        self._closed = False
        self._send_event(RT_START_CONNECTION, None, b"{}")
        self._wait_event(RT_CONNECTION_STARTED, "连接握手")
        self._send_event(RT_START_SESSION, self.session_id,
                         json.dumps(self._session_payload(), ensure_ascii=False).encode("utf-8"))
        self._wait_event(RT_SESSION_STARTED, "会话建立")
        hello = cfg_get(conf, "realtime", "say_hello").strip()
        if hello:
            self._send_event(RT_SAY_HELLO, self.session_id,
                             json.dumps({"content": hello}, ensure_ascii=False).encode("utf-8"))
        self._reader = threading.Thread(target=self._read_loop)
        self._reader.daemon = True
        self._reader.start()

    def _session_payload(self):
        bot_name = cfg_get(self.conf, "realtime", "bot_name")
        manifest_parts = []
        if bot_name:
            manifest_parts.append("名字：%s" % bot_name)
        system_prompt = cfg_get(self.conf, "realtime", "system_prompt").strip()
        if system_prompt:
            manifest_parts.append(system_prompt)
        style = cfg_get(self.conf, "realtime", "speaking_style").strip()
        if style:
            manifest_parts.append("说话风格：%s" % style)
        return {
            "asr": {"extra": {
                "end_smooth_window_ms": int(cfg_get(self.conf, "realtime", "end_smooth_window_ms")),
                "enable_custom_vad": False,
            }},
            "tts": {
                "speaker": cfg_get(self.conf, "realtime", "speaker"),
                "audio_config": {
                    "channel": 1, "format": "pcm_s16le",
                    "sample_rate": int(cfg_get(self.conf, "realtime", "output_sample_rate")),
                },
            },
            "dialog": {
                "character_manifest": "\n".join(manifest_parts),
                "extra": {
                    "strict_audit": False,
                    "recv_timeout": 120,
                    "input_mod": "keep_alive",
                    "model": cfg_get(self.conf, "realtime", "model"),
                },
            },
        }

    def _send_event(self, event, session_id, payload):
        self.ws.send_binary(rt_encode_frame(event, session_id, payload))

    def _wait_event(self, expected, stage, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.ws.recv_message()
            if data is None:
                continue
            frame = rt_decode_frame(data)
            if not frame:
                continue
            if frame["msg_type"] == RT_MSG_ERROR:
                detail = frame["payload"].decode("utf-8", "ignore")
                raise RuntimeError("%s 失败: code=%s %s" % (stage, frame["error_code"], detail[:200]))
            if frame["msg_type"] == RT_MSG_FULL_SERVER and frame["event"] == expected:
                return frame
        raise RuntimeError("%s 超时（未收到事件 %d）" % (stage, expected))

    def send_audio(self, pcm):
        if self._closed:
            return
        try:
            self.ws.send_binary(rt_encode_frame(
                RT_TASK_REQUEST, self.session_id, pcm,
                msg_type=RT_MSG_AUDIO_ONLY_CLIENT, serialization=RT_SERIAL_RAW))
        except (OSError, ConnectionError) as err:
            self.events.put({"type": "error", "message": "发送音频失败: %s" % err})

    def _read_loop(self):
        try:
            while not self._closed:
                data = self.ws.recv_message()
                if data is None:
                    continue
                frame = rt_decode_frame(data)
                if not frame:
                    continue
                if frame["msg_type"] == RT_MSG_ERROR:
                    detail = frame["payload"].decode("utf-8", "ignore")
                    if "IdleTimeout" in detail or "idle" in detail.lower():
                        continue
                    self.events.put({"type": "error", "message": detail or str(frame["error_code"])})
                    continue
                if frame["msg_type"] == RT_MSG_AUDIO_ONLY_SERVER:
                    self.events.put({"type": "audio", "data": frame["payload"]})
                    continue
                if frame["msg_type"] != RT_MSG_FULL_SERVER:
                    continue
                event = frame["event"]
                payload = frame["payload"]
                if event == RT_TTS_RESPONSE and payload[:1] not in (b"{", b"["):
                    self.events.put({"type": "audio", "data": payload})
                    continue
                try:
                    data = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    data = {}
                if event == RT_ASR_INFO:
                    self.events.put({"type": "speech_start"})
                elif event == RT_ASR_RESPONSE:
                    results = data.get("results") or []
                    if results:
                        text = (results[0].get("text") or "").strip()
                        if text:
                            final = not results[0].get("is_interim", True)
                            self.events.put({"type": "asr", "text": text, "final": final})
                elif event == RT_ASR_ENDED:
                    self.events.put({"type": "asr_end"})
                elif event == RT_CHAT_RESPONSE:
                    content = data.get("content") or ""
                    if content:
                        self.events.put({"type": "llm", "text": content})
                elif event == RT_TTS_SENTENCE_END:
                    text = (data.get("text") or "").strip()
                    if text:
                        self.events.put({"type": "tts_text", "text": text})
                elif event == RT_TTS_ENDED:
                    self.events.put({"type": "reply_done"})
                elif event == RT_SESSION_FINISHED:
                    self.events.put({"type": "session_end", "finished": True})
                elif event == RT_SESSION_FAILED:
                    self.events.put({"type": "error",
                                     "message": "会话失败: %s" % json.dumps(data, ensure_ascii=False)[:200]})
        except Exception as err:
            if not self._closed:
                self.events.put({"type": "error", "message": "连接异常: %s" % err})
        finally:
            self.events.put({"type": "closed"})

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._send_event(RT_FINISH_SESSION, self.session_id, b"{}")
        except (OSError, ConnectionError):
            pass
        self.ws.close()


def _start_realtime_player(conf):
    """实时 TTS 输出为 24kHz PCM：PulseAudio 用 paplay --raw，否则退回 aplay --raw。"""
    pulse = find_pulse_server()
    try:
        if pulse:
            _resume_sink(pulse)
            return subprocess.Popen(
                _paplay_raw_cmd(),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env=dict(os.environ, PULSE_SERVER=pulse),
            )
        return subprocess.Popen(
            ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", "24000", "-c", "1"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except OSError:
        return None


def _stop_process(proc):
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def realtime_loop(conf, ssl_ctx):
    """全双工实时对话主循环：麦克风音频持续上行，TTS 音频连续下行播放。"""
    # 注意：必须先建立播放保活流再启动 arecord —— WM8960 在采集流已活跃时打不开播放流
    keepalive = start_sink_keepalive(conf)
    recorder = Recorder(conf)
    dialogue = None
    player = None
    barge_in = cfg_get(conf, "realtime", "barge_in", "true").lower() in ("1", "true", "yes")
    playing = False
    pending = bytearray()
    partial = ""
    reply_text = ""
    turn_text = ""
    printing_reply = False
    try:
        log("麦克风预热中……")
        recorder.discard(MIC_STARTUP_DISCARD_S)

        def drop_player():
            nonlocal player
            _stop_process(player)
            player = None

        while True:
            if dialogue is None:
                try:
                    dialogue = RealtimeDialogue(conf, ssl_ctx)
                    log("实时会话已建立，直接说话即可（服务端自动断句/打断）")
                except Exception as err:
                    log("建立实时会话失败: %s（5 秒后重试）" % err)
                    time.sleep(5)
                    continue
            frame = recorder.read_frame()
            if not playing or barge_in:
                pending += frame
                if len(pending) >= 3200:
                    dialogue.send_audio(bytes(pending))
                    pending = bytearray()
            while True:
                try:
                    event = dialogue.events.get_nowait()
                except queue.Empty:
                    break
                kind = event["type"]
                if kind == "speech_start":
                    playing = False
                    drop_player()
                    partial = ""
                    if printing_reply:
                        print()
                        printing_reply = False
                elif kind == "asr":
                    text = event["text"]
                    if event["final"]:
                        log("你说: %s" % text)
                    elif text != partial:
                        log("识别中… %s" % text)
                    partial = text
                elif kind == "asr_end":
                    pass
                elif kind == "llm":
                    if not printing_reply:
                        print("[%s] AI  : " % time.strftime("%H:%M:%S"), end="", flush=True)
                        printing_reply = True
                        reply_text = ""
                    reply_text += event["text"]
                    print(event["text"], end="", flush=True)
                elif kind == "tts_text":
                    turn_text = event["text"]
                elif kind == "audio":
                    playing = True
                    if player is None:
                        player = _start_realtime_player(conf)
                    if player is not None:
                        try:
                            _write_all(player, event["data"])
                        except (BrokenPipeError, OSError, ValueError):
                            drop_player()
                elif kind == "reply_done":
                    playing = False
                    if printing_reply:
                        print()
                        printing_reply = False
                    elif turn_text:
                        log("AI  : %s" % turn_text)
                    turn_text = ""
                elif kind == "session_end":
                    log("会话已结束，重新建立")
                    dialogue.close()
                    dialogue = None
                    drop_player()
                    playing = False
                elif kind == "error":
                    log("实时对话错误: %s" % event["message"])
                elif kind == "closed":
                    dialogue.close()
                    dialogue = None
                    drop_player()
                    playing = False
    finally:
        if dialogue is not None:
            dialogue.close()
        _stop_process(player)
        recorder.close()
        stop_sink_keepalive(keepalive)


def llm_reply(conf, ssl_ctx, text):
    api_key = cfg_get(conf, "llm", "ark_api_key")
    if not api_key:
        raise RuntimeError("LLM 未配置凭证（[llm] ark_api_key）")
    payload = {
        "model": cfg_get(conf, "llm", "model"),
        "messages": [
            {"role": "system", "content": cfg_get(conf, "llm", "system_prompt")},
            {"role": "user", "content": text},
        ],
        "max_tokens": int(cfg_get(conf, "llm", "max_tokens")),
        "temperature": float(cfg_get(conf, "llm", "temperature")),
    }
    thinking = cfg_get(conf, "llm", "thinking").lower()
    if thinking in ("disabled", "enabled"):
        payload["thinking"] = {"type": thinking}
    url = cfg_get(conf, "llm", "base_url").rstrip("/") + "/chat/completions"
    status, _, body = http_post(url, payload, {"Authorization": "Bearer " + api_key}, 60, ssl_ctx)
    if status != 200:
        raise RuntimeError("LLM 失败: HTTP %d %s" % (status, body[:300].decode("utf-8", "ignore")))
    result = json.loads(body.decode("utf-8"))
    return result["choices"][0]["message"]["content"].strip()


SENTENCE_HARD_END = "。！？!?；;\n"
SENTENCE_SOFT_END = "，,、:："


def split_sentence(text, soft_len=28):
    """从流式文本中切出可送去合成的句子：优先句末标点，长句在逗号处软切。"""
    for index, char in enumerate(text):
        if char in SENTENCE_HARD_END:
            sentence = text[:index + 1].strip()
            if sentence:
                return sentence, text[index + 1:]
    if len(text) >= soft_len:
        for index in range(min(len(text), soft_len) - 1, 0, -1):
            if text[index] in SENTENCE_SOFT_END:
                sentence = text[:index + 1].strip()
                if sentence:
                    return sentence, text[index + 1:]
    return "", text


def llm_stream(conf, ssl_ctx, text):
    """SSE 流式对话，逐段 yield 回复文本。"""
    api_key = cfg_get(conf, "llm", "ark_api_key")
    if not api_key:
        raise RuntimeError("LLM 未配置凭证（[llm] ark_api_key）")
    payload = {
        "model": cfg_get(conf, "llm", "model"),
        "messages": [
            {"role": "system", "content": cfg_get(conf, "llm", "system_prompt")},
            {"role": "user", "content": text},
        ],
        "max_tokens": int(cfg_get(conf, "llm", "max_tokens")),
        "temperature": float(cfg_get(conf, "llm", "temperature")),
        "stream": True,
    }
    thinking = cfg_get(conf, "llm", "thinking").lower()
    if thinking in ("disabled", "enabled"):
        payload["thinking"] = {"type": thinking}
    url = cfg_get(conf, "llm", "base_url").rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + api_key)
    try:
        resp = urllib.request.urlopen(req, timeout=60, context=ssl_ctx)
    except urllib.error.HTTPError as err:
        raise RuntimeError("LLM 失败: HTTP %d %s" % (err.code, err.read()[:300].decode("utf-8", "ignore")))
    with resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                return
            try:
                chunk = json.loads(body)
            except ValueError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            content = (choices[0].get("delta") or {}).get("content")
            if content:
                yield content


def tts_synthesize(conf, ssl_ctx, text):
    return b"".join(tts_stream(conf, ssl_ctx, text))


def tts_stream(conf, ssl_ctx, text):
    """逐块 yield 合成音频；seed 模式边合成边返回，legacy 模式一次性返回。"""
    if cfg_get(conf, "tts", "mode") == "seed":
        for chunk in _tts_seed_stream(conf, ssl_ctx, text):
            yield chunk
    else:
        yield _tts_legacy(conf, ssl_ctx, text)


def _tts_legacy(conf, ssl_ctx, text):
    appid = cfg_get(conf, "tts", "appid")
    token = cfg_get(conf, "tts", "access_token")
    if not (appid and token):
        raise RuntimeError("TTS(legacy) 未配置凭证（[tts] appid + access_token）")
    payload = {
        "app": {"appid": appid, "token": token, "cluster": cfg_get(conf, "tts", "cluster")},
        "user": {"uid": "100ask-board"},
        "audio": {
            "voice_type": cfg_get(conf, "tts", "voice_type"),
            "encoding": cfg_get(conf, "tts", "encoding"),
            "speed_ratio": float(cfg_get(conf, "tts", "speed_ratio")),
        },
        "request": {"reqid": str(uuid.uuid4()), "text": text, "text_type": "plain", "operation": "query"},
    }
    status, _, body = http_post(
        "https://openspeech.bytedance.com/api/v1/tts", payload,
        {"Authorization": "Bearer;%s" % token}, 30, ssl_ctx)
    if status != 200:
        raise RuntimeError("TTS 失败: HTTP %d %s" % (status, body[:300].decode("utf-8", "ignore")))
    result = json.loads(body.decode("utf-8"))
    if result.get("code") != 3000 or not result.get("data"):
        raise RuntimeError("TTS 失败: code=%s message=%s" % (result.get("code"), result.get("message")))
    return base64.b64decode(result["data"])


def _tts_seed_stream(conf, ssl_ctx, text):
    api_key = cfg_get(conf, "tts", "api_key")
    if not api_key:
        raise RuntimeError("TTS(seed) 未配置凭证（[tts] api_key）")
    encoding = playback_encoding(conf)
    payload = {
        "user": {"uid": "100ask-board"},
        "req_params": {
            "text": text,
            "speaker": cfg_get(conf, "tts", "voice_type"),
            "audio_params": {
                "format": "wav" if encoding == "wav" else encoding,
                "sample_rate": 24000,
            },
        },
    }
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": cfg_get(conf, "tts", "resource_id"),
    }
    app_key = cfg_get(conf, "tts", "app_key")
    if app_key:
        headers["X-Api-App-Key"] = app_key
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(cfg_get(conf, "tts", "base_url"), data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        resp = urllib.request.urlopen(req, timeout=30, context=ssl_ctx)
    except urllib.error.HTTPError as err:
        raise RuntimeError("TTS 失败: HTTP %d %s" % (err.code, err.read()[:300].decode("utf-8", "ignore")))
    chunks = 0
    with resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "ignore").strip()
            if not line:
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            code = frame.get("code")
            if code == 0 and frame.get("data"):
                chunks += 1
                yield base64.b64decode(frame["data"])
            elif code not in (0, 20000000):
                raise RuntimeError("TTS 失败: code=%s message=%s" % (code, frame.get("message")))
    if not chunks:
        raise RuntimeError("TTS 返回空音频")


# ------------------------------------------------------------- 流式播报

def _sink_running(pulse):
    try:
        out = subprocess.run(["pactl", "list", "sinks"], env=dict(os.environ, PULSE_SERVER=pulse),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        return b"State: RUNNING" in out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_sink_keepalive(conf):
    """用一路静音流保持 PulseAudio sink 常开；启动后校验，失败重试一次。"""
    pulse = find_pulse_server()
    if not pulse:
        return None
    env = dict(os.environ, PULSE_SERVER=pulse)
    for attempt in (0, 1):
        try:
            proc = subprocess.Popen(
                _paplay_raw_cmd() + ["/dev/zero"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env,
            )
        except OSError:
            return None
        time.sleep(1.0)
        if _sink_running(pulse):
            return proc
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        _resume_sink(pulse)
    log("警告: PulseAudio sink 未能唤醒，播放可能异常")
    return None


def stop_sink_keepalive(player):
    if player is None:
        return
    try:
        player.terminate()
        player.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            player.kill()
        except OSError:
            pass


def _start_stream_player(conf):
    """启动流式播放器：PulseAudio 可用时为 paplay(raw pcm)，否则 mp3 走 mpg123；不支持则返回 None。"""
    encoding = playback_encoding(conf)
    pulse = find_pulse_server()
    try:
        if pulse and encoding == "pcm":
            _resume_sink(pulse)
            env = dict(os.environ, PULSE_SERVER=pulse)
            return subprocess.Popen(
                _paplay_raw_cmd(),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
            )
        if encoding == "mp3":
            return subprocess.Popen(
                ["mpg123", "-q", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
    except OSError:
        return None
    return None


def stream_reply(conf, ssl_ctx, text):
    """LLM 流式生成 -> 按句切分 -> TTS 流式合成 -> mpg123 边合成边播。"""
    started = time.monotonic()
    first_audio = None
    player = _start_stream_player(conf)
    buffered = []
    all_audio = []
    parts = []

    def emit(chunk):
        nonlocal player, first_audio
        all_audio.append(chunk)
        if first_audio is None:
            first_audio = time.monotonic() - started
        if player is not None:
            try:
                _write_all(player, chunk)
                return
            except (BrokenPipeError, OSError, ValueError):
                log("警告: 流式播放器异常，转为缓冲播放")
                try:
                    player.kill()
                except OSError:
                    pass
                player = None
        buffered.append(chunk)

    def speak(sentence):
        for chunk in tts_stream(conf, ssl_ctx, sentence):
            emit(chunk)

    print("[%s] AI  : " % time.strftime("%H:%M:%S"), end="", flush=True)
    try:
        pending = ""
        for delta in llm_stream(conf, ssl_ctx, text):
            parts.append(delta)
            print(delta, end="", flush=True)
            pending += delta
            while True:
                sentence, pending = split_sentence(pending)
                if not sentence:
                    break
                speak(sentence)
        tail = pending.strip()
        if tail:
            speak(tail)
    finally:
        print()
        if player is not None:
            try:
                player.stdin.close()
            except OSError:
                pass
            try:
                player.wait(timeout=60)
            except subprocess.TimeoutExpired:
                player.kill()
                player.wait()
                log("播放超时，已强制结束")
            err = player.stderr.read() if player.stderr else b""
            if player.returncode not in (0, None) and player.returncode != -9:
                log("播放失败: %s" % err.decode("utf-8", "ignore").strip())
        elif buffered:
            play_audio(conf, b"".join(buffered))
    if not parts:
        raise RuntimeError("LLM 返回为空")
    if all_audio:
        with open("/tmp/va_reply." + _audio_ext(playback_encoding(conf)), "wb") as fh:
            fh.write(b"".join(all_audio))
    log("回复完成（首声 %.1fs，总耗时 %.1fs）"
        % (first_audio if first_audio is not None else -1, time.monotonic() - started))
    return "".join(parts)


def reply_and_speak(conf, ssl_ctx, text):
    if cfg_get(conf, "llm", "stream", "true").lower() in ("1", "true", "yes"):
        return stream_reply(conf, ssl_ctx, text)
    reply = llm_reply(conf, ssl_ctx, text)
    log("AI  : %s" % reply)
    play_audio(conf, tts_synthesize(conf, ssl_ctx, reply))
    return reply


# --------------------------------------------------------------------- 主流程

def load_config(path):
    conf = configparser.ConfigParser()
    if os.path.isfile(path):
        conf.read(path)
    return conf


def check_config(conf):
    mode = cfg_get(conf, "dialogue", "mode")
    checks = [
        ("实时语音", cfg_get(conf, "realtime", "api_key") or
         (cfg_get(conf, "realtime", "app_id") and cfg_get(conf, "realtime", "access_token"))),
    ]
    if mode != "realtime":
        checks += [
            ("LLM 方舟", cfg_get(conf, "llm", "ark_api_key")),
            ("ASR 凭证", cfg_get(conf, "asr", "api_key") or
             (cfg_get(conf, "asr", "appid") and cfg_get(conf, "asr", "access_token"))),
            ("TTS 凭证", cfg_get(conf, "tts", "api_key") if cfg_get(conf, "tts", "mode") == "seed"
             else (cfg_get(conf, "tts", "appid") and cfg_get(conf, "tts", "access_token"))),
        ]
    ok = True
    for name, value in checks:
        print("%-12s %s" % (name, "已配置" if value else "缺失"))
        ok = ok and bool(value)
    print("对话模式:     %s" % mode)
    print("实时音色:     %s (model %s)" % (cfg_get(conf, "realtime", "speaker"),
                                            cfg_get(conf, "realtime", "model")))
    if mode != "realtime":
        print("ASR 模式:     %s" % cfg_get(conf, "asr", "mode"))
        print("TTS 模式:     %s" % cfg_get(conf, "tts", "mode"))
        print("LLM 模型:     %s" % cfg_get(conf, "llm", "model"))
    if not ok:
        print("\n请编辑 %s/config.ini 补全凭证（可参考 config.ini.example）" % BASE_DIR)
    return ok


def simulate(conf, path):
    """离线跑一遍 VAD：把 wav 文件当麦克风输入，检查断句逻辑。"""
    with open(path, "rb") as fh:
        wav_bytes = fh.read()
    vad = VAD(
        threshold=int(cfg_get(conf, "audio", "energy_threshold")),
        silence_ms=int(cfg_get(conf, "audio", "silence_ms")),
        min_speech_ms=int(cfg_get(conf, "audio", "min_speech_ms")),
        max_record_ms=int(cfg_get(conf, "audio", "max_record_ms")),
    )
    segments = []
    frames = wav_to_frames(wav_bytes)
    for index, frame in enumerate(frames):
        event = vad.feed(frame)
        stamp = index * FRAME_MS / 1000.0
        if event == "start":
            segments.append([stamp, None, "speech"])
            print("  %7.2fs 说话开始" % stamp)
        elif event in ("end", "timeout"):
            if segments:
                segments[-1][1] = stamp
            print("  %7.2fs 录音结束 (%s, %.2fs)" % (stamp, event, len(vad.frames) * FRAME_MS / 1000.0))
    print("共 %d 帧 / %.2fs 音频，检出 %d 段语音" % (len(frames), len(frames) * FRAME_MS / 1000.0, len(segments)))
    return segments


def conversation_loop(conf, ssl_ctx, once=False):
    vad = VAD(
        threshold=int(cfg_get(conf, "audio", "energy_threshold")),
        silence_ms=int(cfg_get(conf, "audio", "silence_ms")),
        min_speech_ms=int(cfg_get(conf, "audio", "min_speech_ms")),
        max_record_ms=int(cfg_get(conf, "audio", "max_record_ms")),
    )
    keepalive = start_sink_keepalive(conf)
    recorder = Recorder(conf)
    try:
        log("麦克风预热中……")
        recorder.discard(MIC_STARTUP_DISCARD_S)
        while True:
            wav_bytes, text = record_turn(conf, ssl_ctx, vad, recorder)
            with open("/tmp/va_last.wav", "wb") as fh:
                fh.write(wav_bytes)
            if not text:
                log("没有听清，请再说一次")
                if once:
                    return
                continue
            log("你说: %s" % text)
            recorder.discard(0.3)  # 丢弃云端往返期间积压的旧音频
            reply_and_speak(conf, ssl_ctx, text)
            recorder.discard(PLAYBACK_DISCARD_S)  # 丢弃扬声器回声，防止自触发
            if once:
                return
    finally:
        recorder.close()
        stop_sink_keepalive(keepalive)


def main():
    def _on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    parser = argparse.ArgumentParser(description="100ask i.MX6ULL 云 AI 语音对话最小原型")
    parser.add_argument("--config", default=os.path.join(BASE_DIR, "config.ini"), help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只对话一轮就退出")
    parser.add_argument("--check", action="store_true", help="检查配置是否齐全")
    parser.add_argument("--simulate", metavar="WAV", help="离线跑 VAD 断句（检测逻辑自测，不联网）")
    parser.add_argument("--ask", metavar="TEXT", help="跳过录音和识别，直接走 LLM+TTS 播报（调试用）")
    parser.add_argument("--pipeline", action="store_true", help="强制使用 ASR+LLM+TTS 串联模式（默认实时语音）")
    args = parser.parse_args()
    conf = load_config(args.config)
    if args.check:
        sys.exit(0 if check_config(conf) else 1)
    if args.simulate:
        simulate(conf, args.simulate)
        return
    ssl_ctx = make_ssl_context(conf)
    if args.ask:
        log("你说: %s" % args.ask)
        keepalive = start_sink_keepalive(conf)
        try:
            reply_and_speak(conf, ssl_ctx, args.ask)
        finally:
            stop_sink_keepalive(keepalive)
        return
    mode = "pipeline" if args.pipeline else cfg_get(conf, "dialogue", "mode")
    log("语音助手已启动（对话模式: %s）" % mode)
    try:
        if mode == "realtime":
            realtime_loop(conf, ssl_ctx)
        else:
            conversation_loop(conf, ssl_ctx, once=args.once)
    except KeyboardInterrupt:
        print()
        log("已退出")
    except Exception as err:
        log("出错: %s" % err)
        sys.exit(1)


if __name__ == "__main__":
    main()
