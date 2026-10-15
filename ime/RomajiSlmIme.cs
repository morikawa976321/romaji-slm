// romaji-slm の常駐入力（トレイアプリ）。docs/IME.md。
//
// - 低レベルキーボードフックで打鍵を受け、Composer（状態機械）に渡す。フックの中では通信も描画もしない
//   （フックの処理が遅いと Windows が黙ってフックを外す。LowLevelHooksTimeout）。効果は BeginInvoke で後から実行する。
// - 変換は ime/server.py（Python、127.0.0.1）に HTTP で頼む。サーバはこのアプリが起動し、ジョブオブジェクトで
//   このアプリと一緒に終わらせる。
// - 確定した文は SendInput（KEYEVENTF_UNICODE）で 1 回で送り込む。アプリ側の文字を消すことはしない。
// - 他のアプリが送り込んだ打鍵（LLKHF_INJECTED）は扱わない（音声入力などの貼り付けを打鍵列として拾わない）。
//
// 起動: RomajiSlmIme.exe [--root <app のフォルダ>] [--selftest <打鍵列> <結果ファイル>]

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace RomajiSlm
{
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            string root = null, selftest = null, selfOut = null;
            for (int i = 0; i < args.Length; i++)
            {
                if (args[i] == "--root" && i + 1 < args.Length) root = args[++i];
                else if (args[i] == "--selftest" && i + 2 < args.Length) { selftest = args[++i]; selfOut = args[++i]; }
            }
            string exeDir = Path.GetDirectoryName(Application.ExecutablePath);
            if (root == null) root = Path.Combine(exeDir, "app");
            string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "romaji-slm");
            Directory.CreateDirectory(data);

            Native.SetProcessDPIAware();
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            if (selftest != null)
                return SelfTest.Run(root, data, selftest, selfOut);

            // UI スレッドの例外で常駐ごと落とさない（既定はダイアログを出す）。記録して続ける
            string errLog = Path.Combine(data, "ime-error.log");
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
            Application.ThreadException += (s, e) =>
            {
                try { File.AppendAllText(errLog, DateTime.Now.ToString("s") + " " + e.Exception + "\r\n"); } catch { }
            };

            bool created;
            using (var mutex = new Mutex(true, "Local\\romaji-slm-ime", out created))
            {
                if (!created) return 0;   // 二重起動しない
                Application.Run(new App(root, data));
            }
            return 0;
        }
    }

    sealed class Config
    {
        public readonly string Path;
        public bool Enabled;
        public string Device = "auto";
        public int Port = 8767;

        public Config(string path)
        {
            Path = path;
            if (!File.Exists(path)) return;
            foreach (var line in File.ReadAllLines(path, Encoding.UTF8))
            {
                int eq = line.IndexOf('=');
                if (eq < 0 || line.TrimStart().StartsWith("#")) continue;
                string k = line.Substring(0, eq).Trim(), v = line.Substring(eq + 1).Trim();
                if (k == "enabled") Enabled = v == "1";
                else if (k == "device") Device = v;
                else if (k == "port") int.TryParse(v, out Port);
            }
        }

        public void Save()
        {
            File.WriteAllText(Path,
                "# romaji-slm 常駐入力の設定（docs/IME.md）。変えたらトレイの「終了」→ 起動し直す\r\n" +
                "enabled=" + (Enabled ? "1" : "0") + "\r\n" +
                "# device: auto（GPU があれば GPU）/ cuda / cpu\r\n" +
                "device=" + Device + "\r\n" +
                "port=" + Port + "\r\n", new UTF8Encoding(false));
        }
    }

    /// 変換サーバ（python -m ime.server）の起動・監視・要求
    sealed class Server : IDisposable
    {
        readonly string root, log;
        readonly Config cfg;
        Process proc;
        IntPtr job;
        volatile bool disposed;
        readonly object gate = new object();
        public event Action<bool, string> StateChanged;   // (ready, 説明)
        public string Describe = "起動中";

        public Server(string root, string data, Config cfg)
        {
            this.root = root; this.cfg = cfg;
            log = System.IO.Path.Combine(data, "server.log");
            job = Native.CreateKillOnCloseJob();
        }

        public void Start()
        {
            var t = new Thread(Watch) { IsBackground = true, Name = "server-watch" };
            t.Start();
        }

        bool PortOpen()
        {
            try
            {
                using (var c = new TcpClient())
                {
                    var ar = c.BeginConnect("127.0.0.1", cfg.Port, null, null);
                    if (!ar.AsyncWaitHandle.WaitOne(300)) return false;
                    c.EndConnect(ar);
                    return true;
                }
            }
            catch { return false; }
        }

        void Spawn()
        {
            string py = System.IO.Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
            if (!File.Exists(py)) throw new FileNotFoundException("venv が無い: " + py);
            var psi = new ProcessStartInfo(py, "-m ime.server --port " + cfg.Port + " --device " + cfg.Device +
                                           " --log \"" + log + "\"")
            {
                WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
            };
            // 終了処理（Dispose）と競合しないよう、起動とジョブへの割り当てを 1 つの錠の中で行う。
            // 終了後に起動してしまった・ジョブに入れられなかったプロセスはその場で止める（孤児にしない）
            lock (gate)
            {
                if (disposed) return;
                proc = Process.Start(psi);
                if (job == IntPtr.Zero || !Native.AssignProcessToJobObject(job, proc.Handle))
                {
                    try { proc.Kill(); } catch { }
                    throw new InvalidOperationException("変換サーバをジョブに入れられなかった");
                }
            }
        }

        void Watch()
        {
            int restarts = 0;
            while (!disposed)
            {
                try
                {
                    if (!PortOpen() && (proc == null || proc.HasExited))
                    {
                        if (proc != null && restarts++ >= 3) { Notify(false, "変換サーバが落ちました（" + log + "）"); return; }
                        Notify(false, "変換サーバを起動中");
                        Spawn();
                    }
                    string h = Get("/health", 180000);
                    Describe = "準備完了（" + h.Trim() + "）";
                    Notify(true, Describe);
                    // 準備ができたあとは 5 秒ごとに生存を確かめる
                    while (!disposed)
                    {
                        Thread.Sleep(5000);
                        try { Get("/health", 3000); }
                        catch
                        {
                            if (proc != null && !proc.HasExited) continue;   // 変換中で応答が遅いだけ
                            break;
                        }
                    }
                    if (!disposed) Notify(false, "変換サーバが止まりました。起動し直します");
                }
                catch (Exception ex)
                {
                    Notify(false, "変換サーバ: " + ex.Message);
                    Thread.Sleep(3000);
                }
            }
        }

        void Notify(bool ready, string msg)
        {
            Describe = msg;
            var h = StateChanged;
            if (h != null) h(ready, msg);
        }

        string Get(string path, int timeoutMs)
        {
            var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + cfg.Port + path);
            req.Proxy = null;   // 既定のプロキシ自動検出は 1 回目に数百 ms かかる
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            using (var r = (HttpWebResponse)req.GetResponse())
            using (var s = new StreamReader(r.GetResponseStream(), Encoding.UTF8))
                return s.ReadToEnd();
        }

        /// 変換。戻り値は (候補, サーバ側の ms)。失敗は例外
        public string[] Convert(string romaji, string context, out double ms)
        {
            var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + cfg.Port + "/convert");
            req.Proxy = null;
            req.Method = "POST";
            req.Timeout = 15000;
            req.ContentType = "text/plain; charset=utf-8";
            byte[] body = Encoding.UTF8.GetBytes(romaji + "\n" + context.Replace("\r", "").Replace("\n", " "));
            req.ContentLength = body.Length;
            using (var s = req.GetRequestStream()) s.Write(body, 0, body.Length);
            using (var r = (HttpWebResponse)req.GetResponse())
            using (var s = new StreamReader(r.GetResponseStream(), Encoding.UTF8))
            {
                var lines = s.ReadToEnd().Split('\n');
                double.TryParse(lines[0], out ms);
                return lines.Skip(1).Where(x => x.Length > 0).ToArray();
            }
        }

        public void Dispose()
        {
            lock (gate)
            {
                disposed = true;
                // ジョブを閉じると、uv の起動役（pythonw）とその子の本体の両方が終わる（KILL_ON_JOB_CLOSE）
                if (job != IntPtr.Zero) { Native.CloseHandle(job); job = IntPtr.Zero; }
                try { if (proc != null && !proc.HasExited) proc.Kill(); } catch { }
            }
        }
    }

    /// 使用状況の数（打った文字列は持たない）。トレイの「使用状況をコピー」で本人が貼れる形にする
    sealed class Usage
    {
        readonly Dictionary<string, int> counts = new Dictionary<string, int>();
        readonly Dictionary<string, int> apps = new Dictionary<string, int>();
        readonly List<double> wall = new List<double>();
        readonly DateTime since = DateTime.Now;
        readonly string path;
        public Usage(string path) { this.path = path; }

        public void Add(string name, double v)
        {
            int n; counts.TryGetValue(name, out n); counts[name] = n + (int)v;
        }
        public void App(string exe) { int n; apps.TryGetValue(exe, out n); apps[exe] = n + 1; }
        public void Latency(double ms) { wall.Add(ms); if (wall.Count > 500) wall.RemoveAt(0); }

        static double Pct(List<double> xs, double p)
        {
            if (xs.Count == 0) return 0;
            var s = xs.OrderBy(x => x).ToList();
            return s[Math.Min(s.Count - 1, (int)(p * s.Count))];
        }

        public string Text(string server)
        {
            Func<string, int> c = k => { int n; counts.TryGetValue(k, out n); return n; };
            var sb = new StringBuilder();
            sb.AppendLine("【romaji-slm 常駐入力の使用状況】" + since.ToString("yyyy-MM-dd HH:mm") + " から");
            sb.AppendLine("確定: 1位 " + c("commit_first") + " / 別候補 " + c("commit_other") + " / 打鍵列のまま " + c("commit_raw") +
                          "　取り消し(Esc) " + c("cancel") + "　変換の失敗 " + c("fail"));
            sb.AppendLine("変換の待ち（要求から応答まで）: 中央値 " + Pct(wall, 0.5).ToString("0") + " ms / 90% " +
                          Pct(wall, 0.9).ToString("0") + " ms（" + wall.Count + " 件）");
            sb.AppendLine("確定したアプリ: " + string.Join("、", apps.OrderByDescending(kv => kv.Value).Select(kv => kv.Key + " " + kv.Value)));
            sb.AppendLine("サーバ: " + server);
            return sb.ToString();
        }

        public void Save(string server)
        {
            try { File.WriteAllText(path, Text(server), new UTF8Encoding(false)); } catch { }
        }
    }

    /// 未確定の文と候補を出す浮き窓。フォーカスを奪わない（WS_EX_NOACTIVATE）
    sealed class Popup : Form
    {
        readonly Composer comp;
        readonly Font big = new Font("Yu Gothic UI", 15f), small = new Font("Yu Gothic UI", 9.5f), list = new Font("Yu Gothic UI", 11f);
        readonly bool dark;
        string flash;
        public Func<string> ServerStatus = () => "";

        public Popup(Composer comp)
        {
            this.comp = comp;
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            StartPosition = FormStartPosition.Manual;
            TopMost = true;
            DoubleBuffered = true;
            dark = Native.AppsUseDarkTheme();
            BackColor = dark ? Color.FromArgb(36, 36, 34) : Color.FromArgb(255, 255, 252);
        }

        protected override bool ShowWithoutActivation { get { return true; } }

        protected override CreateParams CreateParams
        {
            get
            {
                var cp = base.CreateParams;
                cp.ExStyle |= 0x08000000 | 0x00000080 | 0x00000008;   // NOACTIVATE | TOOLWINDOW | TOPMOST
                return cp;
            }
        }

        protected override void WndProc(ref Message m)
        {
            const int WM_MOUSEACTIVATE = 0x21, MA_NOACTIVATE = 3;
            if (m.Msg == WM_MOUSEACTIVATE) { m.Result = (IntPtr)MA_NOACTIVATE; return; }
            base.WndProc(ref m);
        }

        public void Flash(string text) { flash = text; }
        public void EndFlash() { flash = null; }

        List<Tuple<string, Font, Color, bool>> Lines()
        {
            var fg = dark ? Color.FromArgb(236, 235, 230) : Color.FromArgb(29, 29, 27);
            var muted = dark ? Color.FromArgb(156, 155, 149) : Color.FromArgb(107, 107, 102);
            var accent = dark ? Color.FromArgb(109, 156, 240) : Color.FromArgb(47, 111, 222);
            var err = dark ? Color.FromArgb(229, 115, 103) : Color.FromArgb(192, 57, 43);
            var L = new List<Tuple<string, Font, Color, bool>>();
            if (comp.FieldPending) return L;   // パスワード欄かを確かめ終わるまで、打った文字を画面に出さない
            if (!comp.Composing)
            {
                if (comp.PendingCommits > 0) L.Add(Tuple.Create("変換中…", small, muted, false));
                else if (flash != null) L.Add(Tuple.Create(flash, list, fg, false));
                return L;
            }
            var cands = comp.Candidates;
            string sub = comp.Buffer;
            if (!comp.ServerReady) sub += "　" + ServerStatus();
            else if (comp.Converting && cands == null) sub += "　…";
            if (comp.Phase == Phase.Candidates && cands != null)
            {
                L.Add(Tuple.Create(sub, small, muted, false));
                for (int i = 0; i < cands.Length; i++)
                    L.Add(Tuple.Create((i + 1) + "  " + cands[i], i == comp.CandIndex ? big : list, i == comp.CandIndex ? accent : fg, i == comp.CandIndex));
            }
            else
            {
                L.Add(Tuple.Create(cands != null ? cands[0] : comp.Buffer, big, cands != null ? fg : muted, false));
                L.Add(Tuple.Create(sub, small, muted, false));
            }
            string status = comp.Status;
            if (string.IsNullOrEmpty(status) && comp.Error != null) status = "変換できませんでした（" + comp.Error + "）";
            if (!string.IsNullOrEmpty(status)) L.Add(Tuple.Create(status, small, err, false));
            return L;
        }

        public void Refresh(Point anchor)
        {
            var lines = Lines();
            if (lines.Count == 0) { if (Visible) Hide(); return; }
            int w = 0, h = 0;
            foreach (var l in lines)
            {
                var sz = TextRenderer.MeasureText(l.Item1, l.Item2);
                w = Math.Max(w, sz.Width);
                h += sz.Height + 2;
            }
            var size = new Size(Math.Min(w + 24, 900), h + 14);
            var scr = Screen.FromPoint(anchor).WorkingArea;
            int x = Math.Max(scr.Left, Math.Min(anchor.X, scr.Right - size.Width));
            int y = anchor.Y + size.Height > scr.Bottom ? anchor.Y - size.Height - 40 : anchor.Y;
            Bounds = new Rectangle(x, Math.Max(scr.Top, y), size.Width, size.Height);
            if (!Visible) { Show(); Native.SetWindowPos(Handle, new IntPtr(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010); }
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
            using (var pen = new Pen(dark ? Color.FromArgb(70, 70, 66) : Color.FromArgb(200, 199, 192)))
                g.DrawRectangle(pen, 0, 0, Width - 1, Height - 1);
            int y = 7;
            foreach (var l in Lines())
            {
                var sz = TextRenderer.MeasureText(l.Item1, l.Item2);
                if (l.Item4)
                    using (var b = new SolidBrush(dark ? Color.FromArgb(52, 58, 72) : Color.FromArgb(232, 239, 252)))
                        g.FillRectangle(b, 4, y - 1, Width - 8, sz.Height + 2);
                TextRenderer.DrawText(g, l.Item1, l.Item2, new Point(12, y), l.Item3);
                y += sz.Height + 2;
            }
        }
    }

    sealed class App : ApplicationContext
    {
        readonly Composer comp = new Composer();
        readonly Config cfg;
        readonly Server server;
        readonly Usage usage;
        readonly Popup popup;
        readonly NotifyIcon tray;
        readonly ToolStripMenuItem enabledItem, serverItem;
        readonly System.Windows.Forms.Timer timer;
        readonly Stopwatch clock = Stopwatch.StartNew();
        readonly Dictionary<int, long> sentAt = new Dictionary<int, long>();
        readonly Icon iconJa, iconEn, iconOff;
        readonly Native.HookProc hookProc;
        readonly Native.WinEventProc fgProc, focusProc;
        IntPtr hook, fgHook, focusHook;
        Point anchor;
        readonly bool[] keyDown = new bool[256];
        uint batchTick;   // 最後に SendBatch した時刻（GetTickCount。KBDLLHOOKSTRUCT.time と同じ時計）
        bool wasComposing, caps, posted, caretAnchor;
        long flashUntil, fieldDeadline;
        int fieldSeq;
        const int FieldCheckMs = 300;   // UI Automation が応答しないアプリでは、この時間でパスワード欄ではないとみなす

        long Now { get { return clock.ElapsedMilliseconds; } }

        public App(string root, string data)
        {
            cfg = new Config(Path.Combine(data, "ime.ini"));
            if (!File.Exists(cfg.Path)) cfg.Save();
            usage = new Usage(Path.Combine(data, "usage.txt"));
            popup = new Popup(comp);
            var unused = popup.Handle;   // BeginInvoke の宛先にするため、表示せずにハンドルを作る

            iconJa = Native.TextIcon("あ", Color.FromArgb(47, 111, 222), Color.White);
            iconEn = Native.TextIcon("A", Color.FromArgb(90, 90, 86), Color.White);
            iconOff = Native.TextIcon("M", Color.FromArgb(0, 0, 0, 0), Color.FromArgb(150, 150, 150));

            enabledItem = new ToolStripMenuItem("romaji-slm で日本語を入力する（Ctrl+Alt+Space）", null, (s, e) => Toggle());
            serverItem = new ToolStripMenuItem("変換サーバ: 起動中") { Enabled = false };
            var menu = new ContextMenuStrip();
            menu.Items.Add(enabledItem);
            menu.Items.Add(serverItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("使用状況をコピー", null, (s, e) =>
            {
                string msg = "使用状況をコピーしました";
                try { Clipboard.SetText(usage.Text(server.Describe)); }
                catch (Exception ex) { msg = "コピーできませんでした（" + ex.GetType().Name + "）。usage.txt にもあります"; }
                tray.ShowBalloonTip(2000, "romaji-slm", msg, ToolTipIcon.None);
            });
            menu.Items.Add("設定とログのフォルダを開く", null, (s, e) => Process.Start("explorer.exe", "\"" + data + "\""));
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("終了", null, (s, e) => ExitThread());
            tray = new NotifyIcon { ContextMenuStrip = menu, Visible = true };
            tray.MouseClick += (s, e) => { if (e.Button == MouseButtons.Left) Toggle(); };

            server = new Server(root, data, cfg);
            server.StateChanged += (ready, msg) => popup.BeginInvoke((Action)(() =>
            {
                serverItem.Text = "変換サーバ: " + msg;
                comp.SetServerReady(ready, Now);
                Run();
            }));
            popup.ServerStatus = () => server.Describe;
            server.Start();

            caps = Control.IsKeyLocked(Keys.CapsLock);
            hookProc = HookCallback;
            hook = Native.SetWindowsHookEx(13, hookProc, Native.GetModuleHandle(null), 0);   // WH_KEYBOARD_LL
            fgProc = (h, ev, hwnd, idObj, idChild, thread, time) => { comp.OnForeground(hwnd, Now); Run(); };
            fgHook = Native.SetWinEventHook(3, 3, IntPtr.Zero, fgProc, 0, 0, 0);   // EVENT_SYSTEM_FOREGROUND, OUTOFCONTEXT
            // 同じ窓の中で別の欄に移ったことを知る（確定待ちの文を、移った先へ送らないため）。自分のプロセスの通知は除く
            focusProc = (h, ev, hwnd, idObj, idChild, thread, time) =>
            {
                comp.OnFocus(hwnd.ToInt64() + ":" + idObj + ":" + idChild, Now);
                Run();
            };
            focusHook = Native.SetWinEventHook(0x8005, 0x8005, IntPtr.Zero, focusProc, 0, 0, 0x0002);   // EVENT_OBJECT_FOCUS, SKIPOWNPROCESS
            comp.OnForeground(Native.GetForegroundWindow(), Now);

            if (cfg.Enabled) comp.SetEnabled(true, Now);
            comp.TakeEffects();   // 起動時に MS-IME を開閉しない
            UpdateTray();

            timer = new System.Windows.Forms.Timer { Interval = 50 };
            ResyncModifiers();
            timer.Tick += (s, e) =>
            {
                if (!comp.Holding) ResyncModifiers();   // 離鍵を取りこぼしても（管理者権限の窓の上で離した等）修飾キーが押されたままにならない
                if (fieldDeadline != 0 && Now > fieldDeadline) { fieldDeadline = 0; comp.OnFieldChecked(fieldSeq, false, Now); }
                comp.Tick(Now);
                Run();
            };
            comp.RequireFieldCheck = true;
            // UI Automation の初回の呼び出しは初期化で遅く、欄の確認（0.3 秒で打ち切り）に間に合わないことがあるので、起動時に 1 回済ませる
            ThreadPool.QueueUserWorkItem(_ => { bool pw; Point q; Native.FocusedElement(out pw, out q); });
            timer.Start();
        }

        /// 確定待ちでない（修飾キーを積んでいない）間は、OS の状態が本人の押している状態と一致する
        void ResyncModifiers()
        {
            foreach (int vk in new[] { 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C })
                keyDown[vk] = Native.Down(vk);
        }

        void Toggle()
        {
            comp.SetEnabled(comp.Mode == Mode.Disabled, Now);
            Run();
        }

        IntPtr HookCallback(int nCode, IntPtr wParam, IntPtr lParam)
        {
            // フックの中の例外はネイティブの境界を越えてプロセスを落とすので、ここで止めて素通しにする
            if (nCode >= 0) try
            {
                var k = (Native.KBDLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(Native.KBDLLHOOKSTRUCT));
                bool injected = (k.flags & 0x10) != 0;
                int msg = wParam.ToInt32();
                bool down = msg == 0x100 || msg == 0x104;
                // 修飾キーの状態は自分で追う。確定待ちの間は修飾キーも積んで OS に渡さないので、GetAsyncKeyState は使えない。
                // 他のアプリが送り込んだ修飾キー（貼り付けの Ctrl+V など）も OS の状態を変えるので追う。自分の送り直しは除く
                if (k.dwExtraInfo != Native.Magic && k.vkCode < 256) keyDown[k.vkCode] = down;
                if (!injected)
                {
                    if (down && k.vkCode == Composer.VK_CAPITAL) caps = !caps;
                    var m = new Mods(keyDown[0xA0] || keyDown[0xA1], keyDown[0xA2] || keyDown[0xA3],
                                     keyDown[0xA4] || keyDown[0xA5], keyDown[0x5B] || keyDown[0x5C], caps);
                    // こちらがまとめて送った（SendBatch）時刻より前に押されていたキーは、入力の列では送った分より前にいる。
                    // 素通しにすると確定文を追い越すので、Composer の待ち行列に積ませて後ろに送り直す
                    bool late = batchTick != 0 && unchecked((int)(k.time - batchTick)) < 0;
                    bool swallow = comp.OnKey((int)k.vkCode, down, m, Now, Native.GetForegroundWindow(),
                                              (int)k.scanCode, (k.flags & 0x01) != 0, late);
                    if (!posted) { posted = true; popup.BeginInvoke((Action)Run); }
                    if (swallow) return new IntPtr(1);
                }
            }
            catch { }
            return Native.CallNextHookEx(hook, nCode, wParam, lParam);
        }

        /// Composer が積んだ効果を実行する（フックの外で）
        void Run()
        {
            posted = false;
            bool redraw = false;
            for (int round = 0; round < 8; round++)
            {
                var effs = comp.TakeEffects();
                if (effs.Count == 0) break;
                foreach (var e in effs)
                {
                    var rep = e as ReplayEffect;
                    var req = e as RequestEffect;
                    var ime = e as SetImeEffect;
                    var mode = e as ModeEffect;
                    var stat = e as StatEffect;
                    if (e is DeliverEffect) Deliver();
                    else if (rep != null) Native.SendBatch(rep.Keys.Select(k => new Delivery { IsKey = true, Key = k }).ToList());
                    else if (req != null) StartRequest(req);
                    else if (ime != null)
                    {
                        // 宛先はいま決める（遅れて開閉すると、その間に替わった別の窓に効く）。既定の IME 窓はスレッドごと
                        IntPtr imeWnd = Native.ImmGetDefaultIMEWnd(ime.Window);
                        bool open = ime.Open;
                        if (imeWnd != IntPtr.Zero) ThreadPool.QueueUserWorkItem(_ => Native.SetImeOpen(imeWnd, open));
                    }
                    else if (mode != null) OnMode(mode.Mode);
                    else if (stat != null) usage.Add(stat.Name, stat.Value);
                    else if (e is CheckFieldEffect) CheckField(((CheckFieldEffect)e).Seq, ((CheckFieldEffect)e).DelayMs);
                    else if (e is AltMaskEffect) Native.SendMask();
                    else if (e is RedrawEffect) redraw = true;
                }
                if (effs.Any(x => x is StatEffect)) usage.Save(server.Describe);
            }
            if (flashUntil != 0 && Now > flashUntil) { flashUntil = 0; popup.EndFlash(); redraw = true; }
            if (redraw) Redraw();
        }

        /// 待ち行列の先頭から送れるものを 1 回の SendInput で送る（間に本人の打鍵が割り込まないように）。
        /// 送る直前に前面の窓を確かめ、送れなければ Composer が打鍵列を欄へ戻す
        void Deliver()
        {
            var ds = comp.TakeReady(Now);
            if (ds.Count == 0) return;
            var fg = Native.GetForegroundWindow();
            string why = null;
            if (ds.Any(d => !d.IsKey && d.Window != fg))
                why = "確定の前に窓が替わったので入力していません。Enter で今の窓に入力します";
            else if (!Native.SendBatch(ds))
                why = "この窓には入力できませんでした（管理者権限の窓など）。Esc で捨てます";
            batchTick = unchecked((uint)Environment.TickCount);
            if (why != null) { comp.DeliveryFailed(why, Now); return; }
            if (ds.Any(d => !d.IsKey)) usage.App(Native.ForegroundExe());
            comp.Delivered(Now);
        }

        void Redraw()
        {
            if (comp.Composing && !wasComposing) StartAnchor();
            wasComposing = comp.Composing;
            if (!comp.Composing && comp.PendingCommits == 0 && flashUntil == 0) { if (popup.Visible) popup.Hide(); return; }
            popup.Refresh(anchor);
        }

        /// 浮き窓の位置: キャレット（取れるアプリだけ）→ UI Automation のフォーカス欄（CheckField）→ 前面ウィンドウの下寄り
        void StartAnchor()
        {
            Point p;
            caretAnchor = Native.CaretBelow(out p);
            anchor = caretAnchor ? p : Native.WindowFallback();
        }

        /// フォーカスの欄がパスワード欄かを UI Automation で確かめる（別スレッド。応答しなければ時間切れで「違う」）
        void CheckField(int seq, int delayMs)
        {
            fieldSeq = seq;
            fieldDeadline = Now + delayMs + FieldCheckMs;
            ThreadPool.QueueUserWorkItem(_ =>
            {
                if (delayMs > 0) Thread.Sleep(delayMs);   // 欄を移すキーを送った直後。アプリが移し終えるのを待つ
                bool password; Point q;
                bool ok = Native.FocusedElement(out password, out q);
                popup.BeginInvoke((Action)(() =>
                {
                    if (seq != fieldSeq || fieldDeadline == 0) return;
                    fieldDeadline = 0;
                    if (ok && !caretAnchor && q != Point.Empty) anchor = q;
                    comp.OnFieldChecked(seq, ok && password, Now);
                    Run();
                }));
            });
        }

        void StartRequest(RequestEffect r)
        {
            sentAt[r.Id] = Now;
            ThreadPool.QueueUserWorkItem(_ =>
            {
                string[] cands = null; string err = null; double ms;
                try { cands = server.Convert(r.Romaji, r.Context, out ms); }
                catch (WebException ex) { err = ex.Response != null ? ((HttpWebResponse)ex.Response).StatusCode.ToString() : ex.Status.ToString(); }
                catch (Exception ex) { err = ex.GetType().Name; }
                popup.BeginInvoke((Action)(() =>
                {
                    long t0;
                    if (sentAt.TryGetValue(r.Id, out t0)) { usage.Latency(Now - t0); sentAt.Remove(r.Id); }
                    comp.OnResult(r.Id, cands, err, Now);
                    Run();
                }));
            });
        }

        void OnMode(Mode m)
        {
            cfg.Enabled = m != Mode.Disabled;
            try { cfg.Save(); } catch { }
            UpdateTray();
            if (!comp.Composing)
            {
                popup.Flash(m == Mode.Japanese ? "あ  romaji-slm" : m == Mode.English ? "A  英字" : "MS-IME に戻しました");
                flashUntil = Now + 700;
                StartAnchor();
            }
        }

        void UpdateTray()
        {
            var m = comp.Mode;
            tray.Icon = m == Mode.Japanese ? iconJa : m == Mode.English ? iconEn : iconOff;
            tray.Text = m == Mode.Disabled ? "romaji-slm: 使っていない（MS-IME）。Ctrl+Alt+Space で使う"
                      : m == Mode.Japanese ? "romaji-slm: 日本語（Shift+Space で英字）"
                      : "romaji-slm: 英字（Shift+Space で日本語）";
            enabledItem.Checked = m != Mode.Disabled;
        }

        protected override void ExitThreadCore()
        {
            if (hook != IntPtr.Zero) Native.UnhookWindowsHookEx(hook);
            if (fgHook != IntPtr.Zero) Native.UnhookWinEvent(fgHook);
            if (focusHook != IntPtr.Zero) Native.UnhookWinEvent(focusHook);
            timer.Stop();
            tray.Visible = false;
            usage.Save(server.Describe);
            server.Dispose();
            base.ExitThreadCore();
        }
    }

    /// フックも送り込みもせず、本物のサーバで「打鍵 → Enter → 確定文」を通す（本人の入力を奪わずに配線を確かめる）
    static class SelfTest
    {
        public static int Run(string root, string data, string romaji, string outPath)
        {
            var cfg = new Config(Path.Combine(data, "ime.ini"));
            var comp = new Composer();
            var clock = Stopwatch.StartNew();
            var w = new IntPtr(1);
            string result = null;
            using (var server = new Server(root, data, cfg))
            {
                var ready = new ManualResetEvent(false);
                server.StateChanged += (ok, msg) => { if (ok) ready.Set(); };
                server.Start();
                if (!ready.WaitOne(240000)) result = "TIMEOUT: " + server.Describe;
                else
                {
                    comp.OnForeground(w, 0);
                    comp.SetEnabled(true, 0);
                    comp.SetServerReady(true, 0);
                    foreach (char ch in romaji)
                    {
                        int vk = char.IsLetter(ch) ? char.ToUpperInvariant(ch) : ch == ',' ? 0xBC : ch == '.' ? 0xBE : ch == '?' ? 0xBF : ch;
                        var m = ch == '?' || char.IsUpper(ch) ? Mods.ShiftOnly : Mods.None;
                        comp.OnKey(vk, true, m, clock.ElapsedMilliseconds, w);
                        comp.OnKey(vk, false, m, clock.ElapsedMilliseconds, w);
                    }
                    comp.OnKey(Composer.VK_RETURN, true, Mods.None, clock.ElapsedMilliseconds, w);
                    var deadline = clock.ElapsedMilliseconds + 30000;
                    while (result == null && clock.ElapsedMilliseconds < deadline)
                    {
                        foreach (var e in comp.TakeEffects())
                        {
                            var req = e as RequestEffect;
                            if (e is DeliverEffect)
                            {
                                var ds = comp.TakeReady(clock.ElapsedMilliseconds);
                                if (ds.Count > 0) { result = string.Concat(ds.Where(d => !d.IsKey).Select(d => d.Text)); comp.Delivered(clock.ElapsedMilliseconds); }
                            }
                            if (req != null)
                            {
                                double ms;
                                var t0 = clock.ElapsedMilliseconds;
                                string[] c = null; string err = null;
                                try { c = server.Convert(req.Romaji, req.Context, out ms); } catch (Exception ex) { err = ex.Message; }
                                comp.OnResult(req.Id, c, err, clock.ElapsedMilliseconds);
                                Console.WriteLine("wall " + (clock.ElapsedMilliseconds - t0) + " ms");
                            }
                        }
                        if (result == null) { Thread.Sleep(20); comp.Tick(clock.ElapsedMilliseconds); }
                        if (result == null && !comp.Composing && comp.PendingCommits == 0 && comp.Status != "") result = "FAIL: " + comp.Status;
                    }
                    if (result == null) result = "FAIL: " + (comp.Status ?? "") + " buffer=" + comp.Buffer;
                }
            }
            File.WriteAllText(outPath, result, new UTF8Encoding(false));
            return result.StartsWith("FAIL") || result.StartsWith("TIMEOUT") ? 1 : 0;
        }
    }

    static class Native
    {
        public delegate IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam);
        public delegate void WinEventProc(IntPtr hook, uint ev, IntPtr hwnd, int idObject, int idChild, uint thread, uint time);

        [StructLayout(LayoutKind.Sequential)]
        public struct KBDLLHOOKSTRUCT { public uint vkCode, scanCode, flags, time; public IntPtr dwExtraInfo; }

        [StructLayout(LayoutKind.Sequential)]
        struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }
        [StructLayout(LayoutKind.Sequential)]
        struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }
        [StructLayout(LayoutKind.Explicit)]
        struct InputUnion { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
        [StructLayout(LayoutKind.Sequential)]
        struct INPUT { public uint type; public InputUnion u; }

        [StructLayout(LayoutKind.Sequential)]
        public struct RECT { public int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)]
        struct GUITHREADINFO
        {
            public int cbSize, flags;
            public IntPtr hwndActive, hwndFocus, hwndCapture, hwndMenuOwner, hwndMoveSize, hwndCaret;
            public RECT rcCaret;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct POINT { public int X, Y; }

        [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
        [DllImport("user32.dll", SetLastError = true)] public static extern IntPtr SetWindowsHookEx(int id, HookProc fn, IntPtr mod, uint thread);
        [DllImport("user32.dll")] public static extern bool UnhookWindowsHookEx(IntPtr h);
        [DllImport("user32.dll")] public static extern IntPtr CallNextHookEx(IntPtr h, int n, IntPtr w, IntPtr l);
        [DllImport("kernel32.dll")] public static extern IntPtr GetModuleHandle(string name);
        [DllImport("user32.dll")] public static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr mod, WinEventProc fn, uint pid, uint tid, uint flags);
        [DllImport("user32.dll")] public static extern bool UnhookWinEvent(IntPtr h);
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] static extern short GetAsyncKeyState(int vk);
        [DllImport("user32.dll", SetLastError = true)] static extern uint SendInput(uint n, INPUT[] inputs, int size);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [DllImport("user32.dll")] static extern bool GetGUIThreadInfo(uint tid, ref GUITHREADINFO info);
        [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr hwnd, ref POINT p);
        [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out RECT r);
        [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
        [DllImport("imm32.dll")] public static extern IntPtr ImmGetDefaultIMEWnd(IntPtr hwnd);
        [DllImport("user32.dll")] static extern IntPtr SendMessageTimeout(IntPtr h, uint msg, IntPtr w, IntPtr l, uint flags, uint timeout, out IntPtr result);
        [DllImport("user32.dll")] static extern bool DestroyIcon(IntPtr h);
        [DllImport("kernel32.dll")] static extern IntPtr CreateJobObject(IntPtr attr, string name);
        [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int cls, IntPtr info, uint len);
        [DllImport("kernel32.dll")] public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr proc);
        [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);

        // 自分が送り込んだ打鍵の印（フックは LLKHF_INJECTED で一律に素通しするので、区別は念のため）
        public static readonly IntPtr Magic = new IntPtr(0x52534C4D);

        public static bool Down(int vk) { return GetAsyncKeyState(vk) < 0; }

        static INPUT Key(ushort vk, ushort scan, uint flags)
        {
            var i = new INPUT { type = 1 };
            i.u.ki = new KEYBDINPUT { wVk = vk, wScan = scan, dwFlags = flags, dwExtraInfo = Magic };
            return i;
        }

        /// 確定文と送り直す打鍵を、順を保って 1 回の SendInput で送る。文字は KEYEVENTF_UNICODE（サロゲート対は 2 単位のまま）、
        /// 打鍵は元の仮想キー・スキャンコード・拡張キーの印のまま。送った数が足りなければ false
        /// （UIPI で拒まれたときも 0 が返る。理由は区別できない）
        public static bool SendBatch(List<Delivery> ds)
        {
            var list = new List<INPUT>();
            foreach (var d in ds)
            {
                if (d.IsKey)
                {
                    list.Add(Key((ushort)d.Key.Vk, (ushort)d.Key.Scan, (d.Key.Down ? 0u : 0x0002u) | (d.Key.Extended ? 0x0001u : 0u)));
                    continue;
                }
                foreach (char ch in d.Text)
                {
                    list.Add(Key(0, ch, 0x0004));
                    list.Add(Key(0, ch, 0x0004 | 0x0002));
                }
            }
            if (list.Count == 0) return true;
            return SendInput((uint)list.Count, list.ToArray(), Marshal.SizeOf(typeof(INPUT))) == list.Count;
        }

        /// Alt を押したまま握りつぶしたあと、Alt を離したときにメニューが開かないよう、割り当ての無いキーを 1 回はさむ
        public static void SendMask()
        {
            var a = new[] { Key(0xE8, 0, 0), Key(0xE8, 0, 0x0002) };
            SendInput(2, a, Marshal.SizeOf(typeof(INPUT)));
        }

        /// MS-IME を開閉する（既定の IME 窓へ WM_IME_CONTROL / IMC_SETOPENSTATUS）。応答しないアプリでは 300 ms であきらめる
        public static void SetImeOpen(IntPtr imeWnd, bool open)
        {
            IntPtr r;
            SendMessageTimeout(imeWnd, 0x0283, new IntPtr(0x0006), new IntPtr(open ? 1 : 0), 0x0002, 300, out r);
        }

        public static string ForegroundExe()
        {
            try
            {
                uint pid;
                GetWindowThreadProcessId(GetForegroundWindow(), out pid);
                using (var p = Process.GetProcessById((int)pid)) return p.ProcessName;
            }
            catch { return "?"; }
        }

        public static bool CaretBelow(out Point p)
        {
            p = Point.Empty;
            uint pid;
            var gi = new GUITHREADINFO { cbSize = Marshal.SizeOf(typeof(GUITHREADINFO)) };
            if (!GetGUIThreadInfo(GetWindowThreadProcessId(GetForegroundWindow(), out pid), ref gi)) return false;
            if (gi.hwndCaret == IntPtr.Zero || (gi.rcCaret.Bottom == 0 && gi.rcCaret.Top == 0)) return false;
            var pt = new POINT { X = gi.rcCaret.Left, Y = gi.rcCaret.Bottom };
            ClientToScreen(gi.hwndCaret, ref pt);
            p = new Point(pt.X, pt.Y + 6);
            return true;
        }

        /// フォーカスの要素がパスワード欄か、と浮き窓を置く位置（要素の左下）。取れなければ false
        public static bool FocusedElement(out bool password, out Point below)
        {
            password = false;
            below = Point.Empty;
            try
            {
                var el = System.Windows.Automation.AutomationElement.FocusedElement;
                if (el == null) return false;
                var cur = el.Current;
                password = cur.IsPassword;
                var r = cur.BoundingRectangle;
                if (!r.IsEmpty && r.Width > 0 && !double.IsInfinity(r.Left))
                    below = new Point((int)r.Left, (int)r.Bottom + 6);
                return true;
            }
            catch { return false; }
        }

        public static Point WindowFallback()
        {
            RECT r;
            if (!GetWindowRect(GetForegroundWindow(), out r)) return new Point(200, 200);
            return new Point(r.Left + Math.Max(0, (r.Right - r.Left) / 2 - 200), r.Bottom - 180);
        }

        public static bool AppsUseDarkTheme()
        {
            try
            {
                var v = Microsoft.Win32.Registry.GetValue(@"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme", 1);
                return v is int && (int)v == 0;
            }
            catch { return false; }
        }

        public static Icon TextIcon(string text, Color bg, Color fg)
        {
            int n = SystemInformation.SmallIconSize.Width * 2;
            using (var bmp = new Bitmap(n, n))
            using (var g = Graphics.FromImage(bmp))
            {
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
                g.Clear(Color.Transparent);
                if (bg.A > 0) using (var b = new SolidBrush(bg)) g.FillEllipse(b, 0, 0, n - 1, n - 1);
                using (var f = new Font("Yu Gothic UI", n * 0.5f, FontStyle.Bold, GraphicsUnit.Pixel))
                using (var b = new SolidBrush(fg))
                {
                    var sf = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                    g.DrawString(text, f, b, new RectangleF(0, 1, n, n), sf);
                }
                IntPtr h = bmp.GetHicon();
                var icon = (Icon)Icon.FromHandle(h).Clone();
                DestroyIcon(h);
                return icon;
            }
        }

        /// このアプリが閉じたら（落ちても）変換サーバも終わるジョブ
        public static IntPtr CreateKillOnCloseJob()
        {
            var job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero) return IntPtr.Zero;
            // JOBOBJECT_EXTENDED_LIMIT_INFORMATION（x64 で 144 バイト）。LimitFlags は先頭から 16 バイト目
            int size = 144;
            IntPtr info = Marshal.AllocHGlobal(size);
            try
            {
                for (int i = 0; i < size; i++) Marshal.WriteByte(info, i, 0);
                Marshal.WriteInt32(info, 16, 0x2000);   // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if (!SetInformationJobObject(job, 9, info, (uint)size)) { CloseHandle(job); return IntPtr.Zero; }
            }
            finally { Marshal.FreeHGlobal(info); }
            return job;
        }
    }
}
