# NInfer DFlash2＋按层拆分验证（2026-10-29）

当前源码开放 KVMem 模式的 DFlash2＋layer pipeline。已发布的 0.19-rc1
运行包仍限单卡，需要配套新 worker 与启动器才能使用本次能力。本次源码集成
不重打包或修改 Release；下述验证在提交前完成。

## 实现范围

- 上游固定为 `iamwavecut/ninfer-all@63428ac249da8a6c5d891b508d7e483d8c7def6c`。
  父仓库基于 `efa9633`，后端修改前的完整补丁树为
  `31eae2050c4d316207dde0a3762dc55ff05bab3d`。
- 放开 serving 参数与 Engine 参数中 DFlash2 的单卡判断，保留对 tensor parallel
  的明确拒绝。该固定基线的 tensor-parallel executor 没有 DFlash 特征采集实现。
- 主模型按完整层分布在各 GPU；DFlash2 草稿、局部 KV 和特征缓冲在第一张 GPU。
  使用上游已有的跨 stage 特征传输、各卡 KV/state 分片和 rank-0 草稿状态。
  KVMem 检查点通过 `RankStreams` 保存与恢复完整 StateImage；B/R/H 的含义不变。
- `/props`、共享启动配置和控制台允许 NInfer DFlash2 使用 1–8 张 GPU。
  llama.cpp 的 DFlash2 仍限单 GPU。支持配置上限与实测范围分开：本轮实测双卡 C1/C2，
  参数测试覆盖到 C8，未实测三至八卡、C3–C8、视觉、SSD 或其他 KV 类型。
- 没有修改 CUDA kernel、数值运算、状态布局或公共 ABI。

## 硬件、模型与构建

Stage 顺序固定如下，每轮测试前检查显存占用、利用率和计算进程：

1. RTX 5060 Ti 16GB：`GPU-B`。
2. RTX 5050 Laptop 8GB：`GPU-A`。

公开记录使用设备别名；完整 UUID 保留在本地原始证据中，测量值不变。

模型为 `Qwen3.8-27B-IQ3_S-vision-mtp-dflash2-q3.ninfer`，本轮仅测试文本，
INT8 KV、DFlash2 固定 5 草稿、关闭 ngram、40/24 层切分、device profile off。
主模型与草稿权重合计约 12.5 GiB，双卡分别约 8.40 / 4.11 GiB。
逻辑 context 8192、B=768、R=1024、prefill=128、H=4096 MiB；开启原生传输校验。

复用 CUDA 13.2 / VS2022 / SM120a 的已有构建和版本化依赖包
`ninfer-x64-windows-c7684b1d96347de8`。构建前 `build-deps/check.py` 通过。
Ninja dry run 要求因另一项工作修改了 `src/runtime/CMakeLists.txt` 而重新配置；
本次没有重配置或覆盖该构建。隔离源码在改动前与已保存的原基线 477 个修改文件
逐字节相同。仅编译 6 个 host C++ 文件、最多 8 jobs，并与同基线的兼容库重链接，
CUDA 编译数为 0，未编辑 Ninja 元数据。这是验证构建，不是新发布包。

本次补丁按序追加为 `ninfer-dflash2-layer-pipeline.patch`，SHA-256 为
`c189afb1e85d5f740a6a89aec371e8cea43a8e77ba9a04235f96ebe82baef2b8`；
隔离索引重放得到 `a955693ea2bda1cd0232ada1d5f42f4b42b049a0`，与测试源码一致。

## 验证结果

- Engine 与 serving 参数测试通过：保留设备顺序、层数、草稿配置和每请求逻辑容量；
  DFlash2＋tensor parallel 仍被拒绝。
- Python 启动计划 15 项、控制台 52 项、Windows PowerShell 参数传递 9 项通过。
  PowerShell 测试实际启动一个只打印参数的子进程，确认两卡 UUID 顺序与 DFlash2
  参数抵达子进程。控制台浏览器回归通过双 GPU 选项、配置预览、保存、切换和恢复；
  llama.cpp 双卡 DFlash2 仍被拒绝。
- `query-pressure-regression.py` 在真实双卡上通过 65 项检查。文档作为普通请求
  先发送，随后单独提问，保留请求、响应与原生 Query span。覆盖有界 Query/replay、
  resident-only 工具续接、会话切换、历史改写、取消后恢复、resident 路径及新问题召回。
  服务日志中的 21 次 native view 检查均为 `payload_exact=1 state_exact=1`。
- `ninfer-dflash2-pipeline-regression.py` 使用普通解码作为不依赖草稿的参照，测试两个
  完全驻留的短提示，每题输出上限 96 tokens。下表各 DFlash2 配置的两题输出文本
  均与普通解码相同；不是以旧版本输出作为唯一正确性依据。草稿计数及输出边界另行检查，
  前述换页/检查点测试验证原生 KV/state 不变量。

| 配置 | 两题匹配普通解码 | 接受 / 草稿 token | 接受率 |
|---|---|---:|---:|
| 单卡 DFlash2，CUDA Graph | 2/2 | 136 / 254 | 53.54% |
| 双卡 DFlash2，CUDA Graph | 2/2 | 135 / 259 | 52.12% |
| 双卡 DFlash2，关闭 CUDA Graph | 2/2 | 135 / 259 | 52.12% |
| 双卡 DFlash2，C2 并发 | 2/2 | 137 / 249 | 55.02% |

每行共输出 192 tokens；均有草稿接受与拒绝。双卡串行的接受率比单卡短样本低
1.42 个百分点，输出相同；未将该差异解释为已定位的缺陷或性能回退。
这是功能验证，不是同 GPU/同设置的性能比较，不能据此声称吞吐提升、无性能代价，
也不代表 256K 质量、其他模型、量化格式或显卡组合已通过。

测试过程中保留的失败：沙箱禁止本地 HTTP 连接（WinError 10013）和临时文件原子替换
（WinError 5）；在允许本地测试的环境重跑后通过。浏览器首次运行遇到旧测试断言
“NInfer 双卡 DFlash2 必须拒绝”，已按本次产品变化更新并增加 llama.cpp 的反例。
未发现需要改变继承数值或历史管理行为的缺陷。

## 使用与复现

新源码的控制台选择 NInfer、两张 GPU 和“极速档”；按显存设置预算，可手动填写
40/24 等层数划分。第一张卡额外承载草稿和输出 head。不要将本轮小窗口测试预算
视为所有机器的推荐值。

命令行配置示例：

```powershell
./scripts/windows/start-backend.ps1 -Backend ninfer `
  -Worker 'D:/build/ninfer-serve.exe' -Model 'D:/models/model-dflash2.ninfer' `
  -Gpu 'GPU-first-uuid,GPU-second-uuid' -StageLayers 40,24 `
  -SpeculativeMode dflash2 -DraftTokens 5 -DraftPolicy fixed `
  -Budget 768 -Reserve 1024 -Prefill 128 -HostMiB 4096 `
  -SinkTokens 64 -RecentTokens 64 -Context 8192
```

真实 GPU 回归需要预先配置与构建相同的运行库路径：

```text
python tests/ninfer-dflash2-pipeline-regression.py --exe <worker> --model <artifact> --gpus <first-uuid>,<second-uuid> --stage-layers 40,24 --output <new-result-directory>
```

本次本地原始证据在 `dflash2-multigpu-20261029/`：`core-local-results/`、
`modes-results/`、`browser-tests-final.log`、`windows-launcher-tests.log`、
`build-record.json`、`ninja-plan.json` 与 `patch-verification.json`。
