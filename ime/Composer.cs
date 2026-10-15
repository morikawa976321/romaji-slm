// 常駐入力のキー処理（状態機械）。Windows の API を呼ばない純粋なクラスにして、合成イベントで試験する（ime/ComposerTests.cs）。
// 呼び出し側（RomajiSlmIme.cs）は OnKey などを呼んだあと Effects を取り出して実行する。
//
// 方式（docs/IME.md、D19）: 日本語モードでは打ったローマ字をアプリに渡さず、浮き窓（未確定の欄）にためる。
// 確定のときだけ変換結果を 1 回で送り込む。アプリの文字を Backspace で消して置き換える方式は、
// アプリ側の自動補完などで「覚えた打鍵列 = キャレット直前の文字列」が崩れたとき本人の文章を消すので採らない。
//
// 確定待ち（変換の応答を待つ間）に打たれた、アプリへ素通しするはずの打鍵（Enter・矢印・英字・修飾キー）は、
// その場では渡さず待ち行列に積み、確定文を送ったあとに同じ順で送り直す。先に渡すと、改行や送信が確定文より
// 先に起きる。確定が失敗したら、待ち行列の確定はすべて打鍵列として欄へ戻し、積んだ打鍵は離鍵だけ送る。
//
// csc（.NET Framework 4.x、C# 5）でビルドするので、文字列補間・?. ・式形式のメンバは使わない。

using System;
using System.Collections.Generic;
using System.Text;

namespace RomajiSlm
{
    public enum Mode { Disabled, English, Japanese }
    public enum Phase { Idle, Raw, Candidates }

    public struct Mods
    {
        public bool Shift, Ctrl, Alt, Win, Caps;
        public Mods(bool shift, bool ctrl, bool alt, bool win, bool caps)
        { Shift = shift; Ctrl = ctrl; Alt = alt; Win = win; Caps = caps; }
        public static readonly Mods None = new Mods(false, false, false, false, false);
        public static readonly Mods ShiftOnly = new Mods(true, false, false, false, false);
    }

    public struct KeyEv
    {
        public int Vk, Scan; public bool Extended, Down;
        public KeyEv(int vk, int scan, bool ext, bool down) { Vk = vk; Scan = scan; Extended = ext; Down = down; }
    }

    public abstract class Effect { }
    /// 確定文を送る。Window は確定したときの前面の窓（送る直前にもう一度確かめる）。失敗したら OnInjectFailed に Romaji を返す
    public sealed class InjectEffect : Effect
    {
        public string Text, Romaji; public IntPtr Window;
        public InjectEffect(string t, string r, IntPtr w) { Text = t; Romaji = r; Window = w; }
    }
    /// 確定待ちの間に積んだ打鍵を、同じ順で送り直す
    public sealed class ReplayEffect : Effect { public List<KeyEv> Keys; public ReplayEffect(List<KeyEv> k) { Keys = k; } }
    public sealed class RequestEffect : Effect
    {
        public int Id; public string Romaji, Context;
        public RequestEffect(int id, string r, string c) { Id = id; Romaji = r; Context = c; }
    }
    /// MS-IME の開閉。Window は要求したときの前面の窓（遅れて実行しても宛先を変えない）
    public sealed class SetImeEffect : Effect { public bool Open; public IntPtr Window; public SetImeEffect(bool o, IntPtr w) { Open = o; Window = w; } }
    public sealed class ModeEffect : Effect { public Mode Mode; public ModeEffect(Mode m) { Mode = m; } }
    public sealed class AltMaskEffect : Effect { }   // Alt を押したまま握りつぶしたキーのあと、Alt の単押し（メニュー起動）を防ぐ
    public sealed class RedrawEffect : Effect { }
    public sealed class CheckFieldEffect : Effect { public int Seq; public CheckFieldEffect(int s) { Seq = s; } }
    public sealed class StatEffect : Effect { public string Name; public double Value; public StatEffect(string n, double v) { Name = n; Value = v; } }

