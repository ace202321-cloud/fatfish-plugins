# 肥鱼输入器 · 安装脚本（无需管理员权限）
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host "== 肥鱼输入器 安装 ==" -ForegroundColor Cyan
Write-Host "目录: $here"

# ---------- 1. Python ----------
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Host "❌ 没找到 python，请先装 Python 3.10+ 并勾选 Add to PATH" -ForegroundColor Red; exit 1 }
$ver = & python -c "import sys;print('%d.%d'%sys.version_info[:2])"
Write-Host "Python $ver : $py"

# ---------- 2. 依赖 ----------
Write-Host "`n[1/3] 安装 Python 依赖（sounddevice / numpy / pystray / pillow / sherpa-onnx）..."
& python -m pip install --user --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple `
    sounddevice numpy pystray pillow sherpa-onnx
if ($LASTEXITCODE -ne 0) { Write-Host "❌ 依赖安装失败" -ForegroundColor Red; exit 1 }

# ---------- 3. 语音识别模型（约 232MB，国内 hf-mirror 直连） ----------
$modelDir = Join-Path $here "models\asr-paraformer-zh"
New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
$need = -not (Test-Path (Join-Path $modelDir "model.int8.onnx"))
if ($need) {
    Write-Host "`n[2/3] 下载中文语音识别模型（232MB，只需一次）..."
    $base = "https://hf-mirror.com/csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14/resolve/main"
    curl.exe -L -o (Join-Path $modelDir "model.int8.onnx") "$base/model.int8.onnx" --max-time 3600
    curl.exe -L -o (Join-Path $modelDir "tokens.txt") "$base/tokens.txt" --max-time 300
} else {
    Write-Host "`n[2/3] 模型已存在，跳过下载"
}

# ---------- 4. 配置 ----------
$cfg = Join-Path $here "config.json"
if (-not (Test-Path $cfg)) {
    Write-Host "`n请粘贴 DeepSeek API Key（sk- 开头，回车跳过则稍后在 config.json 里填）:"
    $key = Read-Host
    $obj = [ordered]@{
        hotkey_voice   = "Win+Shift+H"
        hotkey_shot    = "Win+Shift+X"
        hotkey_lang    = "Win+Shift+L"
        target_lang    = "中文"
        api_key        = $key.Trim()
        api_model      = "deepseek-flash"
        silence_ms     = 700
        max_record_ms  = 20000
        auto_paste     = $true
        mt_engine      = "online"
        bar_x          = -1
        bar_y          = -1
    }
    $json = $obj | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($cfg, $json, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "配置已写入 $cfg"
}

# ---------- 5. 开机自启 + 立刻启动 ----------
Write-Host "`n[3/3] 设置开机自启并启动..."
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) "肥鱼输入器.lnk"
$wsh = New-Object -ComObject WScript.Shell
$sc = $wsh.CreateShortcut($lnk)
$sc.TargetPath = "wscript.exe"
$sc.Arguments = "`"$here\start.vbs`""
$sc.WorkingDirectory = $here
$sc.Description = "肥鱼输入器：语音输入 + 截图翻译"
$sc.Save()

Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 500
$r = ([wmiclass]"Win32_Process").Create("wscript.exe `"$here\start.vbs`"")
Start-Sleep -Seconds 4
$p = Get-Process pythonw -ErrorAction SilentlyContinue | Select-Object -First 1
if ($p) {
    Write-Host "`n✅ 已启动（PID $($p.Id)，空闲约 50MB）" -ForegroundColor Green
    Write-Host "   Win+Shift+H  语音输入"
    Write-Host "   Win+Shift+X  截图翻译"
    Write-Host "   Win+Shift+L  切换输出语言"
} else {
    Write-Host "⚠️ 没起来，看日志: $here\fatfish_input.log" -ForegroundColor Yellow
}
