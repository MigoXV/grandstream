# Grandstream SDK

面向 HT813 的 Python SDK，以 **Asterisk ARI 通话控制和双向语音流** 为核心，同时提供声明式设备配置、XML provisioning 和轻量 HTTP 服务。

```text
模拟电话 / PSTN ↔ HT813 FXS / FXO ↔ SIP/RTP ↔ Asterisk
                                              ↕ ARI REST + 事件 WebSocket
                                              ↕ 独立媒体 WebSocket
                                      Python Call / AudioStream
```

Python 应用处理带采样率的 PCM16LE 音频；Asterisk 负责 SIP、电话编解码和媒体桥接。ARI、音频模块可独立使用，HT813 通过 endpoint 注册表为通话附加设备和端口信息。

## 安装

Python 支持 `>=3.10,<3.13`，开发环境使用 Poetry 和 Python 3.10：

```bash
poetry env use python3.10
poetry install -E provisioning
poetry run grandstream --help
```

`provisioning` 是 HTTP 服务可选依赖；XML 生成、ARI 和音频功能属于核心包。依赖版本记录在 `poetry.lock`。SDK 导入时不会启动服务、连接网络或修改日志配置。

本项目参考 `sip-stub` 的媒体链路设计，独立实现通话与音频抽象，不依赖其网页、数据库、ASR、LLM 或 TTS 服务。

## 连接 Asterisk

