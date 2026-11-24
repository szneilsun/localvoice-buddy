# Mac 局域网接入

Mac 只需要浏览器和麦克风。Windows 继续跑识别、问答和播报。两台电脑应在同一家庭局域网，避免使用隔离设备的访客 Wi-Fi。

访问地址为 `https://<Windows 局域网 IPv4>:8443/`。以下命令中的地址和网段是示例，请换成自己的值。

## Windows 一次性设置

先启动 [Windows 推理和网页服务](README.md#3-启动并验证)。在仓库根目录下用 PowerShell 7.6 启动网关：

```powershell
$python312 = py -3.12 -c "import sys; print(sys.executable)"
.\start-localvoice-lan.ps1 -LanIp 192.168.1.20 -ClientNetwork 192.168.1.0/24 -PythonPath $python312
```

再以管理员身份启动 PowerShell，从仓库根目录执行：

```powershell
$python312 = py -3.12 -c "import sys; print(sys.executable)"
.\enable-localvoice-firewall.ps1 -PythonPath $python312
```

规则只允许专用网络、客户端家庭网段、当前 Windows 地址和 TCP `8443`。如果知道 Mac 的 IPv4 地址，可以给同一命令加 `-MacAddress <Mac的IPv4>`，仅放行该电脑。不要在路由器上配置端口映射。

## Mac 一次性设置

1. 通过可信方式把 Windows 的 `.voice-lan/localvoice-ca.cer` 公共证书传到 Mac。不要传 `server-key.pem`，也不要把 `.voice-lan` 整个目录共享。证书的 SHA-256 可在 Windows 用 `Get-FileHash` 核对，在 Mac 用 `shasum -a 256` 核对。
2. 打开「钥匙串访问」，将证书导入登录钥匙串。找到 `LocalVoice LAN CA`，展开「信任」，将「安全套接层（SSL）」设为始终信任；不要跳过 TLS 证书错误。也可以在 Mac 终端运行：

```bash
security add-trusted-cert -r trustRoot -p ssl \
  -k "$HOME/Library/Keychains/login.keychain-db" \
  "$HOME/Downloads/localvoice-ca.cer"
```

3. 重新打开浏览器，访问 HTTPS 地址。用户名是 `voice`，密码在 Windows 的 `.voice-lan/access.json` 的 `password` 字段。登录后允许麦克风，先点「样音测试」，再开启「自动收音与打断」试说一句。

## 运行和排查

- 网关不会对局域网暴露 `8001` 或 `8010`。Windows 本机仍用 `http://127.0.0.1:8010/`。
- Windows 必须保持开机，且问答服务、网页协调器和 HTTPS 网关均在运行。Windows 重启后，先启动推理和网页服务，再用相同参数启动网关。该脚本不会重启模型，也不会自动修改防火墙。
- Mac 连不上时，先确认 Windows 网络设为「专用」、两台电脑在同一网段，并从 Mac 用 `nc -vz` 检查 Windows IPv4 的 `8443` 端口。连得上但证书报错时，核对地址、证书指纹和钥匙串信任。
- 建议在路由器给 Windows 留固定的 DHCP 地址。IP 改变后，需要停止网关，用新的 `-LanIp` 和 `-ReissueCertificate` 启动，更新放行规则，并在 Mac 信任新证书。

当前验证：Windows 通过其局域网地址完成了严格 TLS 校验、密码认证、自动收音和 Qwen 流式播报。远端 Mac 和防火墙实际通行还需确认。
