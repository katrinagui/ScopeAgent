# 来源、复用边界与文件整理

核对日期：2026-10-19。历史资料、源码检查、模拟验证和实机验证分别对待。

## 用户原始资料

开发时参考了用户提供的本地资料包 `instrument-control-environment-record-main.zip`。原始资料和派生分析缓存不随本仓库发布，也不参与运行。资料包 SHA-256：

`a4b7e4a9f15348806c689b4423366776b8225d623dd2b6feee41c73dc266aec5`

ZIP 有 45 个文件。清理前核对解压目录：这 45 个文件均与 ZIP 内对应文件的 SHA-256 相同，额外一项是 graphify-out/cache/stat-index.json，没有额外用户源码。重复解压目录可以由原件完整恢复。

下表路径和行号指 ZIP 内原始文件，不是当前工程文件链接。

| 内容 | 原件依据 | 当前用途 |
| --- | --- | --- |
| MSO5354 Ethernet → VISA/PyVISA-py/pymso5000 | 02_给AI的详细安装与配置手册.md:24、100–116 | 复用已有仪器库 |
| 不同仪器后端分工 | 同手册:100–116 | 本项目不擅自扩展电源/SMU |
| 分级验收、读写区分 | 同手册:201–214 | 模型工具与实机执行入口分开 |
| 默认禁用 Autoscale/Reset/AWG | 同手册:328 | 不自动调整仪器或产生输出 |
| SYST:ERR? 消耗错误队列 | 同手册:214 | 不把所有查询称为无副作用 |
| AI 计划、确定性程序执行 | reports/AI多仪器自动化测试实战复盘_公开版.md:14、52–61 | 分离测量和模型叙述 |
| 逐点 CSV/JSONL、参数记录 | 同报告:83–94 | 保存 CSV、metadata 和工具轨迹 |
| 单设备单会话、身份识别 | 03_本地过程时间线与Bug台账.md:49–79 | 显式资源、身份检查、关闭会话 |
| 校准、探头和同步限制 | reports/AI测试生态理论上限与分布式实验室架构.md:127–141 | 保留未知条件与补测建议 |

资料主要是教程、历史报告、对话整理和宣传素材，没有完整原控制工程或真实波形 CSV/JSONL/二进制。04_来源清单与资料缺口.md:30–38 提到另一工程与扫描结果，但本体未随包交付。README.md:7 说明部分来源为对话二级总结；Bug 台账:88–95 说明整理时没有复验设备。

历史设备还包括 DG1022Z、DP831、Keithley 2400，不能据此认为用户当前持有或连接了它们。

实战报告:85–113 记载的 1000 点/帧、47 帧、约 0.439 帧/秒属于屏幕点读取与电脑轮询，不是 ADC 采样率，也不能证明连续无漏采。报告:135 指出点数受读取模式、存储深度、协议与传输限制。本项目没有将这些历史数字当作当前性能结果。

## 上游接口核对

