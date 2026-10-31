# 肥鱼插件库 · fatfish-plugins

大肥鱼（DeepSeek Harness 里的那个）写的 Windows 小工具合集。
每个插件一个子目录，独立可用，互不依赖。

> 所有插件都**不需要任何宿主程序**（不需要开 DSH / 编辑器 / AI 客户端），装完就在托盘跑。

## 插件列表

| 插件 | 一句话 | 状态 |
|---|---|---|
| [**肥鱼输入器** `fatfish-input`](fatfish-input/) | 全局语音输入 + 截图翻译：本地识别、DeepSeek 翻译，`Win+Shift+H` 说话即出字 | ✅ v4 可用 |

## 肥鱼输入器 速览

![闲置时只有一个小麦克风](fatfish-input/screenshot-mini.png)

```powershell
git clone https://github.com/ace202321-cloud/fatfish-plugins.git
cd fatfish-plugins\fatfish-input
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

安装脚本会装依赖、拉 232MB 中文识别模型、问你要一个 DeepSeek API Key、建开机自启并启动。
详细说明见 [fatfish-input/README.md](fatfish-input/README.md)。

## 目录约定

```
fatfish-plugins/
├── README.md          <- 这个索引
├── LICENSE            <- 仓库级 MIT
└── fatfish-input/     <- 单个插件：代码 + 安装脚本 + README + 截图
```

新增插件就新建一个子目录，目录内自带 `README.md` 和 `install.ps1`，然后在上面表格加一行。

## License

MIT