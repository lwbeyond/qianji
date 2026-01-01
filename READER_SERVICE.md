# 临时只读服务入口

`python -m qianji.reader_service` 是钱迹项目自己维护的公开启动入口，不依赖任何消费者的代码。

```powershell
python -m qianji.reader_service --port 8790 --instance-id "调用方生成的实例ID" --owner-stdin
```

服务读取已有的 `data/qianji.sqlite3`，复用钱迹的快照与 HTTP 契约，连接强制只读，不初始化、导入或迁移资料库。不存在资料库时直接失败。普通 `python -m qianji serve` 和原有客户端不受影响。

`/health` 保留 `app: qianji-local`、`ok`、`api_version`，增加 `instance_id` 和 `service_contract: local-source-service/1`。其余读取接口及数据口径保持原有契约。

`--owner-stdin` 表示启动者关闭标准输入后服务退出；不传时可以前台运行并用 Ctrl+C 停止。`--root` 可指定另一份完整的项目资料目录。端口仅绑定回环地址，在 Windows 上独占监听。

消费者只需启动进程、检查健康/实例标记、调用公开 HTTP 接口。资料库结构及快照函数仅由本项目使用；消费者不导入 `qianji.store` 或 `qianji.api`。

验证：`python -m unittest discover -s tests -p test_reader_service.py -v` 检查读取结果与来源快照一致、无法写入以及缺失库不创建文件。