2026-10-19 通过只读 GitHub 接口核对 [pyMSO5000 v0.2.0](https://github.com/mabl/pyMSO5000/tree/v0.2.0)，没有安装或运行该库。

- [pyproject.toml](https://github.com/mabl/pyMSO5000/blob/v0.2.0/pyproject.toml)：要求 Python >=3.14，与离线 3.12 环境分开。
- [Scope](https://github.com/mabl/pyMSO5000/blob/v0.2.0/src/pymso5000/scope.py)：connect、info、get_channel 与关闭上下文；先检查单位 V 再输出 _V。
- [波形 API](https://github.com/mabl/pyMSO5000/blob/v0.2.0/src/pymso5000/api/waveform.py)：acquire 的 on_preamble 回调、RetainedWaveform.values；工程单位已换算，不再乘探头倍率。
- [前导模型](https://github.com/mabl/pyMSO5000/blob/v0.2.0/src/pymso5000/api/waveform_models.py)：points、x_increment、x_origin、x_reference 等字段。

上游已经具备保留波形、统计、边沿和 MCP 能力。本项目新增价值集中于工具证据链、局部复查、参考对比、用户限值与缺证据时的补测计划。

2026-10-20 再次通过 GitHub 只读 API 核对上述 v0.2.0 的 Scope 和波形源码。`Scope.connect` 首次实际查询才建立网络链路，不能把构造对象等同于连通；`Scope.screenshot()` 查询 `SAVE:IMAGe:DATA?`，上游把仪器图片转换成 PNG 字节。本项目仅调用该截图方法，不调用 tap/触屏接口；曲线使用 acquire 的 NORMal 和 on_preamble。此次仍未安装 SDK 或连接真实设备，源码核对不证明现场兼容性。

模型传输依据：[Chat Completions 官方接口](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)，核对 tool_calls、tool_call_id 和 function arguments。兼容端点可能存在差异，当前仅通过本机 HTTP 假服务验证，不代表任何外部服务已测试。

## 文件清理记录

- 删除 test 下 IDEAS.md、PERSONAL_IDEAS.md、NICHE_OPPORTUNITIES.md、EMBEDDED_IDEAS.md、RESEARCH.md、sources.json：已弃用的多轮选题，由当前开发目标替代。
- 重复解压目录内容已核对，可从原 ZIP 恢复；批量递归删除和逐文件清理两次被自动审批以“策略阻止”拒绝，未提供具体原因，因此目录暂留。
- scope-agent/source-review 的必要核查结论已合并本文；图、提示词、生成程序与缓存属于中间产物，与重复资料目录同样暂留，未用于运行依赖。
- 删除 SCOPE_AGENT_PLAN.md：由 DEVELOPMENT.md 的开发契约替代。
- 在 demo/agent 新增当前 Agent 演示；保留旧版 demo/analysis，避免因删除受阻而覆盖其历史输出。旧 PWM 样本使用浮点相位生成，观测阈值占空比约 30.1%；新 demo 生成器已改为整数周期索引。
- 加入 __pycache__ 等忽略规则；缓存暂留。未修改 Researcher、TaskPilot。

## 验证边界

2026-10-20 实际设备选择：通过 agent-reach 的 GitHub / gh 后端核对 [PyVISA 通信文档源码](https://github.com/pyvisa/pyvisa/blob/main/docs/source/introduction/communication.rst) 的 ResourceManager、list_resources、open_resource、query("*IDN?") 与终止符设置，身份识别只使用该查询。再次核对 [pymso5000 v0.2.0 Scope.info](https://github.com/mabl/pyMSO5000/blob/v0.2.0/src/pymso5000/scope.py)：完整身份应为厂商 / 型号 / 序列号 / 固件四字段。基础通道数依据 [RIGOL MSO5000 数据手册](https://www.rigol.com/dam/global/downloads/brochures/en/data-sheet/oscilloscopes/MSO5000_DataSheet_EN.pdf)；授权升级通道暂不探测。代码只声明已有型号适配范围，不把未知厂商或新系列假定为兼容。当前电脑未安装 PyVISA，也没有实机连接；识别成功与失败路径使用假 VISA 设备验证。

2026-10-20 补充界面连接指南：依据 [RIGOL MSO5000 官方用户手册，第 20 章](https://www.rigol.com/dam/global/downloads/brochures/en/user-manual/oscilloscopes/MSO5000_UserGuide_EN.pdf) 核对后面板 LAN、Utility → IO → LAN、DHCP / Static IP 与 VISA 地址；静态配置需关闭 DHCP / Auto IP。电脑与设备的示例地址仅用于讲解网段，不是扫描所得。另通过 agent-reach 的 GitHub / gh 后端重读 v0.2.0 pyproject.toml 和 Scope.connect，确认 Python >=3.14、PyVISA / PyVISA-py 依赖及首次查询才建立连接。未因此安装依赖、修改电脑网络或连接设备。

命令与结果见 DEVELOPMENT.md 的实现验收记录。硬件误差、固件兼容性、深存储性能、远端模型分析质量和真实用户价值仍需实测。通用波形分析不等于对任意电路都能确定根因。
