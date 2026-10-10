# 钢琴练习监测

树莓派上的 USB 声卡输入会由原生 ALSA 程序直接转发到输出。Python 标准库服务只接收每 100 毫秒一条的电平数值，用于识别练习和提供本地设置页面。它不会保存录音。

## 页面

- 本地控制：`http://树莓派地址:8765/`
- 练习统计：`https://heyya-x.github.io/Piano-Monitor-Web/`

统计按北京时间计算，包含今天、本周、本月及自定义日期。会话结束后写入 `data/sessions/`，同时更新 `docs/data.json` 并推送到 `origin/main`。没有新会话时不会运行 Git；断网时本地提交会保留，在下一次会话结束后再次尝试推送。

## 安装

在树莓派的 `/home/heyya/PianoMonitor` 运行：

```sh
sudo apt-get install -y libasound2-dev
make
sudo cp systemd/piano-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now piano-monitor.service
```

首次启用 GitHub Pages 时，在仓库 Settings → Pages → Build and deployment 中将 Source 设为 **GitHub Actions**。树莓派需要一个可向仓库推送的 SSH 密钥。用 `journalctl -u piano-monitor -f` 查看状态。

## 记录规则

输入 100 毫秒均方根电平达到阈值时开始。超过静音时间后结束，结束时间为最后一次有声音的时刻。练习中短暂的停顿计入同一会话。设置存储在不纳入 Git 的 `config.json`。设备选择 `auto` 时按声卡名称寻找 `USB Audio Device`，拔插后会重试。

软件直通设为 48 kHz、每周期 128 帧，ALSA 缓冲区约 10.7 毫秒。任何数字音频系统都不可能做到物理意义的零延迟；实际听感延迟还包含 USB 声卡与设备本身的缓冲。
