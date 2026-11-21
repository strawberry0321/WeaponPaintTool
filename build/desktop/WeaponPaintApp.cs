using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: AssemblyTitle("WeaponPaintTool 测试版")]
[assembly: AssemblyProduct("WeaponPaintTool")]
[assembly: AssemblyVersion("1.7.4.0")]
[assembly: AssemblyFileVersion("1.7.4.0")]
[assembly: AssemblyInformationalVersion("1.7.4-beta.1")]

internal static class Entry
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetDllDirectory(string path);

    [STAThread]
    private static void Main(string[] args)
    {
        string install = AppDomain.CurrentDomain.BaseDirectory;
        SetDllDirectory(Path.Combine(install, "app", "desktop"));
        AppDomain.CurrentDomain.AssemblyResolve += delegate(object sender, ResolveEventArgs request)
        {
            string name = new AssemblyName(request.Name).Name + ".dll";
            string path = Path.Combine(install, "app", "desktop", name);
            return File.Exists(path) ? Assembly.Load(File.ReadAllBytes(path)) : null;
        };
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        try
        {
            Run(install, args);
        }
        catch (Exception error)
        {
            string logs = Path.Combine(install, "data", "logs");
            Directory.CreateDirectory(logs);
            File.WriteAllText(Path.Combine(logs, "desktop-error.log"), error.ToString(), Encoding.UTF8);
            MessageBox.Show(error.Message, "WeaponPaintTool 测试版", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private static void Run(string install, string[] args)
    {
        var json = new JavaScriptSerializer();
        string data = Environment.GetEnvironmentVariable("HD2_WEAPON_DATA_DIR");
        if (String.IsNullOrWhiteSpace(data))
        {
            string settings = Path.Combine(install, "settings.json");
            if (File.Exists(settings))
            {
                var configuration = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(settings));
                if (configuration.ContainsKey("dataDirectory")) data = Convert.ToString(configuration["dataDirectory"]);
            }
        }
        data = Path.GetFullPath(Path.Combine(install, String.IsNullOrWhiteSpace(data) ? "data" : data));
        Directory.CreateDirectory(data);
        Directory.CreateDirectory(Path.Combine(install, "mods"));
        string identity;
        using (var sha = SHA256.Create())
        {
            identity = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(data.ToLowerInvariant()))).Replace("-", "").Substring(0, 24);
        }
        string eventName = "Local\\WeaponPaint11Focus_" + identity;
        bool first;
        using (var mutex = new Mutex(true, "Local\\WeaponPaint11_" + identity, out first))
        {
            if (!first)
            {
                using (var focus = EventWaitHandle.OpenExisting(eventName)) focus.Set();
                return;
            }
            try
            {
                Application.Run(new PaintWindow(install, data, eventName, args));
            }
            finally
            {
                mutex.ReleaseMutex();
            }
        }
    }
}

internal sealed class PaintWindow : Form
{
    private readonly string install;
    private readonly string data;
    private readonly string[] arguments;
    private readonly JavaScriptSerializer json = new JavaScriptSerializer();
    private readonly WebView2 web;
    private readonly Panel loading;
    private readonly Label message;
    private readonly Button retry;
    private readonly EventWaitHandle focusEvent;
    private readonly object logLock = new object();
    private Process service;
    private string address;
    private bool ready;
    private bool closing;
    private bool approvedClose;
    private bool disposed;

