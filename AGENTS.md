# AGENTS.md

Cloud AI voice chat for the 100ask i.MX6ULL board (Buildroot + Python 3.8, WM8960 audio, system PulseAudio).
Pure Python3 stdlib, no build step; sources at the repo root, `README.md` is the full reproduction guide.

## Deploy & run

```sh
scp voice_assistant.py config.ini.example root@192.168.1.14:/root/voice_assistant/
ssh root@192.168.1.14 'cd /root/voice_assistant && curl -k -L -o cacert.pem https://curl.se/ca/cacert.pem'
# board config (real creds, gitignored): /root/voice_assistant/config.ini
ssh root@192.168.1.14 'cd /root/voice_assistant && python3 voice_assistant.py --check'
ssh root@192.168.1.14 '/etc/init.d/S99voiceassistant start'   # log: /tmp/voice_assistant.log
```

Self-tests: `--simulate WAV` (offline VAD), `--ask "文本"` (LLM→TTS→播放，不占麦), `--pipeline --once`.
Qt UI companion project: `project/voice_assistant_qt` launches this script with `--spectrum`.

## Current state (2026-09, verified on board)

- Default `realtime` mode: Doubao 端到端实时语音全双工 (`/api/v3/realtime/dialogue`, `volc.speech.dialog`);
  `pipeline` mode: ASR (`volc.seedasr.auc` flash / `sauc` stream) → Ark LLM SSE → Doubao TTS `seed-tts-2.0`.
- Features: server VAD + barge-in, auto reconnect, idle session refresh (30min, refreshes injected time),
  time injection (`[dialogue] inject_time`, UTC+8), PulseAudio auto-heal, `--spectrum` base64 PCM output.
- Voice: `ICL_uranus_zh_female_qingxinshaonv_tob` (清新少女) on the board; official S2S voices are `ICL_uranus_*`.

## Gotchas (all handled in code — don't regress)

- **Credentials are two separate systems**: speech console API Key (UUID, `X-Api-Key`) vs Ark key (`ark-`, `ark.cn-beijing.volces.com`).
  Realtime dialogue accepts ONLY the speech API Key — App ID + Access Token handshake gets 403 not-granted.
- Service activation lags minutes (`45000030`); TTS voice generation must match resource generation (`*_uranus_bigtts` ↔ `seed-tts-2.0`).
- **Realtime voices are a separate set from synthesis TTS**: use `ICL_uranus_*` (or legacy `saturn_*`); `mars/moon/plain uranus` TTS voices fail with `ClientError:InvalidSpeaker` (sometimes only on real replies, not SayHello).
- **PulseAudio on this board**: ① sink wedges in SUSPENDED → `module-suspend-on-idle` disabled in `/etc/pulse/system.pa` (script `scripts/fix_pulse_suspend.sh`); ② sometimes starts with `auto_null` dummy → no sound, code auto-restarts PA; ③ with suspend disabled PA holds capture too → card profile set to `output:stereo-fallback`. Playback keepalive (`paplay /dev/zero`) MUST start before `arecord` (WM8960 can't open playback while capture is active). Restarting PA resets WM8960 hardware volume → `alsactl restore` before playback.
- Python FFT is too slow on this single-core board (256-pt ≈ 39 ms) — spectrum DSP lives in the Qt project; this script only forwards PCM.
- `config.ini` / `cacert.pem` are gitignored; never commit real credentials.
