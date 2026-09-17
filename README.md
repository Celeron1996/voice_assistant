# voice_assistant — 100ask i.MX6ULL 板端 AI 语音对话

在嵌入式 Linux 开发板（100ask i.MX6ULL）上实现「说话 → 云端 AI → 语音回答」的实时语音对话。
纯 Python3 标准库实现（无 pip 依赖、无需交叉编译），scp 到板子即可运行。

支持两种模式：

| 模式 | 说明 | 实测效果 |
| --- | --- | --- |
| **realtime（默认）** | 豆包「端到端实时语音大模型-全双工」：语音进、语音出，服务端负责 VAD/ASR/LLM/TTS | 说完 0.5~1s 开始回答，可随时打断 |
| **pipeline** | ASR（流式/整段）+ 方舟 LLM（SSE 流式）+ 豆包 TTS（流式合成）串联 | 首声约 2.6s |

> 本 README 按「陌生人照着做能跑通」编写：从注册云服务、拿凭证、部署到板子、自检、运行、排错
> 全部覆盖。按第 4 章主线操作即可，遇到问题查第 7 章。

---

## 目录

1. [项目简介与架构](#1-项目简介与架构)
2. [硬件与软件环境](#2-硬件与软件环境)
3. [开通云端服务、获取凭证](#3-开通云端服务获取凭证)
4. [从零复刻主线（约 30 分钟）](#4-从零复刻主线约-30-分钟)
5. [配置与命令参考](#5-配置与命令参考)
6. [工作原理（二次开发必读）](#6-工作原理二次开发必读)
7. [故障排查](#7-故障排查)
8. [已知限制与后续方向](#8-已知限制与后续方向)

---

## 1. 项目简介与架构

### 1.1 realtime 模式（默认）

```
                       ┌──────────────── 板子 (Python3) ────────────────┐
麦克风 ──arecord──> PCM 16k ──WebSocket 上行──> │                            │
                                               │   豆包端到端实时语音服务     │
耳机/喇叭 <──paplay── PCM 24k <──WS 音频帧下行── │  (服务端 VAD/ASR/LLM/TTS)  │
                       └───────────────────────────────────────────────────┘
```

- 一路 WebSocket 长连接（`wss://openspeech.bytedance.com/api/v3/realtime/dialogue`）
- 客户端只做两件事：把麦克风 PCM 持续上行；把服务端回来的 PCM 音频帧流式播放
- 服务端自动判断「用户说完」，直接返回语音回答；用户插话时服务端停止当前回答（客户端同步清空播放缓冲）
- 音色与模型：`saturn_*` 音色配 `model = 2.2.0.0`（SC2.0）

### 1.2 pipeline 模式

```
麦克风 ──arecord──> 能量 VAD 自动断句 ──wav──> 录音文件识别 2.0（flash）
                                                      │ 文本
       paplay（PCM 流式播放）<──音频分片── 豆包 TTS <──分句── 方舟 LLM（SSE 流式）
```

- ASR 有两种：`stream`（边说边上行，`sauc/bigmodel_async`，需开通「流式语音识别」）、
  `flash`（整段录完再上传，只需开通「录音文件识别 2.0」）
- LLM 流式输出（SSE），按标点分句后逐句送 TTS
- TTS 返回 NDJSON 分片（base64 PCM/mp3），边收边写进播放器

### 1.3 音频链路约定

| 环节 | 格式 |
| --- | --- |
| 麦克风采集 | 16 kHz / 16 bit / 单声道（`arecord -t raw`） |
| realtime 上行 | 每 100 ms（3200 字节）一个二进制帧 |
| realtime 下行 | 24 kHz / 16 bit / 单声道 PCM，`paplay --raw` 播放 |
| pipeline TTS | 有 PulseAudio 时请求 `pcm`（24k）直接流式播放；否则请求 `mp3` 走 mpg123 |

---

## 2. 硬件与软件环境

### 2.1 硬件

- **开发板**：100ask i.MX6ULL（单核 Cortex-A7 @792MHz，Buildroot Linux，WiFi，LCD）
- **音频编解码**：WM8960（板载），mic 输入 + 耳机/喇叭输出
- **麦克风**：带麦耳机最稳（外放喇叭会与麦克风形成回声，打断功能可能误触发；见 5.2 `barge_in`）
- **网络**：板子能访问公网（实测 `ping api.deepseek.com` 约 48 ms）

板端自检命令：

```bash
arecord -l                      # 应看到 wm8960audio capture 设备
arecord -d 3 -f cd /tmp/t.wav   # 录音自测，能生成文件
aplay -l                        # 播放设备
python3 -V                      # 需要 3.8+
```

### 2.2 板端软件依赖

Buildroot 固件一般已自带；逐项确认：

| 命令 | 用途 | 缺失后果 |
| --- | --- | --- |
| `python3` (3.8+) | 运行本程序 | 无法运行 |
| `arecord` | 麦克风采集 | 无法录音 |
| `paplay` / `pactl` | PulseAudio 播放与音量控制 | 播放降级/失败 |
| `alsactl` | 恢复 WM8960 硬件音量 | 声音极小 |
| `curl` | 下载 CA 证书 | 可手动拷贝替代 |
| `mpg123`（可选） | 无 Pulse 时播 mp3 | pipeline 回退受限 |
| `ffmpeg`（可选） | 有 Pulse 时解码 mp3 | 仅影响 mp3 回退 |

系统需运行 PulseAudio（system 模式）。验证：

```bash
ps w | grep pulseaudio
pactl info | head -2             # 能连上即可
```

### 2.3 主机端

任意 Linux/macOS，只需 `ssh` / `scp`。若板子配了免密登录会更方便（可选）。

---

## 3. 开通云端服务、获取凭证

所有语音服务在 [豆包语音控制台](https://console.volcengine.com/speech/)（需先完成火山引擎账号
注册与实名认证），LLM 在 [方舟控制台](https://console.volcengine.com/ark)。

### 3.1 按模式选择需要开通的服务

| 模式 | 要开通的服务（控制台里的名字） | 资源 ID | 用到的凭证 |
| --- | --- | --- | --- |
| realtime | **端到端实时语音大模型-全双工** | `volc.speech.dialog` | 语音 **API Key** |
| pipeline（flash） | **录音文件识别 2.0** | `volc.seedasr.auc` | 语音 **API Key** |
| pipeline（stream，可选） | **流式语音识别** | `volc.seedasr.sauc.duration` 或 `volc.seedasr.sauc.concurrent` | 语音 **API Key** |
| pipeline（TTS） | **语音合成 2.0** | `seed-tts-2.0` | 语音 **API Key** |
| pipeline（LLM） | 方舟推理接入点（在线推理 → 创建接入点） | `ep-xxxxxxxx` | 方舟 **API Key** |

### 3.2 豆包语音控制台操作步骤

1. 进入 [console.volcengine.com/speech](https://console.volcengine.com/speech/)。
2. **创建 API Key**：左侧「API Key 管理」→ 创建 → 得到 UUID 形式的 Key
   （例：`fdb93d12-0a1e-4e89-92f4-a6ef3a3ea4b3`）。本项目的语音服务全部用这一个 Key。
3. **开通服务**：左侧「服务开通管理」，逐个开通第 3.1 节列出的服务。
   注意：
   - 「流式语音识别」与「端到端实时语音」是**两个独立产品**；
   - 开通后可能有**几分钟延迟**才在 API 侧生效（现象：报 `45000030 requested resource not granted`，
     等 5~10 分钟重试即可）；
   - 服务必须开在 API Key 所属的账号/项目下。
4. **记下音色**：实时语音默认音色 `saturn_zh_female_wenrouwenya_tob`（SC2.0，配 `model = 2.2.0.0`）；
   pipeline TTS 默认 `zh_female_shuangkuaisisi_uranus_bigtts`（TTS 2.0，配 `resource_id = seed-tts-2.0`）。

> **重要陷阱（实测）**：端到端实时语音接口只认语音 API Key（`X-Api-Key`）；
> 在控制台「应用管理」里拿到的 App ID + Access Token 会直接 403 `not granted`。
> 本项目的 `[realtime]` 填 `api_key` 即可，`app_id/access_token` 留空。

### 3.3 方舟控制台操作步骤（仅 pipeline 的 LLM 需要）

1. [console.volcengine.com/ark](https://console.volcengine.com/ark) → 「开通管理」开通豆包大模型（有免费额度）。
2. 「API Key 管理」→ 创建 → 得到方舟 API Key。
3. 「在线推理」→ 创建推理接入点（选模型，如 Doubao-Seed-1.6-flash）→ 复制接入点 ID（`ep-` 开头）。
4. 注意：**方舟 Key 与豆包语音 Key 不通用**。

### 3.4 凭证清单（填进 config.ini 前先备好）

```
语音 API Key（UUID）        -> [asr] api_key / [tts] api_key / [realtime] api_key
方舟 API Key + ep-xxx       -> [llm] ark_api_key / [llm] model   （仅 pipeline）
```

---

## 4. 从零复刻主线（约 30 分钟）

以下命令在**主机**上执行（把 `BOARD` 改成你的板子地址）。

```bash
BOARD=root@192.168.1.14          # 示例，改成你的板子
```

### Step 1 拷贝代码到板子

```bash
cd project/voice_assistant       # 仓库根目录
ssh $BOARD 'mkdir -p /root/voice_assistant'
scp voice_assistant.py config.ini.example README.md $BOARD:/root/voice_assistant/
```

### Step 2 准备 CA 证书（必须）

板子 `/etc/ssl/certs` 为空，Python 默认无法完成 HTTPS 校验。在板子上执行：

```bash
ssh $BOARD 'cd /root/voice_assistant && curl -k -L -o cacert.pem https://curl.se/ca/cacert.pem'
```

程序启动时会依次找：`[network] ca_file` → 环境变量 `SSL_CERT_FILE` →
`/etc/ssl/certs/ca-certificates.crt` → 脚本同目录 `cacert.pem`，找到即用。

### Step 3 准备配置文件

```bash
ssh $BOARD 'cd /root/voice_assistant && cp config.ini.example config.ini && chmod 600 config.ini'
ssh $BOARD 'cd /root/voice_assistant && vi config.ini'       # 填入第 3 章拿到的凭证
```

**realtime 模式**最少只需填一项：

```ini
[dialogue]
mode = realtime

[realtime]
api_key = <你的语音 API Key>
```

**pipeline 模式**最少填：

```ini
[dialogue]
mode = pipeline

[asr]
api_key = <语音 API Key>
resource_id = volc.seedasr.auc        # 与开通的识别服务一致

[tts]
api_key = <语音 API Key>
resource_id = seed-tts-2.0
voice_type = zh_female_shuangkuaisisi_uranus_bigtts

[llm]
ark_api_key = <方舟 API Key>
model = ep-xxxxxxxx                   # 你的接入点 ID
```

### Step 4 配置自检

```bash
ssh $BOARD 'cd /root/voice_assistant && python3 voice_assistant.py --check'
```

期望输出（realtime 模式）：

```
实时语音         已配置
对话模式:     realtime
实时音色:     saturn_zh_female_wenrouwenya_tob (model 2.2.0.0)
```

返回码 0 表示凭证齐全；缺失项会标「缺失」并提示补哪一节。

### Step 5 语音硬件自检（不联网）

```bash
# 放音：恢复硬件音量并播一个 1kHz 测试音（确认能听到）
ssh $BOARD 'alsactl restore; ffmpeg -y -loglevel error -f lavfi -i "sine=frequency=1000:duration=1" -ar 24000 -ac 1 -f s16le /tmp/tone.pcm'
ssh $BOARD 'PULSE_SERVER=unix:$(ls /tmp/pulse-*/native | head -1) paplay --raw --format=s16le --rate=24000 --channels=1 /tmp/tone.pcm'

# 录音：录 3 秒再回放，确认麦克风正常
ssh $BOARD 'arecord -d 3 -f cd /tmp/rec.wav && aplay /tmp/rec.wav'
```

### Step 6 运行 realtime 模式（主线终点）

```bash
# 前台运行（Ctrl+C 退出，适合调试）
ssh $BOARD 'cd /root/voice_assistant && python3 voice_assistant.py'

# 或后台运行 + 看日志
ssh $BOARD 'cd /root/voice_assistant && nohup python3 voice_assistant.py >/tmp/va.log 2>&1 &'
ssh $BOARD 'tail -f /tmp/va.log'
```

启动成功日志（约 3 秒内）：

```
[16:59:26] 语音助手已启动（对话模式: realtime）
[16:59:27] 麦克风预热中……
[16:59:29] 实时会话已建立，直接说话即可（服务端自动断句/打断）
```

然后直接对麦克风说话，期望日志：

```
[16:57:57] 识别中… 你的技能是什么？
[16:57:58] 你说: 你的技能是什么？
[16:57:58] AI  : 我能查信息、定闹钟、提醒事项，还能陪你聊天，有什么需要随时告诉我。
```

同时耳机里应能听到回答。助手说话时再开口 → 应立刻停止播报并听你说（打断）。

### Step 7（可选）运行 pipeline 模式

```bash
# 一轮对话后退出（方便观察全链路）
ssh $BOARD 'cd /root/voice_assistant && python3 voice_assistant.py --pipeline --once'

# 不接麦克风，直接测 LLM→TTS→播放
ssh $BOARD 'cd /root/voice_assistant && python3 voice_assistant.py --ask "你好，介绍一下你自己"'
```

### Step 8（可选）开机自启

仓库 `scripts/S99voiceassistant` 是 init 脚本模板：

```bash
scp scripts/S99voiceassistant $BOARD:/etc/init.d/
ssh $BOARD 'chmod +x /etc/init.d/S99voiceassistant && /etc/init.d/S99voiceassistant start'
# 日志：/tmp/voice_assistant.log；停止：/etc/init.d/S99voiceassistant stop
```

---

## 5. 配置与命令参考

### 5.1 config.ini 字段说明

#### [dialogue]

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `mode` | `realtime` | `realtime` 用端到端实时语音；`pipeline` 用 ASR+LLM+TTS 串联 |

#### [realtime]（realtime 模式）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `api_key` | 空 | 语音 API Key（**必填**；AppID+Token 会被 403） |
| `app_id` / `access_token` | 空 | 旧版凭证，本接口实测不可用，留空 |
| `url` | `wss://openspeech.bytedance.com/api/v3/realtime/dialogue` | 服务端点 |
| `resource_id` | `volc.speech.dialog` | 固定 |
| `app_key` | `PlgvMymc7f3tQnJ6` | 旧版鉴权固定值，仅旧版凭证用 |
| `model` | `2.2.0.0` | SC2.0；O 版音色请用 `1.2.6.1` |
| `speaker` | `saturn_zh_female_wenrouwenya_tob` | 音色。实测实时模型可用：`saturn_zh_female_wenrouwenya_tob`（温柔文雅）、`saturn_zh_female_keainvsheng_tob`（可爱女生）等 `saturn_*` 系列；`mars/moon/uranus` 等合成音色会报 `InvalidSpeaker`（见第 7 章） |
| `bot_name` | `小助手` | 人设：名字 |
| `system_prompt` | 板端助手人设 | 人设：角色设定 |
| `speaking_style` | 自然简洁友好 | 人设：说话风格 |
| `say_hello` | 空 | 连上后主动说的开场白，留空则不打招呼 |
| `barge_in` | `true` | `true`：播放时麦克风继续上行，可打断（戴耳机推荐）；`false`：播放时暂停上行 |
| `output_sample_rate` | `24000` | 下行音频采样率（播放器按 24k 配置） |
| `end_smooth_window_ms` | `1500` | 服务端判定「说完」的静音窗口，越大越不抢话 |

#### [asr]（pipeline 模式）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `mode` | `stream` | `stream` 流式识别（需开通流式服务）；`flash` 整段识别 |
| `api_key` | 空 | 语音 API Key（`X-Api-Key`） |
| `appid` / `access_token` | 空 | 旧版凭证，与 api_key 二选一（新账号留空） |
| `stream_url` | `wss://…/api/v3/sauc/bigmodel_async` | 流式端点 |
| `stream_resource_id` | `volc.seedasr.sauc.duration` | 流式资源，程序会自动依次尝试 duration / concurrent |
| `url` | `https://…/auc/bigmodel/recognize/flash` | flash 端点 |
| `resource_id` | `volc.seedasr.auc` | flash 资源，与开通的识别服务一致 |

#### [tts]（pipeline 模式）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `mode` | `seed` | `seed` 新版（`v3/tts/unidirectional`，API Key）；`legacy` 旧版（appid+token） |
| `api_key` | 空 | 语音 API Key（seed 模式必填） |
| `appid` / `access_token` / `cluster` | 空 / `volcano_tts` | legacy 模式凭证 |
| `voice_type` | `zh_female_shuangkuaisisi_uranus_bigtts` | 音色：2.0 音色配 `seed-tts-2.0`，1.0（mars/moon）配 `seed-tts-1.0` |
| `resource_id` | `seed-tts-2.0` | 资源代次 |
| `encoding` | `mp3` | 无 PulseAudio 时的回退编码；有 Pulse 时程序自动改用 `pcm` 流式播放 |
| `speed_ratio` | `1.0` | 语速 |

#### [llm]（pipeline 模式）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `ark_api_key` | 空 | 方舟 API Key |
| `model` | `doubao-seed-1-6-flash-250828` | 接入点 `ep-xxx` 或模型 ID |
| `base_url` | `https://ark.cn-beijing.volces.com/api/v3` | OpenAI 兼容地址 |
| `system_prompt` / `max_tokens` / `temperature` | 见 example | 常规参数 |
| `stream` | `true` | SSE 流式（配合分句 TTS） |
| `thinking` | `auto` | 建议 `disabled`（关闭深度思考，首字延迟从 ~14s 降到 <1s） |

#### [audio]（pipeline 模式的 VAD）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `energy_threshold` | `0` | `0`=自动噪声门限；环境吵可填固定值（如 800） |
| `silence_ms` | `800` | 静音多久算说完 |
| `min_speech_ms` | `300` | 少于此时长忽略 |
| `max_record_ms` | `15000` | 单句最长录音 |
| `mic_device` | `default` | arecord 设备名（`arecord -L` 查看） |

#### [network]

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `ca_file` | 空 | 自定义 CA 路径；留空按搜索顺序找（见 Step 2） |
| `insecure_skip_verify` | `false` | 仅调试：找不到 CA 时跳过校验（不安全） |

### 5.2 环境变量覆盖

所有敏感/易变配置可用环境变量覆盖（优先级高于 config.ini）：

| 环境变量 | 对应配置 |
| --- | --- |
| `VOICE_DIALOGUE_MODE` | `[dialogue] mode` |
| `VOLC_RT_API_KEY` / `VOLC_RT_APP_ID` / `VOLC_RT_ACCESS_TOKEN` | `[realtime]` 凭证 |
| `VOLC_RT_SPEAKER` / `VOLC_RT_MODEL` | `[realtime]` 音色/模型 |
| `VOLC_ASR_API_KEY` / `VOLC_ASR_APPID` / `VOLC_ASR_ACCESS_TOKEN` | `[asr]` 凭证 |
| `VOLC_ASR_MODE` / `VOLC_ASR_RESOURCE_ID` / `VOLC_ASR_STREAM_RESOURCE_ID` / `VOLC_ASR_STREAM_URL` | `[asr]` 资源与端点 |
| `VOLC_TTS_API_KEY` / `VOLC_TTS_APPID` / `VOLC_TTS_ACCESS_TOKEN` | `[tts]` 凭证 |
| `VOLC_TTS_MODE` / `VOLC_TTS_VOICE` | `[tts]` 模式与音色 |
| `ARK_API_KEY` / `ARK_MODEL` / `ARK_BASE_URL` / `ARK_STREAM` / `ARK_THINKING` | `[llm]` |
| `CA_FILE` | `[network] ca_file` |

### 5.3 命令行参数

| 参数 | 说明 |
| --- | --- |
| （无） | 按 `[dialogue] mode` 运行（默认 realtime，持续会话） |
| `--pipeline` | 强制 pipeline 模式 |
| `--once` | pipeline 只对话一轮后退出 |
| `--ask TEXT` | 跳过录音/ASR，直接 LLM→TTS→播放（调试） |
| `--check` | 检查凭证是否齐全（返回码 0/1） |
| `--simulate WAV` | 离线跑 VAD 断句自测（不联网、不用麦克风） |
| `--config PATH` | 指定配置文件（默认脚本同目录 config.ini） |

---

## 6. 工作原理（二次开发必读）

### 6.1 realtime 模式的 WebSocket 二进制协议

一帧的结构（火山自定义协议，均大端）：

```
4B 头: [0x11, msg_type|0x04(带事件), serial|gzip, 0x00]
4B 事件号
[4B session_id 长度 + session_id]     # 事件 1/2/50/51/52 除外
[4B 连接 ID 长度 + 连接 ID]            # 仅事件 50/51/52
[4B payload 长度 + payload(gzip)]
```

- 客户端消息类型：`0x10` 全量请求（JSON）、`0x20` 纯音频（RAW）
- 服务端：`0x90` 全量响应（JSON）、`0xB0` 纯音频、`0xF0` 错误（带 4B 错误码）

关键事件号：

| 方向 | 事件 | 含义 |
| --- | --- | --- |
| 客户端 | 1 / 100 / 102 | StartConnection / StartSession / FinishSession |
| 客户端 | 200 / 300 | TaskRequest（音频）/ SayHello（开场白） |
| 服务端 | 50 / 150 / 152 | ConnectionStarted / SessionStarted / SessionFinished |
| 服务端 | 450 / 451 / 459 | 用户开始说话 / ASR 结果（`results[0].text`、`is_interim`）/ 用户说完 |
| 服务端 | 550 | LLM 流式文本（`content`） |
| 服务端 | 351 / 352 / 359 | TTS 整句文本 / TTS 音频帧 / 本轮回答完成 |
| 服务端 | 153 / 154 | SessionFailed / UsageResponse |

StartSession 的 JSON（`RealtimeDialogue._session_payload`）：

```json
{
  "asr":   {"extra": {"end_smooth_window_ms": 1500, "enable_custom_vad": false}},
  "tts":   {"speaker": "saturn_zh_female_wenrouwenya_tob",
            "audio_config": {"channel": 1, "format": "pcm_s16le", "sample_rate": 24000}},
  "dialog":{"character_manifest": "名字：小助手\n<system_prompt>\n说话风格：...",
            "extra": {"strict_audit": false, "recv_timeout": 120,
                      "input_mod": "keep_alive", "model": "2.2.0.0"}}
}
```

### 6.2 pipeline 三件套

- **ASR flash**：`POST /api/v3/auc/bigmodel/recognize/flash`，头部 `X-Api-Key`、
  `X-Api-Resource-Id: volc.seedasr.auc`、`X-Api-Request-Id`、`X-Api-Sequence: -1`；
  body 为 `user/audio(base64 wav)/request`；响应头 `X-Api-Status-Code`：
  `20000000` 成功、`20000003` 静音。
- **ASR stream**：`wss://…/sauc/bigmodel_async`，同样的 4B 头 + 事件格式，
  首帧 JSON 声明音频格式，之后逐块发 PCM，末包 flag `0x02` 收尾。
- **LLM**：OpenAI 兼容 `/chat/completions`，`stream:true` 按 `data:` 行解析 SSE；
  `thinking:disabled` 关闭推理。
- **TTS**：`POST /api/v3/tts/unidirectional`，逐行 NDJSON：
  `{"code":0,"data":"<base64 音频>"}`，`code:20000000` 结束。

### 6.3 板端音频链路

- 录音：`arecord -t raw -f S16_LE -r 16000 -c 1`，主循环按 20ms 帧读取；
  arecord 启动前 ~1.2s 有异常高电平瞬态，程序固定丢弃后再做噪声标定
- pipeline VAD：能量门限（4×噪声底，自动） + 800ms 静音判停
- 播放：PulseAudio（`paplay --raw --format=s16le --rate=24000 --channels=1`）；
  无 Pulse 时 pipeline 回退 mpg123/aplay
- **播放保活**：常驻一路 `paplay /dev/zero` 静音流，防止 Pulse sink 自动挂起后无法唤醒
- **启动顺序**：必须先建立播放保活流，再启动 arecord（WM8960 在采集流活跃时打不开播放流）
- **音量**：每次播放前 `pactl` 恢复 sink 音量 100% + `alsactl restore`
  恢复 WM8960 硬件耳机音量（PulseAudio 重启会把硬件音量重置为 0）

### 6.4 线程模型

| 线程 | 职责 |
| --- | --- |
| 主线程 | 读麦克风帧 → 上行；播放下行音频；处理事件队列 |
| WS 读线程 | 收服务端帧 → 解析 → 投递到 `queue.Queue` |
| 播放进程 | paplay（子进程），主线程非阻塞写入，超时降级 |

### 6.5 代码结构

单文件 `voice_assistant.py`（~1700 行），分区如下：

| 区域 | 关键函数/类 |
| --- | --- |
| 配置 | `DEFAULT_CONFIG`、`ENV_MAP`、`cfg_get` |
| 网络/TLS | `make_ssl_context`、`http_post` |
| VAD 与录音 | `VAD`、`Recorder`、`record_turn`、`frames_to_wav` |
| Pulse/播放 | `find_pulse_server`、`_resume_sink`、`_write_all`、`play_audio` |
| ASR | `asr_transcribe`（flash）、`WSClient`、`StreamingASR`（stream） |
| 实时语音 | `rt_encode_frame`/`rt_decode_frame`、`RealtimeDialogue`、`realtime_loop` |
| LLM/TTS | `llm_reply`/`llm_stream`、`tts_synthesize`/`tts_stream`、`split_sentence` |
| 串联模式 | `stream_reply`、`reply_and_speak`、`conversation_loop` |
| CLI | `main`（`--check/--simulate/--ask/--once/--pipeline`） |

---

## 7. 故障排查

| 症状 | 原因 | 解决 |
| --- | --- | --- |
| `找不到 CA 证书` | 板子 `/etc/ssl/certs` 为空 | 执行 Step 2 下载 `cacert.pem` |
| `401 Invalid X-Api-Key` | 方舟 Key 与语音 Key 混用 | 按第 3 章区分：方舟 `ark-` 前缀，语音是 UUID |
| `45000030 requested resource not granted` | 服务没开通 / 刚开通未生效 / 项目不对 | 控制台确认已开通；等 5~10 分钟重试 |
| realtime 握手 403（AppID+Token） | 该接口只认 API Key | `[realtime] api_key` 填语音 API Key，AppID/Token 留空 |
| TTS 报 `55000000` | 音色与资源代次不匹配 | 2.0 音色（`*_uranus_bigtts`）配 `seed-tts-2.0`；1.0（mars/moon）配 `seed-tts-1.0` |
| 声音极小/无声 | PulseAudio 重启后 WM8960 硬件音量被重置为 0 | 代码已每次播放前 `alsactl restore`；手动执行同命令 |
| 启动日志 `PulseAudio sink 未能唤醒` | 先启动了 arecord，播放流打不开 | 已按「先保活流后录音」修复；手动调试同样顺序 |
| 播放卡住、写入超时 | Pulse sink 卡在 SUSPENDED | 程序自动降级缓冲播放；可 `pactl suspend-sink @DEFAULT_SINK@ 0` 或重启 pulseaudio |
| 实时模式有识别但无回复/无声音，日志报 `ClientError:InvalidSpeaker` | 音色不在实时模型支持列表（如 `mars` 系列；且开场白可能不报错，正式回答才失败） | 换 `saturn_*` 音色（如 `saturn_zh_female_wenrouwenya_tob`、`saturn_zh_female_keainvsheng_tob`），以控制台「音色管理」已开通的实时音色为准 |
| 打断不灵 / 助手自说自话 | 用外放喇叭，回声被当成说话 | 戴耳机；或 `[realtime] barge_in = false`（播放时不听） |
| 识别为空 | 说话太快/太慢、环境吵 | 调 `silence_ms`、`energy_threshold` |
| 板子 `No route to host` | WiFi 不稳 | 重试 scp/ssh；`iw dev wlan0 set power_save off` |
| `Text file busy` | 程序正在运行 | 先 kill 再覆盖二进制/脚本 |

---

## 8. 已知限制与后续方向

- 单核 A7 无 NPU，所有模型推理都在云端，断网不可用
- 外放场景没有回声消除（AEC），打断建议戴耳机；后续可在播放期间做半双工（`barge_in=false` 已支持）
- realtime 会话若长时间空闲，服务端会返回 idle timeout 错误帧（程序忽略并保持连接）
- 语音 API Key / 方舟 Key 均为明文存于板端 `config.ini`（`chmod 600`），请勿入库/外传
- 可扩展：唤醒词、按键/触摸打断、接入 photo_album 的 Qt 界面、多音色切换