首版在 **Asterisk 23.5.0 / Linux** 上验证。需要 `chan_websocket`、`res_http_websocket`、ARI channels/events/bridges 模块以及对应编解码器。媒体协议使用 JSON、`MARK_MEDIA` 和 `FLUSH_MEDIA`。JSON 控制和 `transport_data` 自 20.18.0、22.8.0、23.2.0 加入，其他版本尚未纳入本项目实测矩阵。[官方媒体协议](https://docs.asterisk.org/Configuration/Channel-Drivers/WebSocket/)

复制 `.env.example` 后填写 ARI 凭据。SDK 和 CLI **不隐式加载 `.env`**；可在 Shell 导出变量，VS Code 调试配置通过 `envFile` 加载。

```bash
export GRANDSTREAM_ARI_URL=http://127.0.0.1:8088/ari
export GRANDSTREAM_ARI_USERNAME=grandstream
read -rsp 'ARI password: ' GRANDSTREAM_ARI_PASSWORD
export GRANDSTREAM_ARI_PASSWORD
poetry run grandstream doctor
```

`doctor` 只读取 ARI 状态、版本和已加载模块；媒体收发能力由后文的模拟通话验证。ARI 密码与网关 SIP 密码、Web 管理密码是三个不同的配置。

若媒体 WebSocket 位于不同反向代理路径，可设置 `GRANDSTREAM_MEDIA_URL=https://pbx.example/media`；默认由 ARI URL 的 `/ari` 后缀推导 `/media`。HTTP/HTTPS 对应 WS/WSS，客户端默认不使用环境中的 HTTP 代理。

## 最小来电应用

把进入目标 dialplan context 的来电交给应用：

```ini
[grandstream-incoming]
exten => s,1,Stasis(grandstream)
 same => n,Hangup()
```

以下程序接听来电并回送原始采样率的音频：

```python
import asyncio
from grandstream import AriApplication, AriConfig, Call

app = AriApplication("grandstream", ari=AriConfig.from_env())

@app.incoming_call
async def incoming(call: Call):
    await call.answer()
    async for frame in call.audio.receive():
        await call.audio.write(frame)

asyncio.run(app.run())
```

CLI 也提供相同的回声应用：

```bash
poetry run grandstream audio echo --app grandstream
# 显式将接收音频转换到 16 kHz，输出自动适配媒体连接
poetry run grandstream audio echo --app grandstream --sample-rate 16000
```

一个 Stasis 应用名同时由一个 SDK 进程持有；多通电话在该进程中独立运行。请为不同程序使用不同应用名。收到 `ApplicationReplaced` 后应用停止，不反复争抢订阅。

## 音频接口与采样率

`AudioFrame(pcm: bytes, sample_rate: int)` 表示单声道、有符号 16 位小端 PCM。`frame.duration` 给出秒数，块长度可以不同，但必须包含完整采样。

| 接口 | 行为 |
| --- | --- |
| `call.audio.receive()` / `async for frame in call.audio` | 保留媒体源采样率，不默认升到 16 kHz |
| `call.audio.receive(sample_rate=16000)` | 用同一个 soxr 流在线转换到指定采样率 |
| `await call.audio.write(frame)` | 按 frame 声明的源采样率自动适配媒体连接 |
| `await call.audio.drain()` | 刷新重采样尾部、补齐末帧，并等待 Asterisk 播放标记 |
| `await call.audio.play(source)` | 消费异步 `AudioFrame` 流，结束时自动 drain |
| `await call.audio.clear()` | 取消当前 play、清理缓存并 flush Asterisk 播放队列；接收继续 |

媒体连接采样率通过 `AriApplication(..., media_sample_rate=...)` 配置。HT813 默认是 `8000`，对应 `slin`；也支持 Asterisk 的 slin12/16/24/32/44/48/96/192 格式。Python 接收目标采样率独立于连接格式。PSTN 的原始窄带信息不会因为升采样而增加。

转换使用 `soxr.ResampleStream`、HQ 模式，与参考项目同款。每个连续输出流维持转换状态；`drain()` 或 `clear()` 后可开始新的源采样率。连续输出中直接更换源采样率会报错。同率输入直接透传。

流只允许一个接收者和一个输出生产者；两者可以并发。应用应持续消费接收流，即使仅做播放也应启动接收任务丢弃不需要的音频。接收积压默认限制为 2 秒，超限抛出 `AudioOverflow` 并结束通话；默认不合成接收静音。发送窗口默认为约 200 ms，`write()` 会等待容量。`drain()` 的确认来自 Asterisk，不能证明远端听筒实际播放。

接入任意 TTS 的方式是将其分块输出包装为音频帧：

```python
from grandstream import AudioFrame

async def tts_frames(chunks):
    async for chunk in chunks:
        yield AudioFrame(chunk.pcm, sample_rate=chunk.sample_rate)

# 在正在消费接收流的通话中：
# await call.audio.play(tts_frames(tts_chunks))
```

`WavSource(path)` 按 WAV 元数据读入单声道 PCM16；`WavSink(path)` 使用首帧采样率写出 WAV，磁盘 I/O 放在工作线程。完整示例见 `examples/record_call/app.py` 和 `examples/outbound_wav/app.py`。

## 应用集成（0.2.0）

`AriApplication.start(wait_connected=False)` 启动后台连接任务后立即返回，适用于需要在 Asterisk 离线时仍提供管理页面的应用。默认 `start()` 仍等待首次连接并遵循超时；后台模式首次失败后也会退避重试。`connected` 和 `error` 提供当前状态，重连成功清除错误，`ApplicationReplaced` 仍会停止订阅。

```python
@app.connection_changed
def connection_changed(connected, error):
    # 更新健康状态或通知浏览器；error 不包含连接凭据。
    pass

@app.caller_changed
def caller_changed(call):
    # SDK 已更新同一个 Call，业务可用 call.id 定位会话。
    save_caller(call.id, call.caller)

await app.start(wait_connected=False)
# 应用退出时 await app.close()
```

两个状态回调均为同步、非阻塞通知；只更新状态或入队。通知异常被记录，不中断其他通知或通话处理。

### 接收静默与播放归档

```python
async for frame in call.audio.receive(sample_rate=16000, silence_timeout=0.1):
    enqueue_asr(frame.pcm)

def played(frame, acknowledged_at):
    enqueue_recording(frame, acknowledged_at)

await call.audio.play(tts_frames(), on_played=played)
```

- `silence_timeout` 默认为 `None`，保持不补静音；显式设置正数后，在没有音频的间隔补对应时长的静音，统一通过同一个重采样流转换。流控与播放确认消息不会重置音频时钟；关闭后停止补音。
- `on_played(frame, acknowledged_at)` 是可选的同步回调。`frame` 为该次 `play()` 输入的**源采样率 PCM**，按已确认媒体时长分段；`acknowledged_at` 使用 `time.monotonic()`，可与会话起始时间对齐。
- SDK 保存有限窗口内的源音频映射，排除媒体末帧补零；重采样舍入留下的源音频尾部在最终确认后交付。未产生任何媒体采样的极短输入不会产生确认。
- `clear()` 丢弃未确认片段，旧标记不会触发后续播放的回调；`drain()` 完成前已交付确认。回调只应入队，不执行磁盘 I/O、阻塞操作或媒体控制；回调失败以 `MediaError` 结束媒体流。
- 带确认回调的 `play()` 必须从已 drain/clear 的输出流开始。确认表示 Asterisk 处理了播放标记，不证明远端听筒播放。

会话数据库、VAD、ASR/LLM/TTS、双轨时间对齐与分段归档由应用负责；SDK 不依赖这些业务模块。

## HT813 与通话设备信息

```python
from grandstream import (
    HT813, FxoPort, FxoInbound, FxoOutbound, SipAccount, NetworkConfig,
)

device = HT813(
    mac="000b82123456",
    address="192.168.1.36",
    network=NetworkConfig(dhcp=True),
    fxo=FxoPort(
        endpoint="fxo1",  # 可绑定已有 Asterisk endpoint
        local_sip_port=5062,
        random_sip_port=False,
        sip=SipAccount(
            server="192.168.1.25",
            username="fxo1",
            password="请替换为 SIP 密码",
            registration=True,
            transport="udp",
            codecs=("alaw", "ulaw"),
            dtmf="rfc4733",
        ),
        inbound=FxoInbound(
            destination="s", server="192.168.1.25", port=5060,
            rings=2, ring_through_fxs=False,
        ),
        outbound=FxoOutbound(stage_method=1),
    ),
)
```

将设备通过 `devices=[device]` 传给应用后，可使用 `@app.incoming_call(from_port="fxo")` 过滤来电，并读取 `call.device.model`、`call.port.type`、`call.port.index`。HT813 的 capabilities 声明 1 个 FXS、1 个 FXO、2 个 SIP profile 和 XML provisioning。

默认 endpoint 名称为 `ht813-<mac>-fxs1` / `ht813-<mac>-fxo1`。显式 `endpoint` 用于接入现有配置。SDK 优先读取生成的 Stasis endpoint 参数，其次解析 PJSIP channel 名；不会把主叫号码当成设备账号。未登记 endpoint 的通话仍可使用 ARI 和音频，`device`、`port` 为 `None`。

## 主动外呼、DTMF 和生命周期

```python
from grandstream import AriApplication, AriConfig

async def dial_and_send(device):
    app = AriApplication("grandstream-outbound", ari=AriConfig.from_env(), devices=[device])
    async with app:
        async with await app.dial(device.fxo, number="12345678", timeout=30) as call:
            await call.send_dtmf("1#")
            # 可并发消费 call.audio.receive() 和调用 call.audio.play(...)
```

`dial(device.fxo, number=...)` 要求 `stage_method=1`，已应用到设备；`dial(device.fxs)` 呼叫模拟电话，无需号码。`dial()` 返回时 SIP 已进入 Stasis 且媒体准备完成。FXO 网关可能提前建立 SIP，PSTN 远端应答取决于线路与设备检测，不能仅据此判断用户已接听。

```python
@app.dtmf("1")
async def stop_playback(call):
    await call.audio.clear()
```

`Call` 提供 `answer()`、`hangup()`、`send_dtmf()`、`wait_closed()` 和 `caller`、`state`、`error`。号码晚到时会更新同一个 Call。首个匹配的来电处理器负责该通话，处理器返回或抛出异常后释放通话；外呼由调用者或异步上下文管理释放。DTMF 处理器独立调度，超过并发容量时结束该通话。

接收和播放任务应响应 asyncio 取消。正常关闭会唤醒等待者、取消受管理任务、关闭媒体并删除 SDK 拥有的通道和 bridge。ARI 事件断线后终止现有会话，以 1 至 30 秒退避重新连接来接收新通话，不恢复旧通话。若 ARI 本身不可达，远端资源删除只能记录失败；恢复后应检查 Asterisk 残留通道。

公开异常位于 `grandstream.errors`，包括 `AriError`（含 HTTP status）、`ConnectionLost`、`DialFailed`、`CallClosed`、`MediaError`、`AudioOverflow` 和 `PlaybackInterrupted`。请求错误不返回凭据或服务器响应正文。

## XML provisioning 与 Asterisk 配置生成

```python
from grandstream import ProvisioningStore, AsteriskConfig, render_asterisk

store = ProvisioningStore("outputs/provisioning")
xml_path = store.publish(device)
files = render_asterisk(
    [device], AsteriskConfig(ari_password="请替换为 ARI 密码"),
)
# files 是 {文件名: 内容}，由调用方审阅后保存、合并。
```

`publish()` 原子替换 `cfg<mac>.xml`，包括完整的 `gs_provision/mac/config` 结构和 XML 转义。发布成功表示配置可下载，不表示网关已经应用配置。`HT813.render_xml()` 可直接获得 XML bytes。[Grandstream 官方 XML 格式](https://documentation.grandstream.com/knowledge-base/sip-device-provisioning-guide/)

配置模型中 `None` 表示不修改相应设备参数。配置 SIP account 时始终输出服务器、用户、密码；省略 `auth_id` 时使用 username。静态 IPv4 地址按官方的四个 P-value 序列输出，DHCP 下 DNS 使用 Preferred DNS 参数。

P-value 映射依据 [Grandstream 官方配置模板下载页](https://www.grandstream.com/support/tools) 的 `ht813_config_1.0.19.6.txt`。该文件头仍写着 `1.0.17.3`；代码保留这个版本差异，仅支持已明确映射的字段，不宣称通用于所有型号和固件。未提供 Web UI 登录自动化。

```bash
poetry run grandstream provision serve \
  --directory outputs/provisioning --host 0.0.0.0 --port 8000
```

首次在设备上把 Config Server Path 指向 Asterisk/SDK 主机的局域网地址，例如 `192.168.1.25:8000`，选择 HTTP，再手动触发重同步或重启。注意 DHCP provisioning override、配置文件前后缀以及设备使用的 MAC。首版不发送 SIP NOTIFY，也不提供 TFTP/binary 配置。

服务只支持 GET/HEAD、允许的 XML 文件名，不列目录，不允许符号链接跳出目录或 HTTP 写入。未知 MAC 返回 404，只有显式调用 `publish_defaults(..., model="HT813")` 或 `publish_defaults(...)` 后才提供 `cfght813.xml` / `cfg.xml`。这些配置含 SIP 凭据；默认监听回环地址，LAN 部署使用受控网络，HTTPS 可由反向代理提供。

`render_asterisk()` 面向同一 LAN 的注册型 UDP HT813，输出 PJSIP、dialplan、ARI、HTTP、chan_websocket 配置片段。FXS/FXO 使用不同 SIP 来源端口 identify；REGISTER AOR 使用 SIP 用户名，保留来电真实号码。设备 IP 应固定或使用 DHCP 地址保留，并关闭随机 SIP 端口。[Asterisk 来源端口识别](https://docs.asterisk.org/Asterisk_23_Documentation/API_Documentation/Module_Configuration/res_pjsip_endpoint_identifier_ip/)

生成器不读取或修改 `/etc/asterisk`。合并时将 endpoint/context 片段 include 到现有配置，HTTP/ARI 的 `[general]` 参数合并到已有节；需要 reload/restart 的项目由部署方处理。密码包含 Asterisk 配置语法的换行、分号、方括号、反斜杠时生成器会明确拒绝，XML 本身允许的转义字符不受此限制。

[配置生成示例](examples/provision_ht813/README.md) 演示完整生成，通过命令行传入参数：

```bash
poetry run python examples/provision_ht813/app.py \
  --mac 000b82abcdef --address 192.168.1.50 --server 192.168.1.25
```

密码未通过参数传入时，会提示隐藏输入。每个 demo 都有独立目录、入口和说明，不读取环境变量；录音、外呼和回声示例通过 --ari-url、--ari-username、--ari-password 指定 ARI 连接。

输出目录为 `outputs/provisioning` 和 `outputs/asterisk`，均忽略于 Git。示例选择两次响铃给来电显示采集留出时间；实际响铃次数、来显制式、断线检测和回声参数应按线路调整。

## 验证与开发

```bash
poetry run pytest -q
poetry run ruff check src examples tests
poetry run ruff format --check src examples tests
poetry check
poetry build

# 真实 Asterisk 的模拟回声测试：独立应用名，不拨打 PSTN，不修改 dialplan
poetry run python examples/smoke_asterisk/app.py --ari-url http://127.0.0.1:8088/ari
poetry run python examples/smoke_asterisk/app.py --media-sample-rate 16000 --receive-sample-rate 24000
```

自动测试涵盖配置隔离与转义、SIP 双端口识别、PCM/WAV、重采样尾部、流控、播放打断、迟到事件、来电/外呼竞态、忙线/超时、媒体初始化失败和断线清理。模拟回声脚本会创建并清理自己的 ARI 资源，输出收到的音频字节数。

2026-09-07 已在本机 Asterisk 23.5.0 验证：24 kHz 音源适配 8 kHz 媒体并完成双向回声；16 kHz SDK 媒体连接、24 kHz 接收目标的链路也通过。实际 HT813 配置应用、真实 PSTN 外呼与收听效果尚未执行验证。

真实线路验收顺序：发布并应用 XML → 检查两个 PJSIP contacts → FXS/FXO 来电进入独立应用 → 检查号码、端口绑定和双向声音 → 测试 DTMF/打断 → 外呼指定测试号码 → 任一侧挂机后检查通道与 bridge 释放。请使用新应用名接入，避免占用正在运行的 `sip-stub` 应用。

## 模块边界

```text
src/grandstream/
├── ari/           # REST、事件、Application、Call 生命周期
├── audio/         # AudioFrame、双向媒体流、soxr、WAV
├── models/        # HT813、能力、网络与端口配置
├── sip/           # PJSIP 与 Asterisk 配置生成
├── provisioning/  # HT813 P-value 映射、XML、原子发布、HTTP
└── commands/      # Typer CLI
```

后续型号应扩展设备能力与参数映射，ARI 和音频核心保持独立。旧版 RTP 后端、自动 resync、麦克风/扬声器及 ASR/TTS 服务适配不在首版范围。


## 0.3.0 外呼失败信息

`DialFailed.reason` 为 `busy`、`no_answer`、`timeout` 或 `failed`；`cause` 保留 Asterisk 提供的挂机原因整数，未提供时为 `None`。业务无需解析异常文本。PSTN 线路可能先建立 SIP 再拨号，因此这些结果仅反映 SDK 收到的信令，不能推断真人已接听。
