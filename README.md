# 一本通自动提交工具

这个脚本用于把**你自己的账号和代码**提交到
`http://ybt.ssoier.cn:8088/`，并在提交后显示评测结果。提交时使用 Playwright
驱动本机可见的 Microsoft Edge：打开题目列表、搜索题号、打开题目、点击“提交”、
填写代码，再点击提交面板里的真实“提交”按钮。

## 从压缩包安装

1. 将 ZIP 解压到一个固定目录，例如 `C:\YBT-Tool`。
2. 双击 `install_shortcut.cmd`。
3. 桌面会出现“一本通单题工具”快捷方式。
4. 打开快捷方式，选择 `4. Configure AI solver` 配置模型和 API Key。

压缩包内已包含便携 C 编译器，不需要安装 MinGW。账号密码、API Key、生成代码
和提交记录不会随压缩包分发。

## 功能

- 按题目列表和题目页的真实按钮流程打开提交面板。
- 默认只提交一次，并先显示题目、语言和代码长度。
- 默认执行一本通的 40 秒提交间隔；不足 40 秒时自动等待，并额外保留 1 秒安全余量。
- 按站点和账号分别保存最后提交时间，重新启动脚本后仍然遵守间隔。
- 点击后如果站点返回空白或没有新运行号，只生成诊断文件并停止，不会马上重试。
- 提交前需要确认，提交后轮询 `Waiting/Judging` 等状态直到出结果。
- `--dry-run` 会走完打开和填表流程，但不会点击最终提交按钮。
- 密码只从环境变量或交互式输入读取，不接受命令行密码参数。

## 环境要求

- Python 3.10 或更高版本
- Microsoft Edge
- Python Playwright 包
- 能访问 `ybt.ssoier.cn:8088` 的网络

## 使用

先在当前 PowerShell 会话中设置账号和密码：

```powershell
$env:YBT_USERNAME = "你的用户名"
$env:YBT_PASSWORD = "你的密码"
```

建议先做一次不提交的检查：

```powershell
python .\ybt_ui_submit.py --problem 1000 --file .\1000.c --dry-run
```

确认输出的题目、语言、表单字段和代码框正确后，再正式提交：

```powershell
python .\ybt_ui_submit.py --problem 1000 --file .\1000.c
```

脚本会显示最终提示，输入 `y` 后才点击真实提交按钮。自动化运行时可以加
`--yes` 跳过这次确认，但建议先手动确认一次。代码默认以 C 源文件提交，网站语言
选择为 `G++14`。

常用参数：

```text
--problem           题目编号
--file              源代码文件
--username          可选；否则读取 YBT_USERNAME 或提示输入
--dry-run           打开提交面板并填表，但不点击最终提交
--yes               跳过提交前的确认
--submit-interval   同账号两次提交的最小间隔，默认 40 秒
--state-file        保存最后提交时间的文件
--poll-timeout      最长等待评测秒数，默认 180
```

也可以先用公开探测确认网络：

```powershell
python .\ybt_submit.py --probe --problem 1000
```

提交时间记录默认保存在：

```text
%LOCALAPPDATA%\ybt_submit\submit_state.json
```

文件中只保存账号标识的哈希和最后提交时间，不保存用户名或密码。实际等待时间
默认是 41 秒，因为脚本会在站点的 40 秒限制之外再留 1 秒安全余量。

## 退出码

- `0`：提交成功且结果为 Accepted，或 Dry run 正常完成。
- `1`：已经提交，但评测结果不是 Accepted。
- `2`：登录失败、网站改版、网络错误、编译错误或提交被拒绝。

## 注意

脚本特意限制为一次运行只提交一次，不做批量刷题、暴力尝试或验证码绕过。
请只提交自己的代码并遵守一本通网站的用户协议和学术诚信规则。如果网站以后增加
验证码或改变提交接口，脚本会报告错误而不会尝试绕过；此时用 `--dry-run`
检查页面结构即可判断是否需要更新脚本。

## 单题生成、验证与提交

`ybt_auto.py` 增加了单题自动流水线：

1. 抓取指定题目的题面、输入输出说明、样例和正文图片地址。
2. 调用兼容接口生成 C 答案；命令行仍可显式指定 Python 或 C++。
3. 在本地运行公开样例，失败时把错误反馈给求解器并重试。
4. 只有样例通过后才允许提交，并继续遵守 40 秒提交间隔。

默认**不会提交**，只生成和验证代码。只有显式添加 `--submit --yes` 才会执行
完整自动提交。

### 配置求解器

脚本支持 OpenAI 兼容的 Chat Completions 接口。先设置模型和 API Key：

