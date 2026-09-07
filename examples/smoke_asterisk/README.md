# Asterisk 双向回声验证

在仓库根目录执行（先运行 poetry install）：

```bash
poetry run python examples/smoke_asterisk/app.py \
  --ari-url http://127.0.0.1:8088/ari --ari-username grandstream \
  --media-sample-rate 16000 --receive-sample-rate 24000
```

需要可用的 ARI 和 chan_websocket 媒体服务。创建独立 ARI 应用与媒体通道，验证后清理资源，不拨打 PSTN；输出 JSON 结果。默认媒体采样率为 8000，默认不指定接收重采样率。

ARI 密码可通过 --ari-password 传入；未传时隐藏输入。独立媒体服务地址使用 --media-url 指定，默认由 --ari-url 推导。

查看全部命令行参数：

```bash
poetry run python examples/smoke_asterisk/app.py --help
```

配置通过命令行传入，不读取环境变量或 .env 文件。
