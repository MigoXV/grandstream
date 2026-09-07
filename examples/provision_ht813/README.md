# 生成 HT813 与 Asterisk 配置

在仓库根目录执行（先运行 poetry install）：

```bash
poetry run python examples/provision_ht813/app.py \
  --mac 000b82abcdef --address 192.168.1.50 --server 192.168.1.25
```

运行后依次隐藏输入 FXS、FXO 和 ARI 密码，也可使用 --fxs-password、--fxo-password、--ari-password 传入。默认输出到 outputs/provisioning 和 outputs/asterisk，可用 --provisioning-directory 和 --asterisk-directory 修改。只生成文件，需审阅后自行部署。

查看全部命令行参数：

```bash
poetry run python examples/provision_ht813/app.py --help
```

配置通过命令行传入，不读取环境变量或 .env 文件。