    public sealed class Composer
    {
        public const int VK_BACK = 0x08, VK_TAB = 0x09, VK_RETURN = 0x0D, VK_ESCAPE = 0x1B, VK_SPACE = 0x20,
            VK_PRIOR = 0x21, VK_NEXT = 0x22, VK_END = 0x23, VK_HOME = 0x24, VK_LEFT = 0x25, VK_UP = 0x26,
            VK_RIGHT = 0x27, VK_DOWN = 0x28, VK_DELETE = 0x2E, VK_CAPITAL = 0x14;

        // 変換の要求を出すまで、最後の打鍵から待つ時間。打っている途中の要求を減らす
        public int DebounceMs = 120;
        // 1 件の変換をあきらめる時間。サーバが落ちていても未確定の文は失わない（打鍵列を欄に戻す）
        public int TimeoutMs = 8000;
        public const int ContextChars = 60;   // 左文脈として渡す確定済みの文字数（体験版と同じ）
        public const int ContextIdleMs = 5 * 60 * 1000;

        public Mode Mode = Mode.Disabled;
        public Phase Phase = Phase.Idle;
        public bool ServerReady;
        public string Status = "";
        public int CandIndex;
        // 打ち始めに、フォーカスの欄がパスワード欄かを呼び出し側に確かめさせる（CheckFieldEffect → OnFieldChecked）。
        // 確かめ終わるまで変換を要求せず、浮き窓も出さない。パスワード欄なら打鍵列をそのまま入れて、その欄では素通しにする
        public bool RequireFieldCheck;
        public bool FieldPending { get; private set; }

        readonly StringBuilder buf = new StringBuilder();
        readonly List<Effect> effects = new List<Effect>();
        readonly Dictionary<string, string[]> cache = new Dictionary<string, string[]>();
        readonly Queue<string> cacheOrder = new Queue<string>();
        readonly Dictionary<string, string> failed = new Dictionary<string, string>();
        readonly List<Entry> queue = new List<Entry>();   // 確定と、確定待ちの間に積んだ打鍵（送る順）
        readonly Dictionary<int, bool> held = new Dictionary<int, bool>();   // 押されているキーと、押下のときの判断（握りつぶしたか）
        string context = "";
        long contextAt;
        IntPtr foreground = IntPtr.Zero;
        string focus = "";
        long lastKeyAt;
        int nextId = 1, fieldSeq;
        bool passthrough;
        Pending inflight;

        sealed class Item { public string Romaji, Text, Focus; public IntPtr Window; public bool Raw; public int CandIndex; }
        sealed class Entry { public Item Commit; public KeyEv Key; }
        sealed class Pending { public int Id; public string Key; public long SentAt; }

        public string Buffer { get { return buf.ToString(); } }
        public bool Composing { get { return buf.Length > 0; } }
        public string Context { get { return context; } }
        public int PendingCommits { get { int n = 0; foreach (var e in queue) if (e.Commit != null) n++; return n; } }

        public List<Effect> TakeEffects()
        {
            var e = new List<Effect>(effects);
            effects.Clear();
            return e;
        }

        static string Key(string romaji, string ctx) { return romaji + "\u0001" + ctx; }

        /// 今の未確定文の候補（まだ無ければ null）
        public string[] Candidates
        {
            get
            {
                string[] c;
                return buf.Length > 0 && cache.TryGetValue(Key(buf.ToString(), context), out c) ? c : null;
            }
        }

        public string Error
        {
            get
            {
                string e;
                return buf.Length > 0 && failed.TryGetValue(Key(buf.ToString(), context), out e) ? e : null;
            }
        }

        public bool Converting { get { return inflight != null; } }

        // ---- 打鍵 ----