    internal PaintWindow(string installPath, string dataPath, string eventName, string[] args)
    {
        install = installPath;
        data = dataPath;
        arguments = args;
        Text = "WeaponPaintTool 1.7.4 测试版";
        BackColor = Color.FromArgb(246, 245, 242);
        AutoScaleMode = AutoScaleMode.Dpi;
        StartPosition = FormStartPosition.CenterScreen;
        Rectangle area = Screen.PrimaryScreen.WorkingArea;
        float dpiScale;
        using (var graphics = CreateGraphics()) dpiScale = graphics.DpiX / 96f;
        ClientSize = new Size(Math.Min((int)(1460 * dpiScale), area.Width - 70), Math.Min((int)(920 * dpiScale), area.Height - 90));
        MinimumSize = new Size(Math.Min((int)(1040 * dpiScale), area.Width), Math.Min((int)(690 * dpiScale), area.Height));
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        RestoreBoundsFromDisk();

        web = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = BackColor };
        Controls.Add(web);
        loading = new Panel { Dock = DockStyle.Fill, BackColor = BackColor };
        var stack = new TableLayoutPanel { AutoSize = true, ColumnCount = 1, Padding = new Padding(15) };
        string imagePath = Path.Combine(install, "app", "web", "app_icon.png");
        var avatar = new PictureBox
        {
            Size = new Size(86, 86),
            SizeMode = PictureBoxSizeMode.Zoom,
            Margin = new Padding(0, 0, 0, 20)
        };
        if (File.Exists(imagePath)) avatar.Image = Image.FromFile(imagePath);
        var title = new Label
        {
            Text = "武器涂装工具",
            Font = new Font("Microsoft YaHei UI", 22),
            ForeColor = Color.FromArgb(43, 48, 62),
            AutoSize = true,
            Margin = new Padding(0, 0, 0, 13)
        };
        message = new Label
        {
            Text = "正在打开工作台…",
            Font = new Font("Microsoft YaHei UI", 10),
            ForeColor = Color.FromArgb(76, 87, 111),
            AutoSize = true,
            MaximumSize = new Size(680, 230),
            Margin = new Padding(0, 0, 0, 16)
        };
        retry = new Button
        {
            Text = "重新加载",
            AutoSize = true,
            FlatStyle = FlatStyle.Flat,
            BackColor = Color.FromArgb(232, 237, 249),
            ForeColor = Color.FromArgb(58, 77, 123),
            Padding = new Padding(12, 6, 12, 6),
            Visible = false
        };
        retry.Click += async delegate { await StartStudio(); };
        stack.Controls.Add(avatar);
        stack.Controls.Add(title);
        stack.Controls.Add(message);
        stack.Controls.Add(retry);
        loading.Controls.Add(stack);
        loading.Resize += delegate
        {
            stack.Left = Math.Max(20, (loading.Width - stack.Width) / 2);
            stack.Top = Math.Max(20, (loading.Height - stack.Height) / 2);
        };
        Controls.Add(loading);
        loading.BringToFront();

