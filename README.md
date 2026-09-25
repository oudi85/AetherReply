<div align="center">

<img src="assets/aetherreply-icon.png" width="150" alt="AetherReply" />

# AetherReply · 微信回复助手

**装在 Windows 电脑上的微信回复助手：学你平时怎么说话，帮你回指定好友的消息。可以提出3条候选回复供您挑选，也可全自动发送。**

[![License](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-orange?style=flat-square)](LICENSE)
[![Windows](https://img.shields.io/badge/%E5%B9%B3%E5%8F%B0-Windows-0078D4?style=flat-square&logo=windows&logoColor=white)](#快速开始)
[![WeChat](https://img.shields.io/badge/%E5%BE%AE%E4%BF%A1-4.1.13.65%20%E5%AE%9E%E6%B5%8B-07C160?style=flat-square&logo=wechat&logoColor=white)](#已知限制)

[快速开始](#快速开始) · [怎么用](#日常使用) · [常见问题](#常见问题) · [许可证](LICENSE) · [NOTICE](NOTICE)

</div>

## 它能做什么

- **像你本人在回。** 从你过去发过的消息里学语气、用词和回复长短，而不是套一个通用的客服腔。
- **三种模式随时切换。** 自动发送|仅生成回复|暂停。
- **只回复个人白名单。** 自动发送只针对允许名单里的一对一聊天，暂不支持群聊。
- **宁可不发，也不乱发。** 发之前核对会话、输入框和整段文字，有一项不对就停。发完确认不了结果时标成"待核对"，不会自动重发。
- **不用 OCR。** 直接读本机微信数据库，发消息走 Windows 辅助功能接口，不截屏识字。
- **数据留在本机。** 聊天镜像、配置和密钥都存在安装目录里；只有生成回复时，相关上下文会发给你自己配置的模型接口。

## 快速开始

使用前需要：Windows 电脑、已登录的 PC 版微信、64 位 Python 3.11+，以及一个 OpenAI 兼容接口的模型密钥（DeepSeek、智谱、OpenRouter、本地 Ollama 都行）。

**1. 安装。** 双击仓库根目录的 `install.cmd`，选择安装路径，程序会装进其中的 `AetherReply` 文件夹。装好后桌面会多一个“AetherReply”快捷方式，使用上方展示的可爱图标。安装本身不会启动后台，也不会发任何消息。

```powershell
.\install.cmd -InstallDir "E:\Apps\AetherReply"   # 也可以直接指定路径
```

**2. 填密钥。** 打开安装目录里的 `config.toml`，在 `[llm]` 填上接口地址、模型名和 `api_key`。不想写进文件的话，也可以设环境变量 `AUTO_REPLY_API_KEY`。

**3. 读取微信数据。** 保持微信登录，在安装目录运行：

```powershell
.\.venv\Scripts\python.exe -m auto_reply detect   # 找到微信数据目录
.\.venv\Scripts\python.exe -m auto_reply keys     # 从运行中的微信取数据库密钥
.\.venv\Scripts\python.exe -m auto_reply sync     # 同步聊天记录到本地
```

完成后打开“AetherReply”，在左侧搜索好友并加入名单，建议先用“仅生成”模式测试。

## 日常使用

**控制台。** 左边是名单里的会话，中间是像微信一样的聊天记录，每条消息下挂着 AI 提供的候选和发送结果，点击即可复制；右边切模式、开关深度思考。关掉控制台不影响后台，后台随 Windows 登录自动启动，也可以点"启动后台"。

**开启自动发送。** 在 `config.toml` 里把开关打开，再到控制台切到"自动发送"：

```toml
[auto_send]
enabled = true
allow_talkers = ["wxid_xxx"]   # 在控制台加好友时会自动写入
```

**快捷键。** F5 刷新，Ctrl+F 搜索，Ctrl+P 暂停/恢复，Esc 清空搜索；Delete 在会话列表里移除联系人。

**先看看界面。** 演示模式用假数据，不碰真实配置和聊天记录，也不会发送：

```powershell
pythonw tools/run_dashboard.py --demo
```

**可选功能。**

- 连发多条：`[reply_plan].enabled = true`，一次按你的习惯回 1–3 条
- 收藏表情：`stickers scan` 建索引，在控制台"表情包标注"里分类，`stickers calibrate` 校准后再开 `allow_stickers`
- 换电脑：`voice-pack export` / `voice-pack import` 用口令加密带走你的说话习惯

其他命令用 `python -m auto_reply --help` 查看。

## 卸载

双击安装目录里的 `uninstall.cmd`，或者在 Windows“已安装的应用”里卸载“AetherReply”。

**卸载会删除本地聊天镜像、配置和密钥**，要留的话先备份。卸载器只清理本次安装的东西，不动别的目录。

## 常见问题

**支持哪些微信版本？**
目前只在 Windows 微信 4.1.13.65 上实机验证过。其他版本没测，不保证能用；微信升级后可能需要重新适配。

**会不会乱发消息？**
只发给允许名单里的一对一好友。发之前逐项核对，对不上就不发；结果不确定时交给你到微信里确认，不会自动重发。

**我的聊天记录会上传吗？**
聊天镜像只存在本机。生成回复时，会把相关的最近对话发给你配置的模型接口；介意的话可以换成本地模型。

**Jev 能用吗？**
目前只预留了接口，还没接通。

**提示找不到微信数据目录？**
在 `config.toml` 的 `[wechat]` 里手动填 `data_dir`，指向包含 `wxid_xxx` 文件夹的 `xwechat_files` 目录。

## 它怎么工作

```
微信加密数据库 → 提取密钥并解密 → 本地镜像
      → 学习你的表达习惯 + 最近上下文 → 生成候选
      → 核对会话与文字 → 通过辅助功能接口发送 → 在本地消息库确认
```

发送部分参考了 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)（Apache-2.0）的辅助功能控件定位方法，详见 [NOTICE](NOTICE)。

## 已知限制

- **版本：** 只验证了微信 4.1.13.65，其他版本需要自己验证。
- **必须登录：** 电脑上的微信要保持登录，程序靠它读取数据和发送消息。
- **同步延迟：** 默认每 5 秒检查一次新消息；电脑休眠或微信退出时会延迟。
- **仅限本人：** 只用于处理你自己电脑上、你自己账号的数据。

## 许可证

本项目采用 [PolyForm Noncommercial 1.0.0](LICENSE) 许可证。
