# ScopeAgent — 离线分析记录

![波形预览](waveform.svg)

来源是导入 CSV，采集配置尚未验证；未连接仪器，未调用远端模型。

| 通道 | Vpp (V) | 均值 (V) | RMS (V) | 周期候选频率 (Hz) | 阈值占空比 (%) |
| --- | ---: | ---: | ---: | ---: | ---: |
| SYNTH_SINE_V | 2 | 0.200001 | 0.734864 | 5000 | 50 |
| SYNTH_PWM_V | 3.3 | 0.991782 | 1.80911 | 1000 | 30.1 |

## 判断边界

没有用户定义的测试限值，因此没有给出正常/异常或合格/不合格结论。
频率来自稳定阈值交叉间隔；FFT 峰不自动等于基频。正弦波的阈值占空比也不代表它是方波。
上升下降时间见 analysis.json；sample_limited 表示采样限制，不能当真实物理边沿时间。

## 数据与测量提醒

### SYNTH_SINE_V

- ADC range, probe calibration, analog bandwidth and acquisition gaps outside this record are unknown.

### SYNTH_PWM_V

- ADC range, probe calibration, analog bandwidth and acquisition gaps outside this record are unknown.

## 建议补齐

- Confirm units, probe attenuation, coupling, bandwidth, trigger and capture mode.
- Use retained raw samples and an appropriate timebase for suspected narrow events.
- Compare simultaneous channels in a shared acquisition before inferring event order.
