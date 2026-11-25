using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class VoiceBuddy
{
    private const int DefaultPort = 8010;
    private static readonly string Root = AppDomain.CurrentDomain.BaseDirectory;
    private static readonly string Runtime = Path.Combine(Root, "runtime");
    private static readonly string AssetConfig = Path.Combine(Runtime, "voice-buddy-assets.txt");

    [STAThread]
    private static int Main(string[] args)
    {
        bool interactive = args.Length == 0;
        try
        {
            if (args.Length == 2 && args[0] == "--configure")
            {
                SaveAssetRoot(args[1]);
                return 0;
            }

            int port = DefaultPort;
            if (!interactive && (args.Length != 2 || args[0] != "--check" ||
                !int.TryParse(args[1], NumberStyles.None, CultureInfo.InvariantCulture, out port) ||
                port < 1 || port > 65535))
                throw new ArgumentException("Expected --configure <asset folder> or --check <port>.");

            string url = "http://127.0.0.1:" + port.ToString(CultureInfo.InvariantCulture) + "/";
            if (!EnsureService(port, interactive))
                return 1;
            if (interactive)
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            return 0;
        }
        catch (Exception error)
        {
            if (interactive)
                MessageBox.Show(error.Message, "语音 Buddy", MessageBoxButtons.OK, MessageBoxIcon.Error);
            else
                Console.Error.WriteLine(error.Message);
            return 1;
        }
    }

    private static bool EnsureService(int port, bool interactive)
    {
        using (var startup = new Mutex(false, @"Local\VoiceBuddy-Start-" + port))
        {
            bool acquired;
            try { acquired = startup.WaitOne(TimeSpan.FromSeconds(45)); }
            catch (AbandonedMutexException) { acquired = true; }
            if (!acquired)
                throw new TimeoutException("语音服务正在启动，请稍等后再打开。");
            try
            {
                string assets = FindAssetRoot(interactive);
                if (assets == null)
                    return false;
                StartService(assets, port);
                return true;
            }
            finally
            {
                startup.ReleaseMutex();
            }
        }
    }

    private static string FindAssetRoot(bool interactive)
    {
        string configured = Environment.GetEnvironmentVariable("LOCALVOICE_ASSET_ROOT");
        if (HasAssets(configured))
            return Path.GetFullPath(configured);
        if (File.Exists(AssetConfig))
        {
            configured = File.ReadAllText(AssetConfig, Encoding.UTF8).Trim();
            if (HasAssets(configured))
                return Path.GetFullPath(configured);
        }
        if (HasAssets(Root))
            return Root;
        if (!interactive)
            throw new DirectoryNotFoundException("未找到语音模型和样音目录。");

        using (var picker = new FolderBrowserDialog
        {
            Description = "选择已安装语音模型和样音的 LocalVoice 目录",
            ShowNewFolderButton = false
        })
        {
            if (picker.ShowDialog() != DialogResult.OK)
                return null;
            return SaveAssetRoot(picker.SelectedPath);
        }
    }

    private static bool HasAssets(string path)
    {
        return !string.IsNullOrWhiteSpace(path) &&
            File.Exists(Path.Combine(path, @"runtime\python\python.exe")) &&
            File.Exists(Path.Combine(path, @".venv-asr\Scripts\python.exe")) &&
            File.Exists(Path.Combine(path, @"models\Qwen3-ASR-0.6B\model.safetensors")) &&
            File.Exists(Path.Combine(path, @"checks\shanghai-question.wav"));
    }

    private static string SaveAssetRoot(string path)
    {
        string fullPath = Path.GetFullPath(path);
        if (!HasAssets(fullPath))
            throw new DirectoryNotFoundException("这个目录缺少 Python、语音识别模型或样音文件。");
        Directory.CreateDirectory(Runtime);
        File.WriteAllText(AssetConfig, fullPath, new UTF8Encoding(false));
        return fullPath;
    }

    private static void StartService(string assets, int port)
    {
        string log = Path.Combine(Runtime, "voice-buddy-" + port + ".log");
        var start = new ProcessStartInfo(
            Path.Combine(assets, @"runtime\python\python.exe"),
            "-X utf8 -m launcher.start_service " + port.ToString(CultureInfo.InvariantCulture))
        {
            WorkingDirectory = Root,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        start.EnvironmentVariables["LOCALVOICE_ASSET_ROOT"] = assets;
        using (var process = Process.Start(start))
        {
            if (process == null || !process.WaitForExit(42000) || process.ExitCode != 0)
                throw new InvalidOperationException("语音服务未能启动。请查看日志：\n" + log);
        }
    }
}
