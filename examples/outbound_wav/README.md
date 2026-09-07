# 外呼并播放 WAV

在仓库根目录执行（先运行 poetry install）：

```bash
poetry run python examples/outbound_wav/app.py \
  --ari-url http://127.0.0.1:8088/ari --ari-username grandstream \
  --mac 000b82abcdef --number 10000 --wav /path/to/audio.wav
```

运行会实际拨号，请将号码和 WAV 路径替换为自己的测试值。设备与 Asterisk 必须已完成注册及 FXO 一阶段拨号配置。默认 endpoint 为 ht813-000b82abcdef-fxo1，也可通过 --endpoint 指定。此示例直接构建设备端口，不依赖其他 demo 或 SIP 密码。

ARI 密码可通过 --ari-password 传入；未传时隐藏输入。独立媒体服务地址使用 --media-url 指定，默认由 --ari-url 推导。

查看全部命令行参数：

```bash
poetry run python examples/outbound_wav/app.py --help
```

配置通过命令行传入，不读取环境变量或 .env 文件。