```powershell
$env:SOLVER_MODEL = "填写你的模型名称"
$env:SOLVER_API_KEY = "填写你的 API Key"

# 仅在使用第三方兼容服务或本地服务时需要：
$env:SOLVER_BASE_URL = "http://127.0.0.1:11434/v1"
```

也兼容常见的 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 环境变量。不同模型和服务的
名称、资费与可用性由其提供方决定。

### 只生成并验证

```powershell
python .\ybt_auto.py --problem 1000 --show-code
```

生成的代码保存在工具目录下的 `generated\`，样例失败时默认最多生成两次。

### 完整自动提交

先配置一本通账号：

```powershell
$env:YBT_USERNAME = "你的用户名"
$env:YBT_PASSWORD = "你的密码"
```

然后执行：

```powershell
python .\ybt_auto.py --problem 1000 --show-code --submit --yes --ui-submit
```

这条命令会抓题、生成答案、运行样例、打开 Edge、等待提交间隔、点击提交面板里的
真实按钮并轮询评测结果。去掉 `--yes` 时会在提交前要求人工确认。

桌面快捷方式菜单里：

- `1. Fetch, generate and validate only`：只抓题、生成、跑本地样例，不提交。
- `2. Fetch, generate, validate and confirm submit`：样例通过后，提交前还要敲
  `y` 确认（对应不带 `--yes`，仍使用浏览器按钮提交）。
- `3. Fetch, generate, validate and AUTO-submit`：样例一通过就直接提交，不再
  敲 `y` 确认（对应 `--submit --yes --ui-submit`）；40 秒提交间隔和 YBT 登录仍然
  照常执行。

### 外部求解命令

如果已经有自己的模型脚本，可以让它从标准输入读取 JSON，并把完整代码写到标准
输出：

```powershell
python .\ybt_auto.py --problem 1000 `
  --solver command `
  --solver-command "python .\my_solver.py"
```

### 语言与编译器

桌面快捷方式和 `ybt_auto.py` 现在固定默认使用 C17，提交面板语言固定选择
`G++14`。工具目录已经包含一个仅用于本地样例验证的便携 TinyCC：

```text
tools\tcc\tcc\tcc.exe
```

TinyCC 不加入系统 PATH，也不需要安装。网站使用 G++14 编译提交的 C 源文件。

命令行也可以显式生成 C++ 答案，但需要另外安装 MinGW：

```powershell
python .\ybt_auto.py --problem 1000 `
  --language cpp `
  --compiler "C:\mingw64\bin\g++.exe"
```

### 批量按顺序提交

`ybt_batch.py` 把上面的单题流水线按题号顺序循环跑：每道题先抓题面、
让模型生成代码、在本地跑公开样例；样例通过后才打开 Edge，通过题目列表和提交
面板逐题点击真实按钮提交，并继续遵守 40 秒提交间隔。样例不过、没有公开样例、
题号不存在的题会自动跳过，不会把没验证过的代码交上去。

批量模式只启动一次 Edge，后续题目复用同一个已登录会话；单题提交失败会记录结果
并继续下一题，不会在 40 秒限制内立即重登或重复提交同一道题。

先只在本地跑一遍生成和样例验证，不提交：

```powershell
python .\ybt_batch.py --start 1000 --end 1010 --show-code
```

确认没问题后，再加 `--submit --yes` 真正按顺序提交：

```powershell
python .\ybt_batch.py --start 1000 --end 1010 --show-code --submit --yes
```

也可以只跑挑出来的几道题：

```powershell
python .\ybt_batch.py --problems 1000,1003,1005 --submit --yes
```

批量结果会写到
`%LOCALAPPDATA%\ybt_submit\batch_progress.json`。已经 Accepted 的题下次跑会
自动跳过，所以中途 Ctrl+C 之后再跑同一条命令会接着做，不会重复交已经
AC 的题。加 `--redo` 可以忽略进度全部重跑。

桌面快捷方式菜单里选 `7. Batch browser AUTO-submit a problem range`，输入首尾题号
即可，脚本会在真跑前再让你确认一次范围和题数。

### 安全限制

- 不抓取或复用站内其他人的 Accepted 代码。
- 没有公开输出样例的题目会跳过，不会盲交。
- 批量模式只在你显式给出题号范围、且每道题本地样例通过后才提交；
  不对同一道题反复尝试碰运气，也不绕过验证码。遇到登录/接口变化会
  直接报错停下。浏览器点击后若没有新运行号，批量模式不会立即重试同一道题。
- 生成代码会在本地用短超时编译和运行，但它不是安全沙箱。只应使用可信模型，
  重要环境下仍应人工检查生成代码。