        /// 打鍵 1 つ。true ならアプリに渡さない（握りつぶす）。
        /// 押下の判断は離鍵とキーリピートにも引き継ぐ（途中で修飾キーが変わっても、押下と離鍵の対応を崩さない）。
        public bool OnKey(int vk, bool down, Mods m, long now, IntPtr fg, int scan = 0, bool ext = false)
        {
            if (fg != foreground)
                OnForeground(fg, now);
            bool first;
            bool wasHeld = held.TryGetValue(vk, out first);
            bool s;
            if (!down)
            {
                held.Remove(vk);
                s = wasHeld && first;
            }
            else if (wasHeld)
            {
                // キーリピート。握りつぶしたキーなら同じキーとして扱い直す（文字の連打、Backspace の連続）。
                // 修飾キーつきと Shift+Space（モードが往復する）は扱わない
                if (first && !m.Ctrl && !m.Alt && !m.Win && !(vk == VK_SPACE && m.Shift)) HandleDown(vk, m, now);
                s = first;
            }
            else
            {
                s = HandleDown(vk, m, now);
                held[vk] = s;
            }
            if (!s && queue.Count > 0)
            {
                queue.Add(new Entry { Key = new KeyEv(vk, scan, ext, down) });
                return true;
            }
            return s;
        }

        bool HandleDown(int vk, Mods m, long now)
        {
            bool space = vk == VK_SPACE;
            if (space && m.Ctrl && m.Alt && !m.Shift && !m.Win)
            {
                SetEnabled(Mode == Mode.Disabled, now);
                effects.Add(new AltMaskEffect());
                return true;
            }
            if (Mode == Mode.Disabled)
                return false;
            if (space && m.Shift && !m.Ctrl && !m.Alt && !m.Win)
            {
                if (Composing) CommitCurrent(now);
                Mode = Mode == Mode.Japanese ? Mode.English : Mode.Japanese;
                passthrough = false;
                effects.Add(new SetImeEffect(false, foreground));
                effects.Add(new ModeEffect(Mode));
                effects.Add(new RedrawEffect());
                return true;
            }
            if (m.Ctrl || m.Alt || m.Win)
                return false;   // ショートカットはそのまま渡す（未確定の文は残す）
            if (Mode == Mode.English)
            {
                if (IsNav(vk)) ResetContext();
                return false;
            }

            char c = CharFor(vk, m);
            if (!Composing)
            {
                if (passthrough)
                {
                    if (vk == VK_TAB || vk == VK_RETURN || vk == VK_ESCAPE) passthrough = false;   // 欄を離れる
                    return false;
                }
                if (c != '\0')
                {
                    buf.Append(c);
                    Phase = Phase.Raw;
                    lastKeyAt = now;
                    Status = "";
                    if (RequireFieldCheck)
                    {
                        FieldPending = true;
                        effects.Add(new CheckFieldEffect(++fieldSeq));
                    }
                    effects.Add(new RedrawEffect());
                    return true;
                }
                if (vk == VK_BACK && context.Length > 0) context = context.Substring(0, context.Length - 1);
                else if (IsNav(vk)) ResetContext();
                return false;
            }

            // 未確定の文があるとき
            if (c != '\0')
            {
                if (Phase == Phase.Candidates) CommitSelected(now);
                buf.Append(c);
                Phase = Phase.Raw;
                lastKeyAt = now;
                effects.Add(new RedrawEffect());
                return true;
            }
            switch (vk)
            {
                case VK_SPACE:
                case VK_DOWN:
                case VK_TAB:
                    if (Phase == Phase.Raw) { Phase = Phase.Candidates; CandIndex = 0; }
                    else Step(m.Shift && vk == VK_TAB ? -1 : 1);
                    break;
                case VK_UP:
                    if (Phase == Phase.Candidates) Step(-1);
                    break;
                case VK_RETURN:
                    if (m.Shift) CommitRaw(now);
                    else CommitCurrent(now);
                    break;
                case VK_ESCAPE:
                    if (Phase == Phase.Candidates) Phase = Phase.Raw;
                    else { effects.Add(new StatEffect("cancel", 1)); Clear(); }
                    break;
                case VK_BACK:
                    if (Phase == Phase.Candidates) Phase = Phase.Raw;
                    else
                    {
                        buf.Length -= 1;
                        lastKeyAt = now;
                        if (buf.Length == 0) Clear();
                    }
                    break;
                default:
                    if (IsNav(vk) || vk == VK_DELETE || IsCharKey(vk)) break;   // 未確定の間は無視する（IME と同じく欄の中だけで扱う）
                    return false;
            }
            effects.Add(new RedrawEffect());
            return true;
        }

