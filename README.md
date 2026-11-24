# LocalVoice · 语音 Buddy

在 Windows 上本地运行的中文语音助手。用浏览器录音，自动检测开口和停顿，通过开口或 `Esc` 打断播报。LLM 边生成边返回文字；高音质模式使用 Qwen TTS 以 8 帧为单位输出 PCM，可试听并切换预设音色。

- **实时模式：** Piper 中文音色，CPU 合成。
- **高音质模式：** Qwen3-TTS + NVIDIA CUDA Graph，流式播报。
- **局域网：** Windows 运行推理，Mac 只需要浏览器和麦克风。使用带密码的 HTTPS 网关，不暴露模型端口。

```text
Browser → voice_web.py → Qwen3-ASR (CPU)
                       → llama.cpp / Qwen3-8B (Vulkan)
                       → Piper (CPU) or Qwen3-TTS (CUDA)
```

## 网页使用

服务启动后，在 Windows 服务器上打开 [语音页面](http://127.0.0.1:8010/)。从 Mac 使用时，按 [Mac 局域网接入指南](mac-localvoice-setup.md) 打开 Windows 主机的 HTTPS 地址；不要在 Mac 上打开 `127.0.0.1`。推荐使用 Edge 或 Chrome。

1. **先试一轮。** 页面右上角显示「模型在线」后，点击「样音测试」。它不需要麦克风，可以先验证识别、回复和播报。
2. **开始说话。** 在「麦克风」下拉框选择设备，按浏览器提示允许麦克风。点击「开始录音」，说完点「停止录音」即可发送；也可以按住空格说话，松开发送。想用现成录音时，点击「上传音频」，选择 0.3 至 15 秒的音频（如 WAV、MP3 或 WebM）。
3. **连续对话和打断。** 勾选「自动收音与打断」，等页面显示「自动监听中」再开口；停顿后自动发送。助手播报时可直接开口打断。也可以点「停止播报」或按 `Esc`，不必等待它说完。不再需要持续监听时，取消勾选。
4. **选择回复声音。** 选择「实时」可使用 Piper 快速播报；选择「高音质」可使用 Qwen，并在「音色」下拉框里选择预设。点击旁边的播放图标试听。若显示「Qwen 预热中」，等它准备好再开口。
5. **查看或换一段对话。** 「识别与回复」会显示你的转写和助手回复，完成后可重播或下载语音。想换话题且不沿用当前对话上下文时，等本轮结束后点「新对话」。

如果点「样音测试」正常，但录不到声音，先检查浏览器的网站麦克风权限，再核对「麦克风」选择和「输入电平」。Mac 连不上时，先确认 Windows 服务仍在运行、两台电脑在同一局域网，并使用已信任证书的 HTTPS 地址。

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

第一个 PowerShell 窗口运行 `.\start-llama.ps1`，等待 [模型就绪](http://127.0.0.1:8001/health)。脚本默认选择第一个 NVIDIA Vulkan 设备，多显卡可传 `-Device Vulkan1` 等实际设备名。

第二个 PowerShell 窗口运行：

```powershell
$python312 = py -3.12 -c "import sys; print(sys.executable)"
.\start-qwen-realtime.ps1 -PythonPath $python312
```

按 [网页使用](#网页使用) 的步骤验证服务。准备使用 Mac 时，按 [Mac 局域网接入](mac-localvoice-setup.md) 配置 HTTPS 证书和防火墙。不要在路由器上转发端口或将网关公开到互联网。

## 测试与数据

```powershell
py -3.12 -X utf8 -m unittest discover
node --test web/test_voice_input.mjs web/test_interrupt.cjs
```

单元测试不需要模型、GPU 或麦克风。根目录下的 `test_*.cjs` 则是独立的 Playwright 集成测试：需要已启动的服务和 `playwright`（可运行 `npm install --no-save --package-lock=false playwright` 及 `npx playwright install chromium`），部分脚本还需通过 `VOICE_TEST_MIC_WAV` 指定本地测试 WAV。见 [连续对话验收](voice-stress-acceptance.md) 和 [TTS 对比](qwen-realtime-results.md)；其中的延迟是特定机器上的测量，不是跨硬件保证。

录音、转写、回复、模型、环境和证书凭据均不应进入 Git。运行时对话文件位于 `checks/`，局域网凭据位于 `.voice-lan/`。在 Mac 上使用时，录音会通过局域网传到 Windows 处理。浏览器内的 VAD 文件和各自的授权声明见 [`web/vad/`](web/vad/README.md)。
