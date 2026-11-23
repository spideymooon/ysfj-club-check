# 有赞网页每日自动签到

Python + Playwright 实现的「有赞俱乐部」每日自动签到，支持持久化浏览器登录态、
SQLite 记录、PushPlus 微信通知、Docker / 极空间 NAS 部署。

> **安全底线**：代码里没有任何 Cookie / Token / 密码；登录态只存在浏览器持久化 Profile；
> `.env`、`data/`、Profile、数据库、日志全部被 Git 忽略；所有日志与入库文本强制脱敏。

---

## 一、接口实测结论（重要）

在动手前对目标站点做了真实请求验证，结论如下，代码按这些事实实现：

| 项目 | 实测结果 |
|---|---|
| 结果页未登录 | `302` → `https://passport.youzan.com/login?redirectUrl=...`，**手机号 + 短信验证码登录** |
| 签到 API 未登录 | **HTTP 200 + JSON**：`{"code":1000000002,"msg":"userId must be greater than or equal to 1 "}`（**不跳登录页**） |
| 缺 `checkinId` | `{"code":12345,"msg":"缺少必要的参数 - checkinId"}` → `checkinId` 为必需参数 |
| `kdt_id` / `app_id` | API 层 `kdt_id` 可选、`app_id` 对 PC 网页版无影响，均保留但可配置 |
| 配套查询接口 | `get_activity_by_yzuid_v2.json?checkinId=...&kdt_id=...` → `{"code":0,"data":{"isCheckin":false,"continuesDay":0,...}}` |
| 必需请求头 | `Referer`（结果页）、`X-Requested-With: XMLHttpRequest`、`Accept: application/json`、`User-Agent`（从浏览器实时读取） |

因此：

1. **登录失效判定依据 API 返回的 `code=1000000002` / `userId must be...`**，而不是页面 302——页面跳登录只作为辅助信号。
2. 签到前先查 `data.isCheckin`，为 `true` 就直接跳过，**不发签到请求**（最强幂等）。
3. 没有假定参数已全部确定：程序会监听结果页真实发出的 `checkinV2.json` 请求，
   自动合并其中的额外参数与请求头；也可用 `EXTRA_QUERY` / `EXTRA_HEADERS` 手动补充。
   抓包结果中的 Cookie / Token 等凭证会被剔除后才落盘。

---

## 二、目录结构

