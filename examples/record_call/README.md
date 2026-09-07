# 来电录音

在仓库根目录执行（先运行 poetry install）：

```bash
poetry run python examples/record_call/app.py \
  --ari-url http://127.0.0.1:8088/ari --ari-username grandstream \
  --app-name grandstream-record --directory outputs/recordings
```

将 Asterisk dialplan 的来电路由到 Stasis(grandstream-record)，名称应与 --app-name 一致。每通来电接听后保存为独立 WAV，采样率由首帧决定。

ARI 密码可通过 --ari-password 传入；未传时隐藏输入。独立媒体服务地址使用 --media-url 指定，默认由 --ari-url 推导。

查看全部命令行参数：

```bash
poetry run python examples/record_call/app.py --help
```

配置通过命令行传入，不读取环境变量或 .env 文件。