        void Step(int d)
        {
            var c = Candidates;
            if (c == null || c.Length == 0) return;
            CandIndex = ((CandIndex + d) % c.Length + c.Length) % c.Length;
        }

        void Clear()
        {
            buf.Length = 0;
            Phase = Phase.Idle;
            CandIndex = 0;
        }

        bool HasUnresolved
        {
            get { foreach (var e in queue) if (e.Commit != null && e.Commit.Text == null) return true; return false; }
        }

        Item NewItem(string text, int cand, bool raw)
        {
            return new Item { Romaji = buf.ToString(), Text = text, Window = foreground, Focus = focus, CandIndex = cand, Raw = raw };
        }

        /// Enter: 候補を出していればその候補、まだなら 1 位を確定する（変換が済んでいなければ届きしだい）
        void CommitCurrent(long now)
        {
            if (buf.Length == 0) { Clear(); return; }
            if (Phase == Phase.Candidates && Candidates != null) { CommitSelected(now); return; }
            string text = null;
            string[] c;
            if (!HasUnresolved && cache.TryGetValue(Key(buf.ToString(), context), out c)) text = c[0];
            queue.Add(new Entry { Commit = NewItem(text, 0, false) });
            Clear();
            Flush(now);
        }

        void CommitSelected(long now)
        {
            var c = Candidates;
            if (c == null) { CommitCurrent(now); return; }
            int i = Math.Min(CandIndex, c.Length - 1);
            queue.Add(new Entry { Commit = NewItem(c[i], i, false) });
            Clear();
            Flush(now);
        }

        void CommitRaw(long now)
        {
            queue.Add(new Entry { Commit = NewItem(buf.ToString(), 0, true) });
            Clear();
            Flush(now);
        }

        /// 先頭から、変換の済んだ確定と積んだ打鍵を順に送る。確定したときと前面の窓・フォーカスの欄が違えば、送らずに欄へ戻す
        void Flush(long now)
        {
            List<KeyEv> keys = null;
            while (queue.Count > 0)
            {
                var e = queue[0];
                if (e.Commit == null)
                {
                    if (keys == null) keys = new List<KeyEv>();
                    keys.Add(e.Key);
                    queue.RemoveAt(0);
                    continue;
                }
                var it = e.Commit;
                if (it.Text == null) break;
                if (it.Window != foreground || it.Focus != focus)
                {
                    if (keys != null) { effects.Add(new ReplayEffect(keys)); keys = null; }
                    RestoreQueue(MovedStatus);
                    break;
                }
                if (keys != null) { effects.Add(new ReplayEffect(keys)); keys = null; }
                effects.Add(new InjectEffect(it.Text, it.Romaji, it.Window));
                effects.Add(new StatEffect(it.Raw ? "commit_raw" : it.CandIndex > 0 ? "commit_other" : "commit_first", 1));
                AppendContext(it.Text, now);
                queue.RemoveAt(0);
            }
            if (keys != null) effects.Add(new ReplayEffect(keys));
            Pump(now);
        }

