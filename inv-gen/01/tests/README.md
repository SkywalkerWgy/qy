# TEST-03-01 复现说明

本目录对应《QY-NJU中期测试大纲20260906》中的“算法级循环不变式自动生成测试”。目录可以整体复制到：

```text
/nju/inv-gen/01/tests/
```

`01` 至 `45` 分别保存一个测试样例，文件名严格采用大纲规定的 `test-03-01-XX.cbs`。`_runtime` 包含可独立构建 Docker 镜像的 CoL2Inv 源码和验证工具配置。

根目录的 `requirement.txt` 是锁定版本的 Python 依赖清单；标准名称
`requirements.txt` 也可直接使用。它们不包含 Frama-C、Why3、Alt-Ergo、
Z3、CVC5 和 Coq 等原生验证工具，这些工具由 `_runtime/Dockerfile` 统一构建。

## 执行

安装并启动 Docker，然后执行：

```bash
cd /nju/inv-gen/01/tests
export DEEPSEEK_API_KEY="你的 DeepSeek API Key"
bash run-03-01.sh
```

脚本优先读取当前终端中已经导出的 `DEEPSEEK_API_KEY`，并自动将其传递给并行
子进程和 Docker 容器，不会把 Key 写入样例、日志或汇总文件。如果已经在
Shell 启动配置中设置该变量，可以直接执行 `bash run-03-01.sh`。

首次运行会自动构建 `col2inv:nju-03-01` 镜像。默认同时运行10个样例，可通过以下方式调整：

```bash
bash run-03-01.sh --jobs 8
```

固定实验设置为：

- 模型：`deepseek-flash`
- proposal：5
- 单样例运行时间：无限制
- final repair：无轮数上限
- thinking：开启

每个样例的完整结果位于对应编号目录的 `result/` 中；容器标准输出保存在 `runner.log`，最终判定保存在 `status.txt`。

全部样例结束后会生成：

- `summary.txt`：Success、Fail、未完成数、成功率和是否达到70%指标；
- `summary.csv`：45个样例的逐项状态、proposal、运行时间和日志位置。

默认以每个样例目录中的 `status.txt` 作为断点续跑依据：内容为
`Success` 或 `Fail` 时保留全部已有结果；内容为 `Error`、其他值或文件不存在时，
脚本会清除该样例的输出日志并重新运行。若需要清除45个用例的旧结果并全部重跑：

```bash
bash run-03-01.sh --force
```

容器生成结果后，脚本会自动将 `result/` 的文件所有权恢复为启动脚本的宿主机
用户；对于旧版本遗留的 `root:root` 结果，重跑时会由容器安全清理对应样例的
输出目录，因此不需要使用 `sudo rm`。

停止脚本时，脚本会停止本轮由它启动的容器；已完成样例的结果会保留，可再次运行脚本继续。
