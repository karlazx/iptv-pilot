# 2026-11-06 NAS Docker 全体失联复核

复核时间：2026-11-06 21:28–21:35，Asia/Shanghai。仅进行只读检查，未修改 NAS 配置或重启服务。

## 结论与证据强度

已证实的故障放大原因是：内存和 swap 压力触发 earlyoom，而宿主机将
containerd 的 `OOMScoreAdjust` 配成了 **+999**，运行中的 shim 为 **+1000**。
earlyoom 先逐个终止 shim，再终止 containerd 和一个 runc。Docker daemon 本身
存活，无法正确操作已经丢失的容器 task，造成容器仍显示 Up 但应用失联。

初次复核时内存突增的具体进程未确定。用户随后提供批量导入直播源的线索，
进一步发现大型 MP4 被按 HLS 播放列表完整下载、文本解码的高概率触发路径，
见文末补充证据。尚缺事发进程内存快照，不能视为已经直接观测到肇事进程。

## 原始时间线

以下时间均为北京时间，来自 `journalctl -u earlyoom -u containerd -u docker`：

| 时间 | 观察 |
| --- | --- |
| 19:01:57 | 可用内存 7880 MiB，空闲 swap 10650 MiB |
| 19:02:57 | 可用内存 6073 MiB，空闲 swap 10646 MiB |
| 19:03:27 | 可用内存 434 MiB，空闲 swap 10599 MiB |
| 19:03:57 | 可用内存 174 MiB，空闲 swap 3496 MiB |
| 19:04:17 | 进入 SIGTERM 阈值；第一个被选中的是 containerd-shim，badness 1333，RSS 3 MiB |
| 19:04:17–19:04:48 | 连续终止多个 shim，后期升级 SIGKILL |
| 19:04:48 | SIGKILL containerd，PID 1082484，badness 1332，RSS 15 MiB |
| 19:04:49 | SIGKILL runc；systemd 记录 containerd 主进程以 9/KILL 退出 |
| 19:04:56 | containerd 重启完成 |
| 19:04:58 | 可用内存恢复至 12705 MiB，空闲 swap 恢复至 8749 MiB |

这证实了严重的内存压力及运行时被主动杀死。释放内存与容器进程消失接近，
但仍不能据此识别某个容器或进程为唯一内存来源。

## 运行时配置异常

宿主机 `/lib/systemd/system/containerd.service`：

```ini
OOMScoreAdjust=999
CPUWeight=10
CPUQuota=70%
MemoryMax=70%
MemorySwapMax=100M
```

复核 `/proc/<pid>/oom_score_adj` 和 `/proc/<pid>/oom_score` 得到：

| 进程 | oom_score_adj | 当时复核的 oom_score |
| --- | --- | --- |
| containerd | +999 | 1332 |
| 全部被检查的 containerd-shim | +1000 | 1333 |
| dockerd | -500 | 336 |
| 桥接 ffmpeg / udpxy | 0 | 约 666–667 |

这与事故日志中的选择顺序吻合。同版本 containerd v1.7.18 官方单元使用
`OOMScoreAdjust=-999`，符号与本机相反：
https://github.com/containerd/containerd/blob/v1.7.18/containerd.service

文件 mtime 为 2026-06-03，ctime 为 2026-08-02，早于 11 月的 udpxy 安装。
本机文件 MD5 与 dpkg 包记录不一致，说明不同于包记录内容；这不能单独证明
具体由厂商、管理员还是其他程序修改。尚无修改者审计证据。

## 对旧结论的修正

- `nas-service.service/memory.peak` 为 11142057984 字节，但没有峰值时间戳。
  当前占用含大量 file cache，不能认定事故时匿名内存达到 11 GB。
- `docker.service/memory.peak` 为 6970355712 字节，不是全部容器合计。
  实测应用容器位于同级 `docker-<id>.scope`，不是该单元的子 cgroup。
- `ssh.service/memory.peak` 为 3301629952 字节。它不是已退出 ffmpeg 的累计
  用量；其当前 `memory.stat` 还包含 file、shmem 和 kernel。峰值没有时间戳，
  且不能用 RAM 峰值替代包括 swap 的全部占用历史。
- udpxy 父进程峰值约 2 MiB、桥接主进程峰值较小，只能说明这些进程本身；
  不能排除已退出的子进程、tmpfs 和其他调用链开销。
- `earlyoom -r 1` 只改变内存报告的打印间隔，不会增加进程内存历史采样。
- earlyoom 1.7 的 `--avoid` 只给匹配进程减 300 分。对当前 1332/1333 分的
  运行时仅加 avoid，仍可能优先于普通业务进程被杀，不能视为完整修复。
- NAS 实际 uptime 约 86 天；journal 可见记录的起点不是主机启动时间。

earlyoom 参数语义：
https://github.com/rfjakob/earlyoom/blob/v1.7/MANPAGE.md

## 组播桥接和应用代码的待验证风险

已读取 `<bridge-dir>/iptv_hls_bridge.py`：

- HLS 的 `get_worker()` 使用 `MAX_CONCURRENT_STREAMS=20`，并由 reaper 回收。
- `/ts/` 分支直接 `subprocess.Popen(ffmpeg)`，不经过 `get_worker()`，没有共享
  并发限制，也不受 HLS 的 45 秒空闲回收控制。
- HLS 分片和 ffmpeg 错误日志位于 `/dev/shm/iptv_hls`；分片数量限制不是字节
  总量限制，ffmpeg.log 也没有轮转上限。复核时该目录约 79 MiB，不能代表故障时。
