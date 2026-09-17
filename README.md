# voice_assistant — 100ask i.MX6ULL 云 AI 语音对话

在板子上跑实时 AI 语音对话，纯 Python3 标准库实现，无第三方依赖、无需交叉编译，scp 到板子即可运行。
支持两种模式（`[dialogue] mode`）：

**realtime（默认）—— 端到端实时语音全双工**
```
麦克风(WM8960) ──arecord──> WebSocket PCM 上行 ──> 豆包端到端实时语音大模型
        ▲                                              │ 服务端 VAD/ASR/LLM/TTS
        └── 可随时打断 ── paplay(24k PCM 流式播放) <── 音频帧下行
```
语音进、语音出，服务端自动断句，可在助手说话时直接开口打断，回复紧跟话音（约 0.5~1s）。

**pipeline —— ASR + LLM + TTS 串联**
```
麦克风 ──arecord──> 能量 VAD 断句 ──wav──> 录音文件识别 2.0 ──> 方舟 LLM(SSE 流式)
                                                                    │ 按标点分句
        paplay(PCM 流式, 静音流保活) <──分片── 豆包 TTS(流式合成) <──┘
```
支持流式 ASR（边说边上屏，`[asr] mode = stream`，需开通流式语音识别）。

两种模式播放都优先走 PulseAudio（`paplay --raw` PCM，并起一路静音流防止 sink 挂起），
无 PulseAudio 时退回 ALSA。

## 1. 板端依赖（Buildroot 已自带，无需安装）

- `python3`（3.8+）、`arecord`（alsa-utils）、`mpg123`（或 `aplay`）、`curl`
- 麦克风可用：`arecord -d 3 -f cd /tmp/t.wav` 能生成听得见的录音

## 2. 申请火山引擎凭证

| 用途 | 控制台 | 需要的凭证 |
| --- | --- | --- |
| 实时语音（realtime） | [豆包语音控制台](https://console.volcengine.com/speech/) | 开通「端到端实时语音大模型-全双工」→ 新版 **API Key**（填 `[realtime] api_key`）；实测 App ID + Access Token 鉴权会 403，必须用 API Key |
| ASR 语音识别 | 同上 | 开通「录音文件识别 2.0」（资源 ID `volc.seedasr.auc`，已实测可用）或「录音文件识别-极速版」（`volc.bigasr.auc_turbo`）→ 取 **API Key**（新版）或 App ID + Access Token（旧版），资源 ID 与开通的服务保持一致 |
| TTS 语音合成 | 同上 | 开通「语音合成 2.0」→ API Key（`mode = seed`，资源 `seed-tts-2.0`）；旧版用 App ID + Access Token（`mode = legacy`） |
| LLM 大模型 | [方舟控制台](https://console.volcengine.com/ark) | API Key + 推理接入点（`ep-xxx`）或模型 ID |

> TTS 音色与资源代次必须匹配：2.0 音色（`*_uranus_bigtts`）配 `seed-tts-2.0`，
> 1.0 音色（`*_mars/_moon_bigtts`）配 `seed-tts-1.0`，否则报 `55000000`。

> 方舟的 API Key 与豆包语音的凭证**不通用**，两处都要开。

## 3. 部署到板子

```bash
cd project/voice_assistant
scp voice_assistant.py config.ini.example README.md root@192.168.1.14:/root/voice_assistant/
# CA 证书（板子 /etc/ssl/certs 是空的，必须补一个才能过 HTTPS 校验）
ssh root@192.168.1.14 'cd /root/voice_assistant && curl -k -L -o cacert.pem https://curl.se/ca/cacert.pem'
# 板子上填凭证
ssh root@192.168.1.14 'cd /root/voice_assistant && cp config.ini.example config.ini && vi config.ini'
```

## 4. 运行

```bash
# 检查凭证是否填全（返回码 0 表示齐全）
ssh root@192.168.1.14 'cd /root/voice_assistant && python3 voice_assistant.py --check'

# 正式对话（默认 realtime 全双工，持续会话，可打断，Ctrl+C 退出）
ssh root@192.168.1.14 'cd /root/voice_assistant && python3 voice_assistant.py'

# 改用 ASR+LLM+TTS 串联模式
ssh root@192.168.1.14 'cd /root/voice_assistant && python3 voice_assistant.py --pipeline --once'

# 跳过麦克风和 ASR，直接测 LLM+TTS+播放（调试）
ssh root@192.168.1.14 'cd /root/voice_assistant && python3 voice_assistant.py --ask "你好，介绍一下你自己"'
```

对话过程中会把录音存到 `/tmp/va_last.wav`、回复音频存到 `/tmp/va_reply.mp3`，方便排查。
环境变量可覆盖配置：`ARK_API_KEY`、`VOLC_ASR_API_KEY`、`VOLC_TTS_APPID`、`VOLC_TTS_ACCESS_TOKEN`、
`VOLC_TTS_MODE`、`VOLC_TTS_VOICE`、`ARK_MODEL` 等。

## 5. 调试手段

```bash
# 离线验证 VAD 断句逻辑（不联网、不用麦克风，喂任意 16k 单声道 wav）
python3 voice_assistant.py --simulate /tmp/test.wav
```

`--simulate` 会打印噪声门限和每段语音的起止时间；如果说话没被识别，调小
`[audio] energy_threshold`（如 400），环境嘈杂则调大（如 800）。

## 6. 常见问题

- **HTTPS 证书错误**：`/etc/ssl/certs` 为空导致，执行第 3 步的 `curl -k -L -o cacert.pem` 即可；
  调试时也可临时设 `[network] insecure_skip_verify = true`。
- **没声音/声音极小**：PulseAudio 重启会把 WM8960 硬件 `Headphone Playback Volume` 重置为 0。
  程序每次播放前会自动 `alsactl restore` 恢复；手动修复：`alsactl restore`，
  确认保存状态在 `/var/lib/alsa/asound.state`（S51alsa 开机恢复）。
- **播放一直卡住/无声**：本板必须先打开播放流再启动 arecord（WM8960 在有采集流时打不开播放流），
  程序已按此顺序启动；若手动调试，先起 `paplay /dev/zero` 再起录音。
- **ASR 报 401 Invalid X-Api-Key**：用了方舟 Key 去调豆包语音；反之亦然，检查是否对应。
- **ASR 报 45000030 requested resource not granted**：`[asr] resource_id` 对应的识别服务没开通，
  去豆包语音控制台「服务开通」里开通，或把 resource_id 改成已开通的那个。
- **TTS 报 55000000**：音色与资源代次不匹配（2.0 音色必须配 `seed-tts-2.0`）。
- **播放卡住/日志出现「流式播放器异常，转为缓冲播放」**：板子 PulseAudio 的 sink 偶发卡在
  SUSPENDED 不唤醒（耳机插拔后更容易出现）。程序有超时保护不会死等，也可手动
  `pactl suspend-sink 0 0` 或重启 pulseaudio 恢复；播放期间脚本会自起静音流保活。
- **识别为空**：语速太慢或停顿超过 `silence_ms` 会被切成两段；适当调大到 1000。
- **延迟较大**：一轮约 2~5 秒（识别 + 大模型 + 合成），属正常范围。