        focusEvent = new EventWaitHandle(false, EventResetMode.AutoReset, eventName);
        var focusThread = new Thread(FocusLoop) { IsBackground = true };
        focusThread.Start();
        Shown += async delegate { await StartStudio(); };
        FormClosing += OnClosing;
        FormClosed += delegate
        {
            disposed = true;
            SaveBoundsToDisk();
            StopService();
            focusEvent.Set();
            web.Dispose();
            if (avatar.Image != null) avatar.Image.Dispose();
        };
    }

    private void FocusLoop()
    {
        while (true)
        {
            focusEvent.WaitOne();
            if (disposed) return;
            try
            {
                BeginInvoke((Action)delegate
                {
                    if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
                    Show();
                    Activate();
                    BringToFront();
                });
            }
            catch (InvalidOperationException) { return; }
        }
    }

    private async Task StartStudio()
    {
        ready = false;
        loading.Visible = true;
        loading.BringToFront();
        retry.Visible = false;
        message.Text = "正在准备武器与配件…";
        try
        {
            if (service == null || service.HasExited) StartService();
            address = await FindSession();
            if (web.CoreWebView2 == null)
            {
                var options = new CoreWebView2EnvironmentOptions();
                foreach (string argument in arguments)
                {
                    int port;
                    if (argument.StartsWith("--debug-port=") && Int32.TryParse(argument.Substring(13), out port))
                        options.AdditionalBrowserArguments = "--remote-debugging-port=" + port;
                }
                string profile = Path.Combine(data, "tmp", "webview-profile");
                var environment = await CoreWebView2Environment.CreateAsync(null, profile, options);
                await web.EnsureCoreWebView2Async(environment);
                web.CoreWebView2.Settings.IsStatusBarEnabled = false;
                web.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
                web.CoreWebView2.Settings.AreDevToolsEnabled = false;
                web.CoreWebView2.WebMessageReceived += OnWebMessage;
                web.CoreWebView2.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs request)
                {
                    Uri target;
                    if (!Uri.TryCreate(request.Uri, UriKind.Absolute, out target) || target.GetLeftPart(UriPartial.Authority) != new Uri(address).GetLeftPart(UriPartial.Authority))
                        request.Cancel = true;
                };
                web.CoreWebView2.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs request) { request.Handled = true; };
                web.CoreWebView2.ProcessFailed += delegate(object sender, CoreWebView2ProcessFailedEventArgs failure) { ShowFailure("模型显示进程已退出，可重新加载工作台。"); };
            }
            web.CoreWebView2.Navigate(address);
            DateTime deadline = DateTime.UtcNow.AddSeconds(60);
            while (!disposed && DateTime.UtcNow < deadline)
            {
                await Task.Delay(250);
                string result = await web.CoreWebView2.ExecuteScriptAsync("JSON.stringify({ready:window.__studioReady===true,error:window.__startupError||null})");
                string raw = json.Deserialize<string>(result);
                var state = json.Deserialize<Dictionary<string, object>>(raw);
                if (state["error"] != null) throw new Exception(Convert.ToString(state["error"]));
                if (!Convert.ToBoolean(state["ready"])) continue;
                ready = true;
                loading.Visible = false;
                web.Focus();
                if (Array.IndexOf(arguments, "--capture") >= 0)
                {
                    await Task.Delay(700);
                    using (var stream = File.Create(Path.Combine(data, "tmp", "desktop_ready.png")))
                        await web.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream);
                }
                return;
            }
            if (!disposed) throw new Exception("模型加载超时，可重新加载工作台。");
        }
        catch (Exception error)
        {
            if (!disposed) ShowFailure(error.Message);
        }
    }

    private void StartService()
    {
        string temporary = Path.Combine(data, "tmp");
        Directory.CreateDirectory(temporary);
        Directory.CreateDirectory(Path.Combine(data, "logs"));
        var start = new ProcessStartInfo
        {
            FileName = Path.Combine(install, "runtime", "python.exe"),
            Arguments = "-B -X utf8 \"" + Path.Combine(install, "app", "server.py") + "\" --no-browser",
            WorkingDirectory = install,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        start.EnvironmentVariables["HD2_WEAPON_DATA_DIR"] = data;
        start.EnvironmentVariables["TEMP"] = temporary;
        start.EnvironmentVariables["TMP"] = temporary;
        start.EnvironmentVariables["OPENBLAS_NUM_THREADS"] = "1";
        service = new Process { StartInfo = start };
        service.OutputDataReceived += RecordLog;
        service.ErrorDataReceived += RecordLog;
        service.Start();
        service.BeginOutputReadLine();
        service.BeginErrorReadLine();
    }

    private void RecordLog(object sender, DataReceivedEventArgs line)
    {
        if (line.Data == null) return;
        lock (logLock)
        {
            File.AppendAllText(Path.Combine(data, "logs", "startup.log"), line.Data + Environment.NewLine, Encoding.UTF8);
        }
    }

    private string ExpectedVersion()
    {
        // 版本号只在 app/studio_core.py 里维护，避免启动器与服务的版本号写死后互相不认。
        try
        {
            string source = File.ReadAllText(Path.Combine(install, "app", "studio_core.py"));
            Match match = Regex.Match(source, "VERSION\\s*=\\s*\"([0-9][0-9.]*)\"");
            if (match.Success) return match.Groups[1].Value;
        }
        catch (Exception) { }
        return null;
    }

    private async Task<string> FindSession()
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(40);
        string expected = ExpectedVersion();
        string path = Path.Combine(data, "tmp", "session.json");
        while (!disposed && DateTime.UtcNow < deadline)
        {
            try
            {
                var session = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
                string url = Convert.ToString(session["url"]);
                Uri uri = new Uri(url);
                if (uri.Scheme != "http" || uri.Host != "127.0.0.1") throw new InvalidDataException("工作台地址无效");
                using (var client = new WebClient())
                {
                    string response = await client.DownloadStringTaskAsync(new Uri(uri, "/api/health" + uri.Query));
                    var health = json.Deserialize<Dictionary<string, object>>(response);
                    string actual = Convert.ToString(health["version"]);
                    if ((expected == null || actual == expected) && !Convert.ToBoolean(health["closing"])) return url;
                }
            }
            catch (Exception error)
            {
                if (!(error is IOException || error is WebException || error is ArgumentException || error is KeyNotFoundException)) throw;
            }
            if (service.HasExited && service.ExitCode != 0) throw new Exception("服务启动失败，请查看 data\\logs\\startup.log。");
            await Task.Delay(200);
        }
        throw new Exception("工作台服务启动超时，请查看 data\\logs\\startup.log。");
    }

    private void OnWebMessage(object sender, CoreWebView2WebMessageReceivedEventArgs request)
    {
        Uri source;
        if (!Uri.TryCreate(request.Source, UriKind.Absolute, out source) ||
            source.GetLeftPart(UriPartial.Authority) != new Uri(address).GetLeftPart(UriPartial.Authority)) return;
        string command;
        try { command = request.TryGetWebMessageAsString(); }
        catch (ArgumentException) { return; }
        if (command == "studio-close-approved")
        {
            approvedClose = true;
            Close();
        }
        if (command == "studio-save-layout") SaveBoundsToDisk();
        if (command == "author-open-bilibili" || command == "author-open-github")
        {
            string url = command == "author-open-bilibili"
                ? "https://space.bilibili.com/437268141" : "https://github.com/strawberry0321/WeaponPaintTool";
            try
            {
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true, Verb = "open" });
                web.CoreWebView2.PostWebMessageAsJson(json.Serialize(new Dictionary<string, string>
                {
                    {"type", "author-link-opened"}, {"url", url}
                }));
            }
            catch (System.ComponentModel.Win32Exception)
            {
                web.CoreWebView2.PostWebMessageAsJson(json.Serialize(new Dictionary<string, string>
                {
                    {"type", "author-link-error"}, {"message", "无法启动系统浏览器。链接为 " + url}
                }));
            }
            return;
        }
        if (command == "stock-choose-folder")
        {
            using (var dialog = new FolderBrowserDialog())
            {
                dialog.Description = "请选择 Helldivers 2 安装目录";
                dialog.ShowNewFolderButton = false;
                if (dialog.ShowDialog(this) == DialogResult.OK)
                {
                    web.CoreWebView2.PostWebMessageAsJson(json.Serialize(new Dictionary<string, string>
                    {
                        {"type", "stock-folder"}, {"path", dialog.SelectedPath}
                    }));
                }
            }
        }
    }

    private async void OnClosing(object sender, FormClosingEventArgs request)
    {
        if (approvedClose || !ready || web.CoreWebView2 == null) return;
        request.Cancel = true;
        if (closing) return;
        closing = true;
        try
        {
            await web.CoreWebView2.ExecuteScriptAsync("window.studio.requestClose()");
        }
        catch (Exception error)
        {
            closing = false;
            MessageBox.Show("保存工作台失败，请使用保存工程后再次关闭。\r\n" + error.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            closing = false;
        }
    }

    private void StopService()
    {
        try
        {
            if (address != null)
            {
                Uri uri = new Uri(address);
                var request = (HttpWebRequest)WebRequest.Create(new Uri(uri, "/api/quit" + uri.Query));
                request.Method = "POST";
                request.ContentType = "application/json";
                request.Timeout = 3000;
                byte[] body = Encoding.UTF8.GetBytes("{}");
                request.ContentLength = body.Length;
                using (var stream = request.GetRequestStream()) stream.Write(body, 0, body.Length);
                using (var response = request.GetResponse()) { }
            }
            if (service != null && !service.HasExited && !service.WaitForExit(6000)) service.Kill();
        }
        catch (WebException) { }
        finally
        {
            if (service != null) service.Dispose();
        }
    }

    private void ShowFailure(string error)
    {
        ready = false;
        message.Text = error;
        retry.Visible = true;
        loading.Visible = true;
        loading.BringToFront();
    }

    private void SaveBoundsToDisk()
    {
        try
        {
            Rectangle bounds = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
            var state = new Dictionary<string, object>
            {
                { "x", bounds.X }, { "y", bounds.Y }, { "width", bounds.Width }, { "height", bounds.Height },
                { "maximized", WindowState == FormWindowState.Maximized }
            };
            Directory.CreateDirectory(Path.Combine(data, "workspace"));
            File.WriteAllText(Path.Combine(data, "workspace", "window.json"), json.Serialize(state), new UTF8Encoding(false));
        }
        catch (IOException) { }
    }

    private void RestoreBoundsFromDisk()
    {
        try
        {
            string path = Path.Combine(data, "workspace", "window.json");
            if (!File.Exists(path)) return;
            var state = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
            var bounds = new Rectangle(Convert.ToInt32(state["x"]), Convert.ToInt32(state["y"]), Convert.ToInt32(state["width"]), Convert.ToInt32(state["height"]));
            Rectangle area = Screen.FromRectangle(bounds).WorkingArea;
            bounds.Width = Math.Min(area.Width, Math.Max(MinimumSize.Width, bounds.Width));
            bounds.Height = Math.Min(area.Height, Math.Max(MinimumSize.Height, bounds.Height));
            bounds.X = Math.Max(area.Left, Math.Min(bounds.X, area.Right - bounds.Width));
            bounds.Y = Math.Max(area.Top, Math.Min(bounds.Y, area.Bottom - bounds.Height));
            StartPosition = FormStartPosition.Manual;
            Bounds = bounds;
            if (Convert.ToBoolean(state["maximized"])) WindowState = FormWindowState.Maximized;
        }
        catch (Exception error)
        {
            if (!(error is IOException || error is ArgumentException || error is KeyNotFoundException)) throw;
        }
    }
}