- `ts.log` 不记录全部 HLS 请求，不能据 TS 请求次数推算桥接整体负载。

仓库 `scripts/channels/diagnose_live_quality.py` 的播放列表和分片读取使用
`httpx.Client.get()` 全量缓冲响应，没有显式响应大小上限。若上游实际返回
持续 TS 或异常大响应，存在内存增长风险。这是代码检查发现的风险，尚未
证明本次故障实际走到了这条路径。

## 已检查的历史数据边界

- journal 的事故时段没有内核 OOM 记录；NAS 自带
  `<vendor-log>/kern.log` 也未查到对应 OOM 记录。
- NAS `<vendor>/docker_events.log` 在 18:55–19:06 仅查到健康检查 exec 事件，
  没有能定位内存来源的容器 oom 事件或进程内存记录。
- 当前可用容器统计主要来自恢复后新建的 cgroup，不能回溯旧容器峰值。

## 建议后续措施（未执行）

1. 优先修正 containerd 的正向 OOM 优先级，采用 systemd drop-in 保存覆盖；
   同时处理现存 shim 的 +1000，不能只改文件而假定已有进程立即生效。
   应设计可回滚的在线调整，避免为应用配置直接重启整个 Docker 栈。
2. earlyoom 的 avoid 可作为补充，但需包含实际的 `containerd-shim` comm，
   并以实际 `/proc` 值验证效果，不能代替修正 +999/+1000。
3. 部署有日志轮转的进程和 cgroup 采样：RSS、VmSwap、oom_score_adj、
   cgroup anon/file/shmem/kernel/swap、MemAvailable 和 PSI；低内存时提高
   采样频率，保存发生时间和进程命令身份。仅启用默认 sar 不足以保证进程级记录。
4. 给桥接 HLS/TS 设置共同并发预算和独立服务资源限制，控制 tmpfs 与日志；
   给频道检测增加响应大小和总耗时限制。修复后在受限测试环境验证。

本轮没有将 NAS 自带服务、udpxy、桥接或 iptv-pilot 中任何一个标记为已证实的
内存突增责任方。网页 `/health` 在复核时返回 ok，但运行时优先级风险仍未消除。

## 补充：批量导入线索与大型 MP4 检测（同日后续复核）

用户回忆故障前批量导入过直播源。只读查询 sources 的 created_at 和保留的
质量检测结果后得到以下证据（时间均已转换为北京时间）：

- 18:44 导入 43 条线路，其中“广东卫视”的地址实际以 `.mp4` 结尾，主机为
  <private-cdn>。
- 18:59 再导入 169 条线路，主机为 `<iptv-server>`；前一批源当前已禁用。
- 前一批源的检测记录仍保留在 18:59–19:02：CCTV-15 为 19:02:09.966，
  CCTV-16 为 19:02:33.701，另一个 HLS 源为 19:02:26.160。
  证明检测任务在内存下降期间确实处理这批导入源。
- 广东卫视 MP4 没有保存 quality/last_ok_at/last_fail_at。这与处理中断相容，
  但单凭缺少结果不能证明它已被请求。

对该 MP4 执行有大小边界的 Range 请求，服务器返回：

```text
HTTP 206
Content-Type: video/mp4
Content-Range: bytes 0-4095/1845047240
```

实际只读取 4096 字节并确认 ftyp 文件头；文件总长度为 1,845,047,240 字节
（约 1.85 GB / 1.72 GiB）。随后额外获取最多 1 MiB 样本用于有限复现，未下载完整文件。

故障时 rc.6 代码和当前代码均在 media_segments() 中依次执行：

```python
response = client.get(url, headers=HEADERS)
text = response.text
for raw in text.splitlines():
    ...
```

缺少响应大小上限和读取前格式判断。MP4 会先完整进入内存，再解码成 Unicode、
拆成行。HTTPX read timeout 是等待下一块数据的超时，不是整个下载的时间预算。
持续有数据时，20 秒配置不会把整个下载强制截断。

使用实际样本循环生成 **4 MiB 有限响应**，通过 HTTPX MockTransport 调用正在
运行镜像中的 media_segments()，tracemalloc 测得 Python 分配峰值
**32,276,921 字节（输入的约 7.7 倍）**，最终报 playlist contains no media segments。
这是小样本复现，不是完整文件的实测峰值，也不能精确线性外推；它证实了二进制
视频被当作文本处理时的显著内存放大。1.85 GB 输入具有造成十 GB 量级开销的可能。

导入接口及前端不直接启动批量质量检测；独立 stream-quality 服务在启动时和
每轮结束后的配置间隔执行检测。当前配置为 3 个线程、2 个分片、20 秒网络超时、
3600 秒轮次间隔。每轮会先保存候选 URL 列表，处理中禁用源不会撤销已排入该轮的请求。

综合判断：**批量导入的 MP4 被无大小限制的 HLS 检测读取，是目前最有证据支持的
内存突增触发假设**。时间、真实输入大小、代码缺陷、小样本内存放大相互吻合，
但缺少事发 worker 的内存/请求快照，仍应标注为高概率而非直接确证。

修复重点应包含：播放列表采用流式读取并设小体积上限；读取前后校验媒体类型和
EXTM3U 内容；分片流式计数、设置字节和总耗时上限；导入文本及 gzip 解压也设置
上限。容器 mem_limit 提供隔离，不能替代这些边界。此补充复核没有修改运行代码。

HTTPX 超时语义：https://www.python-httpx.org/advanced/timeouts/
