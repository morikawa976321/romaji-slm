// romaji-slm の常駐入力（トレイアプリ）。docs/IME.md。
//
// - 低レベルキーボードフックで打鍵を受け、Composer（状態機械）に渡す。フックの中では通信も描画もしない
//   （フックの処理が遅いと Windows が黙ってフックを外す。LowLevelHooksTimeout）。効果は BeginInvoke で後から実行する。
// - 変換は ime/server.py（Python、127.0.0.1）に HTTP で頼む。サーバはこのアプリが起動し、ジョブオブジェクトで
//   このアプリと一緒に終わらせる。
// - 確定した文は SendInput（KEYEVENTF_UNICODE）で 1 回で送り込む。アプリ側の文字を消すことはしない。
// - 他のアプリが送り込んだ打鍵（LLKHF_INJECTED）も本人の打鍵として扱う（ime.ini の accept_injected）。自分が送ったものと、
//   文字そのものの送り込み（VK_PACKET。音声入力などの貼り付け）は扱わない。
//
// 起動: RomajiSlmIme.exe [--root <app のフォルダ>] [--selftest <打鍵列> <結果ファイル> [--port <番号>]]

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
            string root = null, selftest = null, selfOut = null, checkOut = null;
            int port = 0;
            for (int i = 0; i < args.Length; i++)
            {
                if (args[i] == "--root" && i + 1 < args.Length) root = args[++i];
                else if (args[i] == "--selftest" && i + 2 < args.Length) { selftest = args[++i]; selfOut = args[++i]; }
                else if (args[i] == "--port" && i + 1 < args.Length) int.TryParse(args[++i], out port);   // 自己試験だけ
                else if (args[i] == "--check-update" && i + 1 < args.Length) checkOut = args[++i];
            }
            string exeDir = Path.GetDirectoryName(Application.ExecutablePath);
            if (root == null) root = Path.Combine(exeDir, "app");
            // ROMAJI_SLM_DATA: 置き場所を替える（試験で、動いている常駐に触れずに試すため。tools/review_history.py と同じ名前）
            string data = Environment.GetEnvironmentVariable("ROMAJI_SLM_DATA");
            if (string.IsNullOrEmpty(data))
                data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "romaji-slm");
            Directory.CreateDirectory(data);

            Native.SetProcessDPIAware();
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            if (selftest != null)
                return SelfTest.Run(root, data, selftest, selfOut, port);
            if (checkOut != null)
            {
                // 新しい版の確かめを 1 回だけ行い、結果をファイルに書く（追従モードの試験用。常駐はしない）
                var cfg = new Config(Path.Combine(data, "ime.ini"));
                var up = new Updater(root, data, cfg.Pull);
                up.Check();
                File.WriteAllText(checkOut, "enabled=" + up.Enabled + "\npull=" + up.PullResult + "\ninstalled=" + up.Installed +
                                  "\nlatest=" + (up.Latest ?? "") + "\n", new UTF8Encoding(false));
                return 0;
            }

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
        public bool History = true;
        public bool AcceptInjected = true;
        // 変換サーバの CPU のスレッド数（ONNX Runtime 版と、GPU のときの torch の CPU 側）。既定 4（本人の決定 2026-10-19
        // 「マシン内の他の処理に影響を与えないためにも」。docs/decisions.md D42）。0 なら実行系の既定（論理コア数）
        public int Threads = 4;
        // 変換の実行方式: auto（GPU があれば torch、無ければ ONNX Runtime 版。本人の決定 2026-10-19、D43）/ torch（GPU のときだけ。
        // 無ければ onnx で動く。CPU の torch 版は削除した、D44）/ onnx
        public string Engine = "auto";
        public int AutoUpdate = 3;   // 新しい版があるとき、この分数だけ打鍵が無ければ自動で入れ替える（0 で自動ではしない）
        // 追従モード: 写し元のリポジトリを GitHub のこのブランチに 30 分ごとに追いつかせる（ThinkPad 用。docs/IME.md）。
        // 空なら取りに行かない（このデスクトップのリポジトリは開発用で、複数のセッションが書いているので既定は空）
        public string Pull = "";
        // 記録しないアプリの既定: パスワード管理（打った文字は変換に回らないはずだが、念のため記録からも外す）
        public string NoHistoryApps = "1password,keepass,keepassxc,bitwarden,dashlane,lastpass,enpass,roboform";

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
                else if (k == "history") History = v != "0";
                else if (k == "accept_injected") AcceptInjected = v != "0";
                else if (k == "no_history_apps") NoHistoryApps = v;
                else if (k == "auto_update") int.TryParse(v, out AutoUpdate);
                else if (k == "threads") { int n; if (int.TryParse(v, out n) && n >= 0) Threads = n; }
                else if (k == "engine" && (v == "auto" || v == "torch" || v == "onnx")) Engine = v;
                else if (k == "pull") Pull = v;
            }
        }

        public void Save()
        {
            File.WriteAllText(Path,
                "# romaji-slm 常駐入力の設定（docs/IME.md）。変えたらトレイの「終了」→ 起動し直す\r\n" +
                "enabled=" + (Enabled ? "1" : "0") + "\r\n" +
                "# device: auto（GPU があれば GPU）/ cuda / cpu（cpu は ONNX Runtime 版）\r\n" +
                "device=" + Device + "\r\n" +
                "port=" + Port + "\r\n" +
                "# threads: 変換の CPU のスレッド数（既定 4。0 なら論理コア数で、ほかの処理に影響しやすい）\r\n" +
                "threads=" + Threads + "\r\n" +
                "# engine: auto（GPU があれば torch、無ければ ONNX Runtime 版。どちらも変換結果は同じ）/ torch（GPU のときだけ）/ onnx\r\n" +
                "engine=" + Engine + "\r\n" +
                "# history: 1 なら確定・取り消しのたびに打鍵列・候補・確定文を history.jsonl に残す（次の改良の材料）。0 で止める\r\n" +
                "history=" + (History ? "1" : "0") + "\r\n" +
                "# accept_injected: 1 なら他のアプリが送り込んだキーも本人の打鍵として扱う（リモートデスクトップ越しなど、本人の打鍵が送り込みで届くとき要る）\r\n" +
                "accept_injected=" + (AcceptInjected ? "1" : "0") + "\r\n" +
                "# no_history_apps: 記録（history.jsonl）に残さないアプリの exe 名（拡張子なし、カンマ区切り）。例: slack,line\r\n" +
                "no_history_apps=" + NoHistoryApps + "\r\n" +
                "# auto_update: リポジトリから入れたとき、新しい版があり、この分数だけ打鍵が無ければ自動で入れ替える。0 で自動ではしない\r\n" +
                "auto_update=" + AutoUpdate + "\r\n" +
                "# pull: 写し元のリポジトリを GitHub のこのブランチ（例 main）に 30 分ごとに追いつかせる。空なら取りに行かない\r\n" +
                "pull=" + Pull + "\r\n", new UTF8Encoding(false));
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
                                           " --threads " + cfg.Threads + " --engine " + cfg.Engine + " --log \"" + log + "\"")
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

        /// 変換。戻り値は (候補, サーバ側の ms)。失敗は例外（後から来た要求に置き換えられた先読みは 409）。
        /// full: 別の書き方まで揃った列（false なら 1 位と読みの候補だけ）。prefetch: 後から来た要求があればサーバがやめてよい（D46）
        public string[] Convert(string romaji, string context, bool full, bool prefetch, out double ms)
        {
            var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + cfg.Port + "/convert?mode=" + (full ? "full" : "first") +
                                                        (prefetch ? "&prefetch=1" : ""));
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

    /// 実利用の記録（本人の依頼 2026-10-16）。1 行 1 件の JSON を history.jsonl に追記する。次の改良の材料にする
    /// （eval の評価セット・本人の実入力）。ime.ini の history=0 で止まる
    sealed class History
    {
        readonly string path;
        public bool Enabled = true;
        // 記録しないアプリ（exe 名、小文字）。ime.ini の no_history_apps（本人の決定 2026-10-17「1-4をいれて」）
        public HashSet<string> ExcludedApps = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        public const int KeepDays = 30;   // これより古い行は、振り返られなくても消す（同上）
        public History(string path) { this.path = path; }

        /// KeepDays より古い行を消す。振り返りの道具（tools/review_history.py）が同時に名前を替えていたら何もしない
        public int Prune()
        {
            try
            {
                if (!File.Exists(path)) return 0;
                string limit = DateTime.Now.AddDays(-KeepDays).ToString("yyyy-MM-ddTHH:mm:ss");
                var lines = File.ReadAllLines(path, Encoding.UTF8);
                var keep = lines.Where(l => l.Length > 0 && TimeOf(l).CompareTo(limit) > 0).ToArray();
                if (keep.Length == lines.Length) return 0;
                string tmp = path + ".tmp";
                File.WriteAllLines(tmp, keep, new UTF8Encoding(false));
                File.Copy(tmp, path, true);
                File.Delete(tmp);
                return lines.Length - keep.Length;
            }
            catch { return 0; }
        }

        static string TimeOf(string line)
        {
            int i = line.IndexOf("\"time\":\"", StringComparison.Ordinal);
            return i < 0 ? "" : line.Substring(i + 8, Math.Min(19, Math.Max(0, line.Length - i - 8)));
        }

        static string J(string s)
        {
            if (s == null) return "null";
            var sb = new StringBuilder("\"");
            foreach (char ch in s)
            {
                if (ch == '"' || ch == '\\') sb.Append('\\').Append(ch);
                else if (ch < 0x20) sb.Append("\\u").Append(((int)ch).ToString("x4"));
                else sb.Append(ch);
            }
            return sb.Append('"').ToString();
        }

        public void Write(LogEffect e, string app)
        {
            if (!Enabled || ExcludedApps.Contains(app ?? "")) return;
            var c = e.Candidates == null ? "null" : "[" + string.Join(",", e.Candidates.Select(J)) + "]";
            string line = "{\"time\":" + J(DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss.fff")) + ",\"event\":" + J(e.Event) +
                          ",\"romaji\":" + J(e.Romaji) + ",\"context\":" + J(e.Context) + ",\"text\":" + J(e.Text) +
                          ",\"candidates\":" + c + ",\"chosen\":" + e.Chosen + ",\"raw\":" + (e.Raw ? "true" : "false") +
                          ",\"app\":" + J(app) + ",\"note\":" + J(e.Note) +
                          (e.Backspaces > 0 ? ",\"retype\":{\"backspaces\":" + e.Backspaces + ",\"prev_text\":" + J(e.PrevText) + ",\"prev_romaji\":" + J(e.PrevRomaji) + "}" : "") +
                          "}\n";
            try { File.AppendAllText(path, line, new UTF8Encoding(false)); } catch { }
        }
    }

    /// 診断の記録（ime-debug.log）。状態の変化・欄の確認の結果・フックの付け直しだけで、打った文字は書かない。1 MB で .old に回す
    sealed class DebugLog
    {
        readonly string path;
        public DebugLog(string path) { this.path = path; }
        public void Write(string line)
        {
            try
            {
                var fi = new FileInfo(path);
                if (fi.Exists && fi.Length > 1 << 20) { File.Delete(path + ".old"); File.Move(path, path + ".old"); }
                File.AppendAllText(path, DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss.fff") + " " + line + "\r\n", new UTF8Encoding(false));
            }
            catch { }
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
            const int WM_MOUSEACTIVATE = 0x21, MA_NOACTIVATE = 3, WM_INPUT = 0x00FF;
            if (m.Msg == WM_MOUSEACTIVATE) { m.Result = (IntPtr)MA_NOACTIVATE; return; }
            if (m.Msg == WM_INPUT && RawKey != null) RawKey();
            base.WndProc(ref m);
        }

        public Action RawKey;   // キーボードの Raw Input が届いた（フックの見張りに使う）

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

    /// 写し元のリポジトリ（install.ps1 が source.txt に書く）に新しい版があるかを見て、ime/install.ps1 -Update で入れ替える。
    /// 配布 zip から入れた PC では source.txt が無いので何もしない
    sealed class Updater
    {
        // この下が変わったときだけ入れ直す（docs だけのコミットでは止めない）。install.ps1 が写すものと揃える
        const string Paths = "core eval ime pyproject.toml uv.lock";
        readonly string data, repo, installed;
        // Check（別スレッド）と UI スレッドの両方から触る
        public volatile string Latest;       // 入れ替えるべき HEAD（無ければ null）
        public volatile string Staged;       // 組み立てて試験を通した版（app.next）
        public volatile string FailedHead;   // 組み立てか試験に失敗した版（自動ではもう試さない）
        public volatile bool Running;
        public event Action<bool, string> StageFinished;   // (通ったか, 失敗の説明。別の更新が動いていたときは null)

        readonly string pull;            // 追いつかせるブランチ（空なら取りに行かない）
        DateTime lastFetch = DateTime.MinValue;
        public static readonly TimeSpan FetchInterval = TimeSpan.FromMinutes(30);
        public volatile string PullResult = "";   // 最後に取りに行った結果（診断の記録用）

        public Updater(string root, string data, string pull = "")
        {
            this.data = data;
            this.pull = (pull ?? "").Trim();
            try { repo = File.ReadAllText(Path.Combine(data, "source.txt"), Encoding.UTF8).Trim(); } catch { }
            try { installed = File.ReadAllText(Path.Combine(root, "VERSION"), Encoding.UTF8).Split(' ')[0].Trim(); } catch { }
            if (repo == "" || !Directory.Exists(repo ?? "")) repo = null;
            var r = LastResult();
            if (r != null && r.Length > 1 && r[0] == "fail") FailedHead = r[1];
            try
            {
                // 前の常駐が組み立てたまま終わっていれば、それを使う
                string v = File.ReadAllText(Path.Combine(data, "app.next", "VERSION"), Encoding.UTF8).Split(' ')[0].Trim();
                if (r != null && r.Length > 1 && r[0] == "staged" && r[1] == v) Staged = v;
            }
            catch { }
        }

        public bool Enabled { get { return repo != null && !string.IsNullOrEmpty(installed); } }
        public string Installed { get { return installed; } }

        /// 前回の更新の結果（update-result.txt の「staged|ok|fail <版> <時刻> <説明>」）
        public string[] LastResult()
        {
            try { return File.ReadAllText(Path.Combine(data, "update-result.txt"), Encoding.UTF8).Split(new[] { ' ' }, 4); }
            catch { return null; }
        }

        /// UI スレッドの外で呼ぶ（git を起動する）
        public void Check()
        {
            if (!Enabled) return;
            if (pull != "" && DateTime.Now - lastFetch >= FetchInterval)
            {
                lastFetch = DateTime.Now;
                PullResult = FastForward();
            }
            string head = Git("rev-parse --short HEAD").Trim();
            if (head == "" || head == installed) { Latest = null; return; }
            // 入れた版がリポジトリに無い（履歴を書き換えた）ときも入れ直す
            int code;
            Git("diff --quiet " + installed + " " + head + " -- " + Paths, out code);
            Latest = code == 0 ? null : head;
        }

        /// 組み立てと試験（install.ps1 -Stage）。動いている常駐には触れない
        public void Stage()
        {
            if (!Enabled || Running) return;
            Running = true;
            Staged = null;   // -Stage は始めにすぐ app.next の VERSION を消す（組み立て済みでなくなる）
            string target = Latest;
            DateTime started = DateTime.Now.AddSeconds(-2);
            try
            {
                var p = new Process { StartInfo = Pwsh("-Stage"), EnableRaisingEvents = true };
                p.Exited += (s, e) =>
                {
                    Running = false;
                    var r = LastResult();
                    DateTime at;
                    // この回の結果か（前の回の結果が残っていることがある）
                    bool fresh = r != null && r.Length > 2 && DateTime.TryParse(r[2], out at) && at >= started;
                    var h = StageFinished;
                    if (p.ExitCode == 3) { if (h != null) h(false, null); return; }   // 別の更新が動いていた
                    if (p.ExitCode == 0 && fresh && r[0] == "staged")
                    {
                        Staged = r[1];
                        if (FailedHead == r[1]) FailedHead = null;   // 前に失敗した版を試し直して通った
                        if (h != null) h(true, "");
                        return;
                    }
                    // 結果を書く前に落ちたときも、試した版を失敗として覚える（同じ版を繰り返し試さない）
                    FailedHead = fresh && r[0] == "fail" ? r[1] : target;
                    if (h != null) h(false, fresh && r[0] == "fail" && r.Length > 3 ? r[3] : "終了コード " + p.ExitCode);
                };
                p.Start();
            }
            catch (Exception ex)
            {
                Running = false;
                FailedHead = target;
                var h = StageFinished;
                if (h != null) h(false, "pwsh を起動できません（" + ex.Message + "）");
            }
        }

        public string ReadyPath { get { return Path.Combine(data, "swap.ready"); } }
        public string DecisionPath { get { return Path.Combine(data, "swap.decision"); } }

        /// swap.decision に決定を書く。-Swap（期限の timeout）と先を争い、先に作った側の決定だけが残る。残った決定を返す
        public string Decide(string d)
        {
            try
            {
                using (var f = new FileStream(DecisionPath, FileMode.CreateNew, FileAccess.Write))
                {
                    var b = Encoding.ASCII.GetBytes(d);
                    f.Write(b, 0, b.Length);
                }
                return d;
            }
            catch (IOException)
            {
                try { return File.ReadAllText(DecisionPath).Trim(); } catch { return "error"; }
            }
            catch { return "error"; }   // 書けなければ終わらない側に倒す
        }

        /// 組み立てた版への差し替え（install.ps1 -Swap）を起動する。-Swap は準備ができたら swap.ready に PID を書き、
        /// このプロセスが終わるのを待つ。起動できなければ null
        public Process Swap()
        {
            try
            {
                File.Delete(ReadyPath);
                File.Delete(DecisionPath);
                return Process.Start(Pwsh("-Swap"));
            }
            catch { return null; }
        }

        /// GitHub から pull のブランチを取ってきて、早送りで追いつけるときだけ追いつく（ThinkPad の写し元は取り込むだけで、
        /// 書き込まない）。作業ツリーに変更があれば取り込まない（手で触ったものを消さない）。結果の説明を返す
        string FastForward()
        {
            int code;
            Git("fetch --quiet origin " + pull, out code, 120000);
            if (code != 0) return "fetch に失敗（終了コード " + code + "。ネットワークか GitHub のログインを確かめる）";
            if (Git("status --porcelain").Trim() != "") return "作業ツリーに変更があるので取り込まない";
            string before = Git("rev-parse --short HEAD").Trim();
            Git("merge --ff-only --quiet origin/" + pull, out code);
            if (code != 0) return "早送りで追いつけない（手元に別のコミットがある）";
            string after = Git("rev-parse --short HEAD").Trim();
            return before == after ? "最新" : before + " → " + after;
        }

        ProcessStartInfo Pwsh(string mode)
        {
            return new ProcessStartInfo("pwsh.exe", "-NoProfile -ExecutionPolicy Bypass -File \"" +
                                        Path.Combine(repo, "ime", "install.ps1") + "\" " + mode)
            { UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = repo };
        }

        string Git(string args) { int c; return Git(args, out c); }

        string Git(string args, out int code, int timeoutMs = 10000)
        {
            code = -1;
            try
            {
                var si = new ProcessStartInfo("git", "-C \"" + repo + "\" " + args)
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
                // 無人で動くので、ログインを求める画面を出さない（出すと誰も応えずに止まる）
                si.EnvironmentVariables["GIT_TERMINAL_PROMPT"] = "0";
                si.EnvironmentVariables["GCM_INTERACTIVE"] = "never";
                using (var p = Process.Start(si))
                {
                    var err = p.StandardError.ReadToEndAsync();
                    string o = p.StandardOutput.ReadToEnd();
                    err.Wait(timeoutMs);
                    if (!p.WaitForExit(timeoutMs)) { try { p.Kill(); } catch { } return ""; }
                    code = p.ExitCode;
                    return o;
                }
            }
            catch { return ""; }
        }
    }

    sealed class App : ApplicationContext
    {
        readonly Composer comp = new Composer();
        readonly Config cfg;
        readonly Server server;
        readonly Usage usage;
        readonly History history;
        readonly Popup popup;
        readonly NotifyIcon tray;
        readonly ToolStripMenuItem enabledItem, serverItem, updateItem;
        readonly Updater updater;
        long lastKeyAt, lastUpdateCheckAt = -45000;   // 起動の 15 秒後に最初の確認
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
        string lastStatus = "";
        // フックの見張り: Raw Input にはキーが届いているのにフックが呼ばれていなければ、Windows がフックを外した
        // （LowLevelHooksTimeout を超えたとき黙って外す）とみなして付け直す
        long lastHookAt, lastRawAt, lastReinstallAt = -100000, lastStateAt, lastPruneAt;
        int hookEvents, reinstalls, physicalDowns, injectedDowns, hookErrors;
        string lastInjected = "";
        int keyLogLeft = 30, traceLeft, modTraceLeft = 300;
        long batchAt = -100000;
        readonly DebugLog dbg;
        readonly string statePath;
        const int FieldCheckMs = 300;   // UI Automation が応答しないアプリでは、この時間でパスワード欄ではないとみなす

        long Now { get { return clock.ElapsedMilliseconds; } }

        public App(string root, string data)
        {
            cfg = new Config(Path.Combine(data, "ime.ini"));
            if (!File.Exists(cfg.Path)) cfg.Save();
            dbg = new DebugLog(Path.Combine(data, "ime-debug.log"));
            statePath = Path.Combine(data, "ime-state.txt");
            dbg.Write("start root=" + root + " enabled=" + cfg.Enabled);
            usage = new Usage(Path.Combine(data, "usage.txt"));
            history = new History(Path.Combine(data, "history.jsonl")) { Enabled = cfg.History };
            foreach (var a in cfg.NoHistoryApps.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries)) history.ExcludedApps.Add(a.Trim());
            int pruned = history.Prune();
            if (pruned > 0) dbg.Write("history: removed " + pruned + " rows older than " + History.KeepDays + " days");
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
            updater = new Updater(root, data, cfg.Pull);
            updateItem = new ToolStripMenuItem("", null, (s, e) =>
            {
                manualUpdate = true;
                updater.FailedHead = null;   // 手で頼まれたら、前に失敗した版も試し直す
                // 押したときの HEAD を毎回組み立て直す（前に組み立てた別の版が残っていても使わない）。組み立てたら WatchUpdate が差し替える
                StartStage("menu");
            });
            if (updater.Enabled) menu.Items.Add(updateItem);
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
            updater.StageFinished += (ok, msg) => popup.BeginInvoke((Action)(() =>
            {
                dbg.Write("update stage ok=" + ok + " staged=" + updater.Staged + " " + msg);
                if (!ok && msg != null)
                {
                    manualUpdate = false;
                    tray.ShowBalloonTip(5000, "romaji-slm", "新しい版にできませんでした（今の版のまま使えます）: " + msg, ToolTipIcon.Warning);
                }
                RefreshUpdateItem();
            }));
            // 差し替えた直後（-Swap が起動し直した）なら結果を知らせる
            var last = updater.Enabled ? updater.LastResult() : null;
            DateTime at;
            if (last != null && last.Length > 2 && DateTime.TryParse(last[2], out at) && (DateTime.Now - at).TotalMinutes < 10)
            {
                if (last[0] == "ok" && last[1] == updater.Installed)
                    tray.ShowBalloonTip(3000, "romaji-slm", "新しい版にしました（" + updater.Installed + "）", ToolTipIcon.None);
                else if (last[0] == "fail")
                    tray.ShowBalloonTip(5000, "romaji-slm", "新しい版にできませんでした（前の版で動いています）: " +
                                        (last.Length > 3 ? last[3] : ""), ToolTipIcon.Warning);
            }
            RefreshUpdateItem();

            server = new Server(root, data, cfg);
            server.StateChanged += (ready, msg) => popup.BeginInvoke((Action)(() =>
            {
                serverItem.Text = "変換サーバ: " + msg;
                dbg.Write("server ready=" + ready + " " + msg);
                comp.SetServerReady(ready, Now);
                Run();
            }));
            popup.ServerStatus = () => server.Describe;
            server.Start();

            caps = Control.IsKeyLocked(Keys.CapsLock);
            hookProc = HookCallback;
            hook = Native.SetWindowsHookEx(13, hookProc, Native.GetModuleHandle(null), 0);   // WH_KEYBOARD_LL
            dbg.Write("hook=" + hook);
            popup.RawKey = () => { lastRawAt = Now; };
            if (!Native.RegisterKeyboardRawInput(popup.Handle)) dbg.Write("raw input registration failed");
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
                WatchHook();
                if (Now - lastStateAt > 5000) { lastStateAt = Now; WriteState(); }
                if (Now - lastPruneAt > 3600000)
                {
                    lastPruneAt = Now;   // 1 時間ごとに、30 日より古い記録を消す（常駐し続けても溜まらないように）
                    int n = history.Prune();
                    if (n > 0) dbg.Write("history: removed " + n + " rows older than " + History.KeepDays + " days");
                }
                if (updater.Enabled) WatchUpdate();
                if (fieldDeadline != 0 && Now > fieldDeadline)
                {
                    fieldDeadline = 0;
                    dbg.Write("field check seq=" + fieldSeq + " timed out (treated as not password)");
                    comp.OnFieldChecked(fieldSeq, false, Now);
                }
                comp.Tick(Now);
                Run();
            };
            comp.RequireFieldCheck = true;
            // UI Automation の初回の呼び出しは初期化で遅く、欄の確認（0.3 秒で打ち切り）に間に合わないことがあるので、起動時に 1 回済ませる
            ThreadPool.QueueUserWorkItem(_ => { bool pw, ed; Point q; string kd; Native.FocusedElement(out pw, out q, out ed, out kd); });
            timer.Start();
        }

        void WatchHook()
        {
            if (lastRawAt - lastHookAt < 1500 || Now - lastReinstallAt < 10000) return;
            lastReinstallAt = Now;
            reinstalls++;
            var old = hook;
            hook = Native.SetWindowsHookEx(13, hookProc, Native.GetModuleHandle(null), 0);
            if (old != IntPtr.Zero) Native.UnhookWindowsHookEx(old);
            lastHookAt = Now;
            dbg.Write("hook reinstalled (raw input without hook calls) new=" + hook + " fg=" + Native.ForegroundExe());
        }

        /// 診断用の状態（5 秒ごと）。打った文字は出さない
        void WriteState()
        {
            try
            {
                File.WriteAllText(statePath,
                    "time=" + DateTime.Now.ToString("s") + "\r\nmode=" + comp.Mode + "\r\nphase=" + comp.Phase +
                    "\r\ncomposing=" + comp.Composing + "\r\nfield_pending=" + comp.FieldPending + "\r\npassthrough=" + comp.Passthrough +
                    "\r\nverified=" + comp.Verified + "\r\nwaiting=" + comp.Waiting + "\r\nserver_ready=" + comp.ServerReady +
                    "\r\nhook=" + hook + "\r\nhook_events=" + hookEvents + "\r\nhook_ago_ms=" + (Now - lastHookAt) +
                    "\r\nraw_ago_ms=" + (Now - lastRawAt) + "\r\nreinstalls=" + reinstalls +
                    "\r\nphysical_downs=" + physicalDowns + "\r\ninjected_downs=" + injectedDowns + "\r\nlast_injected=" + lastInjected +
                    "\r\nhook_errors=" + hookErrors + "\r\nforeground=" + Native.ForegroundExe() +
                    "\r\nstatus=" + comp.Status + "\r\n", new UTF8Encoding(false));
            }
            catch { }
        }

        /// 確定待ちでない（修飾キーを積んでいない）間は、OS の状態が本人の押している状態と一致する
        void ResyncModifiers()
        {
            foreach (int vk in new[] { 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C })
                keyDown[vk] = Native.Down(vk);
        }

        string modeSource = "hotkey";   // 次の SetEnabled の出どころ（記録用）

        void Toggle()
        {
            modeSource = "tray";
            comp.SetEnabled(comp.Mode == Mode.Disabled, Now);
            Run();
        }

        IntPtr HookCallback(int nCode, IntPtr wParam, IntPtr lParam)
        {
            // フックの中の例外はネイティブの境界を越えてプロセスを落とすので、ここで止めて素通しにする
            if (nCode >= 0) try
            {
                lastHookAt = Now;
                hookEvents++;
                var k = (Native.KBDLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(Native.KBDLLHOOKSTRUCT));
                bool injected = (k.flags & 0x10) != 0;
                int msg = wParam.ToInt32();
                bool down = msg == 0x100 || msg == 0x104;
                if (down)
                {
                    lastKeyAt = Now;
                    if (!injected) physicalDowns++;
                    else if (k.dwExtraInfo != Native.Magic)
                    {
                        injectedDowns++;
                        lastInjected = "vk=0x" + k.vkCode.ToString("X2") + " flags=0x" + k.flags.ToString("X") + " extra=0x" + k.dwExtraInfo.ToInt64().ToString("X");
                    }
                }
                // 修飾キーの状態は自分で追う。確定待ちの間は修飾キーも積んで OS に渡さないので、GetAsyncKeyState は使えない。
                // 他のアプリが送り込んだ修飾キー（貼り付けの Ctrl+V など）も OS の状態を変えるので追う。自分の送り直しは除く
                if (k.dwExtraInfo != Native.Magic && k.vkCode < 256) keyDown[k.vkCode] = down;
                // 2026-10-16 の 17 時台は、本人の打鍵がすべて送り込まれたキー（LLKHF_INJECTED、extra=0）として届いた（実際のキー 0・
                // 送り込み 239）。リモートデスクトップ越しに使っていた間とみられ、手元のキーボードでは実際のキーで届く（decisions.md D39）。
                // そのため、自分が送ったもの（Magic）以外の送り込みも本人の打鍵として扱う。ime.ini の accept_injected=0 で元に戻す
                // VK_PACKET（0xE7）は文字そのものの送り込みで、本人の打鍵ではない（自分が送った確定文が他の常駐アプリを
                // 通って印なしで戻ってくることもある）。解釈も送り直しもせずに通す
                bool packet = k.vkCode == 0xE7;
                if (traceLeft > 0 && Now - batchAt < 1000)
                {
                    traceLeft--;
                    string tl = "trace +" + (Now - batchAt) + "ms vk=0x" + k.vkCode.ToString("X2") + (down ? " down" : " up") +
                                " flags=0x" + k.flags.ToString("X") + " extra=0x" + k.dwExtraInfo.ToInt64().ToString("X") +
                                " dt=" + unchecked((int)(k.time - batchTick));
                    popup.BeginInvoke((Action)(() => dbg.Write(tl)));
                }
                if (!packet && (!injected || (cfg.AcceptInjected && k.dwExtraInfo != Native.Magic)))
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
                    // Handy のショートカット（Ctrl+Shift+F の押しっぱなし）の診断: Ctrl・Shift が絡む打鍵を 300 件まで残す（仮想キーと判断だけ）
                    bool modKey = k.vkCode >= 0xA0 && k.vkCode <= 0xA3;
                    if (modTraceLeft > 0 && (modKey || m.Ctrl || (m.Shift && (k.vkCode == 0x46 || k.vkCode == 0x20))))
                    {
                        modTraceLeft--;
                        string ml = "mod vk=0x" + k.vkCode.ToString("X2") + (down ? " down" : " up") + " flags=0x" + k.flags.ToString("X") +
                                    " extra=0x" + k.dwExtraInfo.ToInt64().ToString("X") + " swallow=" + swallow + " late=" + late +
                                    " mode=" + comp.Mode + " holding=" + comp.Holding + " ctrl=" + m.Ctrl + " shift=" + m.Shift;
                        popup.BeginInvoke((Action)(() => dbg.Write(ml)));
                    }
                    if (down && keyLogLeft > 0)
                    {
                        // 起動直後の 30 打鍵だけ、判断を診断の記録に残す（仮想キーと判断だけ。1 回限り）
                        keyLogLeft--;
                        string line = "key vk=0x" + k.vkCode.ToString("X2") + " flags=0x" + k.flags.ToString("X") + " swallow=" + swallow +
                                      " mode=" + comp.Mode + " pending=" + comp.FieldPending + " shift=" + m.Shift + " ctrl=" + m.Ctrl +
                                      " alt=" + m.Alt + " win=" + m.Win;
                        popup.BeginInvoke((Action)(() => dbg.Write(line)));
                    }
                    if (swallow) return new IntPtr(1);
                }
            }
            catch (Exception ex)
            {
                if (hookErrors++ < 5) popup.BeginInvoke((Action)(() => dbg.Write("hook exception: " + ex)));
            }
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
                    else if (mode != null) OnMode(mode.Mode, mode.Source);
                    else if (stat != null) usage.Add(stat.Name, stat.Value);
                    else if (e is QueryImeEffect)
                    {
                        var qw = ((QueryImeEffect)e).Window;
                        IntPtr qime = Native.ImmGetDefaultIMEWnd(qw);
                        ThreadPool.QueueUserWorkItem(_ =>
                        {
                            bool open = qime != IntPtr.Zero && Native.GetImeOpen(qime);
                            popup.BeginInvoke((Action)(() =>
                            {
                                dbg.Write("ms-ime open=" + open + " fg=" + Native.ExeOf(qw));
                                comp.OnImeState(qw, open, Now);
                                Run();
                            }));
                        });
                    }
                    else if (e is LogEffect) history.Write((LogEffect)e, Native.ExeOf(((LogEffect)e).Window));
                    else if (e is CheckFieldEffect) CheckField(((CheckFieldEffect)e).Seq, ((CheckFieldEffect)e).DelayMs);
                    else if (e is AltMaskEffect) Native.SendMask();
                    else if (e is RedrawEffect) redraw = true;
                }
                if (effs.Any(x => x is StatEffect)) usage.Save(server.Describe);
            }
            if (comp.Status != lastStatus) { lastStatus = comp.Status ?? ""; if (lastStatus.Length > 0) dbg.Write("status: " + lastStatus); }
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
            batchAt = Now;
            traceLeft = 80;   // 送った直後 1 秒のフックの様子を診断の記録に残す（文字の中身は残さない）
            dbg.Write("deliver items=" + ds.Count + " chars=" + ds.Where(x => !x.IsKey).Sum(x => x.Text.Length) +
                      " keys=" + ds.Count(x => x.IsKey) + (why != null ? " FAILED " + why : ""));
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
                bool password, editable; Point q; string kind;
                var sw = Stopwatch.StartNew();
                bool ok = Native.FocusedElement(out password, out q, out editable, out kind);
                long took = sw.ElapsedMilliseconds;
                popup.BeginInvoke((Action)(() =>
                {
                    dbg.Write("field check seq=" + seq + " ok=" + ok + " password=" + password + " editable=" + editable + " (" + kind + ") ms=" + took +
                              (seq != fieldSeq || fieldDeadline == 0 ? " (late, ignored)" : "") + " fg=" + Native.ForegroundExe());
                    if (seq != fieldSeq || fieldDeadline == 0) return;
                    fieldDeadline = 0;
                    if (ok && !caretAnchor && q != Point.Empty) anchor = q;
                    comp.OnFieldChecked(seq, ok && password, Now, !ok || editable);
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
                try { cands = server.Convert(r.Romaji, r.Context, r.Full, r.Prefetch, out ms); }
                catch (WebException ex) { err = ex.Response != null ? ((HttpWebResponse)ex.Response).StatusCode.ToString() : ex.Status.ToString(); }
                catch (Exception ex) { err = ex.GetType().Name; }
                popup.BeginInvoke((Action)(() =>
                {
                    long t0;
                    // 待ちの記録は変換できた要求だけ（置き換えてサーバがやめた先読みは、すぐ 409 で返るので数えない）
                    if (sentAt.TryGetValue(r.Id, out t0)) { if (cands != null) usage.Latency(Now - t0); sentAt.Remove(r.Id); }
                    comp.OnResult(r.Id, cands, err, Now);
                    Run();
                }));
            });
        }

        void OnMode(Mode m, string source)
        {
            dbg.Write("mode=" + m + " by=" + (source.Length > 0 ? source : modeSource) + " fg=" + Native.ForegroundExe());
            modeSource = "hotkey";
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

        /// 1 分ごとに新しい版を見る。あれば裏で組み立てて試験し（打っている間でもよい。常駐には触れない）、
        /// 打っていない間（変換中・確定待ちでもない）に差し替える
        void WatchUpdate()
        {
            if (!updater.Running && Now - lastUpdateCheckAt > 60000)
            {
                lastUpdateCheckAt = Now;
                ThreadPool.QueueUserWorkItem(_ =>
                {
                    string before = updater.PullResult;
                    updater.Check();
                    string pr = updater.PullResult;
                    popup.BeginInvoke((Action)(() =>
                    {
                        if (pr != before && pr != "") dbg.Write("pull: " + pr);   // 追従モードで取りに行った結果
                        RefreshUpdateItem();
                    }));
                });
            }
            if (swapProc != null) { WatchSwap(); return; }
            if (updater.Running) return;
            string want = updater.Latest, staged = updater.Staged;
            // 自動の組み立ては 5 分に 1 回まで（別の更新が動いていて すぐ返ったときに、起動し直し続けない）
            if (want != null && want != staged && want != updater.FailedHead && cfg.AutoUpdate > 0 && Now - lastStageAt > 300000)
            {
                StartStage("auto");
                return;
            }
            bool ready = staged != null && staged != updater.FailedHead && (manualUpdate ? staged == (want ?? staged) : staged == want);
            // 差し替えの試みは 1 分に 1 回まで（見送られたとき・別の更新が動いていたときに、起動し直し続けない）
            if (ready && SafeToStop() && Now - lastSwapAt > 60000)
            {
                lastSwapAt = Now;
                dbg.Write("update swap start " + updater.Installed + " -> " + staged + (manualUpdate ? " (menu)" : " (auto)"));
                swapProc = updater.Swap();
                swapTarget = staged;
                swapCancelled = false;
                if (swapProc == null) SwapFailed("pwsh を起動できません");
            }
        }

        /// 打鍵が途切れていて（メニューから頼まれたときは問わない）、変換中・確定待ち・欄の確認待ちでもない
        bool SafeToStop()
        {
            bool idle = manualUpdate || (cfg.AutoUpdate > 0 && Now - lastKeyAt > cfg.AutoUpdate * 60000L);
            return idle && !comp.Composing && !comp.Waiting && !comp.Holding;
        }

        Process swapProc;
        string swapTarget;
        bool swapCancelled;
        long lastSwapAt = -60000;

        /// -Swap の準備ができたら（swap.ready にその PID）、まだ止めてよければ go を決めて自分から終わる。
        /// 打ち始めていれば cancel を決めて見送らせる。決定は -Swap の timeout と先を争う（Updater.Decide）
        void WatchSwap()
        {
            if (!swapProc.HasExited)
            {
                string ready = null;
                try { ready = File.ReadAllText(updater.ReadyPath).Trim(); } catch { }
                if (ready == swapProc.Id.ToString() && !swapCancelled)
                {
                    string won = updater.Decide(SafeToStop() ? "go" : "cancel");
                    // go を書くまでに -Swap が見切って終わっていたら、終わらない（起こし直す者がいなくなる）
                    swapProc.Refresh();
                    if (won == "go" && !swapProc.HasExited)
                    {
                        dbg.Write("update swap: exiting for " + swapTarget);
                        swapProc = null;
                        ExitThread();   // -Swap はこのプロセスが終わるのを待って差し替え、起動し直す
                        return;
                    }
                    swapCancelled = true;   // この回ではもう終わらない
                    dbg.Write("update swap: postponed (" + won + ")");
                }
                else if (Now - lastSwapAt > 90000 && !swapCancelled)   // -Swap は 60 秒で見切るはず。応答しなければ止めて次の機会にする
                {
                    swapCancelled = true;
                    updater.Decide("cancel");
                    try { swapProc.Kill(); } catch { }
                }
                return;
            }
            int code = swapProc.ExitCode;
            swapProc = null;
            if (code == 3 || code == 5) dbg.Write("update swap: not now (exit " + code + ")");   // 別の更新が動いていた・見送った
            else SwapFailed("終了コード " + code);
        }

        void SwapFailed(string msg)
        {
            swapProc = null;
            manualUpdate = false;
            updater.FailedHead = swapTarget;
            updater.Staged = null;
            dbg.Write("update swap failed: " + msg);
            tray.ShowBalloonTip(5000, "romaji-slm", "新しい版に切り替えられませんでした（今の版のまま使えます）: " + msg, ToolTipIcon.Warning);
            RefreshUpdateItem();
        }

        long lastStageAt = -300000;
        bool manualUpdate;   // メニューから頼まれた（打鍵の途切れを待たずに切り替える）

        void StartStage(string source)
        {
            if (updater.Running) return;
            lastStageAt = Now;
            dbg.Write("update stage start (" + source + ") " + updater.Installed + " -> " + (updater.Latest ?? "HEAD"));
            updater.Stage();
            RefreshUpdateItem();
        }

        string shownLatest;

        void RefreshUpdateItem()
        {
            string latest = updater.Latest;
            if (latest != shownLatest) { shownLatest = latest; dbg.Write("update available: " + (latest ?? "none")); }
            updateItem.Text = updater.Running ? "新しい版を組み立てて試しています…（今の版はそのまま使えます）"
                            : latest != null && latest == updater.Staged ? "新しい版に切り替える（" + updater.Installed + " → " + latest + "。数秒〜1 分止まる）"
                            : latest != null ? "新しい版に更新する（" + updater.Installed + " → " + latest + "）"
                            : "最新の版です（" + updater.Installed + "）。押すと入れ直す";
            updateItem.Enabled = !updater.Running;
            UpdateTray();
        }

        void UpdateTray()
        {
            var m = comp.Mode;
            tray.Icon = m == Mode.Japanese ? iconJa : m == Mode.English ? iconEn : iconOff;
            tray.Text = m == Mode.Disabled ? "romaji-slm: 使っていない（MS-IME）。Ctrl+Alt+Space で使う"
                      : m == Mode.Japanese ? "romaji-slm: 日本語（Shift+Space で英字）"
                      : "romaji-slm: 英字（Shift+Space で日本語）";
            if (updater != null && updater.Latest != null) tray.Text += "・新しい版あり";
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
        public static int Run(string root, string data, string romaji, string outPath, int port = 0)
        {
            var cfg = new Config(Path.Combine(data, "ime.ini"));
            if (port > 0) cfg.Port = port;   // 動いている常駐のサーバとぶつけずに試す
            var comp = new Composer();
            var clock = Stopwatch.StartNew();
            var w = new IntPtr(1);
            string result = null;
            var selfHistory = new History(outPath + ".history.jsonl");   // 記録の書式も同じクラスで確かめる
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
                    comp.OnImeState(w, true, 0);   // 日本語で始める
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
                            if (e is LogEffect) selfHistory.Write((LogEffect)e, "selftest");
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
                                try { c = server.Convert(req.Romaji, req.Context, req.Full, req.Prefetch, out ms); } catch (Exception ex) { err = ex.Message; }
                                comp.OnResult(req.Id, c, err, clock.ElapsedMilliseconds);
                                Console.WriteLine("wall " + (clock.ElapsedMilliseconds - t0) + " ms");
                            }
                        }
                        if (result == null) { Thread.Sleep(20); comp.Tick(clock.ElapsedMilliseconds); }
                        if (result == null && !comp.Composing && comp.PendingCommits == 0 && comp.Status != "") result = "FAIL: " + comp.Status;
                    }
                    foreach (var e in comp.TakeEffects()) if (e is LogEffect) selfHistory.Write((LogEffect)e, "selftest");   // 送った後の記録
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

        /// MS-IME が開いているか（IMC_GETOPENSTATUS）。応答しなければ閉じているとみなす
        public static bool GetImeOpen(IntPtr imeWnd)
        {
            IntPtr r;
            if (SendMessageTimeout(imeWnd, 0x0283, new IntPtr(0x0005), IntPtr.Zero, 0x0002, 300, out r) == IntPtr.Zero) return false;
            return r != IntPtr.Zero;
        }

        [StructLayout(LayoutKind.Sequential)]
        struct RAWINPUTDEVICE { public ushort usUsagePage, usUsage; public uint dwFlags; public IntPtr hwndTarget; }
        [DllImport("user32.dll", SetLastError = true)] static extern bool RegisterRawInputDevices(RAWINPUTDEVICE[] d, uint n, uint size);

        /// キーボードの Raw Input を、前面でなくても受ける（RIDEV_INPUTSINK）。フックが外れたことに気づくためだけに使う
        public static bool RegisterKeyboardRawInput(IntPtr hwnd)
        {
            var d = new[] { new RAWINPUTDEVICE { usUsagePage = 1, usUsage = 6, dwFlags = 0x00000100, hwndTarget = hwnd } };
            return RegisterRawInputDevices(d, 1, (uint)Marshal.SizeOf(typeof(RAWINPUTDEVICE)));
        }

        /// MS-IME を開閉する（既定の IME 窓へ WM_IME_CONTROL / IMC_SETOPENSTATUS）。応答しないアプリでは 300 ms であきらめる
        public static void SetImeOpen(IntPtr imeWnd, bool open)
        {
            IntPtr r;
            SendMessageTimeout(imeWnd, 0x0283, new IntPtr(0x0006), new IntPtr(open ? 1 : 0), 0x0002, 300, out r);
        }

        public static string ForegroundExe() { return ExeOf(GetForegroundWindow()); }

        public static string ExeOf(IntPtr hwnd)
        {
            try
            {
                uint pid;
                GetWindowThreadProcessId(hwnd, out pid);
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
        /// editable: 文字を打つ欄か（本人の報告 2026-10-16: 入力欄でないところで打鍵を取ると、YouTube の k・f などが効かない）。決まりは FieldKind.Editable
        public static bool FocusedElement(out bool password, out Point below, out bool editable, out string kind)
        {
            password = false;
            editable = true;
            kind = "";
            below = Point.Empty;
            try
            {
                var el = System.Windows.Automation.AutomationElement.FocusedElement;
                if (el == null) return false;
                var cur = el.Current;
                password = cur.IsPassword;
                var ct = cur.ControlType;
                bool hasValue = (bool)el.GetCurrentPropertyValue(System.Windows.Automation.AutomationElement.IsValuePatternAvailableProperty);
                bool hasText = (bool)el.GetCurrentPropertyValue(System.Windows.Automation.AutomationElement.IsTextPatternAvailableProperty);
                bool readOnly = false;
                if (hasValue)
                {
                    object vp;
                    if (el.TryGetCurrentPattern(System.Windows.Automation.ValuePattern.Pattern, out vp))
                        readOnly = ((System.Windows.Automation.ValuePattern)vp).Current.IsReadOnly;
                }
                bool caret = HasWin32Caret();
                string type = ct == null ? "?" : ct.ProgrammaticName.Replace("ControlType.", "");
                editable = FieldKind.Editable(type, caret, hasValue, readOnly, hasText);
                kind = type + " value=" + hasValue + " ro=" + readOnly +
                       " text=" + hasText + " caret=" + caret + " fw=" + cur.FrameworkId;
                var r = cur.BoundingRectangle;
                if (!r.IsEmpty && r.Width > 0 && !double.IsInfinity(r.Left))
                    below = new Point((int)r.Left, (int)r.Bottom + 6);
                return true;
            }
            catch { return false; }
        }

        static bool HasWin32Caret()
        {
            uint pid;
            var gi = new GUITHREADINFO { cbSize = Marshal.SizeOf(typeof(GUITHREADINFO)) };
            if (!GetGUIThreadInfo(GetWindowThreadProcessId(GetForegroundWindow(), out pid), ref gi)) return false;
            return gi.hwndCaret != IntPtr.Zero;
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
