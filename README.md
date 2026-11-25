# LocalVoice · 语音 Buddy

在 Windows 上本地运行的中文语音助手。用浏览器录音，自动检测开口和停顿，通过开口或 `Esc` 打断播报。LLM 边生成边返回文字；高音质模式使用 Qwen TTS 以 8 帧为单位输出 PCM，可试听并切换预设音色。

- **回复模型：** 默认使用本地 llama.cpp；也可切换到远程 OpenAI 兼容 API。
- **实时模式：** Piper 中文音色，CPU 合成。
- **高音质模式：** Qwen3-TTS + NVIDIA CUDA Graph，流式播报。
- **局域网：** Windows 运行推理，同一受信局域网的 Mac 可直接访问 HTTPS，无需登录，不暴露模型端口。

```text
Browser → voice_web.py → Qwen3-ASR (CPU)
                       → llama.cpp / Qwen3-8B (Vulkan) or OpenAI-compatible API (HTTPS)
                       → Piper (CPU) or Qwen3-TTS (CUDA)
```

## 网页使用

服务启动后，在 Windows 服务器上打开 [语音页面](http://127.0.0.1:8010/)。从 Mac 使用时，按 [Mac 局域网接入指南](mac-localvoice-setup.md) 打开 Windows 主机的 HTTPS 地址；不要在 Mac 上打开 `127.0.0.1`。推荐使用 Edge 或 Chrome。

### Windows 一键启动

双击「语音 Buddy」桌面快捷方式或仓库目录的 `VoiceBuddy.exe`，它会在后台启动语音服务，并在默认浏览器打开 [语音页面](http://127.0.0.1:8010/)。已运行的服务会复用。Windows 的局域网入口完成一次性配置后，每次双击也会启动 HTTPS 网关；从页面底部「使用说明与连接端口」可以查看 Mac 地址和下载公用 CA 证书。远程模型设置会自动读取，关掉浏览器不会停止后台服务。

若没有可执行文件，运行 `.\build-voicebuddy.ps1` 生成。首次打开时可选择已部署的 LocalVoice 模型目录，也可在构建时用 `-AssetRoot` 指定。安装了 `.venv-qwen-graph` 时，启动器会预热 CUDA Graph 并启用高音质流式播报；没有该环境时，复用普通 Qwen worker，避免每段都重新加载。启动失败时查看 `runtime/voice-buddy-8010.log`。打开网页不需要等待模型；使用本地回复时，需另外启动 llama.cpp（默认端口 `8001`），等页面显示「模型在线」再开始对话。

### 开始对话

1. **先试一轮。** 页面右上角显示「模型在线」或「远程已配置」后，点击「样音测试」。它不需要麦克风，可以先验证识别、回复和播报。
2. **开始说话。** 在「麦克风」下拉框选择设备，按浏览器提示允许麦克风。点击「开始录音」，说完点「停止录音」即可发送；也可以按住空格说话，松开发送。想用现成录音时，点击「上传音频」，选择 0.3 至 15 秒的音频（如 WAV、MP3 或 WebM）。
3. **连续对话和打断。** 勾选「自动收音与打断」，等页面显示「自动监听中」再开口；停顿后自动发送。助手播报时可直接开口打断。也可以点「停止播报」或按 `Esc`，不必等待它说完。不再需要持续监听时，取消勾选。
4. **选择回复声音。** 选择「实时」可使用 Piper 快速播报；选择「高音质」可使用 Qwen，并在「音色」下拉框里选择预设。点击旁边的播放图标试听。若显示「Qwen 预热中」，等它准备好再开口。
5. **查看或换一段对话。** 「识别与回复」会显示你的转写和助手回复，完成后可重播或下载语音。想换话题且不沿用当前对话上下文时，等本轮结束后点「新对话」。

如果点「样音测试」正常，但录不到声音，先检查浏览器的网站麦克风权限，再核对「麦克风」选择和「输入电平」。Mac 连不上时，先确认 Windows 服务仍在运行、两台电脑在同一局域网，并使用已信任证书的 HTTPS 地址。

### 使用远程回复模型

点击「新对话」旁的设置图标，选择「远程 OpenAI 兼容」，填写 HTTPS API Base URL（例如 `https://api.openai.com/v1`）、API Key 和提供商给出的模型名称（例如 `gpt-4o-mini`）。设置中保留 Base URL，发送请求时自动追加 `/chat/completions`；旧的完整地址仍可使用。服务需要支持流式 Chat Completions。保存后点「样音测试」验证；「远程已配置」只表示设置已保存。

远程模式不需要启动 llama.cpp。语音识别和合成仍在 Windows 主机上运行，只有转写文本和当前对话的历史问答会发给所选服务。回复模型只能在 Windows 本机网页设置，局域网客户端使用主机选定的模型。API Key 保存在 Windows 主机的 `runtime/chat-settings.json`（本地明文，不进入 Git，不会返回给网页）。切回「本地」会保留远程设置，也可以同时勾选「清除远程配置」。更换 URL 时需重新填写 API Key。

## Windows 部署

参考环境：Windows 11 x64、RTX 5070 Ti Laptop GPU（约 12 GiB 显存）、32 GB 内存。准备支持 CUDA 12.8 和 Vulkan 的 NVIDIA 驱动、Python 3.12 x64 和 PowerShell 7.6。Mac 不需要安装模型。以下命令从仓库根目录执行。

### 1. 安装 Python 依赖

ASR 和 TTS 所需的 Transformers 版本不同，应保留独立环境。CUDA Graph worker 复用 TTS 环境中的 PyTorch 和模型依赖。

```powershell
py -3.12 -m venv .venv-asr
py -3.12 -m venv .venv-tts
py -3.12 -m venv .venv-fasttts
py -3.12 -m venv .venv-qwen-graph

& .\.venv-asr\Scripts\python.exe -m pip install torch==2.10.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cu128
& .\.venv-tts\Scripts\python.exe -m pip install torch==2.10.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/cu128
& .\.venv-asr\Scripts\python.exe -m pip install qwen-asr==0.0.6
& .\.venv-tts\Scripts\python.exe -m pip install qwen-tts==0.1.1
& .\.venv-fasttts\Scripts\python.exe -m pip install piper-tts==1.8.0 onnxruntime==1.32.0
& .\.venv-qwen-graph\Scripts\python.exe -m pip install --no-deps faster-qwen3-tts==0.3.0
```

### 2. 准备模型和样音

从 [llama.cpp b12556](https://github.com/ggml-org/llama.cpp/releases/tag/b12556) 下载 Windows x64 Vulkan 构建，解压到 `llama/`，确保 `llama/llama-server.exe` 和同包 DLL 位于同一目录。模型从 [Qwen3-ASR](https://huggingface.co/Qwen/Qwen3-ASR-0.6B)、[Qwen3-TTS](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice)、[Qwen3-8B-GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF) 和 [Piper 中文音色](https://huggingface.co/rhasspy/piper-voices/tree/main/zh/zh_CN/huayan/medium) 获取：

```powershell
$hf = '.\.venv-asr\Scripts\hf.exe'
& $hf download Qwen/Qwen3-ASR-0.6B --local-dir models/Qwen3-ASR-0.6B
& $hf download Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice --local-dir models/Qwen3-TTS-12Hz-0.6B-CustomVoice
& $hf download Qwen/Qwen3-8B-GGUF Qwen3-8B-Q4_K_M.gguf --local-dir models
& $hf download rhasspy/piper-voices --include 'zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx*' --local-dir downloads/piper-voices
New-Item -ItemType Directory -Force models/piper-zh-cn-huayan, checks | Out-Null
Copy-Item -Path 'downloads/piper-voices/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx*' -Destination 'models/piper-zh-cn-huayan'
& .\.venv-fasttts\Scripts\python.exe -X utf8 -c "from fast_tts import PiperTTS; PiperTTS().synthesize('上海有什么好玩的地方？', 'checks/shanghai-question.wav')"
```

请保留 Qwen TTS 下载中的 `speech_tokenizer/`。样音用合成语音在本机生成，用于 CPU ASR 预热和主页「样音测试」；无需提交个人录音。

### 3. 启动并验证

本地回复模式下，在第一个 PowerShell 窗口运行 `.\start-llama.ps1`，等待 [模型就绪](http://127.0.0.1:8001/health)。脚本默认选择第一个 NVIDIA Vulkan 设备，多显卡可传 `-Device Vulkan1` 等实际设备名。使用远程回复模型时可跳过这一步。

第二个 PowerShell 窗口运行：

```powershell
$python312 = py -3.12 -c "import sys; print(sys.executable)"
.\start-qwen-realtime.ps1 -PythonPath $python312
```

按 [网页使用](#网页使用) 的步骤验证服务。准备使用 Mac 时，按 [Mac 局域网接入](mac-localvoice-setup.md) 配置 HTTPS 证书和防火墙。不要在路由器上转发端口或将网关公开到互联网。

已有部署时，可在启动前将 `LOCALVOICE_ASSET_ROOT` 设置为部署目录，复用其中的模型、Python 环境和样音。新录音、回复和模型设置仍保存在当前代码目录下。

## 测试与数据

```powershell
py -3.12 -X utf8 -m unittest discover
node --test web/test_voice_input.mjs web/test_interrupt.cjs
```

单元测试不需要模型、GPU 或麦克风。根目录下的 `test_*.cjs` 则是独立的 Playwright 集成测试：需要已启动的服务和 `playwright`（可运行 `npm install --no-save --package-lock=false playwright` 及 `npx playwright install chromium`），部分脚本还需通过 `VOICE_TEST_MIC_WAV` 指定本地测试 WAV。见 [连续对话验收](voice-stress-acceptance.md) 和 [TTS 对比](qwen-realtime-results.md)；其中的延迟是特定机器上的测量，不是跨硬件保证。

录音、转写、回复、模型、环境和证书凭据均不应进入 Git。运行时对话文件位于 `checks/`，局域网凭据位于 `.voice-lan/`。在 Mac 上使用时，录音会通过局域网传到 Windows 处理。浏览器内的 VAD 文件和各自的授权声明见 [`web/vad/`](web/vad/README.md)。
