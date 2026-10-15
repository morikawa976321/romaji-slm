// Composer の試験。ime/build.ps1 -Test がビルドして実行する（失敗があれば終了コード 1）。
using System;
using System.Collections.Generic;
using System.Linq;

namespace RomajiSlm
{
    static class ComposerTests
    {
        static int failures;
        static readonly IntPtr W1 = new IntPtr(100), W2 = new IntPtr(200);

        static void Check(bool ok, string what)
        {
            if (!ok) { failures++; Console.WriteLine("FAIL: " + what); }
        }

        static Composer Ready()
        {
            var c = new Composer();
            c.SetServerReady(true, 0);
            c.OnForeground(W1, 0);
            c.SetEnabled(true, 0);
            c.TakeEffects();
            return c;
        }

        static int VkFor(char ch)
        {
            if (ch >= 'a' && ch <= 'z') return 0x41 + (ch - 'a');
            if (ch >= '0' && ch <= '9') return 0x30 + (ch - '0');
            if (ch == ',') return 0xBC;
            if (ch == '.') return 0xBE;
            throw new ArgumentException(ch.ToString());
        }

        static List<bool> Type(Composer c, string s, long now)
        {
            var r = new List<bool>();
            foreach (char ch in s)
            {
                r.Add(c.OnKey(VkFor(ch), true, Mods.None, now, W1));
                c.OnKey(VkFor(ch), false, Mods.None, now, W1);
            }
            return r;
        }

        static bool Press(Composer c, int vk, Mods m, long now, IntPtr? w = null)
        {
            bool s = c.OnKey(vk, true, m, now, w ?? W1);
            c.OnKey(vk, false, m, now, w ?? W1);
            return s;
        }

        static T Last<T>(List<Effect> e) where T : Effect { return e.OfType<T>().LastOrDefault(); }
        static string Injected(List<Effect> e) { return string.Concat(e.OfType<InjectEffect>().Select(x => x.Text)); }

        static void Run(string name, Action t)
        {
            try { t(); }
            catch (Exception ex) { failures++; Console.WriteLine("FAIL: " + name + ": " + ex); }
        }