```
.
├── app/
│   ├── config.py          # 配置（全部来自环境变量 / .env）
│   ├── logging_setup.py   # 日志 + 强制凭证脱敏
│   ├── db.py              # SQLite：签到记录 / 运行日志 / 应用状态
│   ├── classifier.py      # 响应判定（成功 / 重复 / 登录失效 / 失败 / 异常）
│   ├── headers.py         # 请求构造 + 真实抓包合并 + 敏感项剔除
│   ├── browser.py         # 持久化 Profile、人工登录、请求探测
│   ├── checkin.py         # 单次签到编排（幂等 / 前置检查 / 有限重试 / 落库）
│   ├── notifier.py        # PushPlus 微信通知
│   ├── scheduler.py       # 每日 08:00 Asia/Shanghai
│   └── main.py            # CLI
├── scripts/
│   ├── local_test.bat     # Windows 一键：init → login → once → status
│   ├── entrypoint.sh      # 容器入口
│   └── start_login.sh     # Xvfb + noVNC 有头登录服务
├── tests/                 # 39 项离线单元测试
├── data/                  # 运行期数据（Git 忽略）
│   ├── checkin.db
│   ├── browser_profile/   # 登录态，严禁提交
│   ├── logs/app.log
│   └── captured_request.json
├── .env.example
├── .gitignore / .dockerignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## 三、本地首次运行（Windows）

> 前置：Python 3.10+，已安装 Chrome（用 `BROWSER_CHANNEL=chrome` 免下载内置 Chromium）。

### 方式 A：网页控制台（推荐）

```bat
pip install -r requirements.txt
copy .env.example .env
scripts\start_serve.bat     :: 启动控制台（等效 python -m app.main web）
```

然后浏览器打开 **http://127.0.0.1:8787** ，在网页上完成所有事：

- **状态面板**：运行状态 / 下次执行时间 / 上次登录 / 上次成功日期 / 最近 10 条签到记录（每 5 秒自动刷新）
- **常用操作**：立即签到 / 打开登录窗口（首次或登录失效后，点击后本机弹出 Chrome，手机号+验证码登录，自动保存登录态）/ 解除暂停 / 发送测试推送
- **设置表单**：PushPlus Token（保存后只显示脱敏形式 `ABC******WXYZ`，留空表示不修改）、群组编码、签到时间、checkinId / kdt_id、各类通知开关、启动补签开关 —— 保存即写入 `.env` 并即时生效（改签到时间会自动重排定时任务，无需重启）
- **运行日志**：最近 120 行（已自动脱敏）

安全说明：控制台默认只绑定 `127.0.0.1`（仅本机可访问）。如需手机等设备访问，改 `.env` 的 `WEB_HOST=0.0.0.0`（局域网内任何人都能操作签到/读取状态，请自担风险），端口用 `WEB_PORT` 修改。

### 方式 B：命令行

```bat
pip install -r requirements.txt
copy .env.example .env
:: 编辑 .env，填入 PUSHPLUS_TOKEN（不填则不推送）
scripts\local_test.bat
```

`local_test.bat` 会依次执行：

1. `python -m app.main init` —— 创建目录与 SQLite
2. `python -m app.main login` —— **弹出 Chrome 窗口，请手动用手机号 + 短信验证码登录**
   登录成功后页面跳回 `club.ysjf.com`，程序自动检测并把登录态写入 `data/browser_profile`
3. `python -m app.main once` —— 单次签到，打印真实 JSON 判定结果
4. `python -m app.main status` —— 查看记录

也可以手工分步执行：

```bat
set BROWSER_CHANNEL=chrome
python -m app.main init
python -m app.main login      :: 首次 / 登录失效后重新登录
python -m app.main probe      :: 可选：抓取页面真实签到请求，补充参数与 Header
python -m app.main once       :: 单次签到测试
python -m app.main once --force  :: 忽略幂等保护强制再跑一次
python -m app.main status
python -m app.main notify-test   :: 测试 PushPlus
python -m app.main web        :: 网页控制台 + 每日定时调度（同方式 A）
python -m app.main resume        :: 手动解除暂停
python -m app.main serve         :: 启动每日 08:00 调度
```

**验证签到结果**：`once` 会输出 `签到结果 / code / 说明 / 响应摘要`，
完整响应（已脱敏）写入 `data/checkin.db` 的 `checkin_records.raw_excerpt`，
请求 URL 与请求头键写入 `app.log`。成功判定按你确认的
`code==0 && data.success==true && data.list[0].isSuccess==true`，并兼容多种变体。

Linux / macOS 把 `BROWSER_CHANNEL` 留空，先执行 `playwright install chromium` 即可。

---

## 四、状态机：幂等、重复签到、登录失效

| 判定 | 是否重试 | 行为 |
|---|---|---|
| `success` 签到成功 | 否 | 落库、可选推送、记录 `last_success_date` |
| `already` 今日已签 | **否** | 视为「已完成」，落库，**不再发签到请求** |
| `login_expired` 登录失效 | **否** | **暂停自动签到** + PushPlus 通知重新登录；后续调度直接跳过 |
| `failed` 业务失败 | 否 | 落库 + 推送，不重复轰炸 |
| `error` 网络/解析异常 | 是 | 最多 `MAX_RETRY` 次，间隔 `RETRY_BACKOFF` 秒 |
| `skipped_done` / `skipped_paused` | — | 调度前的幂等 / 暂停保护，直接返回 |

三道防重复机制：

1. 调度前查 SQLite，当天已有 `success / already / skipped_done` 记录就直接跳过；
2. 调用签到 API 前先查 `isCheckin`，已签到则不请求；
3. 接口返回「今日已签」等业务码 / 关键词时归入 `already` 而非失败。

登录失效后不会无限重试：进入 `paused=1`，每天最多推送一次提醒，
直到重新执行 `login` 成功才自动 `resume()`。

---

## 五、PushPlus 微信通知

1. 打开 <https://www.pushplus.plus> 微信扫码登录，完成**实名认证**（未实名无法调用发送接口）；
2. 复制 `token`，填入 `.env` 的 `PUSHPLUS_TOKEN`；
3. 可选 `PUSHPLUS_TOPIC`（群组编码，不填只推给自己）、`PUSHPLUS_CHANNEL=wechat`；
4. `python -m app.main notify-test` 验证。

推送场景：签到成功 / 失败 / 异常（可配开关）、**登录失效需重新登录**（必推，含操作指引）、
暂停期间每日一次提醒。Token 只出现在 `.env` 与请求体里，日志与异常信息均不打印。

---

## 六、Docker 部署（极空间 NAS）

### 1. 准备

在 NAS 上建一个目录（例如 `/volume1/docker/youzan-checkin`），放入本项目全部文件，然后：

```sh
cp .env.example .env
# 编辑 .env：填 PUSHPLUS_TOKEN；确认 BROWSER_CHANNEL 为空（Docker 用镜像内置 Chromium）
mkdir -p data
```

> **重要**：本地与 NAS 共用同一个 `data/` 卷。若已在本地完成登录，
> 直接把本地 `data/`（含 `browser_profile/`）拷到 NAS，即可跳过容器内首次登录。

### 2. 启动主服务

```sh
docker compose up -d --build checkin
docker compose logs -f checkin
```

主服务无头运行，APScheduler 按 `Asia/Shanghai` 每日 08:00 触发（时区在代码里显式指定，
不依赖容器 TZ）。`RUN_ON_STARTUP=true` 可在启动时立即补签一次。

### 3. 首次登录 / 登录失效后重新登录（noVNC）

容器里没有显示器，项目内置 Xvfb + x11vnc + noVNC，用浏览器网页操作：

```sh
# 两个服务不能同时用同一个 Profile，先停主服务
docker compose stop checkin
docker compose --profile login up login
```

浏览器打开 **`http://<NAS_IP>:6080/vnc.html`** → 看到 Chrome → 用手机号验证码登录 →
页面跳回 `club.ysjf.com` 后程序自动保存登录态并退出。然后：