        /// 待ち行列の確定をすべて打鍵列のまま欄の先頭へ（順を保って）戻す。積んだ打鍵は離鍵だけ送る（押下は捨てる。
        /// 確定文の無いまま Enter などが届かないように）
        void RestoreQueue(string status)
        {
            var romaji = new StringBuilder();
            var ups = new List<KeyEv>();
            foreach (var e in queue)
            {
                if (e.Commit != null) romaji.Append(e.Commit.Romaji);
                else if (!e.Key.Down) ups.Add(e.Key);
            }
            queue.Clear();
            if (ups.Count > 0) effects.Add(new ReplayEffect(ups));
            Restore(romaji.ToString(), status);
        }

        void Restore(string romaji, string status)
        {
            if (romaji.Length == 0) return;
            buf.Insert(0, romaji);
            Phase = Phase.Raw;
            CandIndex = 0;
            lastKeyAt = 0;
            Status = status;
            effects.Add(new RedrawEffect());
        }

        /// 呼び出し側が確定文を送れなかった（SendInput の失敗・前面の窓の変化）。打鍵列を順に欄の先頭へ戻す
        public void OnInjectFailed(List<string> romajis, string why, long now)
        {
            ResetContext();
            Restore(string.Concat(romajis), why);
        }

        // ---- 変換の要求と応答 ----

        /// 時間の経過（呼び出し側が 50 ms ごとに呼ぶ）。間を置いた打鍵の変換を要求し、時間切れを処理する
        public void Tick(long now)
        {
            if (inflight != null && now - inflight.SentAt > TimeoutMs)
                OnResult(inflight.Id, null, "時間切れ", now);
            Pump(now);
        }

        void Pump(long now)
        {
            if (inflight != null || !ServerReady || FieldPending) return;
            foreach (var e in queue)
            {
                if (e.Commit == null || e.Commit.Text != null) continue;
                Send(e.Commit.Romaji, now);   // 先頭の未解決の確定（文脈はそれより前の確定を送った後のもの）
                return;
            }
            if (buf.Length == 0) return;
            string k = Key(buf.ToString(), context);
            if (cache.ContainsKey(k) || failed.ContainsKey(k)) return;
            if (Phase == Phase.Candidates || now - lastKeyAt >= DebounceMs)
                Send(buf.ToString(), now);
        }

        void Send(string romaji, long now)
        {
            inflight = new Pending { Id = nextId++, Key = Key(romaji, context), SentAt = now };
            effects.Add(new RequestEffect(inflight.Id, romaji, context));
            effects.Add(new RedrawEffect());
        }

        /// 変換の応答。cands が null なら失敗（error に理由）
        public void OnResult(int id, string[] cands, string error, long now)
        {
            if (inflight == null || inflight.Id != id) return;
            string k = inflight.Key;
            inflight = null;
            bool ok = cands != null && cands.Length > 0;
            if (ok)
            {
                cache[k] = cands;
                cacheOrder.Enqueue(k);
                while (cacheOrder.Count > 64) cache.Remove(cacheOrder.Dequeue());
                failed.Remove(k);
            }
            else
            {
                failed[k] = error ?? "候補なし";
                effects.Add(new StatEffect("fail", 1));
            }
            Item head = null;
            foreach (var e in queue) if (e.Commit != null && e.Commit.Text == null) { head = e.Commit; break; }
            if (head != null && Key(head.Romaji, context) == k)
            {
                if (ok) head.Text = cands[0];
                else
                {
                    failed.Remove(k);   // 欄に戻した文は、もう一度 Enter で再試行できるように
                    RestoreQueue("変換できませんでした（" + (error ?? "") + "）。Shift+Enter で打鍵列のまま入力します");
                }
            }
            effects.Add(new RedrawEffect());
            Flush(now);
        }

        public void SetServerReady(bool ready, long now)
        {
            ServerReady = ready;
            effects.Add(new RedrawEffect());
            Pump(now);
        }

        /// 欄の確認の結果（呼び出し側が時間切れにしたときは password=false で呼ぶ）
        public void OnFieldChecked(int seq, bool password, long now)
        {
            if (seq != fieldSeq || !FieldPending) return;
            FieldPending = false;
            if (password)
            {
                if (Composing) CommitRaw(now);
                passthrough = true;
            }
            effects.Add(new RedrawEffect());
            Pump(now);
        }