        static int Main()
        {
            Run("無効のときは何も握りつぶさない", () =>
            {
                var c = new Composer();
                c.OnForeground(W1, 0);
                Check(!Type(c, "abc", 0).Any(x => x), "disabled passes letters");
                Check(!Press(c, Composer.VK_SPACE, Mods.ShiftOnly, 0), "disabled passes Shift+Space");
            });

            Run("Ctrl+Alt+Space で有効・無効。無効にするとき日本語なら MS-IME を開く", () =>
            {
                var c = new Composer();
                c.OnForeground(W1, 0);
                c.TakeEffects();
                Check(Press(c, Composer.VK_SPACE, new Mods(false, true, true, false, false), 0), "hotkey swallowed");
                var e = c.TakeEffects();
                Check(c.Mode == Mode.Japanese, "enabled -> Japanese");
                Check(Last<SetImeEffect>(e) != null && !Last<SetImeEffect>(e).Open, "closes MS-IME");
                Check(e.OfType<AltMaskEffect>().Any(), "alt mask");
                Press(c, Composer.VK_SPACE, new Mods(false, true, true, false, false), 0);
                e = c.TakeEffects();
                Check(c.Mode == Mode.Disabled, "disabled");
                Check(Last<SetImeEffect>(e) != null && Last<SetImeEffect>(e).Open, "reopens MS-IME");
            });

            Run("Shift+Space で日本語と英語を切り替える", () =>
            {
                var c = Ready();
                Check(Press(c, Composer.VK_SPACE, Mods.ShiftOnly, 0), "swallowed");
                Check(c.Mode == Mode.English, "english");
                Check(!Type(c, "abc", 0).Any(x => x), "english passes letters");
                Press(c, Composer.VK_SPACE, Mods.ShiftOnly, 0);
                Check(c.Mode == Mode.Japanese, "back to japanese");
            });

            Run("打鍵はためて、間を置くと変換を要求し、Enter で 1 位を送る", () =>
            {
                var c = Ready();
                Check(Type(c, "kyouha", 0).All(x => x), "letters swallowed");
                Check(c.Buffer == "kyouha", "buffer");
                c.Tick(50);
                Check(!c.TakeEffects().OfType<RequestEffect>().Any(), "no request before debounce");
                c.Tick(200);
                var req = Last<RequestEffect>(c.TakeEffects());
                Check(req != null && req.Romaji == "kyouha" && req.Context == "", "request");
                c.OnResult(req.Id, new[] { "今日は", "京は" }, null, 300);
                Check(c.Candidates != null && c.Candidates[0] == "今日は", "preview");
                Check(Press(c, Composer.VK_RETURN, Mods.None, 400), "enter swallowed");
                var e = c.TakeEffects();
                Check(Injected(e) == "今日は", "injected first");
                Check(c.Context == "今日は", "context");
                Check(!c.Composing, "cleared");
            });

            Run("変換が済む前の Enter は、届いてから送る。その間に打った文字は次の文になる", () =>
            {
                var c = Ready();
                Type(c, "ame", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 10);
                var req = Last<RequestEffect>(c.TakeEffects());
                Check(req != null && req.Romaji == "ame", "request for commit");
                Type(c, "da", 20);
                Check(c.Buffer == "da", "next sentence");
                c.OnResult(req.Id, new[] { "雨" }, null, 300);
                var e = c.TakeEffects();
                Check(Injected(e) == "雨", "injected after result");
                req = Last<RequestEffect>(e);   // 続けて次の文の変換を要求する（打鍵から 120 ms 以上たっている）
                Check(req != null && req.Romaji == "da" && req.Context == "雨", "next request has context");
            });

            Run("Space で候補、もう一度で次候補、Enter でその候補", () =>
            {
                var c = Ready();
                Type(c, "kami", 0);
                c.Tick(200);
                var req = Last<RequestEffect>(c.TakeEffects());
                c.OnResult(req.Id, new[] { "紙", "髪", "神" }, null, 250);
                Press(c, Composer.VK_SPACE, Mods.None, 300);
                Check(c.Phase == Phase.Candidates && c.CandIndex == 0, "first space shows list at 1st");
                Press(c, Composer.VK_SPACE, Mods.None, 310);
                Press(c, Composer.VK_SPACE, Mods.None, 320);
                Check(c.CandIndex == 2, "third");
                Press(c, Composer.VK_UP, Mods.None, 330);
                Check(c.CandIndex == 1, "up");
                c.TakeEffects();
                Press(c, Composer.VK_RETURN, Mods.None, 340);
                Check(Injected(c.TakeEffects()) == "髪", "selected");
            });

            Run("候補の表示中に文字を打つと、その候補で確定して次の文を始める", () =>
            {
                var c = Ready();
                Type(c, "kami", 0);
                c.Tick(200);
                var req = Last<RequestEffect>(c.TakeEffects());
                c.OnResult(req.Id, new[] { "紙", "髪" }, null, 250);
                Press(c, Composer.VK_SPACE, Mods.None, 300);
                Press(c, Composer.VK_SPACE, Mods.None, 300);
                c.TakeEffects();
                Type(c, "w", 310);
                Check(Injected(c.TakeEffects()) == "髪", "committed");
                Check(c.Buffer == "w", "new buffer");
            });

            Run("Esc: 候補表示なら戻る、打鍵中なら捨てる。Backspace は 1 字消す", () =>
            {
                var c = Ready();
                Type(c, "abc", 0);
                Press(c, Composer.VK_BACK, Mods.None, 1);
                Check(c.Buffer == "ab", "backspace");
                Press(c, Composer.VK_SPACE, Mods.None, 2);
                Check(c.Phase == Phase.Candidates, "cand");
                Press(c, Composer.VK_ESCAPE, Mods.None, 3);
                Check(c.Phase == Phase.Raw && c.Buffer == "ab", "esc back");
                Press(c, Composer.VK_ESCAPE, Mods.None, 4);
                Check(!c.Composing && c.Phase == Phase.Idle, "esc clear");
                Check(!Injected(c.TakeEffects()).Any(), "nothing injected");
            });

            Run("Shift+Enter は打鍵列のまま確定（サーバが無くても）", () =>
            {
                var c = new Composer();
                c.OnForeground(W1, 0);
                c.SetEnabled(true, 0);
                c.TakeEffects();
                Type(c, "jr", 0);
                c.Tick(500);
                Check(!c.TakeEffects().OfType<RequestEffect>().Any(), "no request while server not ready");
                Press(c, Composer.VK_RETURN, Mods.ShiftOnly, 600);
                Check(Injected(c.TakeEffects()) == "jr", "raw");
            });

            Run("変換に失敗したら、打鍵列を欄に戻して何も送らない", () =>
            {
                var c = Ready();
                Type(c, "abc", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Last<RequestEffect>(c.TakeEffects());
                c.OnResult(req.Id, null, "500", 50);
                var e = c.TakeEffects();
                Check(Injected(e) == "", "nothing injected");
                Check(c.Buffer == "abc" && c.Status.Contains("Shift+Enter"), "restored");
            });

            Run("時間切れでも打鍵列を欄に戻す", () =>
            {
                var c = Ready();
                Type(c, "abc", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                c.TakeEffects();
                c.Tick(1 + c.TimeoutMs + 1);
                Check(c.Buffer == "abc", "restored after timeout");
            });

            Run("確定待ちの間にウィンドウが変わったら送らずに欄へ戻す", () =>
            {
                var c = Ready();
                Type(c, "abc", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Last<RequestEffect>(c.TakeEffects());
                c.OnForeground(W2, 10);
                c.OnResult(req.Id, new[] { "ABC" }, null, 50);
                var e = c.TakeEffects();
                Check(Injected(e) == "", "not injected to other window");
                Check(c.Buffer == "abc", "restored into composition");
                Press(c, Composer.VK_RETURN, Mods.None, 60, W2);
                Check(Injected(c.TakeEffects()) == "ABC", "enter commits in the new window");
            });

            Run("Ctrl・Alt の組み合わせは素通し、未確定の文は残る", () =>
            {
                var c = Ready();
                Type(c, "ab", 0);
                Check(!Press(c, 0x43, new Mods(false, true, false, false, false), 1), "ctrl+c passes");
                Check(!Press(c, Composer.VK_TAB, new Mods(false, false, true, false, false), 1), "alt+tab passes");
                Check(c.Buffer == "ab", "kept");
            });

            Run("押下を握りつぶしたキーは離鍵も握りつぶす", () =>
            {
                var c = Ready();
                Check(c.OnKey(0x41, true, Mods.None, 0, W1), "down");
                Check(c.OnKey(0x41, false, Mods.None, 0, W1), "up swallowed");
                Check(!c.OnKey(0x70, false, Mods.None, 0, W1), "unrelated up passes");
            });

            Run("未確定でない Backspace は左文脈から 1 字消す", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.ShiftOnly, 0);
                Check(c.Context == "a", "ctx");
                Check(!Press(c, Composer.VK_BACK, Mods.None, 1), "passes");
                Check(c.Context == "", "ctx popped");
            });

            Run("記号: , . ? ! は打鍵列に入り、@ などは素通し", () =>
            {
                Check(Composer.CharFor(0xBF, Mods.ShiftOnly) == '?', "?");
                Check(Composer.CharFor(0x31, Mods.ShiftOnly) == '!', "!");
                Check(Composer.CharFor(0x32, Mods.ShiftOnly) == '\0', "@");
                Check(Composer.CharFor(0x41, new Mods(false, false, false, false, true)) == 'A', "caps");
                var c = Ready();
                Check(!Press(c, 0x32, Mods.ShiftOnly, 0), "@ passes when idle");
                Type(c, "a", 0);
                Check(Press(c, 0x32, Mods.ShiftOnly, 0), "@ ignored while composing");
                Check(c.Buffer == "a", "not added");
            });

            Run("パスワード欄: 確かめ終わるまで要求も表示もせず、パスワード欄なら打鍵列のまま入れて以後は素通し", () =>
            {
                var c = Ready();
                c.RequireFieldCheck = true;
                Type(c, "pa", 0);
                var chk = Last<CheckFieldEffect>(c.TakeEffects());
                Check(chk != null && c.FieldPending, "check requested");
                c.Tick(500);
                Check(!c.TakeEffects().OfType<RequestEffect>().Any(), "no request while checking");
                c.OnFieldChecked(chk.Seq, true, 510);
                Check(Injected(c.TakeEffects()) == "pa", "raw injected");
                Check(!Type(c, "ss", 520).Any(x => x), "passthrough afterwards");
                Press(c, Composer.VK_TAB, Mods.None, 530);
                Check(Type(c, "a", 540).All(x => x), "tab leaves the field");
                chk = Last<CheckFieldEffect>(c.TakeEffects());
                c.OnFieldChecked(chk.Seq, false, 550);
                c.Tick(800);
                Check(Last<RequestEffect>(c.TakeEffects()) != null, "normal field converts");
            });

            Run("確定待ちの間の Enter は、確定文を送ったあとに送る", () =>
            {
                var c = Ready();
                Type(c, "ame", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Last<RequestEffect>(c.TakeEffects());
                Check(Press(c, Composer.VK_RETURN, Mods.None, 2), "second enter held back");
                c.OnResult(req.Id, new[] { "雨" }, null, 300);
                var e = c.TakeEffects();
                int iInj = e.FindIndex(x => x is InjectEffect), iRep = e.FindIndex(x => x is ReplayEffect);
                Check(iInj >= 0 && iRep > iInj, "inject before replay");
                var keys = ((ReplayEffect)e[iRep]).Keys;
                Check(keys.Count == 2 && keys[0].Vk == Composer.VK_RETURN && keys[0].Down && !keys[1].Down, "enter down/up replayed");
            });

            Run("確定待ちの間に英字へ切り替えて打った文字も、確定文のあと", () =>
            {
                var c = Ready();
                Type(c, "ame", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Last<RequestEffect>(c.TakeEffects());
                Press(c, Composer.VK_SPACE, Mods.ShiftOnly, 2);
                Check(c.Mode == Mode.English, "english");
                Check(Type(c, "b", 3).All(x => x), "held back");
                c.OnResult(req.Id, new[] { "雨" }, null, 300);
                var e = c.TakeEffects();
                Check(e.FindIndex(x => x is InjectEffect) < e.FindIndex(x => x is ReplayEffect), "order");
                Check(Last<ReplayEffect>(e).Keys[0].Vk == 0x42, "b replayed");
            });

            Run("確定の失敗: 待ち行列の確定を順に欄へ戻し、積んだ打鍵は離鍵だけ送る", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                var req = Last<RequestEffect>(c.TakeEffects());
                Type(c, "b", 2);
                Press(c, Composer.VK_RETURN, Mods.None, 3);
                Press(c, Composer.VK_RETURN, Mods.None, 4);   // 積まれる
                c.OnResult(req.Id, null, "500", 50);
                var e = c.TakeEffects();
                Check(Injected(e) == "", "nothing injected");
                Check(c.Buffer == "ab", "restored in order: " + c.Buffer);
                var rep = Last<ReplayEffect>(e);
                Check(rep != null && rep.Keys.All(k => !k.Down), "only key-ups replayed");
                Check(c.PendingCommits == 0, "queue cleared");
            });

            Run("確定待ちのまま窓が替わったら、応答を待たずに順に戻す", () =>
            {
                var c = Ready();
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                Type(c, "b", 2);
                Press(c, Composer.VK_RETURN, Mods.None, 3);
                c.OnForeground(W2, 4);
                Check(c.Buffer == "ab" && c.PendingCommits == 0, "restored ab: " + c.Buffer);
            });

            Run("確定待ちのままフォーカスの欄が替わったら戻す。同じ欄の重ねての通知では戻さない", () =>
            {
                var c = Ready();
                c.OnFocus("100:-4:1", 0);
                Type(c, "a", 0);
                Press(c, Composer.VK_RETURN, Mods.None, 1);
                c.OnFocus("100:-4:1", 2);
                Check(c.PendingCommits == 1, "same focus keeps");
                c.OnFocus("100:-4:2", 3);
                Check(c.Buffer == "a" && c.PendingCommits == 0, "other field restores");
            });

            Run("キーリピート: 押下の判断を離鍵まで引き継ぐ", () =>
            {
                var c = Ready();
                var ctrl = new Mods(false, true, false, false, false);
                Check(!c.OnKey(0x41, true, ctrl, 0, W1), "ctrl+a passes");
                Check(!c.OnKey(0x41, true, Mods.None, 1, W1), "repeat after ctrl released still passes");
                Check(!c.OnKey(0x41, false, Mods.None, 2, W1), "up passes");
                Check(!c.Composing, "nothing composed");
                Check(c.OnKey(0x42, true, Mods.None, 3, W1), "b swallowed");
                Check(c.OnKey(0x42, true, ctrl, 4, W1), "repeat with ctrl still swallowed");
                Check(c.OnKey(0x42, true, Mods.None, 5, W1), "plain repeat swallowed");
                Check(c.Buffer == "bb", "plain repeat appends, ctrl repeat does not: " + c.Buffer);
                Check(c.OnKey(0x42, false, Mods.None, 6, W1), "up swallowed");
            });

            Run("Shift+Space を押しっぱなしにしてもモードは 1 回だけ替わる", () =>
            {
                var c = Ready();
                c.OnKey(Composer.VK_SPACE, true, Mods.ShiftOnly, 0, W1);
                c.OnKey(Composer.VK_SPACE, true, Mods.ShiftOnly, 1, W1);
                c.OnKey(Composer.VK_SPACE, true, Mods.ShiftOnly, 2, W1);
                Check(c.OnKey(Composer.VK_SPACE, false, Mods.ShiftOnly, 3, W1), "up swallowed");
                Check(c.Mode == Mode.English, "toggled once");
            });

            Run("送れなかった確定は欄に戻る", () =>
            {
                var c = Ready();
                c.OnInjectFailed(new List<string> { "a", "b" }, "送れませんでした", 0);
                Check(c.Buffer == "ab" && c.Status == "送れませんでした", "restored");
            });

            Run("MS-IME の開閉は要求したときの窓に向ける", () =>
            {
                var c = Ready();
                Press(c, Composer.VK_SPACE, new Mods(false, true, true, false, false), 0);
                var ime = Last<SetImeEffect>(c.TakeEffects());
                Check(ime != null && ime.Open && ime.Window == W1, "target W1");
            });

            Console.WriteLine(failures == 0 ? "all passed" : failures + " failure(s)");
            return failures == 0 ? 0 : 1;
        }
    }
}
