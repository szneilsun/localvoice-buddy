# Mac 局域网接入

Mac 只需要浏览器和麦克风。Windows 继续跑识别、问答和播报。两台电脑应在同一家庭局域网，避免使用隔离设备的访客 Wi-Fi。

访问地址为 `https://<Windows 局域网 IPv4>:8443/`。以下命令中的地址和网段是示例，请换成自己的值。

## Windows 一次性设置

先启动 [Windows 推理和网页服务](README.md#windows-一键启动)。尚未配置网关时，在仓库根目录下用 PowerShell 7.6 生成局域网证书，并启动网关：

```powershell
$python312 = py -3.12 -c "import sys; print(sys.executable)"
.\start-localvoice-lan.ps1 -LanIp 192.168.1.20 -ClientNetwork 192.168.1.0/24 -PythonPath $python312
```

再以管理员身份启动 PowerShell，从仓库根目录执行：

```powershell
$python312 = py -3.12 -c "import sys; print(sys.executable)"
.\enable-localvoice-firewall.ps1 -PythonPath $python312
```

规则只允许专用网络、客户端家庭网段、当前 Windows 地址和 TCP `8443`。该入口无需登录，能连接的设备都可以使用 Windows 上选定的回复模型，只在受信网络使用。如果知道 Mac 的 IPv4 地址，可以给同一命令加 `-MacAddress <Mac的IPv4>`，仅放行该电脑。不要在路由器上配置端口映射。

## Mac 一次性设置

1. 从 Windows 语音页面的「使用说明与连接端口」下载公用 CA 证书，或直接取 `.voice-lan/localvoice-ca.cer`，通过可信方式传到 Mac。不要传 `server-key.pem`，也不要把 `.voice-lan` 整个目录共享。证书的 SHA-256 可在 Windows 用 `Get-FileHash` 核对，在 Mac 用 `shasum -a 256` 核对。
2. 打开「钥匙串访问」，将证书导入登录钥匙串。找到 `LocalVoice LAN CA`，展开「信任」，将「安全套接层（SSL）」设为始终信任；不要跳过 TLS 证书错误。也可以在 Mac 终端运行：

```bash
security add-trusted-cert -r trustRoot -p ssl \
  -k "$HOME/Library/Keychains/login.keychain-db" \
  "$HOME/Downloads/localvoice-ca.cer"
```

3. 重新打开浏览器，直接访问 HTTPS 地址，无需用户名或密码。允许麦克风，先点「样音测试」，再开启「自动收音与打断」试说一句。回复模型和 API Key 只在 Windows 本机网页设置。

## 运行和排查

- 网关不会对局域网暴露 `8001` 或 `8010`。Windows 本机仍用 `http://127.0.0.1:8010/`。
- Windows 必须保持开机。完成一次性配置后，Windows 重启只需双击「语音 Buddy」；它会启动网页服务及已配置的 HTTPS 网关，不会自动修改防火墙。使用远程回复模型时，不需要另启 llama.cpp。
- 从旧版更新后，停止旧网关进程再启动新版。已有 CA 证书可继续使用；不要为去掉登录而重新签发证书。
- Mac 连不上时，先确认 Windows 网络设为「专用」、两台电脑在同一网段，并从 Mac 用 `nc -vz` 检查 Windows IPv4 的 `8443` 端口。连得上但证书报错时，核对地址、证书指纹和钥匙串信任。
- 建议在路由器给 Windows 留固定的 DHCP 地址。IP 改变后，需要停止网关，用新的 `-LanIp` 和 `-ReissueCertificate` 启动，更新放行规则，并在 Mac 信任新证书。
