// 常駐入力のキー処理（状態機械）。Windows の API を呼ばない純粋なクラスにして、合成イベントで試験する（ime/ComposerTests.cs）。
// 呼び出し側（RomajiSlmIme.cs）は OnKey などを呼んだあと Effects を取り出して実行する。
//
// 方式（docs/IME.md、D19）: 日本語モードでは打ったローマ字をアプリに渡さず、浮き窓（未確定の欄）にためる。
// 確定のときだけ変換結果を送り込む。アプリの文字を Backspace で消して置き換える方式は、
// アプリ側の自動補完などで「覚えた打鍵列 = キャレット直前の文字列」が崩れたとき本人の文章を消すので採らない。
//
// 送り出しの待ち行列（D22・D23）: 確定と、確定待ちの間に打たれた素通しのはずの打鍵（Enter・矢印・英字・修飾キー）を
// 打たれた順に積む。先頭から「送れるもの」（変換の済んだ確定と打鍵）を呼び出し側が TakeReady で取り出し、
// 1 回の SendInput で送り、Delivered / DeliveryFailed で結果を返す。実際に送るまで待ち行列から消えたことにしないので、
// その間に届いた打鍵は必ず後ろに並ぶ。確定が失敗したら、待ち行列の確定をすべて打鍵列として欄へ戻し、積んだ打鍵は
// 離鍵だけ送る（確定文の無いまま Enter が届かないように）。
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

    /// 送り出す 1 件: 確定文（Text と宛先の窓）か、送り直す打鍵（Key）
    public sealed class Delivery
    {
        public string Text; public IntPtr Window; public bool IsKey; public KeyEv Key;
    }

    public abstract class Effect { }
    /// 待ち行列の先頭に送れるものがある（呼び出し側は TakeReady → 送る → Delivered / DeliveryFailed）
    public sealed class DeliverEffect : Effect { }
    /// 待ち行列を戻したときに送る離鍵（押下は捨てたので、離鍵だけ届けてアプリのキー状態を揃える）
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
    /// 欄の確認。DelayMs だけ待ってから確かめる（欄を移すキーを送った直後は、アプリが移し終えるのを待つ）
    public sealed class CheckFieldEffect : Effect { public int Seq, DelayMs; public CheckFieldEffect(int s, int delay = 0) { Seq = s; DelayMs = delay; } }
    public sealed class StatEffect : Effect { public string Name; public double Value; public StatEffect(string n, double v) { Name = n; Value = v; } }
    /// 実利用の記録 1 件（本人の依頼 2026-10-16「実利用中の入力と出力をすべて保存」）。Event: commit / cancel / restore。
    /// パスワード欄の打鍵は確定にならず（そのまま送る）、ここに来ない
    public sealed class LogEffect : Effect
    {
        public string Event, Romaji, Context, Text, Note; public string[] Candidates; public int Chosen; public bool Raw;
    }

    public sealed class Composer
    {
        public const int VK_BACK = 0x08, VK_TAB = 0x09, VK_RETURN = 0x0D, VK_ESCAPE = 0x1B, VK_SPACE = 0x20,
            VK_PRIOR = 0x21, VK_NEXT = 0x22, VK_END = 0x23, VK_HOME = 0x24, VK_LEFT = 0x25, VK_UP = 0x26,
            VK_RIGHT = 0x27, VK_DOWN = 0x28, VK_DELETE = 0x2E, VK_CAPITAL = 0x14;

        // 変換の要求を出すまで、最後の打鍵から待つ時間。打っている途中の要求を減らす
        public int DebounceMs = 120;
        // 確定 1 件を待つ上限。サーバが落ちていても未確定の文は失わない（打鍵列を欄に戻す）
        public int TimeoutMs = 8000;
        public const int ContextChars = 60;   // 左文脈として渡す確定済みの文字数（体験版と同じ）
        public const int ContextIdleMs = 5 * 60 * 1000;
        const string MovedStatus = "確定の前に入力欄が変わったので入力していません。Enter で今の欄に入力します";

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
        readonly List<Entry> queue = new List<Entry>();
        readonly List<Entry> taken = new List<Entry>();   // TakeReady で渡し、結果を待っている分
        // 押されているキーと、押下をどう扱ったか。離鍵とキーリピートはこれに従う（途中で修飾キーが変わっても対応を崩さない）
        readonly Dictionary<int, int> held = new Dictionary<int, int>();
        // Dropped: 積んだ押下を捨てた（待ち行列を欄へ戻した）。リピートも離鍵も渡さず、キー処理もし直さない
        const int Passed = 0, Swallowed = 1, Queued = 2, Dropped = 3;
        string context = "";
        long contextAt;
        IntPtr foreground = IntPtr.Zero;
        string focus = "";
        long lastKeyAt;
        int nextId = 1, fieldSeq;
        bool passthrough, deliverPosted, reprocessing;
        // postMove: 欄を移しうるキーの後ろの打鍵を預かっている。awaitingMove: そのキーをまだアプリに送っていない（確認はまだ出さない）
        bool postMove, awaitingMove;
        long lastMoveAt = long.MinValue / 2, nowKey;   // 欄を移しうるキーが最後にアプリに届いた時刻
        // 今の欄をパスワード欄でないと確かめた。窓・欄が替わる、Tab・Enter・Esc を素通しする、で消える。
        // フォーカスの通知を出さないアプリ（focus が空）では欄の移動が分からないので、未確定の文が終わるたびに消す
        bool verified;
        readonly List<HeldKey> fieldHold = new List<HeldKey>();   // 欄の確認が済むまで預かった打鍵
        Pending inflight;

        sealed class Item
        {
            public string Romaji, Text, Focus, Context; public IntPtr Window; public bool Raw; public int CandIndex; public long At;
            public string[] Cands;   // 記録用: 確定のときに出ていた候補
        }
        sealed class Entry { public Item Commit; public KeyEv Key; public bool Moves; }   // Moves: 欄を移しうるキーの押下
        sealed class HeldKey { public KeyEv Ev; public Mods M; public long At; }
        sealed class Pending { public int Id; public string Key; public long SentAt; }

        public string Buffer { get { return buf.ToString(); } }
        public bool Composing { get { return buf.Length > 0; } }
        public string Context { get { return context; } }
        public int PendingCommits { get { int n = 0; foreach (var e in queue) if (e.Commit != null) n++; return n; } }
        public bool Waiting { get { return queue.Count > 0 || taken.Count > 0; } }
        /// 打鍵を OS に渡さずに持っている（積んでいる・預かっている）。この間は OS のキー状態が本人の押している状態と違う
        public bool Holding { get { return Waiting || FieldPending; } }

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

        /// 打鍵 1 つ。true ならアプリに渡さない（握りつぶすか、待ち行列に積んだ）。
        /// m は呼び出し側が自分で追った修飾キーの状態（積んだ修飾キーは OS に届いていないので、OS の状態は使えない）
        public bool OnKey(int vk, bool down, Mods m, long now, IntPtr fg, int scan = 0, bool ext = false, bool late = false)
        {
            if (fg != foreground)
                OnForeground(fg, now);
            var kev = new KeyEv(vk, scan, ext, down);
            nowKey = now;
            if (FieldPending)
            {
                // 欄の確認が済むまでは、どの打鍵も解釈せずに預かる（OnFieldChecked で、そのまま送るか処理し直す）
                fieldHold.Add(new HeldKey { Ev = kev, M = m, At = now });
                return true;
            }
            if (RequireFieldCheck && Mode == Mode.Japanese && MoveInFlight)
            {
                // 欄を移しうるキーがまだ待ち行列にある（アプリに届いていない）。この後の打鍵は移った先の欄のものなので、
                // そのキーを送ってから移った先を確かめるまで、解釈せずに預かる
                FieldPending = true;
                postMove = awaitingMove = true;
                fieldHold.Clear();
                fieldHold.Add(new HeldKey { Ev = kev, M = m, At = now });
                return true;
            }
            int st;
            bool known = held.TryGetValue(vk, out st);
            // キーリピートでも、握りつぶしているキー（浮き窓に文字を足している）なら確かめる（押したまま欄を移った）
            if (down && (!known || st == Swallowed) && NeedsFieldCheck(vk, m))
            {
                FieldPending = true;
                postMove = awaitingMove = false;
                fieldHold.Clear();
                fieldHold.Add(new HeldKey { Ev = kev, M = m, At = now });
                // 欄を移しうるキーが届いてすぐなら、アプリが移し終えるまで待ってから確かめる
                long since = now - lastMoveAt;
                effects.Add(new CheckFieldEffect(++fieldSeq, since >= 0 && since < MoveSettleMs ? (int)(MoveSettleMs - since) : 0));
                return true;
            }
            return Decide(kev, m, now, late);
        }

        bool MoveInFlight
        {
            get
            {
                foreach (var e in queue) if (e.Moves) return true;
                foreach (var e in taken) if (e.Moves) return true;
                return false;
            }
        }

        /// 日本語モードで、今の欄をまだ確かめていなければ、パスワード欄かを先に確かめる（文字に限らず Space・記号・Enter も。
        /// どのキーもこの欄では解釈し始める前に確かめる）。ショートカット（Ctrl・Alt・Win つき）と修飾キーそのものは対象外
        bool NeedsFieldCheck(int vk, Mods m)
        {
            if (!RequireFieldCheck || Mode != Mode.Japanese || passthrough || verified) return false;
            if (m.Ctrl || m.Alt || m.Win || IsModifier(vk)) return false;
            return true;
        }

        static bool IsModifier(int vk)
        {
            return (vk >= 0x10 && vk <= 0x12) || (vk >= 0xA0 && vk <= 0xA5) || vk == 0x5B || vk == 0x5C || vk == VK_CAPITAL;
        }
        bool Decide(KeyEv kev, Mods m, long now, bool late = false)
        {
            int vk = kev.Vk;
            bool down = kev.Down;
            int st;
            bool wasHeld = held.TryGetValue(vk, out st);
            if (!down)
            {
                held.Remove(vk);
                if (wasHeld && (st == Swallowed || st == Dropped)) return true;
                return Enqueue(kev, late);
            }
            if (wasHeld)
            {
                // キーリピート。握りつぶしたキーなら同じキーとして扱い直す（文字の連打、Backspace の連続）。
                // 修飾キーつきと Shift+Space（モードが往復する）は扱わない
                if (st == Dropped) return true;
                if (st == Swallowed)
                {
                    if (!m.Ctrl && !m.Alt && !m.Win && !(vk == VK_SPACE && m.Shift)) HandleDown(vk, m, now);
                    return true;
                }
                ReachesApp(vk, m);   // Tab の押しっぱなしなど、リピートでも欄は移る
                if (Enqueue(kev, late, MayMoveFocus(vk, m))) { held[vk] = Queued; return true; }
                return false;
            }
            if (HandleDown(vk, m, now)) { held[vk] = Swallowed; return true; }
            ReachesApp(vk, m);
            if (Enqueue(kev, late, MayMoveFocus(vk, m))) { held[vk] = Queued; return true; }
            held[vk] = Passed;
            return false;
        }

        /// アプリに届く押下（素通し・積んで後で送る）が欄を移しうるなら、「確かめた」印とパスワード欄の素通しをやめる
        /// （次の打鍵で確かめ直す）
        void ReachesApp(int vk, Mods m)
        {
            if (!MayMoveFocus(vk, m)) return;
            if (!Waiting && !reprocessing) lastMoveAt = nowKey;   // いまアプリに届く（積むものは Delivered で記録）
            verified = false;
            passthrough = false;
        }

        /// 欄を移しうるキー: Tab・Enter・Esc・F キー・アプリケーションキーと、Ctrl・Alt・Win つきのショートカットすべて
        /// （Ctrl+Tab、Alt+D、Ctrl+L …）。文字・Space・Backspace・Delete・矢印・Home/End は欄の中の操作とみなす
        static bool MayMoveFocus(int vk, Mods m)
        {
            if (IsModifier(vk)) return false;
            if (m.Ctrl || m.Alt || m.Win) return true;
            return vk == VK_TAB || vk == VK_RETURN || vk == VK_ESCAPE || (vk >= 0x70 && vk <= 0x87) || vk == 0x5D;
        }

        /// 確定待ちなら素通しのはずの打鍵を積む（true）。待っていなければ false（そのまま渡す）。
        /// 預かった打鍵を処理し直している間と、こちらが最後にまとめて送った時刻より前に押されていたキー（late）は、
        /// そのまま渡すと入力の列で順が崩れるので必ず積む
        bool Enqueue(KeyEv k, bool late = false, bool moves = false)
        {
            if (!Waiting && !reprocessing && !late) return false;
            queue.Add(new Entry { Key = k, Moves = moves });
            PostDeliver();
            return true;
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
                verified = false;   // 英字の間に欄を移っても分かるとは限らないので、日本語に戻ったら確かめ直す
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
            if (passthrough)
                return false;   // パスワード欄。未確定の文（前の欄から持ち越したもの）が残っていても、この欄の打鍵はそのまま渡す（抜けるのは Decide）
            if (!Composing)
            {
                if (c != '\0')
                {
                    buf.Append(c);
                    Phase = Phase.Raw;
                    lastKeyAt = now;
                    Status = "";
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
                    else
                    {
                        effects.Add(new StatEffect("cancel", 1));
                        effects.Add(new LogEffect { Event = "cancel", Romaji = buf.ToString(), Context = context, Candidates = Candidates, Chosen = -1 });
                        Clear();
                    }
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
            if (focus.Length == 0) verified = false;
            buf.Length = 0;
            Phase = Phase.Idle;
            CandIndex = 0;
        }

        bool HasUnresolved
        {
            get { foreach (var e in queue) if (e.Commit != null && e.Commit.Text == null) return true; return false; }
        }

        void Add(Item it, long now)
        {
            it.Romaji = buf.ToString();
            it.Window = foreground;
            it.Focus = focus;
            it.At = now;
            it.Context = context;
            if (it.Cands == null) it.Cands = Candidates;
            queue.Add(new Entry { Commit = it });
            Clear();
            Flush(now);
        }

        /// Enter: 候補を出していればその候補、まだなら 1 位を確定する（変換が済んでいなければ届きしだい）
        void CommitCurrent(long now)
        {
            if (buf.Length == 0) { Clear(); return; }
            if (Phase == Phase.Candidates && Candidates != null) { CommitSelected(now); return; }
            string[] c;
            string text = null;
            if (!HasUnresolved && !FieldPending && cache.TryGetValue(Key(buf.ToString(), context), out c)) text = c[0];
            if (text == null && !ServerReady)
            {
                // サーバが動いていない間は待ち行列に入れない（入れると後ろの打鍵をいつまでも止める）
                Status = "変換サーバの準備ができていません。Shift+Enter で打鍵列のまま入力します";
                effects.Add(new RedrawEffect());
                return;
            }
            Add(new Item { Text = text }, now);
        }

        void CommitSelected(long now)
        {
            var c = Candidates;
            if (c == null) { CommitCurrent(now); return; }
            int i = Math.Min(CandIndex, c.Length - 1);
            Add(new Item { Text = c[i], CandIndex = i }, now);
        }

        void CommitRaw(long now)
        {
            Add(new Item { Text = buf.ToString(), Raw = true }, now);
        }

        void PostDeliver()
        {
            if (deliverPosted || taken.Count > 0 || queue.Count == 0) return;
            var head = queue[0];
            if (head.Commit != null && head.Commit.Text == null) return;
            deliverPosted = true;
            effects.Add(new DeliverEffect());
        }

        void Flush(long now)
        {
            PostDeliver();
            Pump(now);
        }

        /// 先頭から送れるもの（変換の済んだ確定と打鍵）を取り出す。確定したときと前面の窓・フォーカスの欄が違えば、
        /// 何も渡さずに待ち行列を欄へ戻す。渡した分は Delivered か DeliveryFailed を呼ぶまで「送っている途中」
        public List<Delivery> TakeReady(long now)
        {
            deliverPosted = false;
            var r = new List<Delivery>();
            if (taken.Count > 0) return r;
            int n = 0;
            for (; n < queue.Count; n++)
            {
                var e = queue[n];
                if (e.Commit == null) { r.Add(new Delivery { IsKey = true, Key = e.Key }); continue; }
                var it = e.Commit;
                if (it.Text == null) break;
                if (it.Window != foreground || it.Focus != focus)
                {
                    RestoreQueue(MovedStatus);
                    return new List<Delivery>();
                }
                r.Add(new Delivery { Text = it.Text, Window = it.Window });
            }
            taken.AddRange(queue.GetRange(0, n));
            queue.RemoveRange(0, n);
            return r;
        }

        /// TakeReady で渡した分を送り終えた
        public void Delivered(long now)
        {
            bool moved = false;
            foreach (var e in taken)
            {
                if (e.Moves) { moved = true; lastMoveAt = now; }
                if (e.Commit != null)
                {
                    var it = e.Commit;
                    effects.Add(new StatEffect(it.Raw ? "commit_raw" : it.CandIndex > 0 ? "commit_other" : "commit_first", 1));
                    effects.Add(new LogEffect { Event = "commit", Romaji = it.Romaji, Context = it.Context, Text = it.Text,
                                                Candidates = it.Cands, Chosen = it.Raw ? -1 : it.CandIndex, Raw = it.Raw });
                    AppendContext(it.Text, now);
                }
                else if (e.Key.Down)
                {
                    int st;
                    if (held.TryGetValue(e.Key.Vk, out st) && st == Queued) held[e.Key.Vk] = Passed;   // 押下が届いたので、離鍵も届ける
                }
            }
            taken.Clear();
            if (moved && FieldPending && awaitingMove && !MoveInFlight)
            {
                // 欄を移すキーが届いた。アプリが移し終えるのを少し待ってから、移った先を確かめる（フォーカスの通知が来ればすぐ確かめ直す）
                awaitingMove = false;
                effects.Add(new CheckFieldEffect(++fieldSeq, MoveSettleMs));
            }
            effects.Add(new RedrawEffect());
            Flush(now);
        }

        public const int MoveSettleMs = 100;

        /// TakeReady で渡した分を送れなかった（SendInput の失敗・前面の窓の変化）。渡した分と残りの待ち行列を順に欄へ戻す
        public void DeliveryFailed(string why, long now)
        {
            queue.InsertRange(0, taken);
            taken.Clear();
            ResetContext();
            effects.Add(new StatEffect("deliver_fail", 1));
            RestoreQueue(why);
        }

        /// 待ち行列の確定をすべて打鍵列のまま欄の先頭へ（順を保って）戻す。積んだ打鍵は離鍵だけ送り、押下は捨てる
        void RestoreQueue(string status)
        {
            foreach (var e in queue)
                if (e.Commit != null)
                    effects.Add(new LogEffect { Event = "restore", Romaji = e.Commit.Romaji, Context = e.Commit.Context, Text = e.Commit.Text,
                                                Candidates = e.Commit.Cands, Chosen = -1, Note = status });
            var romaji = new StringBuilder();
            var ups = new List<KeyEv>();
            foreach (var e in queue)
            {
                if (e.Commit != null) romaji.Append(e.Commit.Romaji);
                else if (!e.Key.Down) ups.Add(e.Key);
                else
                {
                    int st;
                    if (held.TryGetValue(e.Key.Vk, out st) && st == Queued) held[e.Key.Vk] = Dropped;   // 押下を捨てたので、離鍵も渡さない
                }
            }
            queue.Clear();
            deliverPosted = false;
            if (ups.Count > 0) effects.Add(new ReplayEffect(ups));
            if (FieldPending && awaitingMove)
            {
                // 欄を移すキーを送らずに捨てたので、欄は移っていない。預かった打鍵は今の欄で確かめる
                awaitingMove = false;
                effects.Add(new CheckFieldEffect(++fieldSeq));
            }
            Restore(romaji.ToString(), status);
        }

        void Restore(string romaji, string status)
        {
            Status = status;
            effects.Add(new RedrawEffect());
            if (romaji.Length == 0) return;
            buf.Insert(0, romaji);
            Phase = Phase.Raw;
            CandIndex = 0;
            lastKeyAt = 0;
        }

        // ---- 変換の要求と応答 ----

        /// 時間の経過（呼び出し側が 50 ms ごとに呼ぶ）。間を置いた打鍵の変換を要求し、時間切れを処理する
        public void Tick(long now)
        {
            if (inflight != null && now - inflight.SentAt > TimeoutMs)
                OnResult(inflight.Id, null, "時間切れ", now);
            foreach (var e in queue)
            {
                if (e.Commit == null || e.Commit.Text != null) continue;
                if (now - e.Commit.At > TimeoutMs + 2000) RestoreQueue("変換が終わりませんでした。Shift+Enter で打鍵列のまま入力します");
                break;
            }
            Pump(now);
        }

        void Pump(long now)
        {
            // 欄の確認で預かっている間も、それより前に確定した文の変換は進める（止めると、その確定を待つ欄を移すキーと
            // 互いに待ち合って止まる）。預かった打鍵は浮き窓に入っていないので、止めるのは今の未確定文の先読みだけ
            if (inflight != null || !ServerReady || taken.Count > 0) return;
            foreach (var e in queue)
            {
                if (e.Commit == null) continue;
                // 変換済みでまだ送っていない確定が前にあれば、送り終えて左文脈に入るまで次を要求しない
                if (e.Commit.Text != null) return;
                Send(e.Commit.Romaji, now);
                return;
            }
            if (buf.Length == 0 || FieldPending) return;
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
                if (ok) { head.Text = cands[0]; head.Cands = cands; head.Context = context; }
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
            if (seq != fieldSeq || !FieldPending || awaitingMove) return;
            FieldPending = false;
            postMove = false;
            var hs = new List<HeldKey>(fieldHold);
            fieldHold.Clear();
            if (password)
            {
                // 預かった打鍵を、解釈せずそのままの順で送る。この欄の間は素通し
                // 途中に欄を離れうるキー（Tab など）があれば、それまでを送り、その後ろは移った先が分からないので捨てる
                passthrough = true;
                int i = 0;
                for (; i < hs.Count; i++)
                {
                    var h = hs[i];
                    queue.Add(new Entry { Key = h.Ev });
                    if (!h.Ev.Down) { held.Remove(h.Ev.Vk); continue; }
                    int hst;
                    if (!held.TryGetValue(h.Ev.Vk, out hst) || hst == Swallowed) held[h.Ev.Vk] = Queued;   // 押下を送るので離鍵も送る
                    if (MayMoveFocus(h.Ev.Vk, h.M)) { passthrough = false; i++; break; }
                }
                if (i < hs.Count) DropHeld(hs.GetRange(i, hs.Count - i), "入力欄を離れるキーの後に打った文字は、移った先の欄を確かめられないので捨てました");
            }
            else
            {
                // 普通の欄。預かった打鍵を打たれた順に処理し直す。素通しになる打鍵は、アプリへ渡し損ねているので待ち行列に積む
                // 途中で欄を離れうるキー（Tab など）が来て印が消えたら、その後ろに預かった打鍵は移った先の欄のものかもしれない。
                // 移った先を確かめる手段が無い（確認が移動より先に走りうる）ので、漏らさないよう捨てる
                verified = true;
                reprocessing = true;
                try
                {
                    int i = 0;
                    for (; i < hs.Count && verified; i++) Decide(hs[i].Ev, hs[i].M, hs[i].At);
                    if (i < hs.Count) DropHeld(hs.GetRange(i, hs.Count - i), "入力欄を離れるキーの後に打った文字は、移った先の欄を確かめられないので捨てました");
                }
                finally { reprocessing = false; }
            }
            effects.Add(new RedrawEffect());
            Flush(now);
        }

        /// 預かった打鍵を捨てる。新しく押されたキーは Dropped（リピートも離鍵も渡さない）。すでにアプリへ押下が届いている
        /// （届く予定の）キーのリピートは捨てるだけで、その離鍵は送る（待ち行列の後ろに）
        void DropHeld(List<HeldKey> hs, string status)
        {
            foreach (var h in hs)
            {
                int st;
                bool known = held.TryGetValue(h.Ev.Vk, out st);
                if (h.Ev.Down) { if (!known) held[h.Ev.Vk] = Dropped; }
                else if (known && st == Dropped) held.Remove(h.Ev.Vk);
                else { held.Remove(h.Ev.Vk); queue.Add(new Entry { Key = h.Ev }); }
            }
            Status = status;
            effects.Add(new RedrawEffect());
            PostDeliver();
        }

        /// 欄の確認の途中で窓や欄が替わった。預かった打鍵はどちらの欄のものとも言えないので捨てる（パスワードかもしれない）。
        /// 離鍵だけ送ってアプリのキー状態を揃える
        void DropFieldHold()
        {
            if (!FieldPending) return;
            if (postMove && !awaitingMove)
            {
                // 欄を移すキーを送った後の通知。預かった打鍵は移った先のものなので、捨てずに移った先を確かめ直す
                effects.Add(new CheckFieldEffect(++fieldSeq));
                return;
            }
            FieldPending = false;
            postMove = awaitingMove = false;
            fieldSeq++;   // 遅れて届く確認の結果を無視する
            var hs = new List<HeldKey>(fieldHold);
            fieldHold.Clear();
            DropHeld(hs, "入力欄が変わったので、欄を確かめ終わる前に打った文字は捨てました");
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
            verified = false;
            effects.Add(new ModeEffect(Mode));
            effects.Add(new RedrawEffect());
        }

        public void OnForeground(IntPtr fg, long now)
        {
            if (fg == foreground) return;
            DropFieldHold();
            foreground = fg;
            focus = "";   // 新しい窓の欄はまだ分からない（前の窓の欄の組を使い回さない）
            verified = false;
            passthrough = false;
            if (Phase == Phase.Candidates) Phase = Phase.Raw;   // 移った先で文字を打っても、前の欄の候補で確定しない
            ResetContext();
            // 未確定の文は残す（クリックで別の欄に移っても失わない）。MS-IME はウィンドウごとに開閉を持つので閉じ直す
            if (Mode != Mode.Disabled) effects.Add(new SetImeEffect(false, fg));
            effects.Add(new RedrawEffect());
            LeaveField();
        }

        /// フォーカスの欄が替わった（呼び出し側が EVENT_OBJECT_FOCUS の窓・オブジェクト・子の組を文字列にして渡す）
        public void OnFocus(string key, long now)
        {
            if (key == focus) return;
            DropFieldHold();
            focus = key;
            verified = false;
            passthrough = false;
            if (Phase == Phase.Candidates) Phase = Phase.Raw;
            ResetContext();
            LeaveField();
        }

        /// 確定待ちのまま窓や欄が替わったら、応答を待たずに打鍵列を欄へ戻す
        void LeaveField()
        {
            if (PendingCommits > 0) RestoreQueue(MovedStatus);
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