```sh
docker compose stop login
docker compose start checkin
```

可在 `login` 服务的 environment 里设置 `VNC_PASSWORD` 给 noVNC 加密码。
**6080 端口仅建议内网使用，不要映射到公网。**

> 若极空间的 Compose 版本不支持 `profiles`，把 `docker-compose.yml` 中 `login` 服务的
> `profiles: ["login"]` 两行删掉，改用 `docker compose stop/start login` 控制启停；
> 或执行 `docker compose run --rm --service-ports login`。

### 4. 常用运维

```sh
docker compose exec checkin python -m app.main status     # 状态与最近记录
docker compose exec checkin python -m app.main once       # 手动补签一次
docker compose exec checkin python -m app.main notify-test
docker compose restart checkin
```

卷映射 `./data:/app/data` 保证 SQLite、日志、Profile 持久化，容器重建不丢登录态。

---

## 七、配置项速查

| 变量 | 默认 | 说明 |
|---|---|---|
| `CHECKIN_ID` / `KDT_ID` | `6287727` / `149536603` | 活动 ID（公开参数，非凭证） |
| `EXTRA_QUERY` / `EXTRA_HEADERS` | 空 | JSON 字符串，补充抓包发现的额外参数 / 请求头 |
| `TIMEZONE` / `SCHEDULE_HOUR` / `SCHEDULE_MINUTE` | `Asia/Shanghai` / `8` / `0` | 定时 |
| `HEADLESS` | `true` | 日常无头；`login` 强制有头 |
| `BROWSER_CHANNEL` | 空 | 本地 Windows 填 `chrome`；Docker 必须留空 |
| `PRECHECK_ENABLED` | `true` | 签到前查 `isCheckin` |
| `CAPTURE_ENABLED` | `true` | 捕获真实请求并合并参数 / Header |
| `MAX_RETRY` / `RETRY_BACKOFF` | `3` / `30` | 仅网络类错误重试 |
| `RUN_ON_STARTUP` | `false` | 服务启动时补签一次（每天关机的机器建议开） |
| `PUSHPLUS_TOKEN` | 空 | **唯一需要保密的值**，只放 `.env` |
| `NOTIFY_ON_*` | — | 分别控制成功 / 重复 / 失败推送 |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1` / `8787` | 网页控制台监听地址；改 `0.0.0.0` 为局域网可访问（有风险） |

---

## 八、安全说明

- **Cookie 不硬编码**：签到通过 `context.request` 发起，Playwright 的 APIRequestContext
  与浏览器上下文共享 Cookie Jar，自动携带 Profile 中的登录态（已实测：服务端
  `Set-Cookie: KDTSESSIONID / nobody_sign` 会写回浏览器 Jar 并在后续请求自动带上）。
- **抓包模板脱敏**：`captured_request.json` 保存前剔除 `Cookie` / `Authorization` /
  `token` / `access_token` / `signature` 等所有凭证类键值。
- **日志脱敏**：全局 `RedactingFilter` 对 `token=`、`Cookie:`、会话键值对、
  `passport` 跳转 URL 及配置中的明文凭证统一替换为 `***REDACTED***`，
  入库的 `message` / `request_url` / `raw_excerpt` 同样先清洗。
- **Git 忽略**：`.env*`、`data/`、`browser_profile/`、`*.db`、`*.log`、
  `captured_request.json`、`__pycache__` 全部排除；`.dockerignore` 也排除了 `.env` 与 `data/`。
- **不要**把 `data/` 上传到公开仓库、不要在日志里贴完整响应头。

---

## 九、数据表

- `checkin_records`：业务日期、状态、HTTP 码、业务 code、说明、请求 URL、响应摘要、重试次数、起止时间
- `run_logs`：结构化运行日志（级别 / 事件 / 消息 / 上下文）
- `app_state`：`paused` / `pause_reason` / `last_login_at` / `last_success_date` /
  `captured_at` / 告警去重日期等

---

## 十、常见问题

**Q：一直提示登录失效？**
执行 `login` 重新登录。注意该站点是手机号验证码登录，且 `passport.youzan.com` 的
会话可能定期失效；失效后程序会暂停并推微信，不会反复重试。

**Q：每天会不会重复签到？**
不会。SQLite 幂等 + `isCheckin` 前置检查 + 重复签到归入 `already` 三重保护。

**Q：返回结构跟预期不一样？**
先看 `once` 打印的响应摘要与 `app.log`；如站点新增了参数，用 `probe` 抓真实请求，
或用 `EXTRA_QUERY` / `EXTRA_HEADERS` 补充，无需改代码。

**Q：容器里时间不准？**
调度使用显式 `Asia/Shanghai`，与容器本地时区无关；compose 仍设了 `TZ` 便于日志可读。

**Q：两个服务同时启动会怎样？**
Chrome 持久化 Profile 会被锁冲突。务必先 `stop checkin` 再启动 `login`。

---

## 十一、网页视觉定制（仅前端样式，不涉及业务逻辑）

控制台标题为「影视飓风club自动签到」，整体为纯白极简主题（`#FFFFFF`）。
左右留白处分别有黑色「无限」「进步」大字背景水印，来自用户提供的
`wenzi_touming.png` 字体图（保留原图粗体、倾斜、速度线切割效果）：

- 原图为透明底 + 近白文字，处理时**保留 alpha 通道**、仅把字色转为纯黑；
- 因四字连体倾斜，采用「精确裁切 + 内侧渐变淡出」：左侧裁到 x=270（含「进」
  起笔）后向右淡出，右侧从 x=245 起裁（已完全去掉「限」收笔残片，只留完整
  「进步」）再向左淡出；两侧按素材宽度比例设置显示宽度，保证字号一致；
- 状态卡片 6 个信息块为 `repeat(3, 1fr)` 的 **3×2 网格**，窄屏（<760px）自动
  变 2 列，任何宽度都不会出现「一行只有一个」的孤行；
- 素材以 base64 内联在 `app/web.py` 的 `PAGE` 样式中，无需额外静态文件；
- 装饰层为 `position: fixed` + `pointer-events: none` + `z-index: 0`，
  卡片容器 `z-index: 1` 叠在其上，因此水印不遮挡、不影响任何交互与滚动条；
- `opacity` 控制视觉层次，窄屏（<1080px）自动缩小并降低不透明度。

如需更换字体图或调整水印大小/深浅，只需改 `PAGE` 中的 `.bg-deco*` 样式块，
不用触碰任何 Python 业务逻辑。