        // ---- 状態 ----

        public void SetEnabled(bool on, long now)
        {
            if (!on)
            {
                if (Composing) CommitCurrent(now);
                bool wasJapanese = Mode == Mode.Japanese;
                Mode = Mode.Disabled;
                // 「日本語入力中」はそのまま、変換の担い手だけ MS-IME に戻す
                if (wasJapanese) effects.Add(new SetImeEffect(true, foreground));
            }
            else
            {
                Mode = Mode.Japanese;
                effects.Add(new SetImeEffect(false, foreground));
            }
            passthrough = false;
            effects.Add(new ModeEffect(Mode));
            effects.Add(new RedrawEffect());
        }

        public void OnForeground(IntPtr fg, long now)
        {
            if (fg == foreground) return;
            foreground = fg;
            passthrough = false;
            ResetContext();
            // 未確定の文は残す（クリックで別の欄に移っても失わない）。MS-IME はウィンドウごとに開閉を持つので閉じ直す
            if (Mode != Mode.Disabled) effects.Add(new SetImeEffect(false, fg));
            effects.Add(new RedrawEffect());
            LeaveField();
        }

        const string MovedStatus = "確定の前に入力欄が変わったので入力していません。Enter で今の欄に入力します";

        /// 確定待ちのまま窓や欄が替わったら、応答を待たずに打鍵列を欄へ戻す
        void LeaveField()
        {
            if (PendingCommits > 0) RestoreQueue(MovedStatus);
        }

        /// フォーカスの欄が替わった（呼び出し側が EVENT_OBJECT_FOCUS の窓・オブジェクト・子の組を文字列にして渡す）
        public void OnFocus(string key, long now)
        {
            if (key == focus) return;
            focus = key;
            passthrough = false;
            ResetContext();
            LeaveField();
        }

        void ResetContext() { context = ""; }

        void AppendContext(string text, long now)
        {
            if (now - contextAt > ContextIdleMs) context = "";
            context += text;
            if (context.Length > ContextChars) context = context.Substring(context.Length - ContextChars);
            contextAt = now;
        }

        // ---- キーと文字（US 配列） ----

        static bool IsNav(int vk)
        {
            return vk == VK_LEFT || vk == VK_RIGHT || vk == VK_UP || vk == VK_DOWN || vk == VK_HOME || vk == VK_END
                || vk == VK_PRIOR || vk == VK_NEXT || vk == VK_TAB || vk == VK_RETURN || vk == VK_ESCAPE;
        }

        /// 文字を打つキー（英数字と記号）。未確定の間に打たれた、未対応の記号を握りつぶすのに使う
        static bool IsCharKey(int vk)
        {
            return (vk >= 0x30 && vk <= 0x5A) || (vk >= 0xBA && vk <= 0xC0) || (vk >= 0xDB && vk <= 0xDF);
        }

        /// 未確定の欄にためる文字。変換器が打鍵列として扱う英数字と , . ? ! - ' / だけ。それ以外は '\0'
        public static char CharFor(int vk, Mods m)
        {
            if (vk >= 0x41 && vk <= 0x5A)
            {
                bool upper = m.Shift ^ m.Caps;
                return (char)((upper ? 'A' : 'a') + (vk - 0x41));
            }
            if (vk >= 0x30 && vk <= 0x39)
            {
                if (!m.Shift) return (char)('0' + (vk - 0x30));
                return vk == 0x31 ? '!' : '\0';
            }
            switch (vk)
            {
                case 0xBC: return m.Shift ? '\0' : ',';
                case 0xBE: return m.Shift ? '\0' : '.';
                case 0xBD: return m.Shift ? '\0' : '-';
                case 0xBF: return m.Shift ? '?' : '/';
                case 0xDE: return m.Shift ? '\0' : '\'';
            }
            return '\0';
        }
    }
}
